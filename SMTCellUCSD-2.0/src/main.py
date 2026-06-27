"""
SMTCell top-level solve driver (`make spnr` entry point).

The Makefile runs:

    python -m src.main --mode spnr --tech <TECH> --layer <layer.json>
        --lib_name <LIB> --track <N> --height_config <H>
        --cell_config <OUT>/config/<cell>.json --netlist <cdl>
        --cell_names <cell> --output_dir <OUT> --flag_log_constraints <bool>

It builds the per-technology ``<TECH>_Tech`` from the layer stack + CLI args,
reads the netlist, and constructs the matching solve orchestrator
(:class:`QFET` / :class:`CFET` / :class:`FinFET`). Each orchestrator solves in
its ``__init__`` and writes ``<output_dir>/result/<cell>.res`` (+ ``.var`` and a
``<output_dir>/view/<cell>.png``). Output therefore lands under the per-library
tree the Makefile sets up: ``output/<LIBNAME>/<HEIGHT_CONFIG>/result/...``.

This mirrors the reference ``SMTCell`` driver; ``unit_width=46.0`` and
``num_fin=2`` match it. QFET is added here (the reference predated it). Only the
``spnr`` mode is implemented; the research-only ``ecg_*`` modes are out of
scope. FinFET always solves the cell as given.
"""

import argparse

from loguru import logger

from src.cellgen.archit import config
from src.cellgen.core.entity import LayerStack
from src.cellgen.core.util import read_cdl_file
from src.cellgen.archit.QFET.tech import QFET_Tech
from src.cellgen.archit.CFET.tech import CFET_Tech
from src.cellgen.archit.CFFET.tech import CFFET_Tech
from src.cellgen.archit.FinFET.tech import FinFET_Tech
from src.cellgen.archit.QFET.main import QFET
from src.cellgen.archit.CFET.main import CFET
from src.cellgen.archit.CFFET.main import CFFET
from src.cellgen.archit.FinFET.main import FinFET

# Driver-level technology constants (match the reference SMTCell driver). The
# CP-SAT placement objective is independent of finger count (fingers only set
# the drawn device width), so these affect GDS geometry, not the solve.
_UNIT_WIDTH = 46.0
_NUM_FIN = 2

# tech name -> (Tech class, orchestrator class, orchestrator's tech kwarg)
_TECH_REGISTRY = {
    "QFET": (QFET_Tech, QFET, "tech"),
    "CFET": (CFET_Tech, CFET, "c_tech"),
    "CFFET": (CFFET_Tech, CFFET, "c_tech"),
    "FinFET": (FinFET_Tech, FinFET, "fin_tech"),
}


def build_technology(tech, lib_name, layer_stack, num_rt_track, height_config):
    """Construct the ``<tech>_Tech`` instance for the requested technology."""
    if tech not in _TECH_REGISTRY:
        raise ValueError(
            f"Unsupported technology {tech!r}; expected one of {sorted(_TECH_REGISTRY)}."
        )
    Tech = _TECH_REGISTRY[tech][0]
    # CFET_Tech also takes stacking_config (defaults to 'P_on_N'); the shared
    # kwargs below match every <tech>_Tech signature.
    return Tech(
        lib_name=lib_name,
        num_fin=_NUM_FIN,
        num_rt_track=num_rt_track,
        unit_width=_UNIT_WIDTH,
        layer_stack=layer_stack,
        height_config=height_config,
    )


class SMTCell:
    """Read circuits from a CDL and dispatch each to its solve orchestrator."""

    def __init__(self, cdl_file, cell_config, technology, circuit_names=None,
                 output_dir="./output/", flag_log_constraints=False):
        # Populate the PWR/GND/INPUT/OUTPUT net-name globals used by pin
        # assignment - must run once before any circuit is parsed.
        config.init()

        self.technology = technology
        self.cell_config = cell_config
        self.output_dir = output_dir
        self.flag_log_constraints = flag_log_constraints

        all_circuits = read_cdl_file(cdl_file)
        if circuit_names:
            wanted = set(circuit_names)
            self.circuits = [c for c in all_circuits if c.subckt_name in wanted]
            missing = wanted - {c.subckt_name for c in self.circuits}
            if missing:
                raise ValueError(
                    f"Cell(s) {sorted(missing)} not found in netlist {cdl_file}. "
                    f"Available: {sorted(c.subckt_name for c in all_circuits)}"
                )
        else:
            self.circuits = all_circuits

        logger.info(
            f"{technology.TECHNOLOGY}: solving {len(self.circuits)} cell(s): "
            f"{[c.subckt_name for c in self.circuits]}"
        )
        self._gen_cell_lib()

    def _gen_cell_lib(self):
        tech_name = self.technology.TECHNOLOGY
        if tech_name not in _TECH_REGISTRY:
            raise ValueError(f"Technology not supported: {tech_name}")
        if self.technology.height_config != "SH":
            raise ValueError(
                f"{tech_name} height_config {self.technology.height_config!r} not "
                f"supported (only 'SH')."
            )
        _, Orchestrator, tech_kwarg = _TECH_REGISTRY[tech_name]
        for circuit in self.circuits:
            logger.info(f"──── {tech_name}: solving {circuit.subckt_name} ────")
            Orchestrator(
                circuit=circuit,
                output_dir=self.output_dir,
                cell_config=self.cell_config,
                flag_log_constraints=self.flag_log_constraints,
                **{tech_kwarg: self.technology},
            )

    def __repr__(self):
        return (f"SMTCell(tech={self.technology.TECHNOLOGY}, "
                f"cells={[c.subckt_name for c in self.circuits]})")


def _parse_args():
    p = argparse.ArgumentParser(description="SMTCell solve driver (spnr mode).")
    p.add_argument("--mode", default="spnr", help="Run mode (only 'spnr' is implemented).")
    p.add_argument("--tech", required=True, help="Technology: QFET | CFET | FinFET.")
    p.add_argument("--layer", required=True, help="Path to the layer-stack JSON.")
    p.add_argument("--lib_name", default="PROBE", help="Library name.")
    p.add_argument("--track", type=int, default=4, help="Number of routing tracks.")
    p.add_argument("--height_config", default="SH", help="Height configuration (SH).")
    p.add_argument("--cell_config", required=True,
                   help="Per-cell config JSON produced by `make config`.")
    p.add_argument("--netlist", required=True, help="CDL netlist file.")
    p.add_argument("--cell_names", nargs="+", default=[],
                   help="Cell name(s) to solve (default: every subckt in the netlist).")
    p.add_argument("--output_dir", default="./output/", help="Output directory.")
    p.add_argument("--flag_log_constraints", default="False",
                   help="'True' to dump the constraint log, else 'False'.")
    return p.parse_args()


def main():
    args = _parse_args()
    if args.mode != "spnr":
        raise ValueError(f"Unsupported --mode {args.mode!r}; only 'spnr' is implemented.")

    flag_log = str(args.flag_log_constraints).strip().lower() == "true"
    stack = LayerStack(args.layer)
    technology = build_technology(
        tech=args.tech,
        lib_name=args.lib_name,
        layer_stack=stack,
        num_rt_track=args.track,
        height_config=args.height_config,
    )
    SMTCell(
        cdl_file=args.netlist,
        cell_config=args.cell_config,
        technology=technology,
        circuit_names=args.cell_names,
        output_dir=args.output_dir,
        flag_log_constraints=flag_log,
    )


if __name__ == "__main__":
    main()
