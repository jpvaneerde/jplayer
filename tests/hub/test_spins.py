"""The hub's Spins rules: which room, which source, one play per song."""
import json

import pytest

from jplay.hub import db, records, spins

T0 = 1_760_000_000_000   # ms


@pytest.fixture
def hub(tmp_path):
    db.init(str(tmp_path / "hub.db"))
    yield
    db.close()


def unit(room, uid=None):
    return {"id": uid or f"unit-{room}", "name": f"{room} display", "room": room}


def ev(eid, etype, ts, **data):
    return {"id": eid, "type": etype, "ts": ts, "data": data}


def process():
    spins.process_events(at=db.now() + spins.SETTLE_MS + 1000)


def test_radio_heard_while_the_player_plays_radio_is_streamed(hub):
    spins.ingest(unit("Lounge"), [
        ev("r1", "room", T0, state="play", mode="radio", station="P3"),
        ev("m1", "mic", T0 + 20_000, title="Training Season", artist="Dua Lipa"),
    ])
    process()
    plays = spins.list_plays()["plays"]
    assert len(plays) == 1
    p = plays[0]
    assert (p["kind"], p["detail"], p["station"], p["room"]) == ("streamed", "radio", "P3", "Lounge")


def test_two_rooms_hearing_one_song_is_one_play_in_the_more_important_room(hub):
    spins.save_room("Lounge", 10, ["vinyl"])
    spins.save_room("Kitchen", 1, [])
    spins.ingest(unit("Kitchen"), [ev("k1", "mic", T0, title="Purple Rain", artist="Prince")])
    spins.ingest(unit("Lounge"), [ev("l1", "mic", T0 + 30_000, title="Purple Rain (2015 Remaster)", artist="Prince")])
    process()
    plays = spins.list_plays()["plays"]
    assert len(plays) == 1
    assert plays[0]["room"] == "Lounge" and sorted(plays[0]["heard_by"]) == ["Kitchen", "Lounge"]
    # Without JPlay Records the hub can't tell it was a record
    assert plays[0]["kind"] == "heard" and plays[0]["note"] == spins.NOTE_NO_PLAYER


def test_with_jplay_records_a_heard_song_on_your_record_is_a_spin(hub, monkeypatch):
    spins.save_room("Lounge", 10, ["vinyl", "cd"])
    asked = []

    def fake_match(songs):
        asked.extend(songs)
        return [{"match": {"instanceId": 7, "releaseId": 70, "medium": "vinyl", "confidence": "mic",
                           "note": "Matched your vinyl", "alternatives": [{"id": 8, "artist": "Prince", "title": "Hits",
                                                                         "medium": "cd", "cover": None}]},
                 "note": "Matched your vinyl",
                 "record": {"id": 7, "artist": "Prince", "title": "Purple Rain", "year": 1984,
                            "cover": "https://records.example/covers/7.jpg"}} for _ in songs]

    monkeypatch.setattr(records, "config", lambda: ("https://records.example", "tok"))
    monkeypatch.setattr(records, "match_many", fake_match)
    spins.ingest(unit("Lounge"), [
        ev("m1", "mic", T0, title="Let's Go Crazy", artist="Prince", album="Purple Rain"),
        ev("m2", "mic", T0 + 5 * 60_000, title="Take Me with U", artist="Prince", album="Purple Rain"),
    ])
    process()
    assert asked[0]["formats"] == ["vinyl", "cd"]
    plays = spins.list_plays()["plays"]
    assert {p["kind"] for p in plays} == {"physical"}
    assert plays[0]["record"]["title"] == "Purple Rain" and plays[0]["instance_id"] == 7
    s = spins.recent_spins()
    assert len(s) == 1 and s[0]["tracks"] == 2 and s[0]["record"]["year"] == 1984
    # "It was the CD" (one of the suggested copies)
    pid = plays[0]["id"]
    assert spins.set_play_record(pid, 8)
    fixed = next(p for p in spins.list_plays()["plays"] if p["id"] == pid)
    assert fixed["detail"] == "cd" and fixed["confidence"] == "manual"


def test_records_away_leaves_the_play_to_be_matched_later(hub, monkeypatch):
    def away(songs):
        raise records.RecordsError("down")
    monkeypatch.setattr(records, "config", lambda: ("https://records.example", "tok"))
    monkeypatch.setattr(records, "match_many", away)
    spins.ingest(unit("Lounge"), [ev("m1", "mic", T0, title="Song", artist="Artist")])
    process()
    p = spins.list_plays()["plays"][0]
    assert p["kind"] == "heard" and p["note"] == spins.NOTE_RECORDS_AWAY


def test_a_cd_in_the_player_is_a_physical_cd_play_and_the_mic_joins_it(hub):
    spins.ingest(unit("Study"), [
        ev("r1", "room", T0, state="play", mode="cd", title="Hey Jude", artist="The Beatles", album="1"),
        ev("r2", "room", T0 + 60_000, state="play", mode="cd", title="Hey Jude", artist="The Beatles", album="1"),
        ev("m1", "mic", T0 + 30_000, title="Hey Jude", artist="The Beatles"),
    ])
    process()
    plays = spins.list_plays()["plays"]
    assert len(plays) == 1
    p = plays[0]
    assert (p["kind"], p["detail"], p["note"]) == ("physical", "cd", spins.NOTE_PLAYER_CD)
    assert p["heard_by"] == ["Study"]
    # Without JPlay Records a spin is the album
    assert spins.recent_spins()[0]["record"]["title"] == "1"


def test_radio_text_that_disagrees_with_the_microphone_is_dropped(hub):
    spins.ingest(unit("Lounge"), [
        ev("r1", "room", T0, state="play", mode="radio", station="P3", artist="P3 Morgon", title="Nyheter"),
        ev("r2", "room", T0 + 10_000, state="play", mode="radio", station="P3", artist="Wrong", title="Song"),
        ev("m1", "mic", T0 + 20_000, title="Right Song", artist="Right Artist"),
    ])
    process()
    titles = [p["title"] for p in spins.list_plays()["plays"]]
    assert titles == ["Right Song"]


def test_listening_time_per_station(hub):
    spins.ingest(unit("Lounge"), [
        ev("r1", "room", T0, state="play", mode="radio", station="P3"),
        ev("r2", "room", T0 + 3600_000, state="stop"),
    ])
    process()
    st = spins.stats(0)
    assert st["radio"][0]["station"] == "P3" and abs(st["radio"][0]["hours"] - 1) < 0.01


def test_resending_events_adds_nothing(hub):
    batch = [ev("m1", "mic", T0, title="A", artist="B")]
    assert spins.ingest(unit("Lounge"), batch) == 1
    assert spins.ingest(unit("Lounge"), batch) == 0


def test_renaming_a_room_moves_its_plays(hub):
    spins.ingest(unit("Lounge"), [ev("m1", "mic", T0, title="A", artist="B")])
    process()
    spins.save_room("Living room", 5, ["vinyl"], old_name="Lounge")
    assert spins.list_plays()["plays"][0]["room"] == "Living room"
    assert [r["name"] for r in spins.rooms()] == ["Living room"]
    assert spins.units()[0]["room"] == "Living room"


def test_merge_duplicates_keeps_one_play(hub):
    for i, artist in enumerate(["Magasinet i P3 med X, spelar nu Dua Lipa", "Dua Lipa"]):
        db.run("INSERT INTO plays (room, kind, started_at, title, artist, song_key, confidence, created_at) "
               "VALUES ('Lounge', 'streamed', ?, 'Houdini', ?, ?, ?, 0)",
               db.now() - 3600_000 + i * 60_000, artist, spins.song_key(artist, "Houdini"), ["metadata", "mic"][i])
    assert spins.merge_duplicates() == 1
    (p,) = spins.list_plays()["plays"]
    assert p["artist"] == "Dua Lipa"
