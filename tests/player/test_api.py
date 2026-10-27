"""The whole app over HTTP: who the player is, and standby."""
from fastapi.testclient import TestClient

from jplay.player.app import create_app
from jplay.player.config import load_config


def make_client(tmp_path):
    cfg = load_config(None)
    cfg["server"]["name"] = "Lounge"
    cfg["paths"].update(state_dir=str(tmp_path / "state"), runtime_dir=str(tmp_path / "run"))
    cfg["audio"]["device"] = "null"
    for s in ("radio", "plex", "cd", "spotify", "bluetooth"):
        cfg[s]["enabled"] = False
    return TestClient(create_app(cfg))


def test_info_gives_a_stable_id(tmp_path):
    with make_client(tmp_path) as client:
        first = client.get("/api/v1/info").json()
    assert first["name"] == "Lounge"
    assert len(first["id"]) == 16
    assert {s["id"] for s in first["sources"]} == {"radio", "plex", "cd", "spotify", "bluetooth"}
    # A restart (same state directory) keeps the id
    with make_client(tmp_path) as client:
        assert client.get("/api/v1/info").json()["id"] == first["id"]


def test_standby_leaves_nothing_active(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post("/api/v1/standby")
        assert r.status_code == 200
        assert r.json()["active"] is None


def test_update_status_without_the_updater(tmp_path):
    with make_client(tmp_path) as client:
        st = client.get("/api/v1/update").json()
        assert st["supported"] is False and st["available"] is False
        assert st["channel"] == "releases" and st["auto"] == "off"
        assert st["version"]                           # from git, or the package version
        r = client.post("/api/v1/update/check")
        assert r.status_code == 409 and "deploy/install.sh" in r.json()["detail"]


def test_update_settings_are_kept(tmp_path):
    with make_client(tmp_path) as client:
        st = client.put("/api/v1/update/settings", json={"channel": "main", "auto": "night"}).json()
        assert st["channel"] == "main" and st["auto"] == "night"
        assert client.put("/api/v1/update/settings", json={"auto": "sometimes"}).status_code == 422
    with make_client(tmp_path) as client:                # after a restart
        st = client.get("/api/v1/update").json()
        assert st["channel"] == "main" and st["auto"] == "night"


def test_install_refuses_odd_targets(tmp_path):
    with make_client(tmp_path) as client:
        client.app.state.updates.supported = lambda: True
        client.app.state.updates._start = lambda unit: None
        for bad in ("1.2", "v1.2", "main; reboot"):
            assert client.post("/api/v1/update/install", json={"target": bad}).status_code == 409
        assert client.post("/api/v1/update/install", json={"target": "v9.9.9"}).json() == {"ok": True, "target": "v9.9.9"}
