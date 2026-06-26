#!/usr/bin/env bash
# SMTCell 2.0 control GUI launcher. Run from any directory.
#
#   ./src/gui/run.sh
#
# Override the interpreter if PySide6 lives in a specific env:
#   PYTHON=/path/to/python ./src/gui/run.sh
#
# NOTE: no `set -u` — some conda deactivate scripts reference unbound vars.
set -eo pipefail

# Run the package from its parent so `python -m smtcell_gui` resolves.
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PYTHON="${PYTHON:-python3}"
cd "$HERE"
exec "$PYTHON" -m smtcell_gui "$@"
