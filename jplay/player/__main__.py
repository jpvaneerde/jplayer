"""Entry point: `python -m jplay --config /etc/jplay/config.toml`"""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="jplay", description="JPlay multi-source music player")
    parser.add_argument("--config", default=os.environ.get("JPLAY_CONFIG", "/etc/jplay/config.toml"))
    parser.add_argument("--host", help="override server.host")
    parser.add_argument("--port", type=int, help="override server.port")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config(args.config)
    if args.host:
        config["server"]["host"] = args.host
    if args.port:
        config["server"]["port"] = args.port

    uvicorn.run(
        create_app(config),
        host=config["server"]["host"],
        port=config["server"]["port"],
        log_level="warning",
        access_log=False,
        # A CD track that is still being read keeps mpv's stream request open; without a
        # limit, stopping the service waits for the drive (systemctl restart seems to hang).
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
