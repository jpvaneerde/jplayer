"""Configuration: a TOML file merged over built-in defaults."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "name": "JPlay",
    },
    "paths": {
        # Persistent state (favourites, CD metadata cache, settings).
        "state_dir": "/var/lib/jplay",
        # Volatile files (mpv socket, playlists). /run is tmpfs on Raspberry Pi OS.
        "runtime_dir": "/run/jplay",
    },
    "audio": {
        # "auto": a USB DAC if one is switched on, otherwise a DAC HAT.
        # Or an ALSA device such as "hw:CARD=DAC,DEV=0"; "null" plays silently (testing).
        # Can also be changed from the web page (Output), which overrides this.
        "device": "auto",
        # Optional hardware volume (amixer control). Empty = fixed line-level output.
        # mixer_card limits it to one card; empty = whichever card is in use.
        "mixer_card": "",
        "mixer_control": "",
        "mpv_binary": "mpv",
        "mpv_extra_args": [],
    },
    "radio": {
        "enabled": True,
        "radio_browser_url": "https://de1.api.radio-browser.info",
    },
    "plex": {
        "enabled": True,
        "url": "",  # e.g. http://192.168.1.10:32400
        "token": "",  # X-Plex-Token
        "scrobble": True,  # mark tracks played in Plex
    },
    "cd": {
        "enabled": True,
        "device": "/dev/sr0",
        # Drive speed while reading into memory: 0 = as fast as the drive goes.
        # A cap (e.g. 8) makes a noisy drive quieter but playback waits longer at the start.
        "read_speed": 0,
        # Error correction: "light" (fast, fine for clean discs), "full" (slow, for
        # scratched discs) or "off".
        "paranoia": "light",
        "cache_dir": "/dev/shm/jplay-cd",  # tracks are read into RAM
        # Reading always starts when a disc goes in; this also starts playing it
        # (taking over from whatever plays).
        "autoplay": False,
        "paranoia_binary": "cd-paranoia",
        "musicbrainz": True,
    },
    "spotify": {
        "enabled": True,
        "librespot_binary": "librespot",
        "device_name": "",  # defaults to server.name
        "bitrate": 320,
        "extra_args": [],
    },
    "bluetooth": {
        "enabled": True,
        "alias": "",  # defaults to server.name
        "aplay_binary": "bluealsa-aplay",
        "discoverable_seconds": 180,
    },
    "webhook": {
        # POSTed JSON for every new track on any source (e.g. JPlay Spins). Empty = off.
        "url": "",
        "min_seconds": 30,  # only log tracks that played at least this long
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(dict):
    """Nested dict with attribute-free, explicit access: cfg["plex"]["url"]."""

    path: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        return self.get(name, {})


def load_config(path: str | os.PathLike[str] | None) -> Config:
    data: dict[str, Any] = {}
    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path)
        if cfg_path.exists():
            with cfg_path.open("rb") as fh:
                data = tomllib.load(fh)
    cfg = Config(_merge(DEFAULTS, data))
    cfg.path = cfg_path
    # Environment overrides for secrets, so they can live outside the config file.
    if os.environ.get("JPLAY_PLEX_TOKEN"):
        cfg["plex"]["token"] = os.environ["JPLAY_PLEX_TOKEN"]
    if os.environ.get("JPLAY_PLEX_URL"):
        cfg["plex"]["url"] = os.environ["JPLAY_PLEX_URL"]
    name = cfg["server"]["name"]
    cfg["spotify"]["device_name"] = cfg["spotify"]["device_name"] or name
    cfg["bluetooth"]["alias"] = cfg["bluetooth"]["alias"] or name
    return cfg
