"""SMTCell 2.0 - minimal control GUI.

A small, production-focused PySide6 front end for the SMTCell flow:
pick a CONFIG preset, pick cell(s), run ``make config -> spnr -> gds -> lef``
with a live log, then view solve status + the result layout PNG.

Launch with::

    cd src/gui && python -m smtcell_gui
"""

__version__ = "2.0.0"
