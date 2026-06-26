"""Result viewer: a zoomable / pannable PNG of the solved layout.

Shows ``view/<cell>.png`` with simple navigation - zoom -/+ and Fit - and
scroll-to-pan, or a faint ASCII ``SMTCell 2.0`` backdrop when nothing is
loaded. The panel never resizes when switching cells: the canvas is the
pixmap and the scroll area (fixed by its splitter pane) scrolls it.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from .ascii_art import BANNER

_ZOOM_MIN, _ZOOM_MAX = 0.1, 12.0


class _Canvas(QLabel):
    """Shows the rendered pixmap, or the faint ASCII backdrop when empty.

    In *fit* mode (``set_fit_source``) it draws a source pixmap scaled to fit
    its own current size (KeepAspectRatio, centered), recomputed on every
    paint - so the image always fits both axes and tracks resizes for free.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self._fit_src: QPixmap | None = None

    def set_fit_source(self, pm: QPixmap | None) -> None:
        self._fit_src = pm
        if pm is not None:
            super().setPixmap(QPixmap())   # fit mode paints manually
        self.update()

    def fit_scale(self) -> float:
        """Effective scale of the fit-rendered image vs its source (0 if none)."""
        src = self._fit_src
        if src is None or src.isNull() or src.width() == 0 or src.height() == 0:
            return 0.0
        return min(self.width() / src.width(), self.height() / src.height())

    def paintEvent(self, e) -> None:  # noqa: N802
        if self._fit_src is not None and not self._fit_src.isNull():
            scaled = self._fit_src.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p = QPainter(self)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
            p.end()
            return
        pm = self.pixmap()
        if pm is None or pm.isNull():
            self._backdrop()
        super().paintEvent(e)

    def _backdrop(self) -> None:
        p = QPainter(self)
        font = QFont("DejaVu Sans Mono"); font.setStyleHint(QFont.Monospace)
        font.setPixelSize(16)
        fm = QFontMetrics(font)
        bw = max(len(l) for l in BANNER)
        px = int(16 * (self.width() * 0.66) / (fm.horizontalAdvance("█" * bw) or 1))
        px = max(6, min(px, self.height() // (len(BANNER) + 3)))
        font.setPixelSize(px); p.setFont(font); fm = QFontMetrics(font)
        lh = fm.height(); y0 = (self.height() - lh * len(BANNER)) // 2 + fm.ascent()
        p.setPen(QColor(132, 144, 168, 46))
        for i, line in enumerate(BANNER):
            x = (self.width() - fm.horizontalAdvance(line)) // 2
            p.drawText(x, y0 + i * lh, line)
        p.end()


class ResultView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._png: QPixmap | None = None
        self._zoom = 1.0
        self._fit = True   # fit-to-window mode (re-fits automatically on resize)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        bar = QHBoxLayout()
        for txt, cb, tip in [("−", self.zoom_out, "Zoom out"),
                             ("+", self.zoom_in, "Zoom in"),
                             ("Fit", self.fit, "Fit to window")]:
            b = QPushButton(txt); b.setToolTip(tip)
            b.setFixedWidth(46 if len(txt) < 3 else 56)
            b.clicked.connect(cb); bar.addWidget(b)
        self._zoom_lbl = QLabel(""); self._zoom_lbl.setObjectName("hint")
        bar.addWidget(self._zoom_lbl)
        bar.addStretch(1)
        outer.addLayout(bar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._canvas = _Canvas()
        self._scroll.setWidget(self._canvas)
        outer.addWidget(self._scroll, 1)

    # ---------------------------------------------------------------- load
    def load(self, png_path: Path | None, cell: str = "") -> None:
        if png_path and Path(png_path).is_file():
            self._png = QPixmap(str(png_path))
            self.fit()
        else:
            self._png = None
            self._canvas.set_fit_source(None)
            self._render()

    def clear(self) -> None:
        self._png = None
        self._canvas.set_fit_source(None)
        self._render()

    # -------------------------------------------------------------- resize
    def resizeEvent(self, e) -> None:  # noqa: N802
        # In fit mode the canvas refits itself (it fills the viewport and
        # scales on every paint); only the % label needs syncing.
        super().resizeEvent(e)
        if self._fit:
            self._update_zoom_label()

    # -------------------------------------------------------------- render
    def _render(self) -> None:
        """Render in explicit-zoom (scroll/pan) mode."""
        if self._png is not None and not self._png.isNull():
            w = max(1, int(self._png.width() * self._zoom))
            pm = self._png.scaledToWidth(w, Qt.SmoothTransformation)
            self._scroll.setWidgetResizable(False)
            self._canvas.set_fit_source(None)
            self._canvas.setPixmap(pm)
            self._canvas.resize(pm.size())
            self._zoom_lbl.setText(f"{self._zoom * 100:.0f}%")
        else:
            self._scroll.setWidgetResizable(True)
            self._canvas.set_fit_source(None)
            self._canvas.setPixmap(QPixmap())
            self._zoom_lbl.setText("")
            self._canvas.update()

    def _update_zoom_label(self) -> None:
        if self._fit:
            s = self._canvas.fit_scale()
            self._zoom_lbl.setText(f"{s * 100:.0f}%" if s else "")
        else:
            self._zoom_lbl.setText(f"{self._zoom * 100:.0f}%")

    # --------------------------------------------------------------- zoom
    def fit(self) -> None:
        """Scale the image so it sits *entirely* inside the viewport.

        The canvas fills the (scrollbar-free) viewport and scales the image
        with KeepAspectRatio on every paint, so it always fits - wide and tall
        layouts alike - and re-fits as the window resizes.
        """
        self._fit = True
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        if self._png is None or self._png.isNull():
            self._canvas.set_fit_source(None)
            self._render(); return
        self._scroll.setWidgetResizable(True)
        self._canvas.set_fit_source(self._png)
        self._update_zoom_label()

    def _leave_fit(self) -> None:
        if self._fit:
            s = self._canvas.fit_scale()
            if s:
                self._zoom = s   # continue zooming from the current fit scale
        self._fit = False
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def zoom_in(self) -> None:
        self._leave_fit()
        self._zoom = min(_ZOOM_MAX, self._zoom * 1.25); self._render()

    def zoom_out(self) -> None:
        self._leave_fit()
        self._zoom = max(_ZOOM_MIN, self._zoom * 0.8); self._render()
