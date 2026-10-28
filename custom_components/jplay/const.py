"""Constants for the JPlay integration."""
from __future__ import annotations

DOMAIN = "jplay"

CONF_KIND = "kind"

KIND_PLAYER = "player"          # a JPlay player (port 8080)
KIND_NOW = "jplayingnow"        # a JPlaying Now display (port 5000)
KIND_HUB = "hub"                # the JPlay hub: the house's plays and lyrics (port 8090)

PLAYER_PORT = 8080
NOW_PORT = 5000
HUB_PORT = 8090

SOURCE_NAMES = {"radio": "Radio", "plex": "Plex", "cd": "CD", "spotify": "Spotify", "bluetooth": "Bluetooth"}
