"""Unit tests for the pure parts: parsing, IDs, metadata, play logging."""
from jplay.player.core.models import NowPlaying, PlayState
from jplay.player.core.playlog import PlayLogger
from jplay.player.sources.cd import (
    disc_id_from_toc,
    parse_range,
    wav_header,
    musicbrainz_disc_id,
    parse_musicbrainz,
    parse_paranoia_toc,
)
from jplay.player.sources.radio import normalize_station, split_icy_title

PARANOIA_Q = """cdparanoia III release 10.2 (September 11, 2008)

Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    23312 [05:10.62]        0 [00:00.00]    no   no  2
  2.    21107 [04:41.32]    23312 [05:10.62]    no  yes  2
  3.    30071 [06:40.71]    44419 [09:52.19]    no   no  2
TOTAL   74490 [16:33.15]    (audio only)
"""


def test_parse_paranoia_toc():
    toc = parse_paranoia_toc(PARANOIA_Q)
    assert [t["number"] for t in toc] == [1, 2, 3]
    assert toc[1] == {"number": 2, "start": 23312, "length": 21107, "preemphasis": True}
    assert toc[0]["preemphasis"] is False


def test_disc_id_matches_libdiscid_reference():
    # Reference value from libdiscid's own test suite.
    offsets = [150, 9700, 25887, 39297, 53795, 63735, 77517, 94877, 107270, 123552, 135522,
               148422, 161197, 174790, 192022, 205545, 218010, 228700, 239590, 255470, 266932, 288750]
    assert musicbrainz_disc_id(1, 22, 303602, offsets) == "xUp1F2NkfP8s8jaeFn_Av3jNEI4-"


def test_disc_id_from_toc_uses_pregap_offsets():
    toc = parse_paranoia_toc(PARANOIA_Q)
    expected = musicbrainz_disc_id(1, 3, 74490 + 150, [150, 23462, 44569])
    assert disc_id_from_toc(toc) == expected


def test_parse_musicbrainz_picks_matching_medium():
    data = {
        "releases": [
            {
                "id": "rel-1",
                "title": "Kind of Blue",
                "date": "1959-08-17",
                "artist-credit": [{"name": "Miles Davis", "joinphrase": ""}],
                "cover-art-archive": {"front": True},
                "media": [
                    {"position": 1, "discs": [{"id": "other"}], "tracks": []},
                    {
                        "position": 2,
                        "discs": [{"id": "DISC"}],
                        "tracks": [
                            {"position": 1, "title": "So What"},
                            {"position": 2, "title": "Duet", "artist-credit": [
                                {"name": "A", "joinphrase": " & "}, {"name": "B", "joinphrase": ""}]},
                        ],
                    },
                ],
            }
        ]
    }
    meta = parse_musicbrainz(data, "DISC")
    assert meta["album"] == "Kind of Blue" and meta["year"] == "1959" and meta["medium"] == 2
    assert meta["tracks"][1] == {"title": "So What", "artist": "Miles Davis"}
    assert meta["tracks"][2]["artist"] == "A & B"
    assert meta["art_url"].endswith("/release/rel-1/front-500")
    assert parse_musicbrainz(data, "nope") is None


def test_normalize_station_is_idempotent():
    # The UI saves and plays stations exactly as the search returned them (tags as a list).
    raw = {"stationuuid": "u1", "name": "P2", "url_resolved": "http://b", "tags": "jazz,classical",
           "country": "Sweden", "codec": "AAC", "bitrate": 96}
    once = normalize_station(raw)
    assert normalize_station(once) == once


def test_split_icy_title():
    assert split_icy_title("Nina Simone - Sinnerman") == ("Nina Simone", "Sinnerman")
    assert split_icy_title("News at ten") == ("", "News at ten")
    assert split_icy_title(" - ") == ("", "-")


def test_normalize_station_prefers_resolved_url():
    st = normalize_station({"stationuuid": "u1", "name": " P2 ", "url": "http://a", "url_resolved": "http://b",
                            "tags": "jazz,classical,,news,talk,extra"})
    assert st["uuid"] == "u1" and st["name"] == "P2" and st["url"] == "http://b"
    assert st["tags"] == ["jazz", "classical", "news", "talk"]


def test_playlog_posts_once_after_threshold(monkeypatch):
    sent = []
    log = PlayLogger("http://example/hook", min_seconds=30, player_name="JPlay")
    monkeypatch.setattr(log, "_post", lambda payload: sent.append(payload))
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro)
    clock = [1000.0]
    monkeypatch.setattr("jplay.player.core.playlog.time.monotonic", lambda: clock[0])
    np = NowPlaying(state=PlayState.PLAYING, title="So What", artist="Miles Davis", duration=545)
    for _ in range(40):  # 40 one-second ticks
        log.observe("plex", np)
        clock[0] += 1
    assert len(sent) == 1
    assert sent[0]["title"] == "So What" and sent[0]["source"] == "plex"
    # a new track starts counting again
    np2 = NowPlaying(state=PlayState.PLAYING, title="Freddie Freeloader", artist="Miles Davis", duration=580)
    for _ in range(5):
        log.observe("plex", np2)
        clock[0] += 1
    assert len(sent) == 1


def test_parse_range():
    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=500-", 1000) == (500, 999)
    assert parse_range("bytes=-100", 1000) == (900, 999)
    assert parse_range("bytes=900-5000", 1000) == (900, 999)
    assert parse_range("bytes=1000-", 1000) is None
    assert parse_range(None, 1000) is None


def test_wav_header_is_valid():
    import io
    import wave

    data_len = 4 * 44100  # one second
    w = wave.open(io.BytesIO(wav_header(data_len) + b"\0" * data_len))
    assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (2, 2, 44100, 44100)
