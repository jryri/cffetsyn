"""PresetEditor - create / edit an ``input/presets/<name>.mk`` preset.

A small self-contained QDialog that renders the preset variables as
editable fields, then writes a valid GNU-Make ``.mk`` back to disk.

Fixed (non-free) parameters - HEIGHT_CONFIG, CHANNEL, TRACK - are
dropdowns with the currently-supported value(s) only. CPP, M1P and M1OF
are NOT hand-entered: they are derived read-only from the selected
LAYER_FILE (PC pitch / M1 pitch / M1 offset) so a preset can never drift
out of sync with its layer JSON - those are the values the solver actually
uses. CELL_NAME is NOT edited here: the main window's cell list is the
source of truth and writes it live (see :func:`set_preset_cells`); the
editor preserves whatever CELL_NAME the preset already has. NUM_PC_LAYER /
NUM_RT_LAYER are not surfaced (unused) and are dropped on save.

Emits :attr:`saved` (``Signal(str)`` carrying the preset *name*) after a
successful write so a parent window can refresh its preset list.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from . import paths
from .preset_parser import parse_preset, parse_preset_dict

# A preset *name* must be a filesystem-friendly identifier (-> <name>.mk).
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Fixed-choice dropdowns (only these values are supported; an
# unrecognised value loaded from disk is preserved by appending it).
_TECH_CHOICES = ["QFET", "CFET", "FinFET"]
_HEIGHT_CHOICES = ["SH"]
_CHANNEL_CHOICES = ["2F"]
_TRACK_CHOICES = ["4"]

# Read-only fields derived from the LAYER_FILE: which layer + JSON key each
# one reads, and a short human label for the tooltip.
_LINE_KEYS = ["CPP", "M1P", "M1OF"]
_DERIVED_FROM = {
    "CPP":  ("PC", "pitch",  "PC pitch"),
    "M1P":  ("M1", "pitch",  "M1 pitch"),
    "M1OF": ("M1", "offset", "M1 offset"),
}

# Order assignments are emitted (CELL_NAME handled separately; CONFIG_OVERRIDES
# always last as its own block; NUM_PC_LAYER/NUM_RT_LAYER intentionally absent).
_WRITE_ORDER = [
    "TECH", "HEIGHT_CONFIG", "CHANNEL", "TRACK", "CPP", "M1P", "M1OF",
    "CDL_FILE", "LAYER_FILE",
]

# Sensible defaults for a brand-new (blank) preset. CPP/M1P/M1OF are
# intentionally absent - they are derived from the LAYER_FILE, not typed.
_NEW_DEFAULTS: dict[str, str] = {
    "TECH": "FinFET", "HEIGHT_CONFIG": "SH", "CHANNEL": "2F", "TRACK": "4",
    "CDL_FILE": "", "LAYER_FILE": "", "CELL_NAME": "", "CONFIG_OVERRIDES": "",
}

# Matches a whole ``CELL_NAME = ...`` assignment, including backslash-
# continued lines, so it can be replaced in place without touching the rest
# of the file.
_CELL_NAME_RE = re.compile(
    r"(?m)^[ \t]*CELL_NAME[ \t]*(?::=|\?=|\+=|=)(?:[^\n]*\\\n)*[^\n]*"
)


def _layer_dir() -> Path:
    return paths.project_root() / "input" / "layer"


def _implied_layer_file(values: dict[str, str]) -> str:
    """The layer JSON the Makefile auto-selects when a preset omits LAYER_FILE.

    Mirrors the Makefile convention
    ``input/layer/<CELL_PREFIX>_<TECH>_<CHANNEL>_<TRACK>T_<CPP><M1P>OF<M1OF>.json``
    (CELL_PREFIX defaults to PROBE3). Returns a repo-relative path when that
    file exists, else "" - so the layer file (the real source of CPP/M1P/M1OF)
    is shown even for presets that never spelled out LAYER_FILE.
    """
    need = ("TECH", "CHANNEL", "TRACK", "CPP", "M1P", "M1OF")
    v = {k: (values.get(k, "") or "").strip() for k in need}
    if not all(v.values()):
        return ""
    prefix = (values.get("CELL_PREFIX", "") or "PROBE3").strip()
    libname = (f"{prefix}_{v['TECH']}_{v['CHANNEL']}_{v['TRACK']}T_"
               f"{v['CPP']}{v['M1P']}OF{v['M1OF']}")
    rel = f"input/layer/{libname}.json"
    return rel if (paths.project_root() / rel).is_file() else ""


def _cdl_dir() -> Path:
    return paths.project_root() / "input" / "cdl"


def _read_description(mk: Path) -> str:
    """First leading ``# ...`` comment of a preset (the human label)."""
    if not mk.is_file():
        return ""
    for raw in mk.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        if s:
            break
    return ""


def _rel_to_root(path: str) -> str:
    """Normalise a chosen absolute path to a repo-relative one when possible."""
    s = path.strip()
    if not s:
        return ""
    p = Path(s)
    if not p.is_absolute():
        return s
    try:
        return p.resolve().relative_to(paths.project_root().resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _format_config_overrides(text: str) -> list[str]:
    """Split a free-form CONFIG_OVERRIDES blob into ``key=value`` tokens."""
    return [t for t in text.replace("\\", " ").split() if t.strip()]


def _cell_name_block(cells: list[str]) -> str:
    """The ``CELL_NAME = ...`` assignment text for *cells*.

    A backslash continues each line except the last; no space precedes the
    backslash (Make + preset_parser both collapse ``\\\\\\n   `` to a single
    space, so ``tok\\`` round-trips to exactly the right value)."""
    if not cells:
        return "CELL_NAME ="
    if len(cells) == 1:
        return f"CELL_NAME = {cells[0]}"
    out = [f"CELL_NAME = {cells[0]}\\"]
    out += [f"             {c}\\" for c in cells[1:-1]]
    out.append(f"             {cells[-1]}")
    return "\n".join(out)


def set_preset_cells(name: str, cells: list[str]) -> bool:
    """Write *cells* into ``input/presets/<name>.mk`` as CELL_NAME, IN PLACE.

    A surgical replacement of just the CELL_NAME assignment - every other
    line (TECH, CONFIG_OVERRIDES, comments, formatting) is left untouched.
    This is what the main window's cell list calls on every toggle so the
    preset always reflects the live selection. Returns False if the preset
    file does not exist.
    """
    mk = paths.presets_dir() / f"{name}.mk"
    if not mk.is_file():
        return False
    text = mk.read_text(encoding="utf-8", errors="replace")
    block = _cell_name_block(cells)
    if _CELL_NAME_RE.search(text):
        # lambda replacement so backslashes in `block` aren't interpreted.
        text = _CELL_NAME_RE.sub(lambda _m: block, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + block + "\n"
    if not text.endswith("\n"):
        text += "\n"
    mk.write_text(text, encoding="utf-8")
    return True


class PresetEditor(QDialog):
    """Create or edit a single ``input/presets/<name>.mk`` preset."""

    saved = Signal(str)  # emitted with the preset NAME after a successful write

    def __init__(self, parent=None, preset_name: str | None = None) -> None:
        super().__init__(parent)
        self._preset_name = preset_name  # None => brand-new preset
        self._cell_name = ""             # preserved CELL_NAME (edited elsewhere)
        self._line_edits: dict[str, QLineEdit] = {}
        self._fallback: dict[str, str] = {}   # on-disk CPP/M1P/M1OF (used only
                                              # when the layer file can't be read)

        self.setWindowTitle(
            f"Edit preset — {preset_name}" if preset_name else "New preset"
        )
        self.setMinimumWidth(560)
        self._build_ui()
        self._load()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._desc = QLineEdit()
        self._desc.setPlaceholderText("e.g. QFET 4-Track preset")
        form.addRow("Description", self._desc)

        self._tech = self._combo(_TECH_CHOICES)
        form.addRow("TECH", self._tech)

        # Fixed (not freely toggleable) parameters - dropdowns.
        self._height = self._combo(_HEIGHT_CHOICES)
        form.addRow("HEIGHT_CONFIG", self._height)
        self._channel = self._combo(_CHANNEL_CHOICES)
        form.addRow("CHANNEL", self._channel)
        self._track = self._combo(_TRACK_CHOICES)
        form.addRow("TRACK", self._track)

        self._cdl = QLineEdit()
        form.addRow("CDL_FILE", self._browse_row(self._cdl, self._browse_cdl))
        self._layer = QLineEdit()
        # Re-derive CPP/M1P/M1OF whenever the layer file changes (browse or
        # typing). setText() during _load() also fires this, so they always
        # reflect the layer file on open.
        self._layer.textChanged.connect(self._refresh_derived)
        form.addRow("LAYER_FILE", self._browse_row(self._layer, self._browse_layer))

        # CPP / M1P / M1OF are read-only mirrors of the layer JSON (PC pitch,
        # M1 pitch, M1 offset) - the values the solver actually uses. Shown
        # right under LAYER_FILE so the source is obvious; never hand-entered.
        for key in _LINE_KEYS:
            _, _, human = _DERIVED_FROM[key]
            edit = QLineEdit()
            edit.setReadOnly(True)
            edit.setFocusPolicy(Qt.ClickFocus)        # selectable, not editable
            edit.setPlaceholderText("— set LAYER_FILE —")
            edit.setToolTip(f"{human}, read from the layer file (read-only)")
            self._line_edits[key] = edit
            form.addRow(f"{key}  ·  from layer", edit)

        self._overrides = QPlainTextEdit()
        self._overrides.setPlaceholderText(
            "one key=value per line, e.g.\nmax_time.time=3600\nlig_routing=true"
        )
        self._overrides.setMinimumHeight(96)
        form.addRow("CONFIG_OVERRIDES", self._overrides)

        outer.addLayout(form)

        hint = QLabel(
            "Cells are chosen in the main window (and saved to the preset "
            "automatically). Save overwrites the .mk (confirms first); "
            "Save As… writes a new input/presets/<name>.mk."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self._btn_save = QPushButton("Save")
        self._btn_save.setObjectName("primary")
        self._btn_save.clicked.connect(self._on_save)
        self._btn_save_as = QPushButton("Save As…")
        self._btn_save_as.clicked.connect(self._on_save_as)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self.reject)
        bar.addWidget(self._btn_save)
        bar.addWidget(self._btn_save_as)
        bar.addWidget(self._btn_cancel)
        outer.addLayout(bar)

    @staticmethod
    def _combo(choices: list[str]) -> QComboBox:
        c = QComboBox()
        c.addItems(choices)
        return c

    def _browse_row(self, edit: QLineEdit, on_browse) -> QWidget:
        host = QWidget()
        h = QHBoxLayout(host)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(edit, 1)
        btn = QPushButton("Browse…")
        btn.clicked.connect(on_browse)
        h.addWidget(btn)
        return host

    # ---------------------------------------------------------------- load
    def _load(self) -> None:
        if self._preset_name is None:
            self._populate(dict(_NEW_DEFAULTS), description="")
            return
        mk = paths.presets_dir() / f"{self._preset_name}.mk"
        values = dict(_NEW_DEFAULTS)
        values.update(parse_preset_dict(mk))
        self._populate(values, description=_read_description(mk))

    def _populate(self, values: dict[str, str], description: str) -> None:
        self._desc.setText(description)
        self._set_combo(self._tech, values.get("TECH", "FinFET"))
        self._set_combo(self._height, values.get("HEIGHT_CONFIG", "SH"))
        self._set_combo(self._channel, values.get("CHANNEL", "2F"))
        self._set_combo(self._track, values.get("TRACK", "4"))
        # Stash any on-disk CPP/M1P/M1OF as a fallback, then let the layer file
        # drive the (read-only) derived fields.
        self._fallback = {k: (values.get(k, "") or "").strip() for k in _LINE_KEYS}
        self._cdl.setText(values.get("CDL_FILE", ""))
        # Prefer an explicit LAYER_FILE; otherwise resolve the one the Makefile
        # would auto-pick, so the layer file (the real source of CPP/M1P/M1OF)
        # is always shown and CPP/M1P/M1OF can be derived from it.
        layer = (values.get("LAYER_FILE", "") or "").strip() or _implied_layer_file(values)
        self._layer.setText(layer)                         # fires _refresh_derived
        self._refresh_derived()                            # also cover no-change case
        self._cell_name = values.get("CELL_NAME", "")  # preserved verbatim
        ov = _format_config_overrides(values.get("CONFIG_OVERRIDES", ""))
        self._overrides.setPlainText("\n".join(ov))

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        value = (value or "").strip()
        if not value:
            return
        i = combo.findText(value)
        if i < 0:                       # preserve an unsupported on-disk value
            combo.addItem(value)
            i = combo.findText(value)
        combo.setCurrentIndex(max(0, i))

    # ------------------------------------------------------- derived fields
    @staticmethod
    def _fmt_num(v) -> str:
        """45.0 -> "45", 21.5 -> "21.5" (match the LIBNAME / on-disk style)."""
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)

    def _derive_from_layer(self) -> dict[str, str] | None:
        """Read CPP/M1P/M1OF out of the currently-selected LAYER_FILE.

        Returns the three values as formatted strings, or ``None`` if the
        path is empty, unreadable, not valid JSON, or missing PC/M1 pitches.
        """
        rel = self._layer.text().strip()
        if not rel:
            return None
        p = Path(rel)
        if not p.is_absolute():
            p = paths.project_root() / rel
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        out: dict[str, str] = {}
        for key, (layer, field, _human) in _DERIVED_FROM.items():
            try:
                out[key] = self._fmt_num(data[layer][field])
            except (KeyError, TypeError, ValueError):
                return None
        return out

    def _refresh_derived(self) -> None:
        """Repopulate the read-only CPP/M1P/M1OF fields from the layer file."""
        derived = self._derive_from_layer()
        for key, edit in self._line_edits.items():
            _, _, human = _DERIVED_FROM[key]
            if derived is not None:
                edit.setText(derived[key])
                edit.setToolTip(f"{human} = {derived[key]} (from the layer file)")
            elif self._fallback.get(key):
                edit.setText(self._fallback[key])
                edit.setToolTip(
                    f"{human}: stored value; layer file not readable — "
                    "re-select LAYER_FILE to refresh"
                )
            else:
                edit.clear()
                edit.setToolTip(f"{human}: set LAYER_FILE to auto-load")

    # -------------------------------------------------------------- browse
    def _browse_cdl(self) -> None:
        self._browse_into(self._cdl, _cdl_dir(), "CDL files (*.cdl *.sp);;All files (*)")

    def _browse_layer(self) -> None:
        self._browse_into(self._layer, _layer_dir(), "Layer JSON (*.json);;All files (*)")

    def _browse_into(self, edit: QLineEdit, start: Path, filt: str) -> None:
        start_dir = start if start.is_dir() else paths.project_root()
        chosen, _ = QFileDialog.getOpenFileName(self, "Select file", str(start_dir), filt)
        if chosen:
            edit.setText(_rel_to_root(chosen))

    # ------------------------------------------------------------- render
    def _render(self) -> str:
        desc = self._desc.text().strip()
        lines: list[str] = [f"# {desc}" if desc else "#"]

        values = {
            "TECH": self._tech.currentText().strip(),
            "HEIGHT_CONFIG": self._height.currentText().strip(),
            "CHANNEL": self._channel.currentText().strip(),
            "TRACK": self._track.currentText().strip(),
            "CDL_FILE": self._cdl.text().strip(),
            "LAYER_FILE": self._layer.text().strip(),
        }
        for key, edit in self._line_edits.items():
            values[key] = edit.text().strip()

        for key in _WRITE_ORDER:
            val = values.get(key, "")
            if val:
                lines.append(f"{key} = {val}")

        cells = self._cell_name.split()  # preserved from load (edited elsewhere)
        if cells:
            lines.append(_cell_name_block(cells))

        ov = _format_config_overrides(self._overrides.toPlainText())
        if ov:
            lines.append("")
            lines.append("CONFIG_OVERRIDES := \\")
            lines += [f"  {tok}\\" for tok in ov[:-1]]
            lines.append(f"  {ov[-1]}")

        return "\n".join(lines) + "\n"

    # --------------------------------------------------------------- save
    def _validate(self) -> bool:
        if not self._tech.currentText().strip():
            self._warn("TECH is required.")
            return False
        return True

    def _write(self, name: str, mk: Path) -> bool:
        try:
            mk.parent.mkdir(parents=True, exist_ok=True)
            mk.write_text(self._render(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Write failed", f"Could not write {mk}:\n{exc}")
            return False
        self._preset_name = name
        self.setWindowTitle(f"Edit preset — {name}")
        self.saved.emit(name)
        return True

    def _on_save(self) -> None:
        if not self._validate():
            return
        if not self._preset_name:           # brand-new -> Save As
            self._on_save_as()
            return
        mk = paths.presets_dir() / f"{self._preset_name}.mk"
        if mk.exists():
            resp = QMessageBox.question(
                self, "Overwrite preset",
                f"Overwrite the existing preset file?\n\n{mk}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        if self._write(self._preset_name, mk):
            self.accept()

    def _on_save_as(self) -> None:
        if not self._validate():
            return
        suggested = self._preset_name or ""
        while True:
            name, ok = QInputDialog.getText(
                self, "Save preset as",
                "New preset name (letters, digits, underscore):",
                QLineEdit.Normal, suggested,
            )
            if not ok:
                return
            name = name.strip()
            if not _NAME_RE.match(name):
                QMessageBox.warning(
                    self, "Invalid name",
                    "Use a simple identifier: start with a letter or "
                    "underscore, then letters / digits / underscores only.",
                )
                suggested = name
                continue
            mk = paths.presets_dir() / f"{name}.mk"
            if mk.exists():
                resp = QMessageBox.question(
                    self, "Preset exists",
                    f"A preset named '{name}' already exists.\nOverwrite it?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if resp != QMessageBox.Yes:
                    suggested = name
                    continue
            if self._write(name, mk):
                self.accept()
            return

    # --------------------------------------------------------------- misc
    def _warn(self, msg: str) -> None:
        QMessageBox.warning(self, "Cannot save", msg)
