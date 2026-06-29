"""Post-synthesis layout vs schematic (LVS) check for CFFET cells.

Compares CDL subcircuit connectivity against solved placement in ``.res``
(device-level LVS).  Optionally verifies dual-face pin policy via pin_audit.

Usage:
    python -m src.cellgen.postprocess.postsim_lvs \\
        --cell INV_X1 \\
        --res output/.../result/INV_X1.res \\
        --cdl input/cdl/PROBE_2F4T.cdl \\
        --config output/.../config/INV_X1.json \\
        --layer input/layer/PROBE3_CFFET_2F_4T_4530OF0.json
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

from src.cellgen.core.util import read_cdl_file
from src.cellgen.postprocess.gds_CFFET_SH import parse_res


def _base_tran_name(name: str) -> str:
    """MM0S0 -> MM0 (for reporting only)."""
    m = re.match(r"^(MM\d+)", name)
    return m.group(1) if m else name


def _clean_io_pins(pins: list[str]) -> set[str]:
    return {p for p in pins if p and not set(p) <= {"-"}}


def _norm_model(model) -> str:
    if hasattr(model, "name"):
        m = model.name.lower()
    else:
        m = str(model).lower()
    if "pmos" in m:
        return "pmos"
    if "nmos" in m:
        return "nmos"
    return m


@dataclass
class LvsReport:
    cell: str
    passed: bool = True
    device_ok: bool = True
    pin_ok: bool = True
    messages: list[str] = field(default_factory=list)
    layout_devices: int = 0
    schematic_devices: int = 0

    def fail(self, msg: str) -> None:
        self.passed = False
        self.messages.append(msg)


def compare_connectivity(cell: str, cdl_path: str, res_path: str) -> LvsReport:
    """Device-level LVS: CDL terminals vs .res placement nets."""
    from src.cellgen.archit import config as archit_config

    archit_config.init()
    rep = LvsReport(cell=cell)
    circuits = read_cdl_file(cdl_path)
    sch = next((c for c in circuits if c.subckt_name == cell), None)
    if sch is None:
        rep.fail(f"subckt {cell!r} not found in {cdl_path}")
        rep.device_ok = False
        rep.passed = False
        return rep
    trans, _segs, _tech, io_pins = parse_res(res_path)
    layout_by_name = {t.name: t for t in trans}

    rep.schematic_devices = len(sch.transistors)
    rep.layout_devices = len(trans)

    if rep.schematic_devices != rep.layout_devices:
        rep.fail(
            f"device count: schematic={rep.schematic_devices} "
            f"layout={rep.layout_devices}"
        )
        rep.device_ok = False

    for tname, stran in sorted(sch.transistors.items()):
        lt = layout_by_name.get(tname)
        if lt is None:
            rep.fail(f"missing layout device for {tname} ({_base_tran_name(tname)})")
            rep.device_ok = False
            continue
        sch_model = _norm_model(stran.model)
        lay_model = _norm_model(lt.model)
        if sch_model != lay_model:
            rep.fail(f"{tname}: model schematic={sch_model} layout={lay_model}")
            rep.device_ok = False
        for pin, sch_net, lay_net in (
            ("gate", stran.gate, lt.g_net),
            ("source", stran.source, lt.s_net),
            ("drain", stran.drain, lt.d_net),
        ):
            if sch_net != lay_net:
                rep.fail(
                    f"{tname}.{pin}: schematic={sch_net} layout={lay_net}"
                )
                rep.device_ok = False

    extra = set(layout_by_name) - set(sch.transistors)
    if extra:
        rep.fail(f"extra layout devices without schematic: {sorted(extra)}")
        rep.device_ok = False

    sch_io = set(sch.io_pins)
    lay_io = _clean_io_pins(io_pins)
    if sch_io != lay_io:
        rep.fail(f"IO pins schematic={sorted(sch_io)} layout={sorted(lay_io)}")

    if rep.device_ok and sch_io == lay_io:
        rep.messages.append(
            f"{rep.schematic_devices} device(s), IO {len(sch_io)} pin(s) match"
        )
    if not rep.device_ok:
        rep.passed = False
    return rep


def run_pin_audit(cell: str, res_path: str, var_path: str, config_path: str, layer_path: str) -> tuple[bool, str]:
    try:
        from src.cellgen.archit.CFFET.pin_audit import audit_cffet_pins

        audit_cffet_pins(var_path, res_path, config_path, layer_path)
        return True, "pin policy PASS"
    except Exception as exc:
        return False, str(exc)


def run_lvs(
    cell: str,
    *,
    res_path: str,
    cdl_path: str,
    var_path: str | None = None,
    config_path: str | None = None,
    layer_path: str | None = None,
    check_pins: bool = True,
) -> LvsReport:
    rep = compare_connectivity(cell, cdl_path, res_path)
    if check_pins and var_path and config_path and layer_path:
        ok, msg = run_pin_audit(cell, res_path, var_path, config_path, layer_path)
        rep.pin_ok = ok
        if ok:
            rep.messages.append(msg)
        else:
            rep.fail(f"pin audit: {msg}")
            rep.passed = False
    elif check_pins:
        rep.messages.append("pin audit skipped (missing --var/--config/--layer)")
    return rep


def format_report(rep: LvsReport) -> str:
    status = "PASS" if rep.passed else "FAIL"
    lines = [f"{rep.cell}\t{status}"]
    for m in rep.messages:
        lines.append(f"  {m}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import logging
    logging.disable(logging.CRITICAL)
    try:
        from loguru import logger
        logger.remove()
    except Exception:
        pass
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cell", required=True)
    p.add_argument("--res", required=True)
    p.add_argument("--cdl", required=True)
    p.add_argument("--var", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--layer", default=None)
    p.add_argument("--no-pin-audit", action="store_true")
    args = p.parse_args(argv)

    rep = run_lvs(
        args.cell,
        res_path=args.res,
        cdl_path=args.cdl,
        var_path=args.var,
        config_path=args.config,
        layer_path=args.layer,
        check_pins=not args.no_pin_audit,
    )
    print(format_report(rep))
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
