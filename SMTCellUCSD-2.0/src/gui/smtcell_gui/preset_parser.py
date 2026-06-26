"""Minimal Makefile-style .mk parser for `input/presets/*.mk`.

Returns an ordered list of (key, value) so the GUI can render variables
in the same order the preset author wrote them. NOT a full GNU Make
expression evaluator - values are returned verbatim (no `$(VAR)` expansion).
"""
from __future__ import annotations

import re
from pathlib import Path

# `KEY OP VALUE` where OP is one of =, :=, ?=, +=, ::=
_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\?=|:=|::=|\+=|=)\s*(.*)$"
)


def parse_preset(path: Path) -> list[tuple[str, str]]:
    """Parse a preset .mk file and return `[(key, value), ...]` in order."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")

    # Stitch continuation lines: `... \\\n   more` -> `... more`.
    text = re.sub(r"\\\n[ \t]*", " ", text)

    results: list[tuple[str, str]] = []
    seen: dict[str, int] = {}

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        key, _op, value = m.group(1), m.group(2), m.group(3)
        value = _strip_inline_comment(value).strip()
        if key in seen:
            results[seen[key]] = (key, value)
        else:
            seen[key] = len(results)
            results.append((key, value))
    return results


def parse_preset_dict(path: Path) -> dict[str, str]:
    return dict(parse_preset(path))


def _strip_inline_comment(s: str) -> str:
    """Strip a trailing `# ...` comment but keep `#` inside quotes (rare)."""
    in_dq = in_sq = False
    for i, ch in enumerate(s):
        if ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == "#" and not in_dq and not in_sq:
            return s[:i]
    return s
