"""librespot --onevent hook: forwards the event to the JPlay API.

librespot runs this for every player event with the details in environment variables.
It must start fast and never block librespot, so it uses only the standard library.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

FIELDS = (
    "PLAYER_EVENT", "TRACK_ID", "OLD_TRACK_ID", "URI", "NAME", "ARTISTS", "ALBUM_ARTISTS",
    "ALBUM", "COVERS", "DURATION_MS", "POSITION_MS", "ITEM_TYPE", "SHOW_NAME", "NUMBER",
    "DISC_NUMBER", "USER_NAME", "CLIENT_ID", "CLIENT_NAME", "CLIENT_BRAND_NAME",
    "CLIENT_MODEL_NAME", "VOLUME", "SINK_STATUS", "SHUFFLE", "REPEAT",
)


def main() -> int:
    url = os.environ.get("JPLAY_HOOK_URL")
    if not url:
        return 0
    payload = {k: os.environ[k] for k in FIELDS if k in os.environ}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-JPlay-Hook": os.environ.get("JPLAY_HOOK_TOKEN", "")},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception as exc:  # never fail librespot because JPlay is busy
        print(f"jplay hook: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
