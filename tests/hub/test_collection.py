"""The hub's Settings card for jplayazam: status, "look for new music", "start over"; the token stays on the hub."""
import httpx
import pytest
from fastapi.testclient import TestClient

from jplay.fingerprint import app as fp_app
from jplay.hub import app as hub_app, collection


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "PROCESS_EVERY", 3600)
    (tmp_path / "music").mkdir()
    (tmp_path / "hub").mkdir()
    service = fp_app.create_app(str(tmp_path / "fp"), str(tmp_path / "music"), token="s3cret", schedule=False)
    ran = []
    monkeypatch.setattr(service.state.library, "index", lambda progress=None: ran.append("index") or {})
    monkeypatch.setattr(service.state.library, "rebuild", lambda: ran.append("rebuild"))
    with TestClient(hub_app.create_app(tmp_path / "hub", announce=False)) as c:
        # The hub talks to jplayazam (here in-process) through its own HTTP client
        c.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url="http://fp")
        yield c, ran


def test_not_set_up(hub):
    c, _ = hub
    assert c.get("/api/collection").json() == {"url": "", "token_set": False, "status": None, "error": ""}
    assert c.post("/api/collection/index").status_code == 400


def test_status_and_buttons(hub):
    c, ran = hub
    v = c.put("/api/collection", json={"url": "192.168.1.233/", "token": "s3cret"}).json()
    assert v["url"] == "http://192.168.1.233" and v["token_set"] is True and "s3cret" not in str(v)
    tok = collection._token_file()
    assert tok.read_text() == "s3cret" and tok.stat().st_mode & 0o777 == 0o600
    st = c.get("/api/collection").json()["status"]
    assert st["files"] == 0 and st["index"]["running"] is False and st["music_found"] is True
    assert c.post("/api/collection/index").json() == {"started": True}
    assert c.post("/api/collection/rebuild").json() == {"started": True}
    assert "rebuild" in ran
    c.put("/api/collection", json={"url": "http://192.168.1.233", "token": ""})          # empty keeps it
    assert tok.read_text() == "s3cret"
    c.put("/api/collection", json={"url": "http://192.168.1.233", "token": "wrong"})
    r = c.post("/api/collection/index")
    assert r.status_code == 400 and "token is wrong" in r.json()["detail"]
    assert c.post("/api/collection/nonsense").status_code == 400


def test_unreachable(hub):
    c, _ = hub
    c.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    c.put("/api/collection", json={"url": "http://192.168.1.233", "token": "x"})
    assert "Can't reach it" in c.get("/api/collection").json()["error"]
    assert "Can't reach it" in c.post("/api/collection/index").json()["detail"]
