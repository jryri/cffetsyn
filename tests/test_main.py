from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)


def setup_function() -> None:
    store.clear()


def test_health() -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_index_serves_html() -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "cffetsyn" in res.text


def test_messages_empty_by_default() -> None:
    res = client.get("/api/messages")
    assert res.status_code == 200
    assert res.json() == []


def test_create_and_list_message() -> None:
    res = client.post("/api/messages", json={"author": "Ada", "text": "Hello world"})
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == 1
    assert body["author"] == "Ada"
    assert body["text"] == "Hello world"

    listed = client.get("/api/messages").json()
    assert len(listed) == 1
    assert listed[0]["author"] == "Ada"


def test_create_rejects_empty_fields() -> None:
    res = client.post("/api/messages", json={"author": "", "text": "hi"})
    assert res.status_code == 422
