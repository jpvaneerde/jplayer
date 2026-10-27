"""Finding JPlay services on the network (mDNS), with Avahi's command-line tool `avahi-browse`.

browse("_jplay-hub._tcp") -> [{"name", "host", "address", "port", "txt": {...}}]
Without avahi-browse (a PC without Avahi), nothing is found and the configured addresses are used.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time

_cache: dict[str, tuple[float, list[dict]]] = {}


def _unescape(s: str) -> str:
    """Avahi writes special characters as \\DDD (decimal): 'Lounge\\032JPlay' -> 'Lounge JPlay'."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s).replace("\\.", ".").replace("\\\\", "\\")


def parse(output: str) -> list[dict]:
    found: dict[str, dict] = {}
    for line in output.splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";", 9)
        if len(parts) < 9:
            continue
        _, _iface, proto, name, _type, _domain, host, address, port = parts[:9]
        txt = {}
        for item in re.findall(r'"((?:[^"\\]|\\.)*)"', parts[9] if len(parts) > 9 else ""):
            k, _, v = _unescape(item).partition("=")
            txt[k] = v
        name = _unescape(name)
        entry = {"name": name, "host": host, "address": address, "port": int(port or 0), "txt": txt}
        key = txt.get("id") or name
        # Prefer IPv4 addresses (and skip IPv6 link-local ones, which need an interface to work)
        if key not in found or (proto == "IPv4" and ":" in found[key]["address"]):
            if proto == "IPv6" and address.lower().startswith("fe80"):
                continue
            found[key] = entry
    return list(found.values())


def browse(service_type: str, timeout: float = 5, max_age: float = 60) -> list[dict]:
    hit = _cache.get(service_type)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    tool = shutil.which("avahi-browse")
    if not tool:
        return []
    try:
        out = subprocess.run([tool, "-rpt", service_type], capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return hit[1] if hit else []
    result = parse(out)
    _cache[service_type] = (time.time(), result)
    return result
