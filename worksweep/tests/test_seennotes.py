"""The dismissed-notes sidecar: `~/.worksweep/seen-notes.json`.

Small, durable, and deliberately separate from the queue. A dismissal has to
survive the row it dismissed -- the queue row goes `done` and eventually
compacts away, while "Chandler read cmnoble's LGTM" stays true forever (or
until cmnoble says something new).
"""
import json
import os

from worksweep.seennotes import (SEEN_TTL_DAYS, load_seen, prune_seen,
                                 record_seen, save_seen)

NOW = "2026-08-28T12:00:00+00:00"


def _path(tmp_path):
    return str(tmp_path / "seen-notes.json")


def test_a_missing_file_is_an_empty_set_not_an_error():
    assert load_seen("/nonexistent/seen-notes.json") == frozenset()


def test_a_recorded_pair_round_trips(tmp_path):
    p = _path(tmp_path)
    record_seen(p, [("d1", "101")], NOW)
    assert load_seen(p) == frozenset({("d1", "101")})


def test_pairs_accumulate_across_dismissals(tmp_path):
    p = _path(tmp_path)
    record_seen(p, [("d1", "101")], NOW)
    record_seen(p, [("d2", "202"), ("d3", "303")], NOW)
    assert load_seen(p) == frozenset({("d1", "101"), ("d2", "202"),
                                      ("d3", "303")})


def test_recording_the_same_pair_twice_is_idempotent(tmp_path):
    p = _path(tmp_path)
    record_seen(p, [("d1", "101")], NOW)
    record_seen(p, [("d1", "101")], "2026-09-01T00:00:00+00:00")
    assert load_seen(p) == frozenset({("d1", "101")})
    with open(p) as f:
        assert len(json.load(f)) == 1


def test_a_pair_with_an_empty_half_is_never_recorded(tmp_path):
    """An old row carries no note refs, and a thread with no id is not
    evidence of anything. Recording "" would dismiss every such thread at
    once -- silently, and forever."""
    p = _path(tmp_path)
    record_seen(p, [("d1", ""), ("", "101"), ("", "")], NOW)
    assert load_seen(p) == frozenset()
    # asserted on the FILE, not just the loaded set: the reader also drops
    # half-empty entries, so a missing write guard would look fine from
    # load_seen while quietly filling the file with junk that never expires.
    with open(p) as f:
        assert json.load(f) == []


def test_entries_older_than_the_ttl_are_pruned(tmp_path):
    """Same reasoning as queue compaction: a note nobody has seen in three
    months is not going to reappear, and the file should not grow forever."""
    p = _path(tmp_path)
    old = "2026-01-01T00:00:00+00:00"
    record_seen(p, [("d-old", "1")], old)
    record_seen(p, [("d-new", "2")], NOW)
    assert load_seen(p, now=NOW) == frozenset({("d-new", "2")})


def test_pruning_is_written_back_not_just_filtered_on_read(tmp_path):
    p = _path(tmp_path)
    record_seen(p, [("d-old", "1")], "2026-01-01T00:00:00+00:00")
    record_seen(p, [("d-new", "2")], NOW)      # a write prunes
    with open(p) as f:
        assert [e["discussion"] for e in json.load(f)] == ["d-new"]


def test_an_entry_just_inside_the_ttl_survives(tmp_path):
    import datetime
    p = _path(tmp_path)
    edge = (datetime.datetime.fromisoformat(NOW)
            - datetime.timedelta(days=SEEN_TTL_DAYS - 1)).isoformat()
    record_seen(p, [("d1", "101")], edge)
    assert load_seen(p, now=NOW) == frozenset({("d1", "101")})


def test_an_unparseable_timestamp_is_kept_not_destroyed(tmp_path):
    """Never drop a dismissal on bad data -- mirrors the queue's own rule."""
    p = _path(tmp_path)
    with open(p, "w") as f:
        json.dump([{"discussion": "d1", "note": "101", "seen": "not-a-date"}], f)
    assert load_seen(p, now=NOW) == frozenset({("d1", "101")})


def test_a_malformed_file_reads_as_empty_rather_than_crashing(tmp_path):
    p = _path(tmp_path)
    for junk in ("not json", '{"not": "a list"}', "[1, 2, 3]",
                 '[{"discussion": "d1"}]'):
        with open(p, "w") as f:
            f.write(junk)
        assert load_seen(p) == frozenset(), junk


def test_the_write_is_atomic_and_private(tmp_path):
    """Same discipline as save_queue: a unique temp file in the same
    directory, then os.replace. This file records what a human decided."""
    p = _path(tmp_path)
    record_seen(p, [("d1", "101")], NOW)
    assert oct(os.stat(p).st_mode)[-3:] == "600"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_the_parent_directory_is_created(tmp_path):
    p = str(tmp_path / "nested" / "seen-notes.json")
    record_seen(p, [("d1", "101")], NOW)
    assert load_seen(p) == frozenset({("d1", "101")})


def test_prune_seen_is_pure_and_reports_what_it_kept():
    entries = [{"discussion": "d1", "note": "1", "seen": NOW},
               {"discussion": "d2", "note": "2",
                "seen": "2026-01-01T00:00:00+00:00"}]
    kept = prune_seen(entries, NOW)
    assert [e["discussion"] for e in kept] == ["d1"]


def test_save_seen_replaces_wholesale(tmp_path):
    p = _path(tmp_path)
    save_seen(p, [{"discussion": "d1", "note": "1", "seen": NOW}])
    save_seen(p, [{"discussion": "d2", "note": "2", "seen": NOW}])
    assert load_seen(p) == frozenset({("d2", "2")})
