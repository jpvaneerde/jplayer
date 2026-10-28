"""The hub's secure address: devices and their names, nginx settings, the DuckDNS proof, the Home page API."""
import subprocess
import types

import pytest
from fastapi.testclient import TestClient

from jplay.hub import acme_hook, app as hub_app, db, devices, https

FOUND = [
    {"id": "player-p1", "kind": "player", "name": "Lounge", "address": "192.168.1.30", "port": 8080, "room": ""},
    {"id": "player-p2", "kind": "player", "name": "Café Øst", "address": "192.168.1.31", "port": 8080, "room": ""},
    {"id": "display-d1", "kind": "display", "name": "Lounge display", "address": "192.168.1.40", "port": 5000, "room": "Lounge"},
    {"id": "display-d2", "kind": "display", "name": "Study", "address": "192.168.1.41", "port": 5000, "room": "Study"},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "PROCESS_EVERY", 3600)
    monkeypatch.setattr(hub_app, "HTTPS_EVERY", 3600 * 24)
    monkeypatch.setattr(devices, "_browse", lambda: [dict(d) for d in FOUND])
    devices._known.clear()
    with TestClient(hub_app.create_app(tmp_path, announce=False)) as c:
        yield c
    devices._known.clear()


def test_slugs_and_subdomains(client):
    assert devices.slug("Café Øst") == "cafe-ost" and devices.slug("") == "device"
    subs = {d["id"]: d["subdomain"] for d in devices.devices()}
    assert subs == {"hub": "home", "player-p1": "lounge", "player-p2": "cafe-ost",
                    "display-d1": "lounge-display", "display-d2": "study-display"}


def test_nginx_settings(client, tmp_path):
    s = {"domain": "home.jplay.cloud"}
    conf = https.nginx_conf(s, devices.devices(), tmp_path / "certbot", ipv6=False)
    assert "server_name home.jplay.cloud *.home.jplay.cloud;" in conf and "return 301 https://$host" in conf
    assert "server_name lounge-display.home.jplay.cloud;" in conf and "proxy_pass http://192.168.1.40:5000;" in conf
    assert "proxy_pass http://127.0.0.1:8090;" in conf and "[::]" not in conf
    assert conf.count("live/devices/fullchain.pem") == 4 and conf.count("live/home/fullchain.pem") == 1
    assert "listen [::]:443 ssl;" in https.nginx_conf(s, devices.devices(), tmp_path, ipv6=True)


def test_settings_keep_the_token_on_the_pi(client):
    r = client.put("/api/https", json={"domain": "Home.JPlay.cloud.", "duckdns": "jplayer.duckdns.org", "token": "secret"})
    v = r.json()
    assert v["domain"] == "home.jplay.cloud" and v["duckdns"] == "jplayer" and v["token_set"] is True
    assert "secret" not in r.text
    tok = https.base_dir() / "duckdns.token"
    assert tok.read_text() == "secret" and tok.stat().st_mode & 0o777 == 0o600
    # Saving again without a token keeps it
    client.put("/api/https", json={"domain": "home.jplay.cloud", "duckdns": "jplayer", "token": ""})
    assert tok.read_text() == "secret"
    assert client.put("/api/https", json={"domain": "nodot", "duckdns": "x"}).status_code == 400
    urls = [a["url"] for a in client.get("/api/https").json()["addresses"]]
    assert "https://home.jplay.cloud/" in urls and "https://lounge-display.home.jplay.cloud/" in urls


def test_obtain_runs_certbot_twice_then_nginx(client, monkeypatch):
    https.save("home.jplay.cloud", "jplayer", "secret")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if https.CERTBOT_MAIN in cmd:
            assert kw["env"]["JPLAY_DUCKDNS"] == "jplayer"
            d = cmd[cmd.index("-d") + 1]
            name = cmd[cmd.index("--cert-name") + 1]
            _self_signed(https.base_dir() / "certbot" / "live" / name, d)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    https.obtain()
    domains = [c[c.index("-d") + 1] for c in calls if https.CERTBOT_MAIN in c]
    assert domains == ["home.jplay.cloud", "*.home.jplay.cloud"]
    assert any("-t" in c for c in calls) and any("reload-or-restart" in c for c in calls)
    assert (https.base_dir() / "nginx.conf").exists() and https.settings()["enabled"]
    # A second run finds both certificates still valid: no certbot
    calls.clear()
    https.obtain()
    assert not any(https.CERTBOT_MAIN in c for c in calls)


def test_nginx_refusing_keeps_the_old_settings(client, monkeypatch):
    https.save("home.jplay.cloud", "jplayer", "secret")
    for name, d in (("home", "home.jplay.cloud"), ("devices", "*.home.jplay.cloud")):
        _self_signed(https.base_dir() / "certbot" / "live" / name, d)
    (https.base_dir() / "nginx.conf").write_text("old")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="bad"))
    with pytest.raises(https.HttpsError):
        https.write_nginx(force=True)
    assert (https.base_dir() / "nginx.conf").read_text() == "old"


def test_duckdns_hook(monkeypatch):
    asked = []

    class Session:
        def get(self, url, params, timeout):
            asked.append(params)
            return types.SimpleNamespace(text="OK" if params["token"] == "good" else "KO")
    acme_hook.set_txt("jplayer", "good", "abc", session=Session())
    acme_hook.set_txt("jplayer", "good", "", clear=True, session=Session())
    assert asked[0]["txt"] == "abc" and asked[1]["clear"] == "true"
    with pytest.raises(SystemExit):
        acme_hook.set_txt("jplayer", "bad", "abc", session=Session())


def test_home_page_api(client, monkeypatch):
    async def fake_overview(_client):
        return [{**d, "online": True, "state": {}} for d in devices.devices()]
    monkeypatch.setattr(devices, "overview", fake_overview)
    r = client.get("/api/devices").json()
    lounge = next(d for d in r["devices"] if d["id"] == "player-p1")
    assert lounge["url"] == "http://192.168.1.30:8080/"
    https.save("home.jplay.cloud", "jplayer", "secret")
    db.set_setting("https", {**db.setting("https", {}), "enabled": True})
    lounge = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "player-p1")
    assert lounge["url"] == "https://lounge.home.jplay.cloud/"
    assert client.post("/api/devices/nope/control", json={"action": "toggle"}).status_code == 404
    assert client.get("/", follow_redirects=False).status_code == 200
    r = client.get("/?room=Lounge", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/spins?room=Lounge"
    assert client.get("/manifest.webmanifest").json()["start_url"] == "/"
    assert client.get("/sw.js").headers["service-worker-allowed"] == "/"


def _self_signed(folder, name):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=89))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (folder / "cert.pem").write_bytes(pem)
    (folder / "fullchain.pem").write_bytes(pem)
    (folder / "privkey.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
