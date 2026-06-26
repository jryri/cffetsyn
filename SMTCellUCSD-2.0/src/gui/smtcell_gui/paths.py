"""Project paths + `make show-config` resolution for the chosen preset.

The GUI never hardcodes output locations: it asks the Makefile (via
``make show-config``) for the derived ``LIBNAME`` and ``OUT_DIR`` so it
always agrees with the flow about where logs / view PNGs / GDS / LEF land.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def project_root() -> Path:
    # .../src/gui/smtcell_gui/paths.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def makefile_path() -> Path:
    return project_root() / "Makefile"


def presets_dir() -> Path:
    return project_root() / "input" / "presets"


def list_config_presets() -> list[str]:
    """Sorted preset names (``input/presets/*.mk`` stems)."""
    d = presets_dir()
    return sorted(p.stem for p in d.glob("*.mk")) if d.is_dir() else []


def preset_description(config: str) -> str:
    """First comment line of a preset .mk (used as a human label)."""
    mk = presets_dir() / f"{config}.mk"
    if not mk.is_file():
        return ""
    for raw in mk.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        if s:
            break
    return ""


def show_config(config: str, cell_name: str = "") -> dict[str, str]:
    """Run ``make show-config`` and return the derived ``KEY=VALUE`` dict.

    Keys include TECH, HEIGHT_CONFIG, CHANNEL, TRACK, CPP, M1P, M1OF,
    CDL_FILE, CELL_NAME, LIBNAME, OUT_DIR. Returns ``{}`` on any failure.
    Always read TECH from here, never infer it from the preset name.
    """
    if not config:
        return {}
    args = ["make", "show-config", f"CONFIG={config}"]
    if cell_name:
        args.append(f"CELL_NAME={cell_name}")
    try:
        out = subprocess.run(
            args, cwd=project_root(), capture_output=True,
            text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    vals: dict[str, str] = {}
    for ln in out.stdout.splitlines():
        if "=" in ln:
            k, _, v = ln.partition("=")
            vals[k.strip()] = v.strip()
    return vals


def out_dir(cfg: dict[str, str]) -> Path | None:
    """Absolute OUT_DIR from a show_config() dict (e.g. ./output/<LIB>/SH)."""
    od = cfg.get("OUT_DIR")
    return (project_root() / od).resolve() if od else None


def gds_path(cfg: dict[str, str]) -> Path | None:
    od, lib = out_dir(cfg), cfg.get("LIBNAME")
    return (od / "gds" / f"{lib}.gds") if (od and lib) else None


def lef_path(cfg: dict[str, str]) -> Path | None:
    od, lib = out_dir(cfg), cfg.get("LIBNAME")
    return (od / "lef" / f"{lib}.lef") if (od and lib) else None


def view_png(cfg: dict[str, str], cell: str) -> Path | None:
    od = out_dir(cfg)
    return (od / "view" / f"{cell}.png") if od else None


def log_path(cfg: dict[str, str], cell: str) -> Path | None:
    od = out_dir(cfg)
    return (od / "logs" / f"{cell}.log") if od else None


def config_json_path(cfg: dict[str, str], cell: str) -> Path | None:
    od = out_dir(cfg)
    return (od / "config" / f"{cell}.json") if od else None


def preset_overrides(config: str) -> list[str]:
    """The preset's CONFIG_OVERRIDES as ``key=value`` tokens (for generating a
    cell's default config the same way ``make config`` does).

    NOTE: tokens are returned verbatim from ``preset_parser`` (no GNU Make
    ``$(VAR)`` expansion). All current presets use plain scalars here, so the
    GUI popup's default-config preview matches ``make config`` exactly. If a
    future preset references a Make variable in CONFIG_OVERRIDES (e.g.
    ``foo=$(TRACK)``), this would pass the literal ``$(TRACK)`` token while
    ``make config`` passes the expanded value -- a GUI-vs-CLI divergence. Keep
    CONFIG_OVERRIDES values as literals, or teach this function to resolve
    ``$(...)`` via ``show_config()`` before relying on it for such presets."""
    from .preset_parser import parse_preset_dict
    mk = presets_dir() / f"{config}.mk"
    if not mk.is_file():
        return []
    raw = parse_preset_dict(mk).get("CONFIG_OVERRIDES", "")
    return [t for t in raw.split() if "=" in t]
