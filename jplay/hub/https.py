"""HTTPS for the whole house, on the hub: https://home.jplay.cloud (the hub, installable as an app) and
https://<device>.home.jplay.cloud for every player and display (lounge, lounge-display, ...).

- Certificates: Let's Encrypt, through certbot (in the hub's own Python environment, as the jplay user). The proof
  is a DNS record: certbot asks acme_hook.py to put it on DuckDNS; _acme-challenge.<domain> at your DNS host is a
  CNAME to <name>.duckdns.org, so Let's Encrypt finds it there. DuckDNS holds one proof at a time, so there are two
  certificates, fetched one after the other: <domain>, and *.<domain> for the devices. Renewed when 30 days are left.
- nginx (a system package) serves them and passes each name on to the right device; the hub writes its settings
  (STATE/hub/https/nginx.conf, included from /etc/nginx/conf.d/jplay.conf) and reloads it.
- Your home DNS (the router) must send <domain> and *.<domain> to the hub's address.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..common.updates import SYSTEMCTL
from . import db, devices

log = logging.getLogger(__name__)
NGINX = "/usr/sbin/nginx"
RENEW_DAYS = 30
CERTBOT_MAIN = "import sys; from certbot.main import main; sys.exit(main())"

status: dict[str, Any] = {"state": "off", "message": "", "log": [], "updated_at": None}


class HttpsError(Exception):
    pass


def base_dir() -> Path:
    from ..common import system
    p = system.state_dir("hub") / "https"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings() -> dict:
    s = db.setting("https", {}) or {}
    return {"domain": s.get("domain") or "", "duckdns": s.get("duckdns") or "", "email": s.get("email") or "",
            "enabled": bool(s.get("enabled")), "token_set": (base_dir() / "duckdns.token").exists()}


def save(domain: str, duckdns: str, token: str | None, email: str = "") -> dict:
    domain = domain.strip().lower().strip(".")
    duckdns = duckdns.strip().lower().removesuffix(".duckdns.org")
    if not domain or "." not in domain or " " in domain:
        raise HttpsError("Give the address, e.g. home.jplay.cloud")
    if not duckdns or "." in duckdns:
        raise HttpsError("Give your DuckDNS name, e.g. jplayer (for jplayer.duckdns.org)")
    if token is not None and token.strip():
        path = base_dir() / "duckdns.token"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token.strip())
    db.set_setting("https", {"domain": domain, "duckdns": duckdns, "email": email.strip(),
                             "enabled": settings()["enabled"]})
    return settings()


def _certbot_dirs() -> list[str]:
    b = base_dir()
    return ["--config-dir", str(b / "certbot"), "--work-dir", str(b / "work"), "--logs-dir", str(b / "logs")]


def certificate_info(name: str) -> dict | None:
    """{"names", "expires"} of one of the two certificates, or None."""
    path = base_dir() / "certbot" / "live" / name / "cert.pem"
    if not path.exists():
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(path.read_bytes())
        expires = cert.not_valid_after_utc.timestamp()
        try:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        return {"names": names, "expires": expires, "days_left": int((expires - time.time()) / 86400)}
    except Exception as e:
        return {"names": [], "expires": None, "days_left": None, "error": str(e)}


def _say(msg: str) -> None:
    status["log"] = (status["log"] + [time.strftime("%H:%M:%S ") + msg])[-30:]
    status["message"] = msg
    status["updated_at"] = time.time()
    log.info("HTTPS: %s", msg)


def _certbot(cert_name: str, domain: str, s: dict, staging: bool = False) -> None:
    hook = f"{sys.executable} -m jplay.hub.acme_hook"
    # certbot 5 can't be started with "python -m certbot": call its main() (the same as its own launcher)
    cmd = [sys.executable, "-c", CERTBOT_MAIN, "certonly", "--non-interactive", "--agree-tos",
           *(["--email", s["email"]] if s["email"] else ["--register-unsafely-without-email"]),
           *_certbot_dirs(), "--manual", "--preferred-challenges", "dns",
           "--manual-auth-hook", f"{hook} auth", "--manual-cleanup-hook", f"{hook} cleanup",
           "--cert-name", cert_name, "-d", domain, "--keep-until-expiring", "--key-type", "ecdsa",
           *(["--staging"] if staging else [])]
    env = {**os.environ, "JPLAY_DUCKDNS": s["duckdns"], "JPLAY_DUCKDNS_TOKEN_FILE": str(base_dir() / "duckdns.token")}
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    if res.returncode != 0:
        tail = (res.stderr or res.stdout).strip().splitlines()[-6:]
        raise HttpsError("Let's Encrypt didn't give the certificate: " + " ".join(tail)[-500:])


def obtain(staging: bool = False) -> None:
    """Get (or renew, when 30 days are left) both certificates, one after the other, then write and load nginx."""
    s = settings()
    if not (s["domain"] and s["duckdns"] and s["token_set"]):
        raise HttpsError("Fill in the address, the DuckDNS name and its token first")
    status.update(state="working", log=[])
    try:
        for name, domain in (("home", s["domain"]), ("devices", f"*.{s['domain']}")):
            info = certificate_info(name)
            if info and info.get("days_left") is not None and info["days_left"] > RENEW_DAYS and domain in info["names"]:
                _say(f"Certificate for {domain} is valid for {info['days_left']} more days")
                continue
            _say(f"Asking Let's Encrypt for {domain} (takes a minute or two)")
            _certbot(name, domain, s, staging)
            _say(f"Got the certificate for {domain}")
        write_nginx()
        db.set_setting("https", {**db.setting("https", {}), "enabled": True})
        status["state"] = "on"
        _say(f"https://{s['domain']} is ready")
    except Exception as e:
        status["state"] = "failed"
        _say(str(e))
        raise


def ipv6_ok() -> bool:
    """Does this Pi have IPv6? (nginx refuses its settings when asked to listen on IPv6 without it.)"""
    import socket
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM):
            return True
    except OSError:
        return False


def nginx_conf(s: dict, devs: list[dict], cert_dir: Path, hub_port: int = devices.HUB_PORT, ipv6: bool | None = None) -> str:
    """The nginx settings: the hub on <domain>, each device on <subdomain>.<domain>, plain http sent to https."""
    d = s["domain"]
    v6 = ipv6_ok() if ipv6 is None else ipv6
    listen = lambda port, ssl="": f"    listen {port}{ssl};" + (f"\n    listen [::]:{port}{ssl};" if v6 else "")  # noqa: E731
    proxy = """        proxy_http_version 1.1;
        proxy_set_header Host $proxy_host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $jplay_connection;
        proxy_read_timeout 3600s;
        proxy_buffering off;
        client_max_body_size 200m;"""
    tls = lambda name: f"""    ssl_certificate {cert_dir}/live/{name}/fullchain.pem;
    ssl_certificate_key {cert_dir}/live/{name}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;"""  # noqa: E731
    out = [f"""# Written by the JPlay hub (jplay/hub/https.py): don't edit, it is written again.
map $http_upgrade $jplay_connection {{ default upgrade; '' close; }}

server {{
{listen(80)}
    server_name {d} *.{d};
    return 301 https://$host$request_uri;
}}

server {{
{listen(443, " ssl")}
    server_name {d};
{tls("home")}
    location / {{
        proxy_pass http://127.0.0.1:{hub_port};
{proxy}
    }}
}}
"""]
    for dev in devs:
        if dev["kind"] == "hub":
            continue
        out.append(f"""server {{
{listen(443, " ssl")}
    server_name {dev['subdomain']}.{d};   # {dev['kind']}: {dev['name']}
{tls("devices")}
    location / {{
        proxy_pass http://{dev['address']}:{dev['port']};
{proxy}
    }}
}}
""")
    return "\n".join(out)


def write_nginx(force: bool = False) -> bool:
    """Write nginx's settings for the devices known now and reload it when they changed. True when reloaded."""
    s = settings()
    b = base_dir()
    if not (certificate_info("home") and certificate_info("devices")):
        return False
    conf = nginx_conf(s, devices.devices(), b / "certbot")
    path = b / "nginx.conf"
    if not force and path.exists() and path.read_text() == conf:
        return False
    old = path.read_text() if path.exists() else None
    path.write_text(conf)
    test = subprocess.run(["sudo", "-n", NGINX, "-t", "-q"], capture_output=True, text=True, timeout=30)
    if test.returncode != 0:
        if old is not None:
            path.write_text(old)
        else:
            path.unlink()
        raise HttpsError("nginx refused the settings: " + (test.stderr or test.stdout).strip()[-300:])
    res = subprocess.run(["sudo", "-n", SYSTEMCTL, "reload-or-restart", "nginx"], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise HttpsError("Couldn't reload nginx: " + (res.stderr or res.stdout).strip()[-300:])
    return True


def disable() -> None:
    db.set_setting("https", {**db.setting("https", {}), "enabled": False})
    path = base_dir() / "nginx.conf"
    if path.exists():
        path.unlink()
        subprocess.run(["sudo", "-n", SYSTEMCTL, "reload-or-restart", "nginx"], capture_output=True, timeout=30)
    status.update(state="off", message="Switched off")


def maintain() -> None:
    """Every few hours: renew what is close to expiring, and follow the devices (a new display gets its address)."""
    if not settings()["enabled"]:
        return
    try:
        if any((certificate_info(n) or {}).get("days_left", 0) is None or (certificate_info(n) or {}).get("days_left", 0) <= RENEW_DAYS
               for n in ("home", "devices")):
            obtain()
        else:
            write_nginx()
            status["state"] = "on"
    except Exception as e:
        status["state"] = "failed"
        _say(f"{type(e).__name__}: {e}")


def view() -> dict:
    s = settings()
    devs = devices.devices()
    return {**s, "state": status["state"] if s["enabled"] or status["state"] == "working" else
            ("failed" if status["state"] == "failed" else "off"),
            "message": status["message"], "log": status["log"][-12:],
            "certificates": {"home": certificate_info("home"), "devices": certificate_info("devices")},
            "nginx": Path(NGINX).exists(), "address": devices.own_address(),
            "addresses": [{"name": d["name"], "kind": d["kind"],
                           "url": f"https://{d['subdomain']}.{s['domain']}/" if d["kind"] != "hub" else f"https://{s['domain']}/"}
                          for d in devs] if s["domain"] else []}


def dumps(x: Any) -> str:
    return json.dumps(x)
