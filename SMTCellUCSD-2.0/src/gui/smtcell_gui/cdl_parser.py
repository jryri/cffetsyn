"""Extract `.SUBCKT` (cell) names from a CDL / SPICE netlist.

Slimmed from the previous GUI's parser: only ``scan_cdl`` is needed to
populate the cell picker. Case-insensitive on the directive; the cell
name is the first whitespace token following ``.subckt``.
"""
from __future__ import annotations

import re
from pathlib import Path

_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)", re.IGNORECASE)


def scan_cdl(path: Path) -> list[str]:
    """Return the subckt names found in `path`, in declaration order."""
    if not path.is_file():
        return []
    names: list[str] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = _SUBCKT_RE.match(raw)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names
