"""Entry point: `python -m jplay.hub` (the jplay-hub service)."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .app import PORT, create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="jplay-hub", description="JPlay hub: the house's plays and lyrics")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--data", help="data folder (default /var/lib/jplay/hub)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    uvicorn.run(create_app(Path(args.data) if args.data else None, args.port), host="0.0.0.0", port=args.port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
