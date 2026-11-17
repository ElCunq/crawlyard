import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

panel = load_module("panel_main", Path(__file__).parent.parent / "panel" / "main.py")
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:test-password-123").decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "hello.py").write_text("print('hi')\n")
    # A sibling directory whose name starts with the base dir's name: a plain
    # string-prefix check would let "../code-secrets/..." through.
    (tmp_path / "code-secrets").mkdir()
    (tmp_path / "code-secrets" / "key.txt").write_text("secret")
    monkeypatch.setattr(panel, "BASE_CODE_DIR", (tmp_path / "code").resolve())
    return TestClient(panel.app)


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "../code-secrets/key.txt"])
def test_path_traversal_is_rejected(client, path):
    resp = client.get("/api/files/read", params={"path": path}, headers=AUTH)
    assert resp.status_code == 403


def test_reading_inside_base_dir_works(client):
    resp = client.get("/api/files/read", params={"path": "hello.py"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["content"] == "print('hi')\n"


def test_requests_without_credentials_get_401(client):
    for path in ["/api/health", "/", "/app.js"]:
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_gets_401(client):
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert client.get("/api/files", headers=bad).status_code == 401


def test_websocket_without_credentials_is_refused(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/logs/abc"):
        pass
