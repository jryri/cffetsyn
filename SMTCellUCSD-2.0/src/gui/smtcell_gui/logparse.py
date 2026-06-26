"""Parse a solver log for solve status / elapsed / objective.

The Makefile ``status`` target's time and objective columns are BROKEN
(wrong awk field; greps a token the loguru log never emits), so the GUI
parses ``<OUT_DIR>/logs/<CELL>.log`` directly with these patterns.
"""
from __future__ import annotations

import re
from pathlib import Path

# 'status: OPTIMAL' is printed raw at line start (no loguru timestamp).
# Anchor on '^status:' so it never matches the distinct 'LRAT_status: NA'.
_STATUS = re.compile(r"^status:\s*(\S+)", re.MULTILINE)
# loguru: '... - Elapsed time: 0.54 seconds'
_ELAPSED = re.compile(r"Elapsed time:\s*([0-9.]+)\s*seconds")
# ' Obj#1 <lambda> = 1  (min, w=1000, result=1000)'  -> primary objective
_OBJ = re.compile(r"Obj#1\s+\S+\s*=\s*(-?[0-9]+)")

_OK = {"OPTIMAL", "FEASIBLE"}


def parse_log(path: Path | None) -> dict[str, str]:
    """Return ``{'status','elapsed','obj'}`` (missing fields -> '').

    Takes the FIRST status match (CFET logs emit it twice).
    """
    res = {"status": "", "elapsed": "", "obj": ""}
    if not path:
        return res
    p = Path(path)
    if not p.is_file():
        return res
    text = p.read_text(encoding="utf-8", errors="replace")
    if (m := _STATUS.search(text)):
        res["status"] = m.group(1)
    if (m := _ELAPSED.search(text)):
        res["elapsed"] = m.group(1)
    if (m := _OBJ.search(text)):
        res["obj"] = m.group(1)
    return res


def status_is_ok(status: str) -> bool:
    """True for a solved cell (OPTIMAL / FEASIBLE)."""
    return status.upper() in _OK
