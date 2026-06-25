"""FastAPI application exposing a small in-memory guestbook.

The app serves a single-page UI at ``/`` and a small JSON API under ``/api``.
State is kept in-memory which keeps the starter dependency-free; restarting the
server clears all messages.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")

app = FastAPI(title="cffetsyn guestbook", version="0.1.0")


class Message(BaseModel):
    id: int
    author: str
    text: str
    created_at: str


class MessageCreate(BaseModel):
    author: str = Field(min_length=1, max_length=50)
    text: str = Field(min_length=1, max_length=500)


class _Store:
    """Thread-safe in-memory message store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = count(1)
        self._messages: list[Message] = []

    def list(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def add(self, payload: MessageCreate) -> Message:
        with self._lock:
            message = Message(
                id=next(self._ids),
                author=payload.author.strip(),
                text=payload.text.strip(),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._messages.append(message)
            return message

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()


store = _Store()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/messages", response_model=list[Message])
def list_messages() -> list[Message]:
    return store.list()


@app.post("/api/messages", response_model=Message, status_code=201)
def create_message(payload: MessageCreate) -> Message:
    if not payload.author.strip() or not payload.text.strip():
        raise HTTPException(status_code=422, detail="author and text are required")
    return store.add(payload)
