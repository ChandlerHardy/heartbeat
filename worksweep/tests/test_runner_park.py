"""Runner integration for the `park` executor.

park shares the magi/keep-current pass and lock (a branch sync plus one API
write -- not worth its own lock file). Mirrors test_runner_keepcurrent.py.
"""
import pytest

from worksweep.config import WorksweepConfig
from worksweep.models import QueueRecord, WorkItem
from worksweep.park import ParkResult
from worksweep.runner import (_KEEP_CURRENT, _MAGI, _PARK, RunnerError,
                              pick_claim, run_once)

NOW = "2026-08-25T12:00:00+00:00"
DEV_URL = "https://dev2.chandlerhardy-dev.performancebeef.com/"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="me",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, stale_threshold=5)
    base.update(kw)
    return WorksweepConfig(**base)


def _rec(number, status="approved", executor="park", iid=4078,
         branch="chardy/1588-ranch-data"):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"hygiene-devurl:pb-www!{iid}",
                      repo="pb-www", kind="mr", executor=executor, risk="low",
                      why="description missing dev-server link",
                      web_url=f"https://gl/x/-/merge_requests/{iid}", sha="s",
                      status=status, branch=branch))


def _result(**kw):
    base = dict(iid=4078, box_name="dev2", dev_url=DEV_URL,
                result_sha="newsha123", description_updated=True)
    base.update(kw)
    return ParkResult(**base)


def _deps(records, execute_park=None, posts=None, saves=None):
    posts = posts if posts is not None else []
    saves = saves if saves is not None else []
    state = {"records": list(records)}
    d = {"load": lambda: list(state["records"]),
         "save": lambda recs: (state.update(records=list(recs)),
                               saves.append(list(recs)))[0],
         "post": lambda hook, content: posts.append(content),
         "now": lambda: NOW,
         "execute": lambda item, cfg: ("s1", "/r.md"),
         "execute_park": execute_park or (lambda item, cfg: _result())}
    return d, posts, saves, state


def _locks(tmp_path):
    return dict(lock_path=str(tmp_path / "runner.lock"),
                implement_lock_path=str(tmp_path / "runner-implement.lock"))


# --- claim selection --------------------------------------------------------

def test_pick_claim_includes_park_by_default():
    """Falsifying: leave park out of _ALL_EXECUTORS and the runner never
    claims it -- an approved park row would sit `approved` forever with no
    un-approve path, exactly the zombie F1 exists to prevent."""
    assert pick_claim([_rec(1)]).number == 1


def test_pick_claim_spans_all_three_shared_executors_lowest_first():
    recs = [_rec(3, executor=_PARK),
            _rec(2, executor=_KEEP_CURRENT, iid=1),
            _rec(4, executor=_MAGI, iid=2)]
    assert pick_claim(recs, (_MAGI, _KEEP_CURRENT, _PARK)).number == 2


def test_a_proposed_park_item_is_never_claimed():
    """park is not auto-approved: it must wait for a human ✅ or checkbox."""
    assert pick_claim([_rec(1, status="proposed")]) is None


# --- the happy path ---------------------------------------------------------

def test_park_claim_runs_and_completes(tmp_path):
    seen = {}

    def execute_park(item, cfg):
        seen.update(item=item)
        return _result()

    deps, posts, saves, state = _deps([_rec(7)], execute_park=execute_park)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 0
    assert seen["item"].branch == "chardy/1588-ranch-data"

    # claimed running, then completed done -- both persisted
    assert state["records"][0].item.status == "done"
    assert [r[0].item.status for r in saves] == ["running", "done"]

    done = [p for p in posts if p.startswith("🅿️")]
    assert len(done) == 1
    assert "!4078 parked on dev2 (200)" in done[0]
    assert "description updated" in done[0]


def test_park_done_post_says_when_the_description_was_left_alone(tmp_path):
    deps, posts, _, _ = _deps(
        [_rec(7)], execute_park=lambda i, c: _result(description_updated=False))
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    assert "already had a dev link" in [p for p in posts if p.startswith("🅿️")][0]


# --- failure surfaces -------------------------------------------------------

def test_no_free_slot_errors_and_posts(tmp_path):
    """Falsifying: a swallowed failure leaves the item `running` until the
    45-minute reap, with the human told nothing."""
    def boom(item, cfg):
        raise RunnerError("no free dev slot to park !4078 on — free one or "
                          "reclaim a box, then re-approve")
    deps, posts, _, state = _deps([_rec(7)], execute_park=boom)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 1
    assert state["records"][0].item.status == "error"
    assert "no free dev slot" in state["records"][0].item.error_summary
    assert any("no free dev slot" in p for p in posts)


def test_an_unexpected_exception_still_errors_and_posts(tmp_path):
    """A glab PUT failure raises RuntimeError, not RunnerError -- it must not
    escape as an un-flipped `running` claim."""
    def boom(item, cfg):
        raise RuntimeError("glab api ... exited 1: 403 Forbidden")
    deps, posts, _, state = _deps([_rec(7)], execute_park=boom)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 1
    assert state["records"][0].item.status == "error"
    assert "403" in state["records"][0].item.error_summary
    assert any("403" in p for p in posts)


def test_an_errored_park_is_re_proposed_by_the_next_sweep(tmp_path):
    """reconcile turns error -> proposed, so a freed box gets a retry."""
    from worksweep.queue import reconcile
    deps, _, _, state = _deps(
        [_rec(7)], execute_park=lambda i, c: (_ for _ in ()).throw(
            RunnerError("no free dev slot")))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    errored = state["records"]
    assert errored[0].item.status == "error"
    fresh = [errored[0].item.__class__(
        **{**errored[0].item.__dict__, "status": "proposed", "error_summary": ""})]
    after = reconcile(errored, fresh, NOW)
    assert after[0].item.status == "proposed"
    assert after[0].number == 7                 # keeps its approval handle


def test_park_without_the_dep_wired_errors_clearly(tmp_path):
    deps, posts, _, state = _deps([_rec(7)])
    del deps["execute_park"]
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 1
    assert state["records"][0].item.status == "error"
    assert "not wired" in state["records"][0].item.error_summary


# --- CLI wiring -------------------------------------------------------------

def test_run_subcommand_wires_the_park_executor(tmp_path):
    """Falsifying: without the dep the runner claims a park item and then
    immediately errors it with "not wired" -- worse than never claiming it."""
    from unittest.mock import patch
    from worksweep import __main__ as m
    seen = {}

    def fake_run_once(cfg, deps, *a, **kw):
        seen["deps"] = deps
        return 0

    cfgfile = tmp_path / "hb.json"
    cfgfile.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                       '"runner": {"checkouts_root": "/co"}}')
    real_load = m.load_config
    with patch.object(m, "load_config", lambda: real_load(str(cfgfile))), \
            patch("worksweep.runner.run_once", fake_run_once):
        assert m.main(["run"]) == 0
    assert "execute_park" in seen["deps"]
    assert seen["deps"]["execute_park"] is m._execute_park


def test_dry_run_park_takes_no_box_and_rewrites_nothing(tmp_path):
    from unittest.mock import patch
    from worksweep import __main__ as m
    seen = {}

    def fake_run_once(cfg, deps, *a, **kw):
        seen["deps"] = deps
        return 0

    cfgfile = tmp_path / "hb.json"
    cfgfile.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                       '"runner": {"checkouts_root": "/co"}}')
    real_load = m.load_config
    with patch.object(m, "load_config", lambda: real_load(str(cfgfile))), \
            patch("worksweep.runner.run_once", fake_run_once):
        assert m.main(["run", "--dry-run"]) == 0
    assert seen["deps"]["execute_park"] is m._dry_run_park
    result = m._dry_run_park(_rec(1).item, None)
    assert result.box_name == "(dry-run)"
    assert result.description_updated is False
