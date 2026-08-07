"""Runner claim/reap/complete state machine + lockfile."""
import datetime
import os
from unittest.mock import patch

from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import (
    _lock_holder_pid, acquire_lock, claim, complete, fail, pick_claim,
    reap_stale, release_lock)

NOW = "2026-08-07T12:00:00+00:00"


def _rec(number, status="approved", executor="magi-review", claimed_at=""):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"review:pb-www!{number}",
                      repo="pb-www", kind="review_request", executor=executor,
                      risk="low", why="", web_url=f"https://gl/x/-/merge_requests/{number}",
                      sha=f"s{number}", status=status, claimed_at=claimed_at))


def test_pick_lowest_approved_magi_item():
    recs = [_rec(3), _rec(1, status="proposed"), _rec(2)]
    assert pick_claim(recs).number == 2


def test_pick_ignores_other_executors():
    assert pick_claim([_rec(1, executor="triage")]) is None


def test_claim_sets_running_and_timestamp():
    out = claim([_rec(1)], 1, NOW)
    assert out[0].item.status == "running" and out[0].item.claimed_at == NOW


def test_reap_stale_running():
    old = (datetime.datetime.fromisoformat(NOW)
           - datetime.timedelta(minutes=46)).isoformat()
    fresh = (datetime.datetime.fromisoformat(NOW)
             - datetime.timedelta(minutes=10)).isoformat()
    recs = [_rec(1, status="running", claimed_at=old),
            _rec(2, status="running", claimed_at=fresh)]
    updated, reaped = reap_stale(recs, NOW)
    assert [r.number for r in reaped] == [1]
    assert updated[0].item.status == "error"
    assert updated[1].item.status == "running"


def test_complete_and_fail():
    done = complete([_rec(1, status="running")], 1, "s1", "/r.md", NOW)
    assert done[0].item.status == "done"
    assert done[0].item.report_path == "/r.md"
    err = fail([_rec(2, status="running")], 2, "x" * 600, NOW)
    assert err[0].item.status == "error" and len(err[0].item.error_summary) == 500


def test_lockfile_excludes_second_holder(tmp_path):
    p = str(tmp_path / "runner.lock")
    assert acquire_lock(p) is True
    assert acquire_lock(p) is False      # held by a live pid (ours)
    release_lock(p)
    assert not os.path.exists(p)


def test_stale_lock_from_dead_pid_is_broken(tmp_path):
    p = str(tmp_path / "runner.lock")
    with open(p, "w") as f:
        f.write("999999999")             # certainly not a live pid
    assert acquire_lock(p) is True
    release_lock(p)


def test_lock_holder_pid_reads_valid_pid(tmp_path):
    p = str(tmp_path / "lock")
    with open(p, "w") as f:
        f.write("12345")
    assert _lock_holder_pid(p) == 12345


def test_lock_holder_pid_missing_file_returns_none(tmp_path):
    p = str(tmp_path / "missing.lock")
    assert _lock_holder_pid(p) is None


def test_lock_holder_pid_unparseable_content_returns_none(tmp_path):
    p = str(tmp_path / "lock")
    with open(p, "w") as f:
        f.write("not-a-number")
    assert _lock_holder_pid(p) is None


def test_acquire_lock_tolerates_vanishing_lock_file(tmp_path):
    """Lock file deleted between FileExistsError and pid-read: acquire_lock retries and succeeds.

    Simulates: attempt 1's O_EXCL fails (file exists), helper deletes it and returns None,
    loop continues without os.remove, attempt 2's O_EXCL succeeds.
    """
    p = str(tmp_path / "runner.lock")
    # Create the lock file first so attempt 1's O_EXCL fails
    with open(p, "w") as f:
        f.write("12345")

    call_count = [0]

    def mock_lock_holder_pid(path):
        call_count[0] += 1
        assert path == p
        # Simulate the race: holder released between FileExistsError and read
        os.remove(p)
        return None

    with patch("worksweep.runner._lock_holder_pid", side_effect=mock_lock_holder_pid):
        # Attempt 1: O_EXCL fails (file exists), calls helper which deletes it and returns None.
        # Loop continues (no os.remove call). Attempt 2: O_EXCL succeeds.
        result = acquire_lock(p)

    assert result is True
    assert call_count[0] == 1
    # Lock file now exists with our pid
    assert os.path.exists(p)
    with open(p) as f:
        assert int(f.read().strip()) == os.getpid()
    release_lock(p)
