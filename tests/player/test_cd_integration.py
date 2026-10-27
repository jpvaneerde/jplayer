"""End to end: insert a (fake) CD, read it while playing, through the real HTTP stream.

Runs the whole app (uvicorn) with mpv playing silently and a fake cd-paranoia that reads
a 3-track disc at twice real time.
"""
import os
import shutil
import socket
import stat
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import httpx
import pytest
import uvicorn

from jplay.player.app import create_app
from jplay.player.config import load_config

pytestmark = pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")

FAKE = Path(__file__).with_name("fake_paranoia.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = cond()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"{what} not met in {timeout}s")


@pytest.fixture
def player(tmp_path, monkeypatch):
    # cd-paranoia stand-in, run through a wrapper so it uses this Python
    wrapper = tmp_path / "cd-paranoia"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    drive = tmp_path / "sr0"
    drive.write_text("")
    radio_file = tmp_path / "radio.wav"
    with wave.open(str(radio_file), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(44100)
        w.writeframes(struct.pack("<hh", 0, 0) * 44100 * 60)

    port = _free_port()
    cfg = load_config(None)
    cfg["server"]["port"] = port
    cfg["paths"].update(state_dir=str(tmp_path / "state"), runtime_dir=str(tmp_path / "run"))
    cfg["audio"]["device"] = "null"
    cfg["cd"].update(device=str(drive), cache_dir=str(tmp_path / "shm"), paranoia_binary=str(wrapper),
                     musicbrainz=False)
    for name in ("plex", "spotify", "bluetooth"):
        cfg[name]["enabled"] = False
    monkeypatch.setenv("FAKE_PARANOIA_SPEED", "2")

    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started, what="server start")
    cd = app.state.ctx.manager.get("cd")
    drive_state = {"value": "empty"}
    cd.drive_status = lambda dev: drive_state["value"]
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=10)
    yield client, drive_state, radio_file, tmp_path, cd
    client.close()
    server.should_exit = True
    thread.join(10)


def status(client):
    return client.get("/status").json()


def cd_info(client):
    return client.get("/cd").json()


def test_insert_reads_without_taking_over_then_plays_while_reading(player):
    client, drive, radio_file, tmp, _ = player

    # Radio is playing.
    r = client.post("/radio/play", json={"station": {"uuid": "r1", "name": "Test FM", "url": str(radio_file)}})
    assert r.status_code == 200
    wait_for(lambda: status(client)["now_playing"]["state"] == "playing", what="radio playing")

    # Insert a disc: it is read straight away, but the radio keeps playing.
    drive["value"] = "audio"
    wait_for(lambda: cd_info(client)["reading"].get("track") == 1, what="reading track 1")
    time.sleep(1.0)
    s = status(client)
    assert s["active"] == "radio" and s["now_playing"]["state"] == "playing"

    # Press play on the CD: sound within a couple of seconds, while the drive is still reading.
    started = time.monotonic()
    assert client.post("/cd/play", json={"track": 1}).status_code == 200
    wait_for(lambda: status(client)["now_playing"]["state"] == "playing"
             and status(client)["active"] == "cd", timeout=5, what="CD playing")
    assert time.monotonic() - started < 4
    info = cd_info(client)
    track1 = next(t for t in info["disc"]["tracks"] if t["number"] == 1)
    assert not track1["in_memory"], "playback should start before the track is fully read"

    # Seek past what the drive has read: it waits for the drive, then carries on from there.
    assert client.post("/player/seek", json={"position": 8}).status_code == 200
    wait_for(lambda: (status(client)["now_playing"]["position"] or 0) >= 8, timeout=10, what="seek past read point")

    # Skip to the next track (not read yet): it plays as soon as the drive gets there.
    assert client.post("/player/next").status_code == 200
    wait_for(lambda: status(client)["now_playing"]["track_number"] == 2
             and status(client)["now_playing"]["state"] == "playing", timeout=8, what="track 2 playing")

    # Eventually the whole disc is in memory and the drive rests.
    wait_for(lambda: cd_info(client)["reading"].get("done"), timeout=20, what="whole disc read")
    assert all(t["in_memory"] for t in cd_info(client)["disc"]["tracks"])

    # Take the disc out: memory is freed, CD stops.
    drive["value"] = "empty"
    wait_for(lambda: cd_info(client)["disc"] is None, what="disc gone")
    assert not any((tmp / "shm").glob("*.wav"))
    assert status(client)["now_playing"]["state"] == "stopped"


def test_stream_endpoint_serves_ranges(player):
    client, drive, _, _, cd = player
    drive["value"] = "audio"
    wait_for(lambda: cd_info(client)["disc"] is not None, what="disc loaded")
    # Unknown token: stale URLs from an earlier disc don't play.
    assert client.get("/cd/stream/nope/1.wav").status_code == 404
    url = f"/cd/stream/{cd.token}/2.wav"
    r = client.get(url, headers={"Range": "bytes=0-43"})
    assert r.status_code == 206 and r.content[:4] == b"RIFF" and len(r.content) == 44
    assert r.headers["content-range"] == f"bytes 0-43/{44 + 300 * 2352}"
    assert client.get(url, headers={"Range": "bytes=999999999-"}).status_code == 416
