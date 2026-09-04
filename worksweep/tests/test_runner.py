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
    # keep-current, not magi-review: magi runs on its own (much wider) window
    # as of 2026-08-26, so it is no longer the executor that demonstrates the
    # generic 45-minute one.
    recs = [_rec(1, status="running", executor="keep-current", claimed_at=old),
            _rec(2, status="running", executor="keep-current",
                 claimed_at=fresh)]
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


# --- magi 0.2.4 reap window (2026-08-26) -----------------------------------
#
# The generic 45-minute window was sized for a rebuttal-less tribunal. With
# the rebuttal round mandatory a healthy run can take an hour, and reaping it
# mid-flight both loses the work and re-proposes it to run again.

def _at(minutes):
    import datetime
    base = datetime.datetime.fromisoformat("2026-08-26T12:00:00+00:00")
    return (base + datetime.timedelta(minutes=minutes)).isoformat()


def _running(number, executor, claimed_at):
    return QueueRecord(
        number=number, first_seen=_at(0), last_seen=_at(0),
        item=WorkItem(schema_version=1, id=f"x{number}", repo="pb-www",
                      kind="mr", executor=executor, risk="low", why="w",
                      web_url="u", sha="s", status="running",
                      claimed_at=claimed_at))


def test_the_magi_reap_window_is_wider_than_the_magi_timeout():
    """FALSIFYING for the whole relationship: a reap at or below the run's own
    budget kills healthy tribunals, and the item is then re-proposed and runs
    again -- an hour of tokens per cycle, forever."""
    from worksweep.runner import (MAGI_REAP_GRACE_SECONDS,
                                  MAGI_TIMEOUT_SECONDS, STALE_RUNNING_MINUTES)
    assert MAGI_TIMEOUT_SECONDS > STALE_RUNNING_MINUTES * 60
    assert MAGI_REAP_GRACE_SECONDS > 0
    assert MAGI_TIMEOUT_SECONDS + MAGI_REAP_GRACE_SECONDS > MAGI_TIMEOUT_SECONDS


def test_a_healthy_hour_long_tribunal_is_not_reaped():
    """50 minutes in: over the old 45-minute window, well inside its own."""
    from worksweep.runner import reap_stale
    recs = [_running(1, "magi-review", _at(0))]
    updated, reaped = reap_stale(recs, _at(50))
    assert reaped == []
    assert updated[0].item.status == "running"


def test_a_genuinely_stuck_tribunal_is_still_reaped():
    from worksweep.runner import reap_stale
    recs = [_running(1, "magi-review", _at(0))]
    updated, reaped = reap_stale(recs, _at(95))
    assert [r.number for r in reaped] == [1]
    assert updated[0].item.status == "error"


def test_the_wider_windows_are_the_long_executors_alone():
    """magi-review, implement and (since 2026-09-04) address-feedback each
    have their own budget and window; keep-current and park stay on the
    generic 45 minutes -- a stuck short op must not sit on its branch for an
    extra half hour."""
    from worksweep.runner import reap_stale
    recs = [_running(1, "magi-review", _at(0)),
            _running(2, "address-feedback", _at(0)),
            _running(3, "keep-current", _at(0)),
            _running(4, "park", _at(0))]
    _, reaped = reap_stale(recs, _at(50))
    assert sorted(r.number for r in reaped) == [3, 4]


def test_a_feedback_claim_is_reaped_only_past_its_own_budget_plus_grace():
    """A substantive feedback fix runs the implement ceremony, so its window
    is feedback_timeout + grace -- inside it the claim is healthy, past it
    it is stuck."""
    from worksweep.runner import (FEEDBACK_REAP_GRACE_SECONDS, reap_stale)
    recs = [_running(2, "address-feedback", _at(0))]
    budget_min = 3600 // 60
    grace_min = FEEDBACK_REAP_GRACE_SECONDS // 60
    _, healthy = reap_stale(recs, _at(budget_min + grace_min - 1),
                            feedback_timeout=3600)
    assert healthy == []
    _, stuck = reap_stale(recs, _at(budget_min + grace_min + 1),
                          feedback_timeout=3600)
    assert [r.number for r in stuck] == [2]
    # and the configured budget moves the window with it
    _, still_healthy = reap_stale(recs, _at(budget_min + grace_min + 1),
                                  feedback_timeout=7200)
    assert still_healthy == []


def test_a_configured_magi_timeout_moves_the_reap_window_with_it():
    from worksweep.runner import reap_stale
    recs = [_running(1, "magi-review", _at(0))]
    _, reaped = reap_stale(recs, _at(95), magi_timeout=6000)
    assert reaped == []
