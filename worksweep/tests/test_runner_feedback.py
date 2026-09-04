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
                runner_timeout=1800, stale_threshold=5,
                feedback_hold=False)
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
    assert ("!3997 — 4 waiting: 2 addressed, 1 replied, 0 noted, "
            "1 escalated") in done[0]
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
    assert done == ["💬 !3997 — 0 addressed, 0 replied, 0 noted, "
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


# --- branch mutual exclusion (2026-08-26 live failure) ---------------------
#
# address-feedback got its own lock in the previous round, which is right for
# throughput and wrong for branches: keep-current and address-feedback can now
# run CONCURRENTLY, and if both target the same branch the second one's
# `checkout -B` lands in a worktree the first is actively working in.

def _rec_on(number, branch, executor=_ADDRESS_FEEDBACK, status="approved",
            iid=3997):
    """A record on `branch`. A `running` one is stamped with a claim time --
    without it the reap treats the claim as unprovable and frees the branch
    before the guard ever sees it."""
    import dataclasses
    rec = _rec(number, status=status, executor=executor, iid=iid,
               branch=branch)
    if status == "running":
        rec = dataclasses.replace(
            rec, item=dataclasses.replace(rec.item, claimed_at=NOW))
    return rec


def test_a_branch_already_being_worked_is_not_claimed_again():
    """FALSIFYING: a running keep-current on branch X must block an approved
    address-feedback row on X, even though they are different executors on
    different locks."""
    recs = [_rec_on(1, "refactor/1681-analytics", executor=_KEEP_CURRENT,
                    status="running", iid=4020),
            _rec_on(2, "refactor/1681-analytics")]
    assert pick_claim(recs, (_ADDRESS_FEEDBACK,)) is None


def test_the_pass_moves_on_to_a_different_branch():
    """Blocking the branch must not stall the queue -- the next eligible item
    on a free branch is still claimed."""
    recs = [_rec_on(1, "refactor/1681-analytics", executor=_KEEP_CURRENT,
                    status="running", iid=4020),
            _rec_on(2, "refactor/1681-analytics"),
            _rec_on(3, "chardy/1588-ranch-data", iid=4001)]
    assert pick_claim(recs, (_ADDRESS_FEEDBACK,)).number == 3


def test_a_free_branch_is_claimed_normally():
    recs = [_rec_on(1, "some/other-branch", executor=_KEEP_CURRENT,
                    status="running", iid=4020),
            _rec_on(2, "refactor/1681-analytics")]
    assert pick_claim(recs, (_ADDRESS_FEEDBACK,)).number == 2


def test_branchless_items_are_unaffected():
    """magi-review and triage rows carry no branch. An empty branch must not
    collide with every other empty branch in the queue."""
    recs = [_rec_on(1, "", executor=_MAGI, status="running", iid=4020),
            _rec_on(2, "", executor=_MAGI)]
    assert pick_claim(recs, (_MAGI,)).number == 2


def test_the_guard_holds_across_the_whole_queue_not_just_one_pass():
    """A running implement on branch X blocks a feedback claim on X too --
    the exclusion is by branch, not by which pass happens to be looking."""
    from worksweep.runner import _IMPLEMENT
    recs = [_rec_on(1, "feat/1775-thing", executor=_IMPLEMENT,
                    status="running", iid=4020),
            _rec_on(2, "feat/1775-thing")]
    assert pick_claim(recs, (_ADDRESS_FEEDBACK,)) is None


def test_the_feedback_pass_claims_nothing_and_stays_quiet(tmp_path):
    """End to end: the pass sees a blocked branch, claims nothing, runs no
    executor, saves nothing and posts nothing. It is not an error -- the item
    is simply claimed on a later pass."""
    ran = []
    recs = [_rec_on(1, "refactor/1681-analytics", executor=_KEEP_CURRENT,
                    status="running", iid=4020),
            _rec_on(2, "refactor/1681-analytics")]
    deps, posts, saves, state = _deps(
        recs, execute=lambda i, c: (ran.append(1), _result())[1])
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0
    assert ran == []
    assert posts == []
    assert saves == []
    assert [r.item.status for r in state["records"]] == ["running", "approved"]


# --- chaining a re-review onto pushed commits (2026-08-26) -----------------
#
# An address-feedback run that FIXED something has pushed code nobody has
# reviewed. Chandler asked for a magi pass on it automatically: the trigger is
# scoped to commits our own executor made, and magi is read-only plus draft
# comments, so this is the one sanctioned bypass of the ✅ gate.

AUTO_WHY = "post-feedback re-review (auto)"


def _magi_rows(records):
    return [r for r in records if r.item.executor == _MAGI]


def test_a_run_that_pushed_commits_queues_its_own_re_review(tmp_path):
    deps, posts, saves, state = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=2,
                                                result_sha="newsha123"))
    assert run_once(_cfg(tmp_path), deps, **_locks(tmp_path)) == 0

    chained = _magi_rows(state["records"])
    assert len(chained) == 1
    it = chained[0].item
    assert it.id == "magi:pb-www!3997@newsha123"
    assert it.kind == "mr"
    assert it.executor == "magi-review"
    assert it.status == "approved"           # pre-approved, no ✅ needed
    assert it.why == AUTO_WHY
    assert it.sha == "newsha123"
    assert it.web_url == "https://gl/x/-/merge_requests/3997"
    assert it.title == _rec(7).item.title


def test_the_chained_row_takes_the_next_free_number(tmp_path):
    deps, _, _, state = _deps(
        [_rec(7), _rec(12, iid=4001)],
        execute=lambda i, c: _result(addressed=1, result_sha="newsha123"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert _magi_rows(state["records"])[0].number == 13


def test_a_reply_only_run_chains_nothing(tmp_path):
    """No commit, nothing new to review -- the branch is exactly where the
    last review left it."""
    deps, _, _, state = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=0, replied=3,
                                                result_sha="unchanged"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert _magi_rows(state["records"]) == []


def test_an_already_answered_run_chains_nothing(tmp_path):
    """FALSIFYING for the trigger. `result_sha` is populated on this path too
    (it is the branch head, read before the run), so a `result_sha`-only test
    would queue a review of code nobody touched, every single time."""
    from worksweep.feedback import FeedbackResult
    deps, _, _, state = _deps(
        [_rec(7)],
        execute=lambda i, c: FeedbackResult(iid=3997, result_sha="oldsha",
                                            already_answered=True))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert _magi_rows(state["records"]) == []


def test_a_run_with_no_resulting_sha_chains_nothing(tmp_path):
    deps, _, _, state = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=1, result_sha=""))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert _magi_rows(state["records"]) == []


def test_a_failed_run_chains_nothing(tmp_path):
    deps, _, _, state = _deps(
        [_rec(7)],
        execute=lambda i, c: (_ for _ in ()).throw(RunnerError("nope")))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert _magi_rows(state["records"]) == []


def test_an_escalating_run_chains_nothing(tmp_path):
    deps, _, _, state = _deps(
        [_rec(7)],
        execute=lambda i, c: (_ for _ in ()).throw(NeedsInputError("q")))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert _magi_rows(state["records"]) == []


def test_the_same_sha_is_never_queued_twice(tmp_path):
    """A re-run against an unchanged head (the reviewer replied again, we
    fixed nothing new) must not stack a second review of the same commits."""
    existing = QueueRecord(
        number=9, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id="magi:pb-www!3997@newsha123",
                      repo="pb-www", kind="mr", executor=_MAGI, risk="low",
                      why=AUTO_WHY, web_url="https://gl/x/-/merge_requests/3997",
                      sha="newsha123", status="approved"))
    deps, _, _, state = _deps(
        [_rec(7), existing],
        execute=lambda i, c: _result(addressed=1, result_sha="newsha123"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    rows = _magi_rows(state["records"])
    assert len(rows) == 1
    assert rows[0].number == 9               # the original, untouched


def test_the_completion_and_the_chain_land_in_one_write(tmp_path):
    """Two saves would leave a window where the feedback row is `done` and
    the review it earned does not exist -- and a crash in between loses it."""
    deps, _, saves, _ = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=1,
                                                result_sha="newsha123"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert len(saves) == 2                   # the claim, then the completion
    final = saves[-1]
    feedback_rows = [r for r in final if r.item.executor == _ADDRESS_FEEDBACK]
    assert [r.item.status for r in feedback_rows] == ["done"]
    assert [r.item.id for r in _magi_rows(final)] == \
        ["magi:pb-www!3997@newsha123"]


def test_the_chained_review_is_not_run_in_the_same_invocation(tmp_path):
    """One claim per pass stays one claim per pass: the magi pass already ran
    (and found nothing) before the feedback pass appended this."""
    magi_ran = []
    deps, _, _, state = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=1,
                                                result_sha="newsha123"))
    deps["execute"] = lambda item, cfg: (magi_ran.append(item), ("s", "/r"))[1]
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert magi_ran == []
    assert _magi_rows(state["records"])[0].item.status == "approved"


def test_the_next_fire_does_claim_it(tmp_path):
    """And it really is claimable -- pre-approved means the runner takes it
    on the following pass with no human step."""
    deps, _, _, state = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=1,
                                                result_sha="newsha123"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert pick_claim(state["records"], (_MAGI,)).item.id == \
        "magi:pb-www!3997@newsha123"


def test_the_chained_row_is_blanket_safe_and_not_a_zombie(tmp_path):
    """It is auto-approved without a ✅, so it has to be an executor the
    runner will actually claim -- otherwise it strands forever."""
    from worksweep.models import RUNNABLE_EXECUTORS
    from worksweep.queue import is_dismissable
    deps, _, _, state = _deps(
        [_rec(7)], execute=lambda i, c: _result(addressed=1,
                                                result_sha="newsha123"))
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    it = _magi_rows(state["records"])[0].item
    assert it.executor in RUNNABLE_EXECUTORS
    assert is_dismissable(it) is False


# --- 2026-09-04: held for review ---------------------------------------------

def test_a_held_result_parks_the_row_with_everything_publish_needs(tmp_path):
    """The run committed locally and posted nothing. The runner parks the row
    needs-input carrying the commit, its worktree and the report, and says so
    with a 📦 -- never a ⚠️, never `done`."""
    held = _result(addressed=1, replied=1, replies=(), result_sha="base0",
                   held=True, hold_sha="deadbeef", hold_dir="/wt/hold-pb-www-3997",
                   hold_report='{"addressed": [{"thread": "t1"}]}')
    deps, posts, _, state = _deps([_rec(7)], execute=lambda i, c: held)
    rc = run_once(_cfg(tmp_path, feedback_hold=True), deps, **_locks(tmp_path))
    assert rc == 0
    row = state["records"][0].item
    assert row.status == "needs-input"
    assert row.hold_sha == "deadbeef" and row.hold_dir == "/wt/hold-pb-www-3997"
    assert row.hold_report == '{"addressed": [{"thread": "t1"}]}'
    assert row.hold_action == ""
    assert "Held for review" in row.error_summary
    assert "1 fix(es), 1 repl(ies)" in row.error_summary
    assert [p for p in posts if p.startswith("📦")]
    assert not [p for p in posts if p.startswith("⚠️")]
    assert not [p for p in posts if p.startswith("💬")]


def test_a_published_row_completes_with_its_hold_cleared(tmp_path):
    """After Publish the executor returns an ordinary result; the row goes
    done and no hold bookkeeping survives to be published twice."""
    import dataclasses
    queued = _rec(7)
    queued = dataclasses.replace(queued, item=dataclasses.replace(
        queued.item, hold_sha="deadbeef", hold_dir="/wt/h",
        hold_report="{}", hold_action="publish"))
    deps, posts, _, state = _deps([queued], execute=lambda i, c: _result())
    run_once(_cfg(tmp_path, feedback_hold=True), deps, **_locks(tmp_path))
    row = state["records"][0].item
    assert row.status == "done"
    assert row.hold_sha == "" and row.hold_dir == ""
    assert row.hold_report == "" and row.hold_action == ""
    assert [p for p in posts if p.startswith("💬")]


def test_the_feedback_pass_gcs_holds_before_claiming(tmp_path):
    seen = []
    deps, _, _, _ = _deps([_rec(7)])
    deps["gc_holds"] = lambda records, cfg: seen.append(len(records)) or []
    run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert seen == [1]


def test_a_gc_failure_never_blocks_the_pass(tmp_path):
    def boom(records, cfg):
        raise OSError("disk went away")
    deps, posts, _, state = _deps([_rec(7)])
    deps["gc_holds"] = boom
    rc = run_once(_cfg(tmp_path), deps, **_locks(tmp_path))
    assert rc == 0
    assert state["records"][0].item.status == "done"      # the claim still ran
    assert any("hold GC failed" in p for p in posts)
