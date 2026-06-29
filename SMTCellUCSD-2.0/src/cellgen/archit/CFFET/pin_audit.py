"""Audit CFFET I/O pin (SON) placement against the dual-face pin policy.

Reads a solved ``.var`` dump plus cell config / layer stack and checks:
  - **Input** nets: exactly one SON on the assigned face (M0 or BM0).
  - **Output** nets: exactly one SON on M0 **and** one on BM0.

Usage:
    python -m src.cellgen.archit.CFFET.pin_audit \\
        --var output/.../INV_X1.var \\
        --res output/.../INV_X1.res \\
        --config output/.../config/INV_X1.json \\
        --layer input/layer/PROBE3_CFFET_2F_3T_4530OF0.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

from src.cellgen.core.entity import LayerStack

SON_RE = re.compile(
    r"^(\d+)\s+net_isSON_(?P<net>\w+)_(?P<k>\d+)_L(?P<layer>\d+)_R(?P<row>[\d.]+)_C(?P<col>[\d.]+)"
)

FRONT_METAL = "M0"
BACK_METAL = "BM0"


@dataclass
class SonPin:
    net: str
    commodity_k: int
    layer_idx: int
    layer_name: str
    row: float
    col: float


def parse_io_pins(res_path: str) -> list[str]:
    pins: list[str] = []
    in_cell = False
    with open(res_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("** Cell Information"):
                in_cell = True
                continue
            if in_cell and line.startswith("**"):
                break
            if in_cell and line and line != "IO Pins" and not line.startswith("-"):
                pins.extend(line.split())
    return pins


def parse_active_sons(var_path: str, idx_to_name: dict[int, str]) -> list[SonPin]:
    sons: list[SonPin] = []
    with open(var_path) as f:
        for raw in f:
            m = SON_RE.match(raw.strip())
            if not m or m.group(1) != "1":
                continue
            layer_idx = int(m.group("layer"))
            sons.append(SonPin(
                net=m.group("net"),
                commodity_k=int(m.group("k")),
                layer_idx=layer_idx,
                layer_name=idx_to_name.get(layer_idx, f"L{layer_idx}"),
                row=float(m.group("row")),
                col=float(m.group("col")),
            ))
    return sons


def _load_pin_collections() -> tuple[set[str], set[str]]:
    """Load input/output pin name sets from JSON (no global init required)."""
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    with open(os.path.join(root, "input/pin_input_collection.json")) as f:
        inputs = set(json.load(f).keys())
    with open(os.path.join(root, "input/pin_output_collection.json")) as f:
        outputs = set(json.load(f).keys())
    return inputs, outputs


def classify_io_nets(io_pins: list[str]) -> tuple[list[str], list[str]]:
    """Classify IO pins using the same rules as ``Circuit.is_input/output_net``."""
    input_names, output_names = _load_pin_collections()
    inputs, outputs = [], []
    for p in io_pins:
        if p in output_names:
            outputs.append(p)
        elif p in input_names:
            inputs.append(p)
        elif p == io_pins[-1]:
            outputs.append(p)
        else:
            inputs.append(p)
    return inputs, outputs


def resolve_input_faces(
    io_pins: list[str],
    pin_face: dict | None,
    subckt_name: str = "",
) -> dict[str, str]:
    """Return net -> route metal (M0 / BM0) for each input."""
    face_to_layer = {"front": FRONT_METAL, "back": BACK_METAL}
    explicit: dict[str, str] = {}
    default_face = "front"
    assignment_mode = "round_robin"
    if pin_face:
        face_to_layer = dict(pin_face.get("face_to_layer", face_to_layer))
        in_cfg = pin_face.get("input", {}) or {}
        assignment_mode = in_cfg.get("assignment", "round_robin")
        explicit_raw = in_cfg.get("explicit", {}) or {}
        for net, face in explicit_raw.items():
            explicit[net] = face_to_layer.get(face, FRONT_METAL)
        default_face = in_cfg.get("default_face", "front")

    if assignment_mode == "round_robin" and subckt_name.startswith(
        ("AOI", "OAI", "MUX", "LHQ", "LAT", "DFF")
    ):
        assignment_mode = "same_face"
    if assignment_mode == "same_face":
        explicit = {}

    inputs, _ = classify_io_nets(io_pins)
    assignment: dict[str, str] = {}
    rr = 0
    faces = ["front", "back"]
    for net in inputs:
        if net in explicit:
            assignment[net] = explicit[net]
        elif assignment_mode == "same_face":
            face = default_face
            assignment[net] = face_to_layer.get(face, FRONT_METAL)
        else:
            face = faces[rr % len(faces)]
            rr += 1
            assignment[net] = face_to_layer.get(face, FRONT_METAL)
    return assignment


def audit_cffet_pins(
    var_path: str,
    res_path: str,
    config_path: str,
    layer_path: str,
) -> dict:
    """Run pin-policy checks. Returns a report dict; raises ValueError on failure."""
    ls = LayerStack(layer_path)
    idx_to_name = {i: m.layer_name for i, m in enumerate(ls.metal_layers)}

    io_pins = parse_io_pins(res_path)
    if not io_pins:
        raise ValueError(f"No IO pins found in {res_path}")

    with open(config_path) as f:
        cell_cfg = json.load(f)
    pin_face_entry = cell_cfg.get("pin_face", {})
    pin_face = pin_face_entry.get("value") if isinstance(pin_face_entry, dict) else None

    inputs, outputs = classify_io_nets(io_pins)
    subckt_name = res_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    input_faces = resolve_input_faces(io_pins, pin_face, subckt_name=subckt_name)
    sons = parse_active_sons(var_path, idx_to_name)
    by_net: dict[str, list[SonPin]] = {}
    for s in sons:
        by_net.setdefault(s.net, []).append(s)

    errors: list[str] = []
    report_inputs: dict[str, dict] = {}
    report_outputs: dict[str, dict] = {}

    for net in inputs:
        active = by_net.get(net, [])
        expected = input_faces.get(net, FRONT_METAL)
        if len(active) != 1:
            errors.append(f"input {net}: expected 1 SON, got {len(active)}")
        elif active[0].layer_name != expected:
            errors.append(
                f"input {net}: expected SON on {expected}, "
                f"got {active[0].layer_name} @ row={active[0].row} col={active[0].col}"
            )
        report_inputs[net] = {
            "expected_layer": expected,
            "actual": [
                {"layer": s.layer_name, "row": s.row, "col": s.col, "k": s.commodity_k}
                for s in active
            ],
        }

    for net in outputs:
        active = by_net.get(net, [])
        layers = {s.layer_name for s in active}
        if len(active) != 2:
            errors.append(f"output {net}: expected 2 SONs (M0+BM0), got {len(active)}")
        elif layers != {FRONT_METAL, BACK_METAL}:
            errors.append(
                f"output {net}: expected SON on both {FRONT_METAL} and {BACK_METAL}, "
                f"got layers {sorted(layers)}"
            )
        report_outputs[net] = {
            "expected_layers": [FRONT_METAL, BACK_METAL],
            "actual": [
                {"layer": s.layer_name, "row": s.row, "col": s.col, "k": s.commodity_k}
                for s in active
            ],
        }

    return {
        "cell_io_pins": io_pins,
        "inputs": inputs,
        "outputs": outputs,
        "input_face_assignment": input_faces,
        "input_pins": report_inputs,
        "output_pins": report_outputs,
        "ok": not errors,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--var", required=True)
    p.add_argument("--res", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--layer", required=True)
    args = p.parse_args(argv)

    report = audit_cffet_pins(args.var, args.res, args.config, args.layer)
    print(f"IO pins (CDL order): {' '.join(report['cell_io_pins'])}")
    print(f"Inputs:  {report['inputs']}")
    print(f"Outputs: {report['outputs']}")
    print("Input face assignment (round-robin / explicit):")
    for net, layer in report["input_face_assignment"].items():
        pins = report["input_pins"][net]["actual"]
        loc = (f"{pins[0]['layer']} row={pins[0]['row']} col={pins[0]['col']}"
               if pins else "MISSING")
        print(f"  {net}: assigned={layer}  SON={loc}")
    print("Output dual-face SON:")
    for net, info in report["output_pins"].items():
        parts = [f"{p['layer']}@({p['row']},{p['col']})" for p in info["actual"]]
        print(f"  {net}: {' + '.join(parts) if parts else 'MISSING'}")

    if report["ok"]:
        print("PASS: pin policy satisfied")
        return 0
    for err in report["errors"]:
        print(f"FAIL: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
