"""Lyrics from the music share: .lrc files found over SMB (a fake share here) win over LRCLIB, the newest file per
song counts, removed files send the song back to LRCLIB, and lyrics pasted by hand are never overwritten."""
import io
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jplay.common import lrclib
from jplay.hub import app as hub_app, db, lyrics, lyrics_share

LRC = "[ar:ABBA]\n[ti:Dancing Queen]\n[00:01.00]You can dance\n[00:03.50]You can jive\n"
WORDS = "[00:01.00]<00:01.00>You <00:01.40>can <00:01.80>dance\n[00:03.50]You can jive\n"


class FakeShare:
    """Just enough of smbclient: files and folders under \\\\nas\\music, with times."""

    def __init__(self):
        self.files: dict[str, tuple[bytes, float]] = {}
        self.broken: set[str] = set()          # folders that fail to list
        self.sessions: list = []

    def put(self, path: str, data: bytes | str, mtime: float | None = None):
        self.files[path] = (data.encode() if isinstance(data, str) else data, mtime or time.time())

    def remove(self, path: str):
        del self.files[path]

    # --- smbclient API
    def register_session(self, server, username=None, password=None, connection_timeout=60):
        if password == "wrong":
            raise Exception("STATUS_LOGON_FAILURE")
        self.sessions.append((server, username, password))

    def delete_session(self, server):
        pass

    def _rel(self, unc: str) -> str:
        assert unc.lower().startswith("\\\\nas\\music")
        return unc[len("\\\\nas\\music"):].strip("\\").replace("\\", "/")

    def scandir(self, unc: str):
        rel = self._rel(unc)
        if rel in self.broken:
            raise Exception("STATUS_ACCESS_DENIED")
        prefix = rel + "/" if rel else ""
        seen = {}
        for p, (data, mt) in self.files.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            name, _, more = rest.partition("/")
            if more:
                seen.setdefault(name, None)
            else:
                seen[name] = (len(data), mt)
        if not seen and rel:
            raise Exception("STATUS_OBJECT_NAME_NOT_FOUND")
        out = []
        for name, st in seen.items():
            is_dir = st is None
            out.append(SimpleNamespace(name=name, is_dir=(lambda d=is_dir: d),
                                       stat=(lambda st=st: SimpleNamespace(st_size=st[0], st_mtime=st[1]))))
        return out

    def open_file(self, unc: str, mode="rb"):
        return io.BytesIO(self.files[self._rel(unc)][0])


@pytest.fixture
def share(monkeypatch):
    fake = FakeShare()
    monkeypatch.setattr(lyrics_share, "_smb", lambda: fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch, share):
    monkeypatch.setattr(hub_app, "PROCESS_EVERY", 3600)
    monkeypatch.setattr(lyrics, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LRCLIB asked")))
    with TestClient(hub_app.create_app(tmp_path, announce=False)) as c:
        c.put("/api/lyrics/share", json={"address": "\\\\nas\\music", "username": "jp", "password": "pw", "every": 15})
        yield c


@pytest.fixture(scope="module")
def flac_bytes(tmp_path_factory):
    """A tiny real FLAC tagged ABBA – Dancing Queen (the tags are what the hub goes by)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("flac") / "a.flac"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2", str(p)],
                   check=True)
    from mutagen.flac import FLAC
    f = FLAC(p)
    f["artist"], f["title"], f["album"] = "ABBA", "Dancing Queen", "Arrival"
    f.save()
    return p.read_bytes()


def ask(client, artist="ABBA", title="Dancing Queen"):
    return client.get("/api/lyrics", params={"artist": artist, "title": title}).json()


def test_parse_lrc_keeps_word_times():
    rows = lrclib.parse_lrc(WORDS)
    assert rows[0] == [1.0, "You can dance", [[1.0, "You "], [1.4, "can "], [1.8, "dance"]]]
    assert rows[1] == [3.5, "You can jive"]
    assert lrclib.lrc_tags(LRC) == {"ar": "ABBA", "ti": "Dancing Queen"}


def test_a_file_on_the_share_wins_and_follows_changes(client, share, flac_bytes):
    share.put("ABBA/Arrival/02 - Dancing Queen.flac", flac_bytes)
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", WORDS, mtime=1000)
    counts = lyrics_share.scan()
    assert counts["files"] == 1 and counts["new"] == 1 and counts["updated"] == 1
    a = ask(client)
    assert a["found"] and a["synced"][0][2][1] == [1.4, "can "]
    song = client.get("/api/lyrics/song", params={"key": lrclib.cache_key("ABBA", "Dancing Queen")}).json()
    assert song["source"] == "file" and song["album"] == "Arrival" and song["duration"] == 2.0

    # nothing changed: nothing is read again
    assert lyrics_share.scan()["new"] == 0 and lyrics_share.scan()["changed"] == 0

    # a timing correction, then a newer version of the file: new lyrics, the correction starts over
    key = lrclib.cache_key("ABBA", "Dancing Queen")
    client.put("/api/lyrics/song", params={"key": key}, json={"offset": 2})
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", LRC, mtime=2000)
    assert lyrics_share.scan()["changed"] == 1
    a = ask(client)
    assert a["synced"] == [[1.0, "You can dance"], [3.5, "You can jive"]] and a["offset"] == 0


def test_newest_file_wins_and_removed_files_go_back_to_lrclib(client, share, monkeypatch):
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", LRC, mtime=1000)                     # [ar:]/[ti:] tags
    share.put("Various/Hits/07 - Dancing Queen.lrc", "[ar:ABBA]\n[ti:Dancing Queen]\n[00:02.00]Newer\n", mtime=5000)
    lyrics_share.scan()
    assert ask(client)["plain"] == ["Newer"]
    share.remove("Various/Hits/07 - Dancing Queen.lrc")
    assert lyrics_share.scan()["gone"] == 1
    assert ask(client)["plain"] == ["You can dance", "You can jive"]
    share.remove("ABBA/Arrival/02 - Dancing Queen.lrc")
    lyrics_share.scan()
    monkeypatch.setattr(lyrics, "fetch", lambda *a, **k: {**lrclib.NOT_FOUND, "found": True, "plain": ["From LRCLIB"]})
    assert ask(client)["plain"] == ["From LRCLIB"]


def test_lyrics_pasted_by_hand_stay(client, share):
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", LRC, mtime=1000)
    lyrics_share.scan()
    key = lrclib.cache_key("ABBA", "Dancing Queen")
    client.put("/api/lyrics/song", params={"key": key}, json={"text": "[00:05.00] Mine"})
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", LRC + "[00:09.00]More\n", mtime=3000)
    lyrics_share.scan()
    assert ask(client)["synced"] == [[5.0, "Mine"]]


def test_song_from_the_folders_and_files_that_cant_be_used(client, share):
    share.put("Kent/Isola/03 - Om du var här.lrc", "[00:01.00]Om du var här\n")          # no tags: the folders say
    share.put("Kent/Isola/04 - Empty.lrc", "\n\n")
    share.put("loose.lrc", "[00:01.00]Who am I\n")                                         # no artist anywhere
    share.put("@eaDir/thumbs/x.lrc", LRC)                                                  # Synology's own folders: skipped
    c = lyrics_share.scan()
    assert c["files"] == 3 and c["problems"] == 2
    assert ask(client, "Kent", "Om du var här")["synced"] == [[1.0, "Om du var här"]]
    v = client.get("/api/lyrics/share").json()
    assert v["files"] == 3 and v["songs"] == 1 and v["problems"] == 2 and "password" not in str(v.get("password_set", ""))
    assert {p["path"] for p in v["problem_files"]} == {"Kent/Isola/04 - Empty.lrc", "loose.lrc"}


def test_a_folder_that_cant_be_read_doesnt_remove_lyrics(client, share):
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", LRC, mtime=1000)
    lyrics_share.scan()
    share.broken.add("ABBA")
    c = lyrics_share.scan()
    assert c["gone"] == 0 and lyrics_share.status["state"] == "partly"
    assert ask(client)["found"]


def test_settings_test_and_another_share(client, share):
    v = client.get("/api/lyrics/share").json()
    assert v["address"] == "\\\\nas\\music" and v["password_set"] and v["every"] == 15
    assert share.sessions == [] or share.sessions[-1][2] == "pw"
    share.put("ABBA/Arrival/02 - Dancing Queen.lrc", LRC)
    r = client.post("/api/lyrics/share/test", json={"address": "//nas/music", "username": "jp"}).json()
    assert r["ok"] and "1 folder " in r["message"]
    r = client.post("/api/lyrics/share/test", json={"address": "//nas/music", "username": "jp", "password": "wrong"}).json()
    assert not r["ok"] and "password" in r["message"]
    assert not client.post("/api/lyrics/share/test", json={"address": "nas"}).json()["ok"]
    assert client.put("/api/lyrics/share", json={"address": "nas"}).status_code == 400

    lyrics_share.scan()
    assert ask(client)["found"]
    # the share is cleared: what came from it goes back to LRCLIB
    client.put("/api/lyrics/share", json={"address": ""})
    assert db.get("SELECT COUNT(*) AS n FROM lyric_files")["n"] == 0
    assert db.get("SELECT COUNT(*) AS n FROM lyrics WHERE source = 'file'")["n"] == 0


def test_parse_address():
    assert lyrics_share.parse_address("\\\\synology\\music\\Albums") == ("synology", "music", "Albums")
    assert lyrics_share.parse_address("smb://192.168.1.5/music/") == ("192.168.1.5", "music", "")
    with pytest.raises(lyrics_share.ShareError):
        lyrics_share.parse_address("synology")
