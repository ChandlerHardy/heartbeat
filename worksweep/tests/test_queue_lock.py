"""f-007 / f-028 / f-029: the cross-process queue write lock.

queue.json is a whole-file replace, and four independent processes write it --
the sweep (launchd), intake (launchd), each runner pass (launchd), and the
dashboard (a live HTTP server). Atomic replace stops corruption but does
nothing about lost updates: two processes that both read, both mutate, and
both write leave only the second one's work.

These tests use real files and real fcntl, in tmp_path -- the lock IS the
subject, so faking it would prove nothing.
"""
import multiprocessing
import os
import time

import pytest

from worksweep.queue import (QueueLockError, QUEUE_LOCK_TIMEOUT, null_lock,
                             write_lock)


def _qpath(tmp_path):
    return str(tmp_path / "queue.json")


# Module level, not closures: macOS spawns rather than forks, so a worker has
# to be importable in the child.
def _hold_for(qpath, started, order, seconds):
    with write_lock(qpath):
        started.set()
        time.sleep(seconds)
        order.put("first-done")


def _hold_until(qpath, started, release):
    with write_lock(qpath):
        started.set()
        release.wait(10)


# --- the lock itself --------------------------------------------------------

def test_the_lock_is_a_sidecar_and_never_the_queue_file(tmp_path):
    """Locking the queue file itself would fight save_queue's atomic replace:
    os.replace swaps the inode, so the lock would follow the old file."""
    qpath = _qpath(tmp_path)
    with write_lock(qpath) as lock_path:
        assert lock_path != qpath
        assert lock_path.startswith(qpath)
        assert os.path.exists(lock_path)
    assert not os.path.exists(qpath)          # the queue itself is untouched


def test_the_lock_is_reentrant_across_sequential_holders(tmp_path):
    qpath = _qpath(tmp_path)
    for _ in range(3):
        with write_lock(qpath):
            pass


def test_a_second_holder_waits_for_the_first(tmp_path):
    """The whole point: process B blocks until A's read-modify-write ends."""
    qpath = _qpath(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    order = ctx.Queue()
    started = ctx.Event()
    p = ctx.Process(target=_hold_for, args=(qpath, started, order, 0.4))
    p.start()
    assert started.wait(5)
    with write_lock(qpath, timeout=5):
        order.put("second-acquired")
    p.join(5)
    assert [order.get(timeout=5), order.get(timeout=5)] == \
        ["first-done", "second-acquired"]


def test_a_wedged_holder_fails_loudly_rather_than_hanging(tmp_path):
    """Silence is never an outcome. A lock nobody releases must surface as an
    error the caller reports, not a launchd job that hangs forever."""
    qpath = _qpath(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    release = ctx.Event()
    p = ctx.Process(target=_hold_until, args=(qpath, started, release))
    p.start()
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        with pytest.raises(QueueLockError) as e:
            with write_lock(qpath, timeout=0.3):
                pass
        assert 0.3 <= time.monotonic() - t0 < 4
        assert "queue" in str(e.value).lower()
    finally:
        release.set()
        p.join(5)


def test_the_default_timeout_is_short_enough_to_notice(tmp_path):
    """A dashboard tap waits on this. Minutes would read as a hung page."""
    assert 0 < QUEUE_LOCK_TIMEOUT <= 30


def test_the_lock_is_released_even_when_the_body_raises(tmp_path):
    qpath = _qpath(tmp_path)
    with pytest.raises(ValueError):
        with write_lock(qpath):
            raise ValueError("mutate blew up")
    with write_lock(qpath, timeout=0.5):       # would block if still held
        pass


def test_null_lock_is_a_no_op_for_dry_runs_and_tests():
    with null_lock():
        pass
    with null_lock("anything", timeout=1):
        pass


# --- every writer takes it (f-007) -----------------------------------------
#
# The lock only helps if all four writers use it. These assert the property
# that matters -- no save happens outside the lock -- rather than that some
# particular line calls some particular function.

import contextlib  # noqa: E402


class _LockSpy:
    """Records lock/save interleaving so a save outside the lock is visible."""

    def __init__(self):
        self.events, self.depth, self.max_depth = [], 0, 0

    @contextlib.contextmanager
    def lock(self, *a, **kw):
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.events.append("lock")
        try:
            yield None
        finally:
            self.depth -= 1
            self.events.append("unlock")

    def save(self, records):
        self.events.append("save" if self.depth else "UNLOCKED-SAVE")

    @property
    def unlocked_saves(self):
        return self.events.count("UNLOCKED-SAVE")


def _runner_deps(spy, records, execute=None):
    state = {"records": list(records)}
    return {"load": lambda: list(state["records"]),
            "save": spy.save,
            "post": lambda hook, content: None,
            "now": lambda: "2026-08-26T12:00:00+00:00",
            "queue_lock": spy.lock,
            "execute": execute or (lambda item, cfg: ("s1", "/r.md"))}


def _cfg(tmp_path):
    from worksweep.config import WorksweepConfig
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="", checkouts_root=str(tmp_path))


def _magi_rec(number=1, status="approved"):
    from worksweep.models import QueueRecord, WorkItem
    return QueueRecord(
        number=number, first_seen="2026-08-26T00:00:00+00:00",
        last_seen="2026-08-26T00:00:00+00:00",
        item=WorkItem(schema_version=1, id=f"review:pb-www!{number}",
                      repo="pb-www", kind="review_request",
                      executor="magi-review", risk="low", why="w",
                      web_url=f"https://gl/x/-/merge_requests/{number}",
                      sha="s1", status=status))


def test_the_runner_claims_and_completes_under_the_lock(tmp_path):
    from worksweep.runner import run_once
    spy = _LockSpy()
    deps = _runner_deps(spy, [_magi_rec()])
    run_once(_cfg(tmp_path), deps,
             lock_path=str(tmp_path / "r.lock"),
             implement_lock_path=str(tmp_path / "ri.lock"),
             address_feedback_lock_path=str(tmp_path / "rf.lock"))
    assert spy.events.count("save") >= 2       # the claim, then the completion
    assert spy.unlocked_saves == 0


def test_the_runner_never_holds_the_lock_across_the_executor(tmp_path):
    """Scope, not just existence: holding it across a 30-minute claude run
    would stall the sweep, intake and every dashboard tap."""
    from worksweep.runner import run_once
    spy = _LockSpy()
    held = []

    def execute(item, cfg):
        held.append(spy.depth)
        return ("s1", "/r.md")

    deps = _runner_deps(spy, [_magi_rec()], execute=execute)
    run_once(_cfg(tmp_path), deps,
             lock_path=str(tmp_path / "r.lock"),
             implement_lock_path=str(tmp_path / "ri.lock"),
             address_feedback_lock_path=str(tmp_path / "rf.lock"))
    assert held == [0]                          # executor ran lock-free


def test_the_sweep_saves_under_the_lock(tmp_path):
    import json as _json
    from worksweep.__main__ import run_sweep
    from worksweep.config import WorksweepConfig
    spy = _LockSpy()
    gql = _json.dumps({"data": {"currentUser": {
        "username": "me", "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": []},
        "assignedMergeRequests": {"nodes": []}}}})
    deps = {"graphql": lambda: gql, "todos": lambda: [],
            "issues": lambda repo, user: [],
            "post": lambda hook, content: None,
            "load": lambda: [], "save": spy.save,
            "now": lambda: "2026-08-26T12:00:00+00:00",
            "queue_lock": spy.lock}
    cfg = WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="")
    assert run_sweep(cfg, deps) == 0
    assert spy.events.count("save") == 1
    assert spy.unlocked_saves == 0


def test_the_lock_is_never_nested(tmp_path):
    """Real flock is per-open-file-description, so a nested acquire in one
    process blocks against ITSELF until the timeout. Every writer must take it
    exactly once per cycle."""
    from worksweep.runner import run_once
    spy = _LockSpy()
    deps = _runner_deps(spy, [_magi_rec()])
    run_once(_cfg(tmp_path), deps,
             lock_path=str(tmp_path / "r.lock"),
             implement_lock_path=str(tmp_path / "ri.lock"),
             address_feedback_lock_path=str(tmp_path / "rf.lock"))
    assert spy.max_depth == 1


def test_the_dashboard_approve_path_takes_the_cross_process_lock(tmp_path):
    """The dashboard keeps its threading lock for concurrent taps in ITS
    process, and adds this one for the sweep/intake/runner."""
    import inspect
    from worksweep import dashboard
    src = inspect.getsource(dashboard.DashboardHandler._approve)
    assert "write_lock(" in src
    assert "_WRITE_LOCK" in src                # both, not one instead of the other
    src_dismiss = inspect.getsource(dashboard.DashboardHandler._dismiss)
    assert "write_lock(" in src_dismiss
