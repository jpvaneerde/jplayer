"""Plays go to JPlay Records too: new and changed ones once, deletions follow, nothing lost while it's away."""
import json

import pytest

from jplay.hub import db, records, records_sync, spins


@pytest.fixture
def hub(tmp_path, monkeypatch):
    db.init(str(tmp_path / "hub.db"))
    monkeypatch.setattr(records, "config", lambda: ("https://records.example", "tok"))
    monkeypatch.setattr(records, "match_many", lambda songs: [{"match": None, "note": "Not yours", "record": None} for _ in songs])
    sent = []
    state = {"down": False}

    def post(path, body, timeout=30):
        if state["down"]:
            raise records.RecordsError("away")
        sent.append((path, json.loads(json.dumps(body))))
        return {"saved": len(body["plays"]), "deleted": len(body["deleted"])}
    monkeypatch.setattr(records, "_post", post)
    yield sent, state
    db.close()


def add_play():
    spins.ingest({"id": "u1", "room": "Lounge"},
                 [{"id": "m1", "type": "mic", "ts": db.now() - 60_000, "data": {"title": "Houdini", "artist": "Dua Lipa"}}])
    spins.process_events(at=db.now() + spins.SETTLE_MS + 1000)
    return spins.list_plays()["plays"][0]["id"]


def test_new_changed_and_deleted_plays_reach_records(hub):
    sent, state = hub
    state["down"] = True
    pid = add_play()
    assert records_sync.sync_once() == 0 and records_sync.status["last_error"] == "away"
    state["down"] = False
    assert records_sync.sync_once() == 1
    path, body = sent[-1]
    assert path == "/api/spins/hub-plays"
    (p,) = body["plays"]
    assert p["hub_id"] == records_sync.hub_id(pid) and p["title"] == "Houdini" and p["records_id"] is None
    assert records_sync.sync_once() == 0                       # nothing new
    spins.mark_physical(pid, "vinyl")
    records_sync.changed(pid)
    assert records_sync.sync_once() == 1 and sent[-1][1]["plays"][0]["detail"] == "vinyl"
    spins.delete_play(pid)
    assert records_sync.sync_once() == 1
    assert sent[-1][1]["deleted"] == [{"hub_id": records_sync.hub_id(pid), "records_id": None}]


def test_imported_plays_go_back_only_when_corrected(hub):
    sent, _ = hub
    cur = db.run("""INSERT INTO plays (room, kind, started_at, title, artist, song_key, confidence, origin, import_id, created_at)
                    VALUES ('Lounge', 'heard', 1000, 'Old', 'Song', 'song|old', 'mic', 'records', 'records:77', 0)""")
    records_sync.mark_imported(cur.lastrowid)
    assert records_sync.sync_once() == 0
    spins.mark_heard(cur.lastrowid)
    records_sync.changed(cur.lastrowid)
    assert records_sync.sync_once() == 1
    assert sent[-1][1]["plays"][0]["records_id"] == 77


def test_switched_off_sends_nothing(hub):
    sent, _ = hub
    db.set_setting("records_sync", False)
    add_play()
    assert records_sync.sync_once() == 0 and sent == []
