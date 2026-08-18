"""M4 Task H: runner integration for the `keep-current` executor.

keep-current shares the magi-review pass/lock (a short git op, no third lock
file) -- these tests exercise pick_claim's inclusion of the new executor and
run_once's magi pass dispatching to it, mirroring test_runner_implement.py's
shape for the `implement` executor.
"""
import pytest

from worksweep.config import WorksweepConfig
from worksweep.keepcurrent import KeepCurrentResult
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import RunnerError, pick_claim, run_once

NOW = "2026-08-18T12:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="me",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, stale_threshold=5)
    base.update(kw)
    return WorksweepConfig(**base)


def _rec(number, status="approved", executor="keep-current", iid=4020,
        branch="feat/1701-thing"):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"stale:pb-www!{iid}", repo="pb-www",
                     kind="stale", executor=executor, risk="low",
                     why="7 commits behind master",
                     web_url=f"https://gl/x/-/merge_requests/{iid}", sha="s",
                     status=status, branch=branch))


def _result(**kw):
    base = dict(iid=4020, ahead_count=7, box_name="dev4",
               scss_recompiled=False, result_sha="abc123",
               dev_url="https://dev4.x/")
    base.update(kw)
    return KeepCurrentResult(**base)


def _deps(records, execute_keep_current=None, posts=None, saves=None,
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
        "execute_keep_current": execute_keep_current
        or (lambda item, cfg: _result())}
    return d, posts, saves, state


def _locks(tmp_path):
    return dict(lock_path=str(tmp_path / "runner.lock"),
               implement_lock_path=str(tmp_path / "runner-implement.lock"))


# --------------------------------------------------------------------------
# pick_claim
# --------------------------------------------------------------------------

def test_pick_claim_includes_keep_current_by_default():
    assert pick_claim([_rec(1)]).number == 1


def test_pick_claim_spans_magi_and_keep_current_lowest_first():
    recs = [_rec(3, executor="keep-current"),
           _rec(2, executor="magi-review", iid=1)]
    from worksweep.runner import _MAGI, _KEEP_CURRENT
    assert pick_claim(recs, (_MAGI, _KEEP_CURRENT)).number == 2


def test_keep_current_is_not_single_flight_like_implement():
    """Unlike `implement`, a running keep-current claim must not block a
    second keep-current from being picked (the magi pass only ever runs one
    claim per invocation anyway, so this only matters for pick_claim's own
    contract)."""
    recs = [_rec(1, status="running", executor="keep-current"),
           _rec(2, status="approved", executor="keep-current")]
    from worksweep.runner import _KEEP_CURRENT
    assert pick_claim(recs, (_KEEP_CURRENT,)).number == 2


# --------------------------------------------------------------------------
# run_once — the shared magi/keep-current pass
# --------------------------------------------------------------------------

def test_run_once_keep_current_happy_path(tmp_path):
    deps, posts, saves, state = _deps([_rec(1)])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    final = {r.number: r for r in state["records"]}
    assert final[1].item.status == "done"
    assert final[1].item.result_sha == "abc123"
    done_post = next(p for p in posts if p.startswith("🔄"))
    assert "🔄 !4020 merged master (+7 commits, scss unchanged) · dev4 verified 200" \
        == done_post


def test_run_once_keep_current_scss_recompiled_in_post(tmp_path):
    deps, posts, saves, state = _deps(
        [_rec(1)], execute_keep_current=lambda i, c: _result(scss_recompiled=True))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    done_post = next(p for p in posts if p.startswith("🔄"))
    assert "scss recompiled" in done_post


def test_run_once_keep_current_no_box_serving_branch_still_posts_done(tmp_path):
    deps, posts, saves, state = _deps(
        [_rec(1)], execute_keep_current=lambda i, c: _result(box_name="", dev_url=""))
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    final = state["records"][0]
    assert final.item.status == "done"
    done_post = next(p for p in posts if p.startswith("🔄"))
    assert "no dev box serving branch" in done_post


def test_run_once_keep_current_conflict_errors_and_posts_warning(tmp_path):
    def boom(item, cfg):
        raise RunnerError("merge conflicts in: www/home/php/Foo.php")

    deps, posts, saves, state = _deps([_rec(1)], execute_keep_current=boom)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert "merge conflicts in" in state["records"][0].item.error_summary
    warn = next(p for p in posts if p.startswith("⚠️"))
    assert "keep-current failed" in warn and "merge conflicts in" in warn


def test_run_once_keep_current_sync_failure_errors_and_posts(tmp_path):
    def boom(item, cfg):
        raise RunnerError("dev4 HEAD abc != pushed abc123 — sync did NOT land")

    deps, posts, saves, state = _deps([_rec(1)], execute_keep_current=boom)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") and "did NOT land" in p for p in posts)


def test_run_once_keep_current_unexpected_exception_errors_and_posts(tmp_path):
    def boom(item, cfg):
        raise OSError("[Errno 2] No such file or directory: 'git'")

    deps, posts, saves, state = _deps([_rec(1)], execute_keep_current=boom)
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") and "OSError" in p for p in posts)


def test_run_once_keep_current_without_dep_errors_loudly(tmp_path):
    deps, posts, saves, state = _deps([_rec(1)])
    del deps["execute_keep_current"]
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 1
    assert state["records"][0].item.status == "error"
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_keep_current_and_magi_review_both_run_one_pass(tmp_path):
    """keep-current shares magi-review's lock/pass, but pick_claim only
    returns ONE lowest-numbered claim per invocation across both -- so a
    magi-review item and a keep-current item present together only run the
    lower-numbered one per pass, exactly like two magi-review items would."""
    from worksweep.models import WorkItem as WI
    magi_rec = QueueRecord(
        number=1, first_seen=NOW, last_seen=NOW,
        item=WI(schema_version=1, id="review:pb-www!1", repo="pb-www",
               kind="review_request", executor="magi-review", risk="low",
               why="", web_url="https://gl/x/-/merge_requests/1", sha="s",
               status="approved"))
    keep_rec = _rec(2)
    deps, posts, saves, state = _deps([magi_rec, keep_rec])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    final = {r.number: r.item.status for r in state["records"]}
    assert final == {1: "done", 2: "approved"}    # keep-current waits its turn


def test_run_once_keep_current_does_not_touch_implement_lock(tmp_path):
    from worksweep.runner import acquire_lock
    locks = _locks(tmp_path)
    assert acquire_lock(locks["implement_lock_path"])   # implement lock held elsewhere
    deps, posts, saves, state = _deps([_rec(1)])
    assert run_once(_cfg(tmp_path), deps, **locks) == 0
    assert state["records"][0].item.status == "done"


def test_run_once_without_keep_current_deps_still_runs_magi(tmp_path):
    """Back-compat: every pre-Task-H caller has no execute_keep_current dep
    and no keep-current items -- must not crash."""
    deps, posts, saves, state = _deps([])
    del deps["execute_keep_current"]
    from worksweep.models import WorkItem as WI
    magi_rec = QueueRecord(
        number=1, first_seen=NOW, last_seen=NOW,
        item=WI(schema_version=1, id="review:pb-www!1", repo="pb-www",
               kind="review_request", executor="magi-review", risk="low",
               why="", web_url="https://gl/x/-/merge_requests/1", sha="s",
               status="approved"))
    deps["load"] = lambda: [magi_rec]
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
