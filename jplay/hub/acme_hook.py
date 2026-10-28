"""certbot's helper for the DNS proof (see https.py): python -m jplay.hub.acme_hook auth|cleanup

certbot passes the proof in CERTBOT_VALIDATION; this puts it on DuckDNS (<name>.duckdns.org, which
_acme-challenge.<domain> points to) and waits until DuckDNS's own name servers answer with it.
"""
from __future__ import annotations

import os
import sys
import time

import requests

DUCKDNS = "https://www.duckdns.org/update"
WAIT_SECONDS = 240          # at most, for the record to show up
SETTLE_SECONDS = 20         # after it shows up: Let's Encrypt asks from several places


def _token() -> str:
    with open(os.environ["JPLAY_DUCKDNS_TOKEN_FILE"]) as f:
        return f.read().strip()


def set_txt(name: str, token: str, value: str, clear: bool = False, session=requests) -> None:
    params = {"domains": name, "token": token, "txt": value or "removed", "verbose": "true"}
    if clear:
        params["clear"] = "true"
    r = session.get(DUCKDNS, params=params, timeout=30)
    if not r.text.startswith("OK"):
        raise SystemExit(f"DuckDNS refused the record: {r.text.strip()[:100]} (check the DuckDNS name and token)")


def visible(name: str, value: str) -> bool:
    """Do DuckDNS's name servers already answer with the value? (Asked directly, so no cache in between.)"""
    try:
        import dns.resolver  # dnspython (comes with certbot)

        ns_ips = []
        for ns in dns.resolver.resolve("duckdns.org", "NS"):
            ns_ips += [a.to_text() for a in dns.resolver.resolve(ns.target, "A")]
        res = dns.resolver.Resolver(configure=False)
        res.nameservers = ns_ips[:3]
        res.lifetime = 8
        for rr in res.resolve(f"{name}.duckdns.org", "TXT"):
            if value in "".join(s.decode() for s in rr.strings):
                return True
    except Exception:
        return False
    return False


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    name = os.environ["JPLAY_DUCKDNS"]
    value = os.environ.get("CERTBOT_VALIDATION", "")
    if mode == "auth":
        set_txt(name, _token(), value)
        start = time.time()
        while time.time() - start < WAIT_SECONDS:
            if visible(name, value):
                time.sleep(SETTLE_SECONDS)
                return 0
            time.sleep(10)
        time.sleep(30)            # couldn't check (no DNS library or no answer): give it time anyway
        return 0
    if mode == "cleanup":
        try:
            set_txt(name, _token(), "", clear=True)
        except BaseException:
            pass
        return 0
    print("usage: python -m jplay.hub.acme_hook auth|cleanup", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
