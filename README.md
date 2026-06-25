# cffetsyn

A tiny [FastAPI](https://fastapi.tiangolo.com/) guestbook starter application with a
small single-page UI. Messages are stored in-memory (cleared on restart), which keeps
the project dependency-free and easy to run.

## Requirements

- Python 3.10+

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Run (development)

```bash
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 to use the guestbook UI.

## API

| Method | Path             | Description              |
| ------ | ---------------- | ------------------------ |
| GET    | `/`              | Guestbook web UI         |
| GET    | `/api/health`    | Health check             |
| GET    | `/api/messages`  | List all messages        |
| POST   | `/api/messages`  | Create a message         |

## Test

```bash
pytest
```

## Lint

```bash
ruff check .
```
