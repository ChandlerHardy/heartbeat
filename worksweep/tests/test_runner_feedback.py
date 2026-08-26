"""Runner integration for the `address-feedback` executor.

It shares the magi/keep-current/park pass and lock (a git op plus one claude
run, well inside the 45-minute reap window). Mirrors test_runner_park.py, with
the one thing park has no use for: a `needs-input` branch, because a run that
found only judgment calls has asked a question, not failed.
"""
import pytest

from worksweep.config import WorksweepConfig
from worksweep.feedback import FeedbackResult
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import (_ADDRESS_FEEDBACK, _KEEP_CURRENT, _MAGI, _PARK,
                              NeedsInputError, RunnerError, pick_claim,
                              run_once)

NOW = "2026-08-25T12:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="chandler.hardy",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, stale_threshold=5)
    base.update(kw)
    return WorksweepConfig(**base)


def _rec(number, status="approved", executor=_ADDRESS_FEEDBACK, iid=3997,
         branch="chardy/1588-ranch-data"):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"feedback:pb-www!{iid}",
                      repo="pb-www", kind="feedback", executor=executor,
                      risk="low", why="2 unaddressed threads",
                      web_url=f"https://gl/x/-/merge_requests/{iid}", sha="s",
                      status=status, branch=branch))


def _result(**kw):
    base = dict(iid=3997, waiting=4, addressed=2, replied=1,
                escalated=("leyang: “should this be cached?” — product call",),
                replies=("t1: addressed in deadbee",),
                result_sha="newsha123")
    base.update(kw)
    return FeedbackResult(**base)


def _deps(records, execute=None, posts=None, saves=None):
    posts = posts if posts is not None else []
    saves = saves if saves is not None else []
    state = {"records": list(records)}
    d = {"load": lambda: list(state["records"]),
         "save": lambda recs: (state.update(records=list(recs)),
                               saves.append(list(recs)))[0],
         "post": lambda hook, content: posts.append(content),
         "now": lambda: NOW,
         "execute": lambda item, cfg: ("s1", "/r.md"),
         "execute_address_feedback": execute or (lambda item, cfg: _result())}
    return d, posts, saves, state


def _locks(tmp_path):
    return dict(lock_path=str(tmp_path / "runner.lock"),
                implement_lock_path=str(tmp_path / "runner-implement.lock"))


# --- claim selection --------------------------------------------------------

def test_pick_claim_includes_address_feedback_by_default():
    """Falsifying: leave it out of _ALL_EXECUTORS and an approved feedback row
    sits `approved` forever with no un-approve path -- the zombie the runnable
    gate exists to prevent."""
    assert pick_claim([_rec(1)]).number == 1


def test_the_shared_short_op_pass_no_longer_claims_address_feedback():
    """It runs a claude pass, not a git op -- it belongs on its own lock."""
    recs = [_rec(1, executor=_ADDRESS_FEEDBACK),
            _rec(2, executor=_KEEP_CURRENT, iid=1),
            _rec(4, executor=_MAGI, iid=2),
            _rec(5, executor=_PARK, iid=3)]
    assert pick_claim(recs, (_MAGI, _KEEP_CURRENT, _PARK)).number == 2
    assert pick_claim(recs, (_ADDRESS_FEEDBACK,)).number == 1


def test_a_proposed_address_feedback_item_is_never_claimed():
    """✅-gated: replies go out under Chandler's name, so nothing claims this
    row until a human approves it."""
    assert pick_claim([_rec(1, status="proposed")]) is None


# --- the happy path ---------------------------------------------------------

def test_a_finished_run_completes_done_and_posts_the_tally(tmp_path):
    seen = {}

    def execute(item, cfg):
        seen.update(item=item)
        return _result()

    deps, posts, saves, state = _deps([_rec(7)], execute=execute)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 0
    assert seen["item"].branch == "chardy/1588-ranch-data"

    assert state["records"][0].item.status == "done"
    assert state["records"][0].item.result_sha == "newsha123"
    assert [r[0].item.status for r in saves] == ["running", "done"]

    done = [p for p in posts if p.startswith("💬")]
    assert len(done) == 1
    assert "!3997 — 4 waiting: 2 addressed, 1 replied, 1 escalated" in done[0]
    assert "said: t1: addressed in deadbee" in done[0]
    assert "needs you: leyang: “should this be cached?” — product call" in done[0]
    assert not [p for p in posts if p.startswith("⚠️")]


def test_the_reviewer_getting_there_first_is_done_not_an_error(tmp_path):
    """AC #17: between the sweep and the run the reviewer replied or closed
    the threads. Nothing to do is a finished item -- never a ⚠️."""
    deps, posts, _, state = _deps(
        [_rec(7)],
        execute=lambda i, c: FeedbackResult(iid=3997, result_sha="s",
                                            already_answered=True))
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 0
    assert state["records"][0].item.status == "done"
    done = [p for p in posts if p.startswith("💬")]
    assert done == ["💬 !3997 — 0 addressed, 0 replied, "
                    "0 escalated — threads already answered"]
    assert not [p for p in posts if p.startswith("⚠️")]


# --- needs-input ------------------------------------------------------------

def test_zero_handled_with_escalations_asks_rather_than_errors(tmp_path):
    """FALSIFYING (AC #12): NeedsInputError subclasses RunnerError, so a
    handler copied straight from park would record this as a hard `error`
    with a ⚠️. Drop the NeedsInputError branch and this goes red."""
    def execute(item, cfg):
        raise NeedsInputError("!3997: 2 threads need your call — "
                              "leyang: “rename this table?” — schema call; "
                              "leyang: “is this the right tab?” — product call")

    deps, posts, _, state = _deps([_rec(7)], execute=execute)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 0                       # a question is a handled outcome
    assert state["records"][0].item.status == "needs-input"
    assert "schema call" in state["records"][0].item.error_summary

    assert not [p for p in posts if p.startswith("⚠️")]
    asked = [p for p in posts if p.startswith("❓")]
    assert len(asked) == 1
    assert "#7" in asked[0]
    assert "product call" in asked[0]


def test_a_needs_input_row_is_not_reclaimed_on_the_next_pass(tmp_path):
    """needs-input is terminal-ish: only a fresh ✅ flips it back."""
    deps, _, _, state = _deps(
        [_rec(7)],
        execute=lambda i, c: (_ for _ in ()).throw(NeedsInputError("q")))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert pick_claim(state["records"]) is None


# --- failure surfaces -------------------------------------------------------

def test_an_unverifiable_run_errors_and_posts(tmp_path):
    """Falsifying: a swallowed failure leaves the item `running` until the
    45-minute reap with the human told nothing."""
    def boom(item, cfg):
        raise RunnerError("the address-feedback run on !3997 claims 2 "
                          "commit(s), but origin/chardy/1588-ranch-data "
                          "never moved")
    deps, posts, _, state = _deps([_rec(7)], execute=boom)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 1
    assert state["records"][0].item.status == "error"
    assert "never moved" in state["records"][0].item.error_summary
    assert any("never moved" in p for p in posts)


def test_an_unexpected_exception_still_errors_and_posts(tmp_path):
    def boom(item, cfg):
        raise RuntimeError("glab api ... exited 1: 403 Forbidden")
    deps, posts, _, state = _deps([_rec(7)], execute=boom)
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 1
    assert state["records"][0].item.status == "error"
    assert "403" in state["records"][0].item.error_summary
    assert any("403" in p for p in posts)


def test_address_feedback_without_the_dep_wired_errors_clearly(tmp_path):
    deps, posts, _, state = _deps([_rec(7)])
    del deps["execute_address_feedback"]
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 1
    assert state["records"][0].item.status == "error"
    assert "not wired" in state["records"][0].item.error_summary
    assert any("not wired" in p for p in posts)


def test_an_errored_run_is_re_proposed_by_the_next_sweep(tmp_path):
    from worksweep.queue import reconcile
    deps, _, _, state = _deps(
        [_rec(7)], execute=lambda i, c: (_ for _ in ()).throw(
            RunnerError("the address-feedback run failed")))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    errored = state["records"]
    assert errored[0].item.status == "error"
    fresh = [errored[0].item.__class__(
        **{**errored[0].item.__dict__, "status": "proposed",
           "error_summary": ""})]
    after = reconcile(errored, fresh, NOW)
    assert after[0].item.status == "proposed"
    assert after[0].number == 7                # keeps its approval handle


# --- CLI wiring -------------------------------------------------------------

def _cfgfile(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                 '"runner": {"checkouts_root": "/co"}}')
    return p


def test_run_subcommand_wires_the_address_feedback_executor(tmp_path):
    """Falsifying: without the dep the runner claims the item and immediately
    errors it "not wired" -- worse than never claiming it."""
    from unittest.mock import patch
    from worksweep import __main__ as m
    seen = {}
    real_load = m.load_config
    with patch.object(m, "load_config",
                      lambda: real_load(str(_cfgfile(tmp_path)))), \
            patch("worksweep.runner.run_once",
                  lambda cfg, deps, *a, **kw: (seen.update(deps=deps), 0)[1]):
        assert m.main(["run"]) == 0
    assert seen["deps"]["execute_address_feedback"] is m._execute_address_feedback


def test_dry_run_never_posts_a_reply(tmp_path):
    from unittest.mock import patch
    from worksweep import __main__ as m
    seen = {}
    real_load = m.load_config
    with patch.object(m, "load_config",
                      lambda: real_load(str(_cfgfile(tmp_path)))), \
            patch("worksweep.runner.run_once",
                  lambda cfg, deps, *a, **kw: (seen.update(deps=deps), 0)[1]):
        assert m.main(["run", "--dry-run"]) == 0
    assert seen["deps"]["execute_address_feedback"] is m._dry_run_address_feedback
    result = m._dry_run_address_feedback(_rec(1).item, None)
    assert (result.addressed, result.replied, result.escalated) == (0, 0, ())


# --- lock hold (fix-mode round 2, warning 13) ------------------------------

def test_a_feedback_run_does_not_hold_the_short_op_lock(tmp_path):
    """A 30-minute claude pass inside the shared pass would block magi,
    keep-current, park AND the stale-claim reap for its whole duration --
    long enough for the reap window itself to be missed."""
    from worksweep.runner import acquire_lock, release_lock
    locks = _locks(tmp_path)
    held = {}

    def execute(item, cfg):
        # Mid-run: could another pass take the short-op lock right now?
        held["short_op_free"] = acquire_lock(locks["lock_path"])
        if held["short_op_free"]:
            release_lock(locks["lock_path"])
        return _result()

    deps, _, _, state = _deps([_rec(7)], execute=execute)
    assert run_once(_cfg(tmp_path), deps, **locks) == 0
    assert held["short_op_free"] is True
    assert state["records"][0].item.status == "done"


def test_the_feedback_pass_takes_its_own_lock(tmp_path):
    """And it is a REAL lock: a second overlapping fire must not double-run a
    claim that posts replies under Chandler's name."""
    from worksweep.runner import acquire_lock
    locks = _locks(tmp_path)
    feedback_lock = str(tmp_path / "runner-address-feedback.lock")
    assert acquire_lock(feedback_lock)

    ran = []
    deps, posts, _, state = _deps(
        [_rec(7)], execute=lambda i, c: (ran.append(1), _result())[1])
    assert run_once(_cfg(tmp_path), deps, **locks) == 0
    assert ran == []                       # the other fire still holds it
    assert state["records"][0].item.status == "approved"
    assert posts == []


def test_the_done_post_is_clamped_under_the_discord_cap(tmp_path):
    """A thread body is arbitrary third-party text and there can be twenty of
    them. An over-long post is REJECTED by Discord, which turns a completed
    run into silence -- exactly the outcome that is never allowed."""
    from worksweep.formatter import DISCORD_MAX_CHARS
    huge = tuple(f"leyang: {'x' * 400} — call {i}" for i in range(20))
    deps, posts, _, _ = _deps(
        [_rec(7)], execute=lambda i, c: _result(escalated=huge, waiting=20))
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    done = [p for p in posts if p.startswith("💬")][0]
    assert len(done.encode("utf-8")) <= DISCORD_MAX_CHARS - 100
    assert "20 waiting:" in done


def test_the_needs_input_post_is_clamped_too(tmp_path):
    from worksweep.formatter import DISCORD_MAX_CHARS
    question = "!3997: 20 threads need your call - " + "; ".join(
        f"leyang: {'y' * 400} — call {i}" for i in range(20))
    deps, posts, _, state = _deps(
        [_rec(7)],
        execute=lambda i, c: (_ for _ in ()).throw(NeedsInputError(question)))
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    asked = [p for p in posts if p.startswith("❓")][0]
    assert len(asked.encode("utf-8")) <= DISCORD_MAX_CHARS - 100
    assert state["records"][0].item.status == "needs-input"
