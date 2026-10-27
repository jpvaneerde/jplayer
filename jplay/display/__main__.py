"""Entry point: `python -m jplay.display` (the jplay-display service)."""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="jplay-display", description="JPlaying Now: the display")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    from .app import app            # imported here: it silences ALSA's noise while it loads
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
