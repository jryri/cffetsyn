# AGENTS.md

## Project overview

`cffetsyn` is a small FastAPI guestbook starter. The backend lives in `app/main.py`
(in-memory message store, JSON API under `/api`) and serves a single-page UI from
`app/templates/index.html` at `/`. There is no database; restarting the server clears
all messages.

Standard commands are documented in `README.md` (setup, run, test, lint).

## Cursor Cloud specific instructions

- Use the project virtualenv at `.venv` for all Python commands: `source .venv/bin/activate`.
  Dev dependencies come from `requirements-dev.txt` (installed by the update script).
- Run the dev server with `uvicorn app.main:app --reload` (defaults to `127.0.0.1:8000`).
  Start it in a long-lived tmux session rather than a one-shot background process.
- Tests rely on `pythonpath = ["."]` in `pyproject.toml` so `pytest` can import `app`
  from the repo root — run `pytest` from `/workspace`, not from inside `tests/`.
- Message state is in-memory only; `--reload` restarts (triggered by editing files)
  wipe all posted messages, which is expected.
