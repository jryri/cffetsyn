"""Per-cell config editor: every parameter of a cell's config JSON, editable.

Opened from a (clickable) cell name in the status panel. Shows the cell's
config - its existing ``config/<cell>.json`` if generated, otherwise the
defaults baked in by ``src.cellgen.archit.config`` (same defaults +
preset-overrides + per-cell heuristics ``make config`` would write). On
Save it writes ``config/<cell>.json`` directly; because ``make config`` now
keeps an existing config, that file is then never overwritten by a re-run.

Note: the form is built from the keys present in the config it is handed.
For an already-generated ``config/<cell>.json`` written by an older
``CONFIG_TEMPLATE``, only that file's keys are shown; parameters added to
the template afterwards do not appear (existing keys are preserved on save,
nothing is dropped). Delete the cell's config to regenerate with the full,
current parameter set.
"""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget,
    QVBoxLayout, QWidget,
)

_TAG_RE = re.compile(r"\[(\w+)\]")
# Matches a single leading "[TAG] " prefix so it can be stripped from the
# human-readable description (the tag is already shown as the tab name).
_TAG_PREFIX_RE = re.compile(r"^\s*\[\w+\]\s*")
# Preferred tab order; any other tag found is appended after these.
_TAG_ORDER = ["TECH", "COST", "ROUTING", "PIN", "GDS", "SOLVER", "SPEEDUP", "INJECT"]


def _tag_of(info: str) -> str:
    m = _TAG_RE.search(info or "")
    return m.group(1) if m else "OTHER"


def _describe(info: str) -> str:
    """Human-readable description: the info string with its leading [TAG] removed."""
    return _TAG_PREFIX_RE.sub("", info or "").strip()


class CellConfigEditor(QDialog):
    """Edit one cell's config JSON. Emits ``saved(cell)`` after writing."""

    saved = Signal(str)

    def __init__(self, parent, cell: str, config: dict, save_path: Path) -> None:
        super().__init__(parent)
        self._cell = cell
        self._config = copy.deepcopy(config)
        self._save_path = Path(save_path)
        # (key, subkey_or_None, widget, type-tag) for reassembly on save.
        self._editors: list[tuple] = []

        self.setWindowTitle(f"Cell config — {cell}")
        self.resize(760, 680)
        self._build()

    # ------------------------------------------------------------------ UI
    # Scoped to this dialog only (objectName selectors), so it does not
    # perturb the app-wide stylesheet. Cards are subtle panels; names are
    # bold; descriptions reuse the global dimmed "hint" colour.
    _CARD_QSS = """
    QFrame#paramCard { border: 1px solid palette(mid); border-radius: 8px; }
    QLabel#paramName { font-weight: 700; }
    QLabel#sectionHead { font-weight: 700; font-size: 14px; }
    """

    def _build(self) -> None:
        self.setStyleSheet(self._CARD_QSS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        hint = QLabel(
            f"Config for <b>{self._cell}</b>. Save writes "
            f"<code>{self._save_path}</code>; once saved, <code>make config</code> "
            f"keeps it as-is (never overwritten)."
        )
        hint.setObjectName("hint"); hint.setWordWrap(True)
        outer.addWidget(hint)

        groups: dict[str, list[tuple[str, dict]]] = {}
        for key, entry in self._config.items():
            tag = _tag_of(entry.get("info", "")) if isinstance(entry, dict) else "OTHER"
            groups.setdefault(tag, []).append((key, entry))

        tabs = QTabWidget()
        for tag in _TAG_ORDER + [t for t in groups if t not in _TAG_ORDER]:
            if tag in groups:
                tabs.addTab(self._make_tab(tag, groups[tag]), tag)
        outer.addWidget(tabs, 1)

        bar = QHBoxLayout(); bar.addStretch(1)
        save = QPushButton("Save"); save.setObjectName("primary")
        save.clicked.connect(self._on_save)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        bar.addWidget(save); bar.addWidget(cancel)
        outer.addLayout(bar)

    def _make_tab(self, tag: str, entries: list[tuple[str, dict]]) -> QScrollArea:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(12, 12, 12, 12)
        col.setSpacing(10)

        head = QLabel(f"{tag} parameters")
        head.setObjectName("sectionHead")
        col.addWidget(head)

        for key, entry in entries:
            if not isinstance(entry, dict):           # a bare value (rare)
                w, typ = self._editor(entry)
                col.addWidget(self._card(key, None, "", w))
                self._editors.append((key, None, w, typ))
                continue
            info = entry.get("info", "")
            subkeys = [k for k in entry if k != "info"]
            for subkey in subkeys:
                w, typ = self._editor(entry[subkey])
                # Only the primary "value" carries the description; extra
                # sub-keys (e.g. max_time.time) get a derived caption so it
                # is clear which parameter they qualify.
                if subkey == "value":
                    name, sub, desc = key, None, _describe(info)
                else:
                    name, sub = key, subkey
                    desc = f"Sub-setting of {key}." if not info else \
                        f"Sub-setting of {key} — {_describe(info)}"
                if info:
                    w.setToolTip(info)
                col.addWidget(self._card(name, sub, desc, w))
                self._editors.append((key, subkey, w, typ))

        col.addStretch(1)
        scroll.setWidget(host)
        return scroll

    def _card(self, name: str, subkey: str | None, desc: str, editor: QWidget) -> QFrame:
        """A tidy row: bold name (+ optional .subkey tag), the editor, a
        dimmed wrapped description. JSON editors stack the field below the
        header; scalar editors sit on the same line for compactness."""
        card = QFrame(); card.setObjectName("paramCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        is_json = isinstance(editor, QPlainTextEdit)
        is_check = isinstance(editor, QCheckBox)

        header = QHBoxLayout(); header.setSpacing(8)
        title = QLabel(name); title.setObjectName("paramName")
        header.addWidget(title)
        if subkey is not None:
            tag = QLabel(f".{subkey}"); tag.setObjectName("hint")  # dimmed
            header.addWidget(tag)
        # Scalar / boolean editors: name on the left, field on the right.
        if not is_json:
            header.addStretch(1)
            if is_check:
                header.addWidget(editor)
            else:
                editor.setMinimumWidth(220)
                editor.setMaximumWidth(360)
                header.addWidget(editor)
        lay.addLayout(header)

        if desc:
            d = QLabel(desc); d.setObjectName("hint")
            d.setWordWrap(True)
            d.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(d)

        # JSON / rule editors: clearly labelled, full-width below the text.
        if is_json:
            field_lbl = QLabel("Rule (JSON):"); field_lbl.setObjectName("hint")
            lay.addWidget(field_lbl)
            editor.setMinimumHeight(90)
            lay.addWidget(editor)

        return card

    @staticmethod
    def _editor(val):
        if isinstance(val, bool):
            w = QCheckBox(); w.setChecked(val); return w, "bool"
        if isinstance(val, int):
            return QLineEdit(str(val)), "int"
        if isinstance(val, float):
            return QLineEdit(repr(val)), "float"
        if isinstance(val, str):
            return QLineEdit(val), "str"
        if isinstance(val, (dict, list)):
            te = QPlainTextEdit(json.dumps(val, indent=2))
            te.setMaximumHeight(120)
            return te, "json"
        # None / unknown -> editable text, parsed back leniently
        return QLineEdit("" if val is None else str(val)), "none"

    @staticmethod
    def _read(w, typ):
        if typ == "bool":
            return w.isChecked()
        if typ == "int":
            return int(w.text().strip())
        if typ == "float":
            v = float(w.text().strip())
            if not math.isfinite(v):          # inf/-inf/nan are not valid JSON
                raise ValueError("must be a finite number")
            return v
        if typ == "str":
            return w.text()
        if typ == "json":
            return json.loads(w.toPlainText())
        t = w.text().strip()                          # "none"
        return None if t in ("", "null", "None") else t

    # --------------------------------------------------------------- save
    def _on_save(self) -> None:
        cfg = self._config
        for key, subkey, w, typ in self._editors:
            try:
                v = self._read(w, typ)
            except (ValueError, json.JSONDecodeError) as exc:
                name = key if subkey in (None, "value") else f"{key}.{subkey}"
                QMessageBox.warning(self, "Invalid value", f"{name}: {exc}")
                return
            if subkey is None:
                cfg[key] = v
            else:
                cfg[key][subkey] = v
        try:
            self._save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._save_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)
        except OSError as exc:
            QMessageBox.critical(self, "Write failed", str(exc))
            return
        self.saved.emit(self._cell)
        self.accept()
