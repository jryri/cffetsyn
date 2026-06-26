"""SMTCell 2.0 control window.

Pick a CONFIG preset, pick cell(s), run the flow (config -> spnr -> gds ->
lef) with a live log, then view per-cell solve status and the result PNG.
Everything runs through ``make`` via :class:`MakeRunner`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QSettings, QProcess
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QStatusBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__, paths, style
from .cdl_parser import scan_cdl
from .flow import STAGES
from .logparse import parse_log, status_is_ok
from .result_view import ResultView
from .runner import MakeRunner

_FLOW_TARGETS = [s.target for s in STAGES]   # config, spnr, gds, lef

# The solver streams "Saved to .../view/<CELL>.png" as each cell finishes -
# the cue to flip that cell's status row live (mid-run, not at the end).
_CELL_DONE_RE = re.compile(r"/view/([A-Za-z0-9_]+)\.png\b")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SMTCell 2.0 GUI")
        self.resize(1240, 800)

        self._settings = QSettings("SMTCell", "smtcell-gui")
        self._cfg: dict[str, str] = {}          # last show-config result
        self._queue: list[str] = []             # remaining flow stages
        self._chain = False                     # Run-all in progress
        self._t0 = 0.0
        self._run_cells: list[str] = []         # cells snapshot for this run
        self._run_started_at = 0.0              # wall clock (vs file mtime)
        self._solved: dict[str, dict] = {}      # per-cell parsed log this session
        self._row_for_cell: dict[str, int] = {} # cell -> status-table row
        self._cell_for_row: dict[int, str] = {} # status-table row -> cell
        self._live_marked: set[str] = set()     # cells already flipped live this run
        self._cancelled = False                 # user cancelled the active run
        self._closing = False                   # window is tearing down

        self._runner = MakeRunner(paths.project_root(), self)
        self._runner.line.connect(self._on_line)
        self._runner.finished.connect(self._on_finished)
        self._runner.error.connect(self._on_error)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

        # Debounce live cell-selection writes to the preset .mk (batch rapid
        # toggles into one write that lands shortly after the user settles).
        self._cellsave_timer = QTimer(self)
        self._cellsave_timer.setSingleShot(True)
        self._cellsave_timer.setInterval(350)
        self._cellsave_timer.timeout.connect(self._live_save_cells)

        self._build_menu()
        self._build_ui()
        self._apply_theme()

        self._populate_presets()

    # ----------------------------------------------------------------- UI
    def _build_menu(self) -> None:
        m = self.menuBar()
        editm = m.addMenu("&Edit")
        a_ep = QAction("Edit current preset…", self); a_ep.triggered.connect(self._edit_preset)
        a_np = QAction("New preset…", self); a_np.triggered.connect(self._new_preset)
        editm.addActions([a_ep, a_np])

        helpm = m.addMenu("&Help")
        act_about = QAction("About", self); act_about.triggered.connect(self._about)
        helpm.addAction(act_about)

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        # --- header: preset + derived info ---
        head = QHBoxLayout()
        head.addWidget(QLabel("Config preset"))
        self._preset = QComboBox()
        self._preset.setMinimumWidth(220)
        self._preset.currentTextChanged.connect(self._on_config_changed)
        head.addWidget(self._preset)
        self._derived = QLabel("")
        self._derived.setObjectName("derived")
        head.addWidget(self._derived, 1)
        outer.addLayout(head)

        # --- main split: cells/status | run/log/result ---
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 900])
        outer.addWidget(split, 1)

        # --- status bar ---
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_cwd = QLabel(str(paths.project_root()))
        self._sb_out = QLabel("")
        sb.addWidget(self._sb_cwd)
        sb.addPermanentWidget(self._sb_out)

    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        cells = QGroupBox("Cells")
        cl = QVBoxLayout(cells)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("filter…")
        self._filter.textChanged.connect(self._apply_filter)
        cl.addWidget(self._filter)
        self._cells = QListWidget()
        self._cells.itemChanged.connect(self._on_cell_toggled)
        cl.addWidget(self._cells, 1)
        row = QHBoxLayout()
        self._b_all = QPushButton("All"); self._b_all.clicked.connect(lambda: self._check_all(True))
        self._b_none = QPushButton("None"); self._b_none.clicked.connect(lambda: self._check_all(False))
        self._cell_count = QLabel("0 selected"); self._cell_count.setObjectName("hint")
        row.addWidget(self._b_all); row.addWidget(self._b_none); row.addStretch(1)
        row.addWidget(self._cell_count)
        cl.addLayout(row)
        lay.addWidget(cells, 1)

        status = QGroupBox("Status")
        sl = QVBoxLayout(status)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Cell", "Status", "Time (s)", "Obj"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self._table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_status_selected)
        sl.addWidget(self._table)
        lay.addWidget(status, 1)
        return w

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)

        # run bar
        bar = QHBoxLayout()
        self._stage_btns: dict[str, QPushButton] = {}
        for s in STAGES:
            b = QPushButton(s.label.replace("&", "&&"))   # literal & (not a mnemonic)
            b.setToolTip(s.tip)
            b.clicked.connect(lambda _=False, t=s.target: self._run_single(t))
            bar.addWidget(b)
            self._stage_btns[s.target] = b
        self._btn_all = QPushButton("▶ Run all")
        self._btn_all.setObjectName("primary")
        self._btn_all.setToolTip("Run config → spnr → gds → lef in order, stopping on the first failure.")
        self._btn_all.clicked.connect(self._run_all)
        bar.addWidget(self._btn_all)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setObjectName("danger")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel)
        bar.addWidget(self._btn_cancel)
        bar.addStretch(1)
        self._elapsed = QLabel(""); self._elapsed.setObjectName("hint")
        bar.addWidget(self._elapsed)
        self._badge = QLabel("idle"); self._badge.setObjectName("badge")
        self._set_badge("idle", "idle")
        bar.addWidget(self._badge)
        lay.addLayout(bar)

        # result (top) | log (bottom) - the layout preview is the focus
        vsplit = QSplitter(Qt.Vertical)

        resbox = QGroupBox("Result")
        rl = QVBoxLayout(resbox)
        rrow = QHBoxLayout()
        self._result_title = QLabel(""); self._result_title.setObjectName("hint")
        # Ignored width + min 0: a long/short path can never move the buttons
        # or resize the row (this was part of the "panel pushes around" bug).
        self._result_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._result_title.setMinimumWidth(0)
        rrow.addWidget(self._result_title, 1)
        b_dir = QPushButton("Open output dir"); b_dir.clicked.connect(self._open_output_dir)
        b_gds = QPushButton("Open GDS in KLayout"); b_gds.clicked.connect(self._open_gds)
        rrow.addWidget(b_dir); rrow.addWidget(b_gds)
        rl.addLayout(rrow)
        self._resultview = ResultView()      # zoom/pan + per-net/layer toggles
        rl.addWidget(self._resultview, 1)
        vsplit.addWidget(resbox)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(20000)
        self._log.setPlaceholderText("make output streams here…")
        vsplit.addWidget(self._log)

        vsplit.setSizes([520, 300])
        lay.addWidget(vsplit, 1)
        return w

    # ------------------------------------------------------------- presets
    def _populate_presets(self) -> None:
        presets = paths.list_config_presets()
        self._preset.blockSignals(True)
        self._preset.clear()
        self._preset.addItems(presets)
        # Default the on-screen config to FinFET (4-track if present).
        default = ("FinFET_4T_SH" if "FinFET_4T_SH" in presets
                   else next((p for p in presets if p.startswith("FinFET")),
                             presets[0] if presets else ""))
        if default:
            self._preset.setCurrentText(default)
        self._preset.blockSignals(False)
        if not presets:
            self._log.appendPlainText("[no presets found under input/presets/*.mk]")
            return
        self._on_config_changed(self._preset.currentText())

    def _on_config_changed(self, name: str) -> None:
        if not name:
            return
        self._settings.setValue("config", name)
        self._cellsave_timer.stop()         # drop any pending live cell-save
        # show-config shells `make` (usually ~ms, but can stall on NFS); give a
        # cue and repaint before the bounded-timeout blocking call.
        self._derived.setText("resolving config…")
        QApplication.processEvents()
        self._cfg = paths.show_config(name)
        if not self._cfg:
            self._derived.setText("(could not resolve — is `make` available / preset valid?)")
            self._cells.clear()
            self._update_cell_count()
            self._reset_status_and_result()
            self._sb_out.setText("")
            return
        g = self._cfg.get
        self._derived.setText(
            f"TECH {g('TECH','?')}  ·  {g('TRACK','?')}T  ·  {g('HEIGHT_CONFIG','?')}  "
            f"·  {g('LIBNAME','?')}"
        )
        out = paths.out_dir(self._cfg)
        self._sb_out.setText(f"OUT_DIR: {out}" if out else "")
        self._reset_status_and_result()
        self._populate_cells()
        self._sync_status_table(self._selected_cells())  # show the preset's cells

    # --------------------------------------------------------------- cells
    def _populate_cells(self) -> None:
        cdl = self._cfg.get("CDL_FILE", "")
        cdl_path = (paths.project_root() / cdl) if cdl else None
        names = scan_cdl(cdl_path) if cdl_path else []
        default = set(self._cfg.get("CELL_NAME", "").split())
        self._cells.blockSignals(True)
        self._cells.clear()
        for nm in names:
            it = QListWidgetItem(nm)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if nm in default else Qt.Unchecked)
            self._cells.addItem(it)
        self._cells.blockSignals(False)
        if not names:
            self._log.appendPlainText(f"[no cells found in CDL: {cdl or '(unset)'}]")
        self._apply_filter(self._filter.text())
        self._update_cell_count()

    def _check_all(self, on: bool) -> None:
        # Operate over the ENTIRE list (not just filtered-visible items) so the
        # check state always matches what _selected_cells() will run - otherwise
        # a hidden-but-checked cell would run silently after "None".
        st = Qt.Checked if on else Qt.Unchecked
        self._cells.blockSignals(True)
        for i in range(self._cells.count()):
            self._cells.item(i).setCheckState(st)
        self._cells.blockSignals(False)
        self._update_cell_count()
        self._schedule_cell_save()          # signals were blocked; save explicitly
        self._live_status_sync()

    def _on_cell_toggled(self, *_) -> None:
        self._update_cell_count()
        self._schedule_cell_save()
        self._live_status_sync()

    def _live_status_sync(self) -> None:
        """Reflect the current selection in the status panel immediately (each
        toggled cell appears with a clickable name). Skipped during a run,
        where the locked snapshot drives the table."""
        if not self._runner.is_running:
            self._sync_status_table(self._selected_cells())

    def _schedule_cell_save(self) -> None:
        """Queue a live write of the current cell selection to the preset .mk
        (skipped while a run is in flight or before a preset has resolved)."""
        if self._cfg and self._preset.currentText() and not self._runner.is_running:
            self._cellsave_timer.start()

    def _live_save_cells(self) -> None:
        # A debounced save queued just before a run must not fire mid-run: it
        # would rewrite the preset's CELL_NAME from the live (not snapshot)
        # selection. The active run is unaffected (CELL_NAME is passed in kv),
        # but the preset on disk should stay stable while a run is in flight.
        if self._runner.is_running:
            return
        name = self._preset.currentText()
        if not name:
            return
        try:
            from .preset_editor import set_preset_cells
            set_preset_cells(name, self._selected_cells())
        except Exception as exc:  # never let a write hiccup disrupt the UI
            self._log.appendPlainText(f"[could not save cell selection: {exc}]")

    def _apply_filter(self, text: str) -> None:
        t = text.strip().lower()
        for i in range(self._cells.count()):
            it = self._cells.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _selected_cells(self) -> list[str]:
        return [self._cells.item(i).text() for i in range(self._cells.count())
                if self._cells.item(i).checkState() == Qt.Checked]

    def _update_cell_count(self) -> None:
        self._cell_count.setText(f"{len(self._selected_cells())} selected")

    # ----------------------------------------------------------------- run
    def _run_single(self, target: str) -> None:
        if not self._guard():
            return
        self._begin_run([target], chain=False)

    def _run_all(self) -> None:
        if not self._guard():
            return
        self._begin_run(list(_FLOW_TARGETS), chain=True)

    def _begin_run(self, queue: list[str], chain: bool) -> None:
        # Snapshot the cell selection ONCE so every stage of a Run-all uses the
        # identical CELL_NAME (the list is also locked during the run).
        self._run_cells = self._selected_cells()
        self._run_started_at = time.time()
        self._cancelled = False
        self._chain = chain
        self._queue = queue
        self._start_next()

    def _guard(self) -> bool:
        if self._runner.is_running:
            return False
        if not self._preset.currentText():
            self._warn("Pick a CONFIG preset first.")
            return False
        if not self._cfg:
            self._warn("Config did not resolve — is `make` available and the preset valid?")
            return False
        if not self._selected_cells():
            self._warn("Select at least one cell to run.")
            return False
        return True

    def _start_next(self) -> None:
        if not self._queue:
            self._set_running(False)
            return
        target = self._queue.pop(0)
        kv = {"CONFIG": self._preset.currentText(),
              "CELL_NAME": " ".join(self._run_cells)}
        self._current = target
        if target == "spnr":
            # Pre-seed the table with this run's cells (all "-"); each flips
            # to its result live as the solver streams its "Saved to ..." line.
            self._solved = {}
            self._live_marked = set()
            self._sync_status_table(self._run_cells)
        self._set_running(True)
        self._set_badge("run", f"{target}…")
        self._log.appendPlainText("")
        self._log.appendPlainText("─" * 64)
        self._t0 = time.monotonic()
        self._timer.start()
        self._runner.start(target, kv)

    def _on_line(self, line: str) -> None:
        self._log.appendPlainText(line)
        # Live status: when a cell's view PNG is written, that cell is done.
        if self._current == "spnr" and self._run_cells:
            m = _CELL_DONE_RE.search(line)
            if m:
                cell = m.group(1)
                if cell in self._row_for_cell and cell not in self._live_marked:
                    self._live_marked.add(cell)
                    self._mark_cell_done(cell)

    def _on_finished(self, code: int, elapsed: float) -> None:
        self._timer.stop()
        if self._closing:
            return
        self._elapsed.setText(f"{elapsed:.1f}s")
        # Cancelled run: report neutrally, never as a failure.
        if self._cancelled:
            self._cancelled = False
            self._log.appendPlainText(f"[{self._current}: cancelled]")
            self._set_badge("idle", "cancelled")
            self._chain = False
            self._queue = []
            self._set_running(False)
            return

        # spnr's exit code can be masked by `tee`; the authoritative success
        # signal is the parsed per-cell solve status, so refresh from logs first.
        if self._current == "spnr":
            self._update_solved()
            self._refresh_status()

        ok = code == 0
        if self._current == "spnr" and ok:
            unsolved = [c for c in self._run_cells
                        if not status_is_ok(self._solved.get(c, {}).get("status", ""))]
            if unsolved:
                ok = False
                self._log.appendPlainText(
                    f"[spnr: {len(unsolved)} cell(s) not solved: {' '.join(unsolved)}]")
        self._log.appendPlainText(f"[{self._current}: exit {code}  ·  {elapsed:.1f}s]")

        if ok and self._current in ("spnr", "gds"):
            self._auto_show_result()
        if ok and self._chain and self._queue:
            self._set_badge("ok", f"{self._current} ✓")
            self._start_next()
            return
        if not ok:
            self._log.appendPlainText("━" * 64)
            self._log.appendPlainText(f"FAIL: make {self._current} (exit {code})")
        # The whole run/flow is finished now - say "Done", not the last stage.
        self._set_badge("ok" if ok else "fail",
                        "Done" if ok else f"{self._current} ✗")
        self._chain = False
        self._queue = []
        self._set_running(False)

    def _on_error(self, msg: str) -> None:
        self._timer.stop()
        self._log.appendPlainText(f"[error] {msg}")
        self._set_badge("fail", "error")
        self._chain = False
        self._queue = []
        self._set_running(False)

    def _on_cancel(self) -> None:
        self._cancelled = True
        self._chain = False
        self._queue = []
        self._runner.cancel()

    def _set_running(self, running: bool) -> None:
        for b in self._stage_btns.values():
            b.setEnabled(not running)
        self._btn_all.setEnabled(not running)
        self._preset.setEnabled(not running)
        # Lock the cell selection so a Run-all can't have its CELL_NAME changed
        # mid-flight (every stage already shares the start-of-run snapshot).
        # All/None are included: setCheckState() bypasses the list's disabled
        # state, so leaving them live would silently change the selection.
        self._cells.setEnabled(not running)
        self._b_all.setEnabled(not running)
        self._b_none.setEnabled(not running)
        self._filter.setEnabled(not running)
        self._btn_cancel.setEnabled(running)
        if not running and self._badge.property("state") == "run":
            self._set_badge("idle", "idle")

    def _tick(self) -> None:
        self._elapsed.setText(f"running… {time.monotonic() - self._t0:.1f}s")

    def _set_badge(self, state: str, text: str) -> None:
        self._badge.setText(text)
        self._badge.setProperty("state", state)
        self._badge.style().unpolish(self._badge)
        self._badge.style().polish(self._badge)

    # -------------------------------------------------------------- status
    def _clear_cell_links(self) -> None:
        """Delete the col-0 QLabel cell widgets before the rows are dropped.

        ``setRowCount`` / overwriting a cell widget does NOT delete the widget
        already installed via ``setCellWidget`` (it stays alive, re-parented to
        the table viewport). Without this, every toggle / preset-switch would
        orphan one QLabel per displayed cell and leak without bound.
        """
        for r in range(self._table.rowCount()):
            w = self._table.cellWidget(r, 0)
            if w is not None:
                self._table.removeCellWidget(r, 0)
                w.deleteLater()

    def _reset_status_and_result(self) -> None:
        self._clear_cell_links()
        self._table.setRowCount(0)
        self._solved = {}
        self._row_for_cell = {}
        self._cell_for_row = {}
        self._live_marked = set()
        self._run_cells = []
        self._result_title.setText("")
        self._resultview.clear()

    def _update_solved(self) -> None:
        """Record per-cell solve status from THIS run's logs (fresh only).

        A log older than the run start is stale (the solver didn't write one
        this run - e.g. it crashed before emitting status), so it is not
        recorded as a result.
        """
        for cell in self._run_cells:
            p = paths.log_path(self._cfg, cell)
            fresh = bool(p) and p.is_file() and p.stat().st_mtime >= self._run_started_at - 1
            self._solved[cell] = parse_log(p) if fresh else {"status": "", "elapsed": "", "obj": ""}

    def _sync_status_table(self, cells: list[str]) -> None:
        """One row per cell - a CLICKABLE name (opens its config) + status.
        Called live as cells are toggled, and at run start / finish."""
        self._row_for_cell = {}
        self._cell_for_row = {}
        self._clear_cell_links()
        self._table.setRowCount(len(cells))
        for r, cell in enumerate(cells):
            self._row_for_cell[cell] = r
            self._cell_for_row[r] = cell
            self._table.setCellWidget(r, 0, self._cell_link(cell))
            self._set_status_row(cell, self._solved.get(cell, {}))

    def _cell_link(self, cell: str) -> QLabel:
        # Cell names come from scan_cdl, which accepts any `\S+` token. Escape
        # the display text, and pass the *real* `cell` to the click handler
        # directly instead of round-tripping through the href: Qt entity/percent-
        # decodes hrefs before linkActivated fires, which would otherwise hand
        # _open_cell_config a mismatched name -> wrong config/<cell>.json.
        link = QLabel(f'<a href="#" style="text-decoration:underline;">{escape(cell)}</a>')
        link.setToolTip("Click to view / edit this cell's config")
        link.setContentsMargins(6, 0, 0, 0)
        link.linkActivated.connect(lambda _href, c=cell: self._open_cell_config(c))
        return link

    def _set_status_row(self, cell: str, info: dict) -> None:
        r = self._row_for_cell.get(cell)
        if r is None:
            return
        for c, v in zip((1, 2, 3),
                        (info.get("status") or "—", info.get("elapsed") or "—",
                         info.get("obj") or "—")):
            item = QTableWidgetItem(v)
            if c == 1 and info.get("status"):
                item.setForeground(Qt.darkGreen if status_is_ok(info["status"]) else Qt.red)
            self._table.setItem(r, c, item)

    def _mark_cell_done(self, cell: str) -> None:
        """A cell just finished mid-run: parse its log, flip its row, and
        live-preview its layout."""
        info = parse_log(paths.log_path(self._cfg, cell))
        self._solved[cell] = info
        self._set_status_row(cell, info)
        self._show_result(cell)

    def _refresh_status(self) -> None:
        """Final sync of every run-cell's row from self._solved (catches cells
        that never streamed a 'Saved to ...' line, e.g. INFEASIBLE)."""
        cells = self._run_cells or self._selected_cells()
        if set(self._row_for_cell) != set(cells):
            self._sync_status_table(cells)
        for cell in cells:
            self._set_status_row(cell, self._solved.get(cell, {}))

    def _on_status_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if rows:
            cell = self._cell_for_row.get(rows[0].row())
            if cell:
                self._show_result(cell)

    # ----------------------------------------------------------- cell config
    def _open_cell_config(self, cell: str) -> None:
        if not self._cfg:
            self._warn("Pick a preset first.")
            return
        cfg_path = paths.config_json_path(self._cfg, cell)
        if cfg_path is None:
            self._warn("Could not resolve OUT_DIR for this preset.")
            return
        if cfg_path.is_file():
            try:
                config = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._warn(f"Could not read {cfg_path}: {exc}")
                return
        else:
            config = self._gen_default_config(cell)
            if config is None:
                self._warn(f"Could not build default config for {cell}.")
                return
        from .cell_config_editor import CellConfigEditor
        dlg = CellConfigEditor(self, cell, config, cfg_path)
        dlg.saved.connect(self._on_cell_config_saved)
        dlg.exec()

    def _gen_default_config(self, cell: str) -> dict | None:
        """The defaults `make config` would write for *cell* - generated to a
        gitignored temp dir (under output/) so the real config is untouched."""
        root = paths.project_root()
        base = root / "output"; base.mkdir(exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=str(base), prefix=".cellcfg_"))
        (tmp / "config").mkdir(exist_ok=True)
        g = self._cfg.get
        cmd = [sys.executable, "-m", "src.cellgen.archit.config",
               "--cell_names", cell,
               "--track", str(g("TRACK", "4")), "--tech", str(g("TECH", "FinFET")),
               "--height_config", str(g("HEIGHT_CONFIG", "SH")),
               "--output_dir", f"output/{tmp.name}"]
        for o in paths.preset_overrides(self._preset.currentText()):
            cmd += ["--override", o]
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=40)
            p = tmp / "config" / f"{cell}.json"
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-12:]
            if tail:
                self._log.appendPlainText(
                    f"[default config for {cell} failed (exit {r.returncode}):]"
                )
                self._log.appendPlainText("\n".join(tail))
            return None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _on_cell_config_saved(self, cell: str) -> None:
        self._warn(f"Saved config for {cell} — 'make config' will keep it (not overwrite).")

    def _auto_show_result(self) -> None:
        cells = self._run_cells or self._selected_cells()
        if not cells:
            return
        if self._table.rowCount():
            self._table.selectRow(0)
        self._show_result(cells[0])

    # -------------------------------------------------------------- result
    def _show_result(self, cell: str) -> None:
        png = paths.view_png(self._cfg, cell)
        disp = png
        if png is not None:
            try:
                disp = png.relative_to(paths.project_root())
            except ValueError:
                pass
        self._result_title.setText(f"{cell}   ·   {disp}" if png else cell)
        self._resultview.load(png, cell)

    # --------------------------------------------------------------- open
    def _open_output_dir(self) -> None:
        out = paths.out_dir(self._cfg)
        if out and out.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
        else:
            self._warn("Output directory does not exist yet — run the flow first.")

    def _open_gds(self) -> None:
        gds = paths.gds_path(self._cfg)
        if not gds or not gds.is_file():
            self._warn("GDS not found — run 'GDS' (step 3) first.")
            return
        klayout = shutil.which("klayout")
        if klayout:
            QProcess.startDetached(klayout, [str(gds)])
            self._log.appendPlainText(f"[launched {klayout} {gds}]")
        else:
            self._warn("klayout not found on PATH — install KLayout or open the GDS manually.")

    # --------------------------------------------------------------- misc
    def _apply_theme(self) -> None:
        QApplication.instance().setStyleSheet(style.qss())

    def _warn(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 5000)
        self._log.appendPlainText(f"[{msg}]")

    # --------------------------------------------------------------- editors
    def _edit_preset(self) -> None:
        from .preset_editor import PresetEditor
        name = self._preset.currentText()
        dlg = PresetEditor(self, preset_name=name or None)
        dlg.saved.connect(self._on_preset_saved)
        dlg.exec()

    def _new_preset(self) -> None:
        from .preset_editor import PresetEditor
        dlg = PresetEditor(self, preset_name=None)
        dlg.saved.connect(self._on_preset_saved)
        dlg.exec()

    def _on_preset_saved(self, name: str) -> None:
        # Refresh the preset list and jump to the saved one.
        presets = paths.list_config_presets()
        self._preset.blockSignals(True)
        self._preset.clear(); self._preset.addItems(presets)
        if name in presets:
            self._preset.setCurrentText(name)
        self._preset.blockSignals(False)
        self._on_config_changed(self._preset.currentText())
        self._warn(f"Preset saved: {name}")


    def _about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("About SMTCell 2.0")
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"<div align='center'><b>SMTCell 2.0 GUI</b> &nbsp;·&nbsp; v{__version__}"
            "<br><br>Constraint-programming standard-cell layout"
            "<br>for FinFET / CFET / QFET.<br><br>VLSI Lab, UC San Diego.</div>"
        )
        box.setStandardButtons(QMessageBox.Ok)
        lbl = box.findChild(QLabel, "qt_msgbox_label")
        if lbl is not None:
            lbl.setAlignment(Qt.AlignCenter)
        box.exec()

    def closeEvent(self, e) -> None:  # noqa: N802
        # Mark closing first so the synchronous finished() from cancel() does
        # not drive widget updates during teardown.
        self._closing = True
        if self._runner.is_running:
            self._runner.cancel()
        super().closeEvent(e)
