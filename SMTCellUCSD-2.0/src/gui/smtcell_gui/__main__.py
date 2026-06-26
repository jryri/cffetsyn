"""Entry point: `cd src/gui && python -m smtcell_gui`."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SMTCell 2.0")
    app.setOrganizationName("SMTCell")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
