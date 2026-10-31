"""The fingerprint service: Panako's output read right, and (when Panako is installed: PANAKO_JAR) the whole thing
against real Panako: index a small collection, recognise a room-like clip, moves, retags, removals, covers."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jplay.fingerprint import app as fp_app, panako as pk
from tests.fingerprint.make_audio import song

QUERY_OUT = """Index; Total ; Query path;Query start (s);Query stop (s); Match path;Match id; Match start (s); Match stop (s); Match score; Time factor (%); Frequency factor(%); Seconds with match (%)
1 ; 2 ; /tmp/clip.wav ; 1.021 ; 9.709 ; /data/links/3.flac ; 3 ; 41.053 ; 49.885 ; 94 ; 1.016 % ; 1.016 %; 0.89
2 ; 2 ; /tmp/clip.wav ; 2.000 ; 8.000 ; /data/links/7.flac ; 7 ; 10.000 ; 16.000 ; 12 ; 1.000 % ; 1.000 %; 0.40
"""
NO_MATCH = """Index; Total ; Query path;Query start (s);Query stop (s); Match path;Match id; Match start (s); Match stop (s); Match score; Time factor (%); Frequency factor(%); Seconds with match (%)
1 ; 1 ; /tmp/clip.wav ; 0.000 ; 0.000 ; null ; null ; -1.000 ; -1.000 ; -1 ; -1.000 % ; -1.000 %; 0.00
"""
STORE_OUT = """Audiofile;Audio duration;Fingerprinting duration;ratio
1/3;12.flac;00:01:28;1.36 s;64.80
2/3;13.flac;Skipped: resource already stored;
3/3;14.flac;Could not decode;
"""


def test_panako_output_is_read(monkeypatch, tmp_path):
    p = pk.Panako(tmp_path)
    monkeypatch.setattr(p, "_run", lambda args, timeout: QUERY_OUT)
    m = p.query("/tmp/clip.wav")
    assert [x.id for x in m] == [3, 7] and m[0].score == 94 and m[0].time_factor == 1.016
    assert abs(m[0].offset - (41.053 - 1.021 * 1.016)) < 0.01
    monkeypatch.setattr(p, "_run", lambda args, timeout: NO_MATCH)
    assert p.query("/tmp/clip.wav") == []
    monkeypatch.setattr(p, "_run", lambda args, timeout: STORE_OUT)
    assert p.store(["/data/links/12.flac", "/data/links/13.flac", "/data/links/14.flac"]) == {12, 13}


# ---------------------------------------------------------------- with real Panako
JAR = os.environ.get("PANAKO_JAR", "/tmp/claude-0/pk/panako.jar")
real = pytest.mark.skipif(not (Path(JAR).exists() and shutil.which("java") and shutil.which("ffmpeg")),
                          reason="Panako (PANAKO_JAR), Java and ffmpeg are needed")


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def audio(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    for s in range(1, 7):
        song(s, str(d / f"song{s}.wav"))
    # a microphone in a room: 12 s from 40 s into song 3, 2 % fast (a turntable), highs gone, quieter, noise
    ff("-ss", "40", "-t", "12.5", "-i", str(d / "song3.wav"), "-af",
       "asetrate=22050*1.02,aresample=16000,lowpass=f=5000,volume=0.5", "-ar", "16000", str(d / "raw.wav"))
    ff("-i", str(d / "raw.wav"), "-f", "lavfi", "-t", "12.5", "-i",
       "anoisesrc=color=pink:amplitude=0.08:sample_rate=16000", "-filter_complex", "amix=inputs=2:duration=first",
       "-ar", "16000", "-ac", "1", str(d / "room3.wav"))
    ff("-ss", "20", "-t", "12", "-i", str(d / "song6.wav"), "-ar", "16000", "-ac", "1", str(d / "unknown.wav"))
    return d


def flac(audio, n, dest, title=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    ff("-i", str(audio / f"song{n}.wav"), "-metadata", f"title={title or f'Song {n}'}", "-metadata", "artist=Artist A",
       "-metadata", "album=Album 1", "-metadata", "date=2024", "-metadata", f"tracknumber={n}", str(dest))


@real
def test_the_whole_service(audio, tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "JAR", JAR)
    music, data = tmp_path / "music", tmp_path / "data"
    for n in range(1, 6):
        flac(audio, n, music / "Artist A" / "Album 1" / f"0{n} Song {n}.flac")
    (music / "Artist A" / "Album 1" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0 a jpeg")
    (music / "@eaDir").mkdir()
    (music / "@eaDir" / "junk.flac").write_bytes(b"not audio")      # Synology's thumbnails: skipped

    c = TestClient(fp_app.create_app(str(data), str(music), token="secret", schedule=False))
    lib = c.app.state.library
    first = lib.index()
    assert first["new"] == 5 and first["fingerprinted"] == 5 and first["failed"] == 0
    st = c.get("/api/status").json()
    assert st["files"] == 5 and st["indexed"] == 5 and st["waiting"] == 0

    wav = (audio / "room3.wav").read_bytes()
    assert c.post("/api/recognize", content=wav).status_code == 401
    auth = {"Authorization": "Bearer secret"}
    r = c.post("/api/recognize", content=wav, headers=auth).json()
    assert r["found"] and r["song"]["title"] == "Song 3" and r["song"]["album"] == "Album 1" and r["song"]["year"] == "2024"
    assert 39 <= r["offset"] <= 42 and 1.0 < r["time_factor"] < 1.04
    assert c.get(r["song"]["cover"]).content == b"\xff\xd8\xff\xe0 a jpeg"
    assert c.post("/api/recognize", content=(audio / "unknown.wav").read_bytes(), headers=auth).json() == \
        {"found": False, "elapsed_ms": c.get("/api/status").json()["recognize"]["last_ms"]}
    sid = r["song"]["id"]

    # Moved (another folder, another name): same number, not fingerprinted again
    old = music / "Artist A" / "Album 1" / "03 Song 3.flac"
    new = music / "Artist A" / "Album 1 (2024)" / "3 - Song 3.flac"
    new.parent.mkdir()
    old.rename(new)
    moved = lib.index()
    assert moved["moved"] == 1 and moved["fingerprinted"] == 0 and moved["new"] == 0
    r = c.post("/api/recognize", content=wav, headers=auth).json()
    assert r["song"]["id"] == sid and r["song"]["path"] == "Artist A/Album 1 (2024)/3 - Song 3.flac"

    # New tags: read again, not fingerprinted again
    time.sleep(1.1)
    ff("-i", str(new), "-c", "copy", "-metadata", "title=Song Three", "-metadata", "artist=Artist A", str(tmp_path / "t.flac"))
    shutil.move(str(tmp_path / "t.flac"), str(new))
    retag = lib.index()
    assert retag["changed"] == 1 and retag["fingerprinted"] == 0
    assert c.post("/api/recognize", content=wav, headers=auth).json()["song"]["title"] == "Song Three"

    # Removed: ignored from then on
    new.unlink()
    assert lib.index()["gone"] == 1
    assert c.post("/api/recognize", content=wav, headers=auth).json()["found"] is False
    assert c.get("/api/status").json()["gone"] == 1

    # Rebuild: removed songs tidied away, the rest back from the cache
    assert c.post("/api/rebuild", headers=auth).json() == {"started": True}
    for _ in range(300):                       # up to 90 s on a busy machine
        if not lib.run["running"] and c.get("/api/status").json()["indexed"] == 4:
            break
        time.sleep(0.3)
    st = c.get("/api/status").json()
    assert st["indexed"] == 4 and st["gone"] == 0 and st["files"] == 4
