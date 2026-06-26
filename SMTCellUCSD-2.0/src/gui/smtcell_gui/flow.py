"""The four-stage SMTCell flow, as data.

Each stage maps to a single ``make <target>`` invocation. The GUI runs
them individually (one button each) or chained in order via "Run all".
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    target: str            # make target name
    label: str             # button text
    tip: str               # tooltip / status hint
    long_running: bool = False
    produces_gds: bool = False


STAGES: tuple[Stage, ...] = (
    Stage("config", "1 · Config",
          "Generate the per-cell config JSON (fast).", False, False),
    Stage("spnr", "2 · SP&R",
          "Run the CP-SAT place & route solver (writes the result + view PNG).",
          True, False),
    Stage("gds", "3 · GDS",
          "Write the GDS layout from the solved result.", False, True),
    Stage("lef", "4 · LEF",
          "Emit the abstract LEF (requires the gds2gdt tool).", False, False),
)


def stage(target: str) -> Stage | None:
    for s in STAGES:
        if s.target == target:
            return s
    return None
