"""M4 Task G: runner integration for the `implement` executor.

Covers the state-machine changes (pick_claim single-flight, dev_box stamping,
needs-input, the implement-specific reap window) and run_once's implement
pass — every failure path must end in a Discord post AND a queue status, never
silence and never an uncaught exception.
"""
import datetime

import pytest

from worksweep.config import WorksweepConfig
from worksweep.devslots import DevBox
from worksweep.implementer import ImplementResult
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import (
    NeedsInputError, RunnerError, claim, needs_input, pick_claim, reap_stale,
    run_once,
)

NOW = "2026-08-17T12:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="me",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, implement_timeout=5400)
    base.update(kw)
    return WorksweepConfig(**base)


def _rec(number, status="approved", executor="implement", claimed_at="",
         iid=1775, title="Add cost page inline validation for entry"):
    kind = "issue" if executor == "implement" else "review_request"
    ident = (f"issue:pb-www#{iid}" if executor == "implement"
             else f"review:pb-www!{iid}")
    url = (f"https://gl/x/-/issues/{iid}" if executor == "implement"
           else f"https://gl/x/-/merge_requests/{iid}")
    return QueueRecord(number=number, first_seen=NOW, last_seen=NOW,
                       item=WorkItem(schema_version=1, id=ident, repo="pb-www",
                                     kind=kind, executor=executor, risk="low",
                                     why="w", web_url=url, sha="s",
                                     status=status, claimed_at=claimed_at,
                                     title=title))


def _box(name="dev1", tier="free", mr_iid=0):
    return DevBox(name=name, host="h", path="/p", url=f"https://{name}.x/",
                  branch="master", sha="s", tier=tier, mr_iid=mr_iid)


def _result(**kw):
    base = dict(iid=1775, mr_iid=42, mr_url="https://gl/x/-/merge_requests/42",
                dev_url="https://dev1.x/", dev_box="dev1",
                branch="feat/1775-add-cost-page-inline-validation",
                report_path="/r.md", verdict="SHIP with nits\nsecond line",
                result_sha="abc123", reassigned_from="", magi_note="")
    base.update(kw)
    return ImplementResult(**base)


def _deps(records, execute_implement=None, boxes=None, posts=None, saves=None,
          execute=None):
    posts = posts if posts is not None else []
    saves = saves if saves is not None else []
    state = {"records": list(records)}

    def load():
        return list(state["records"])

    def save(recs):
        state["records"] = list(recs)
        saves.append(list(recs))

    d = {"load": load, "save": save,
         "post": lambda hook, content: posts.append(content),
         "now": lambda: NOW,
         "execute": execute or (lambda item, cfg: ("s1", "/r.md")),
         "boxes": (lambda: list(boxes)) if boxes is not None else (lambda: [_box()]),
         "execute_implement": execute_implement
         or (lambda item, cfg, bx: _result())}
    return d, posts, saves, state


# --------------------------------------------------------------------------
# pick_claim / claim / needs_input / reap
# --------------------------------------------------------------------------

def test_pick_claim_spans_both_executors_lowest_first():
    recs = [_rec(3, executor="implement"), _rec(2, executor="magi-review")]
    assert pick_claim(recs).number == 2


def test_pick_claim_filters_by_requested_executor():
    recs = [_rec(1, executor="magi-review"), _rec(2, executor="implement")]
    assert pick_claim(recs, ("implement",)).number == 2
    assert pick_claim(recs, ("magi-review",)).number == 1


def test_second_implement_waits_while_one_is_running():
    recs = [_rec(1, status="running", executor="implement"),
            _rec(2, status="approved", executor="implement")]
    assert pick_claim(recs, ("implement",)) is None


def test_magi_review_is_unaffected_by_a_running_implement():
    recs = [_rec(1, status="running", executor="implement"),
            _rec(2, status="approved", executor="magi-review")]
    assert pick_claim(recs, ("magi-review",)).number == 2
    # and a running magi-review does not block a second magi-review claim
    recs = [_rec(1, status="running", executor="magi-review"),
            _rec(2, status="approved", executor="magi-review")]
    assert pick_claim(recs, ("magi-review",)).number == 2


def test_claim_stamps_dev_box():
    out = claim([_rec(1)], 1, NOW, dev_box="dev1")
    assert out[0].item.status == "running" and out[0].item.dev_box == "dev1"


def test_needs_input_sets_status_and_excerpt():
    out = needs_input([_rec(1, status="running")], 1, "Q: which table?" * 100, NOW)
    assert out[0].item.status == "needs-input"
    assert out[0].item.error_summary.startswith("Q: which table?")
    assert len(out[0].item.error_summary) <= 500


def test_implement_reap_uses_the_longer_window():
    def ago(minutes):
        return (datetime.datetime.fromisoformat(NOW)
                - datetime.timedelta(minutes=minutes)).isoformat()

    # #2 is keep-current rather than magi-review: since 2026-08-26 magi has a
    # long window of its own, so it no longer contrasts with implement's.
    recs = [_rec(1, status="running", executor="implement", claimed_at=ago(60)),
            _rec(2, status="running", executor="keep-current",
                 claimed_at=ago(60)),
            _rec(3, status="running", executor="implement", claimed_at=ago(106))]
    updated, reaped = reap_stale(recs, NOW, implement_timeout=5400)
    assert sorted(r.number for r in reaped) == [2, 3]
    by = {r.number: r for r in updated}
    assert by[1].item.status == "running"     # 60 min < 90 + 15
    assert by[3].item.status == "error"       # 106 min > 105


# --------------------------------------------------------------------------
# run_once implement pass
# --------------------------------------------------------------------------

def _locks(tmp_path):
    return dict(lock_path=str(tmp_path / "runner.lock"),
                implement_lock_path=str(tmp_path / "runner-implement.lock"))


def test_run_once_implement_happy_path(tmp_path):
    deps, posts, saves, state = _deps([_rec(1)])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    final = {r.number: r for r in state["records"]}
    assert final[1].item.status == "done"
    assert final[1].item.mr_iid == 42
    assert final[1].item.dev_box == "dev1"
    assert final[1].item.result_sha == "abc123"
    assert final[1].item.report_path == "/r.md"
    claim_post = next(p for p in posts if "implementing" in p)
    assert "🛠️ implementing #1775 on dev1 (branch feat/1775-" in claim_post
    done_post = next(p for p in posts if "implemented" in p)
    assert "🛠️ implemented #1775 → Draft !42 (https://dev1.x/)" in done_post
    assert "magi: SHIP with nits" in done_post
    assert "branch feat/1775-add-cost-page-inline-validation" in done_post


def test_run_once_implement_claims_box_before_long_work(tmp_path):
    """The dev_box stamp + running status must be persisted BEFORE execute
    runs, so a concurrent sweep can't hand the same box to something else."""
    seen = {}

    def execute_implement(item, cfg, boxes):
        seen["at_execute"] = [(r.number, r.item.status, r.item.dev_box)
                              for r in deps["load"]()]
        return _result()

    deps, posts, saves, state = _deps([_rec(1)],
                                      execute_implement=execute_implement)
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert seen["at_execute"] == [(1, "running", "dev1")]


def test_run_once_implement_no_slot_errors_and_posts(tmp_path):
    deps, posts, saves, state = _deps([_rec(1)], boxes=[_box(tier="live")])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    rec = state["records"][0]
    assert rec.item.status == "error"
    assert "no dev slot" in rec.item.error_summary
    assert any(p.startswith("⚠️") and "no dev slot" in p for p in posts)


def test_run_once_implement_box_probe_failure_errors_and_posts(tmp_path):
    def boom():
        raise OSError("ssh: Could not resolve hostname")

    deps, posts, saves, state = _deps([_rec(1)])
    deps["boxes"] = boom
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_implement_halt_sets_needs_input_and_posts_question(tmp_path):
    def halt(item, cfg, boxes):
        raise NeedsInputError("HALT_SPEC_AMBIGUITY: two totals columns")

    deps, posts, saves, state = _deps([_rec(1)], execute_implement=halt)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    rec = state["records"][0]
    assert rec.item.status == "needs-input"
    assert "HALT_SPEC_AMBIGUITY" in rec.item.error_summary
    q = next(p for p in posts if p.startswith("❓"))
    assert "#1775 needs your input" in q and "two totals columns" in q


def test_run_once_implement_runner_error_posts_warning(tmp_path):
    def boom(item, cfg, boxes):
        raise RunnerError("implementer produced no commits")

    deps, posts, saves, state = _deps([_rec(1)], execute_implement=boom)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") and "no commits" in p for p in posts)


def test_run_once_implement_unexpected_exception_posts_warning(tmp_path):
    def boom(item, cfg, boxes):
        raise OSError("[Errno 2] No such file or directory: 'glab'")

    deps, posts, saves, state = _deps([_rec(1)], execute_implement=boom)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") and "OSError" in p for p in posts)


def test_run_once_implement_reassignment_note_is_prepended(tmp_path):
    deps, posts, saves, state = _deps(
        [_rec(1)], boxes=[_box("dev4", "handed_off", mr_iid=4006)],
        execute_implement=lambda i, c, b: _result(dev_box="dev4",
                                                  reassigned_from="4006"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    claim_post = next(p for p in posts if "implementing" in p)
    assert claim_post.startswith(
        "dev4 reassigned from !4006 (approved, awaiting merge)")
    assert "🛠️ implementing #1775 on dev4" in claim_post


def test_run_once_implement_missing_report_says_no_report(tmp_path):
    deps, posts, saves, state = _deps(
        [_rec(1)], execute_implement=lambda i, c, b: _result(report_path="",
                                                             verdict=""))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert any("magi: no report" in p for p in posts)


def test_run_once_runs_one_of_each_executor_per_pass(tmp_path):
    ran = []
    deps, posts, saves, state = _deps(
        [_rec(1, executor="magi-review"), _rec(2, executor="implement")],
        execute=lambda item, cfg: (ran.append("magi"), ("s1", "/r.md"))[1],
        execute_implement=lambda i, c, b: (ran.append("impl"), _result())[1])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    assert ran == ["magi", "impl"]
    final = {r.number: r.item.status for r in state["records"]}
    assert final == {1: "done", 2: "done"}


def test_run_once_implement_lock_held_skips_quietly(tmp_path):
    from worksweep.runner import acquire_lock
    locks = _locks(tmp_path)
    assert acquire_lock(locks["implement_lock_path"])
    deps, posts, saves, state = _deps([_rec(1)])
    assert run_once(_cfg(tmp_path), deps, **locks) == 0
    assert state["records"][0].item.status == "approved"   # untouched
    assert posts == []


def test_run_once_implement_uses_a_separate_lock_from_magi(tmp_path):
    from worksweep.runner import acquire_lock
    locks = _locks(tmp_path)
    assert acquire_lock(locks["lock_path"])       # magi lock held elsewhere
    deps, posts, saves, state = _deps([_rec(1, executor="implement")])
    assert run_once(_cfg(tmp_path), deps, **locks) == 0
    assert state["records"][0].item.status == "done"


def test_run_once_implement_no_approved_items_is_quiet(tmp_path):
    deps, posts, saves, state = _deps([_rec(1, status="proposed")])
    deps["boxes"] = lambda: pytest.fail("boxes must not be probed with no work")
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    assert posts == []


def test_run_once_without_implement_deps_still_runs_magi(tmp_path):
    """Back-compat: a deps dict with no boxes/execute_implement (every pre-M4
    caller) must not crash — the implement pass is a no-op."""
    deps, posts, saves, state = _deps([_rec(1, executor="magi-review")])
    del deps["boxes"], deps["execute_implement"]
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    assert state["records"][0].item.status == "done"


def test_run_once_implement_without_deps_errors_loudly(tmp_path):
    """An approved implement item with no executor wiring must not sit silent."""
    deps, posts, saves, state = _deps([_rec(1, executor="implement")])
    del deps["boxes"], deps["execute_implement"]
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_pass_crash_is_caught_and_posted(tmp_path):
    """A pass that blows up in an unexpected place (here: the queue write that
    records the claim) must not take the runner down — ⚠️ and rc 1, and the
    other executor still gets its turn."""
    ran = []
    deps, posts, saves, state = _deps(
        [_rec(1, executor="magi-review"), _rec(2, executor="implement")],
        execute=lambda item, cfg: (ran.append("magi"), ("s1", "/r.md"))[1])

    real_save = deps["save"]

    def flaky_save(recs):
        if any(r.item.executor == "implement" and r.item.status == "running"
               for r in recs):
            raise OSError("[Errno 28] No space left on device")
        real_save(recs)

    deps["save"] = flaky_save
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert ran == ["magi"]
    assert any("implement pass crashed" in p for p in posts)


def test_run_once_implement_lock_is_released_after_a_crash(tmp_path):
    from worksweep.runner import acquire_lock
    locks = _locks(tmp_path)

    def boom(item, cfg, boxes):
        raise KeyboardInterrupt("operator ^C")

    deps, posts, saves, state = _deps([_rec(1)], execute_implement=boom)
    try:
        run_once(_cfg(tmp_path), deps, **locks)
    except KeyboardInterrupt:
        pass
    assert acquire_lock(locks["implement_lock_path"]) is True


def test_fail_post_is_bounded_for_discord(tmp_path):
    """I6: a 6 kB stderr tail must not produce a post Discord rejects."""
    from worksweep.formatter import DISCORD_MAX_CHARS

    def boom(item, cfg, boxes):
        raise RunnerError("x" * 9000)

    deps, posts, saves, state = _deps([_rec(1)], execute_implement=boom)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    warn = next(p for p in posts if p.startswith("⚠️"))
    assert len(warn.encode("utf-8")) <= DISCORD_MAX_CHARS
    assert state["records"][0].item.status == "error"


# --------------------------------------------------------------------------
# the drain (2026-09-01): one pass, every approved item
# --------------------------------------------------------------------------

def test_one_pass_drains_every_approved_implement_item(tmp_path):
    """FALSIFYING for the drain: two approved rows used to cost two launchd
    fires 10 minutes apart. One pass now works the queue until it is empty."""
    ran = []
    deps, posts, saves, state = _deps(
        [_rec(1), _rec(2, iid=1776)],
        execute_implement=lambda item, cfg, bx: (ran.append(item.id),
                                                 _result())[1])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    assert len(ran) == 2
    final = {r.number: r.item.status for r in state["records"]}
    assert final == {1: "done", 2: "done"}


def test_a_failed_claim_does_not_stop_the_drain(tmp_path):
    """The failure already posted; the next item deserves its run."""
    from worksweep.runner import RunnerError as _RE
    calls = {"n": 0}

    def flaky(item, cfg, bx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RE("first run fell over")
        return _result()
    deps, posts, saves, state = _deps([_rec(1), _rec(2, iid=1776)],
                                      execute_implement=flaky)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    final = {r.number: r.item.status for r in state["records"]}
    assert final == {1: "error", 2: "done"}
    assert any("#1" in p and "failed" in p for p in posts)


def test_a_needs_input_park_does_not_stop_the_drain(tmp_path):
    from worksweep.runner import NeedsInputError as _NIE
    calls = {"n": 0}

    def asks(item, cfg, bx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _NIE("which sheet wins?")
        return _result()
    deps, posts, saves, state = _deps([_rec(1), _rec(2, iid=1776)],
                                      execute_implement=asks)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    final = {r.number: r.item.status for r in state["records"]}
    assert final == {1: "needs-input", 2: "done"}
