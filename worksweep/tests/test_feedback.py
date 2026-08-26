"""The `address-feedback` executor: answer the threads waiting on Chandler.

Every edge is injected — this file must never touch glab, ssh, the network or
a real subprocess. The one thing it does touch is the report file the claude
run is supposed to leave behind, and that lives in pytest's tmp_path.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep import feedback  # noqa: E402
from worksweep.config import WorksweepConfig  # noqa: E402
from worksweep.models import WorkItem  # noqa: E402
from worksweep.runner import NeedsInputError, RunnerError  # noqa: E402

ME = "chandler.hardy"
BRANCH = "chardy/1588-ranch-data"
PRE_SHA = "1111111111111111111111111111111111111111"
POST_SHA = "2222222222222222222222222222222222222222"


# --- fixtures ---------------------------------------------------------------

def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username=ME,
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800)
    base.update(kw)
    return WorksweepConfig(**base)


def _item(branch=BRANCH, iid=3997, repo="pb-www"):
    return WorkItem(schema_version=1, id=f"feedback:{repo}!{iid}", repo=repo,
                    kind="feedback", executor="address-feedback", risk="low",
                    why="2 unaddressed threads",
                    web_url=f"https://gl/x/-/merge_requests/{iid}",
                    sha="abc123", status="approved", title="Ranch data tab",
                    branch=branch)


def _note(author, body="b", system=False, resolvable=True, resolved=False):
    return {"body": body, "system": system, "resolvable": resolvable,
            "resolved": resolved, "author": {"username": author}}


def _thread(tid, last="leyang"):
    """A resolvable, unresolved thread. `last` is who spoke last."""
    notes = [_note("leyang", f"question on {tid}")]
    if last != "leyang":
        notes.append(_note(last, f"addressed in abc1234 ({tid})"))
    return {"id": tid, "notes": notes}


def _payload(*threads):
    return json.dumps(list(threads))


class _Glab:
    """Serves the discussions payloads in order; the last one repeats."""

    def __init__(self, *payloads):
        self.payloads = list(payloads) or ["[]"]
        self.calls = []

    def __call__(self, args, body=None):
        self.calls.append((list(args), body))
        return (self.payloads.pop(0) if len(self.payloads) > 1
                else self.payloads[0])


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Subprocess:
    """A git/claude stand-in. Records every command; serves shas and writes
    whatever report the fake claude run is configured to leave behind."""

    def __init__(self, checkout, report=None, claude_rc=0, claude_raises=None,
                 remote_shas=(PRE_SHA, PRE_SHA)):
        self.checkout, self.report = checkout, report
        self.claude_rc, self.claude_raises = claude_rc, claude_raises
        self.remote_shas = list(remote_shas)
        self.calls, self.claude_kw = [], {}

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0] == "claude":
            self.claude_kw = dict(kw)
            if self.claude_raises is not None:
                raise self.claude_raises
            if self.report is not None:
                os.makedirs(self.checkout, exist_ok=True)
                with open(os.path.join(self.checkout,
                                       feedback._REPORT_NAME), "w") as f:
                    f.write(self.report if isinstance(self.report, str)
                            else json.dumps(self.report))
            return _Proc(self.claude_rc)
        if cmd[:2] == ["git", "-C"] and "rev-parse" in cmd:
            if any(a.startswith("origin/") for a in cmd):
                sha = (self.remote_shas.pop(0) if len(self.remote_shas) > 1
                       else self.remote_shas[0])
                return _Proc(0, sha + "\n")
            if "--git-dir" in cmd:
                return _Proc(0, ".git\n")
            return _Proc(0, PRE_SHA + "\n")
        if "status" in cmd and "--porcelain" in cmd:
            return _Proc(0, "")
        return _Proc(0, "")

    def ran(self, *fragments):
        return [c for c in self.calls
                if all(any(f in a for a in c) for f in fragments)]


@pytest.fixture
def worktree(tmp_path):
    """The layout checkouts.worktree_for expects: a shared clone plus this
    executor's own worktree, already present so no `worktree add` is needed."""
    (tmp_path / "pb-www").mkdir()
    wt = tmp_path / ".worktrees" / "pb-www-address-feedback"
    wt.mkdir(parents=True)
    return str(wt)


def _report(addressed=(), replied=(), escalated=()):
    return {"addressed": list(addressed), "replied": list(replied),
            "escalated": list(escalated)}


# --- AC #8 (falsifying): it never resolves anything -------------------------

def test_feedback_prompt_never_resolves():
    """FALSIFYING. Resolution belongs to whoever opened the thread, full stop.
    The prompt may not so much as mention the idea, and no code path may reach
    the resolve endpoint or send a resolved body.

    Mutation: add a resolve instruction to the prompt and this goes red.
    """
    import inspect
    import re
    prompt = feedback.render_prompt(
        "pb-www", 3997, BRANCH, _unaddressed(_payload(_thread("t1"))))
    assert re.search(r"resolv", prompt, re.I) is None

    src = inspect.getsource(feedback)
    assert "/resolve" not in src
    assert '"resolved"' not in src
    assert "resolved=" not in src
    # and the tally has no room to count one: three outcomes, no fourth
    assert set(feedback.FeedbackResult.__dataclass_fields__) == {
        "iid", "addressed", "replied", "escalated", "result_sha",
        "already_answered"}


def _unaddressed(payload, username=ME):
    from worksweep.collectors import unaddressed_threads
    return unaddressed_threads(payload, username)


# --- AC #9 / #11: what the prompt actually instructs ------------------------

def test_feedback_prompt_states_the_three_classes():
    prompt = feedback.render_prompt(
        "pb-www", 3997, BRANCH, _unaddressed(_payload(_thread("t1"))))
    assert "addressed in <short-sha>" in prompt
    for token in ("FIXABLE", "QUESTION", "ESCALATE"):
        assert token in prompt
    assert "Uncertainty" in prompt          # uncertainty biases to escalate
    assert "Do NOT reply" in prompt


def test_feedback_prompt_names_every_unaddressed_thread_and_its_last_word():
    threads = _unaddressed(_payload(_thread("t1"), _thread("t2")))
    prompt = feedback.render_prompt("pb-www", 3997, BRANCH, threads)
    assert "t1" in prompt and "t2" in prompt
    assert "question on t1" in prompt
    assert "leyang" in prompt


def test_feedback_prompt_carries_pbwww_hygiene():
    prompt = feedback.render_prompt(
        "pb-www", 3997, BRANCH, _unaddressed(_payload(_thread("t1"))))
    assert "maintenance/compile-css" in prompt
    assert "www/home/scss/*" in prompt
    assert "$script_version" in prompt
    assert "www/home/php/templates/tab_bar_common_logic.php" in prompt
    assert f"git push origin {BRANCH}" in prompt
    assert "Available on" in prompt          # the dev-box sync condition


def test_feedback_prompt_posts_replies_to_the_notes_endpoint():
    prompt = feedback.render_prompt(
        "pb-www", 3997, BRANCH, _unaddressed(_payload(_thread("t1"))))
    assert "/discussions/<thread-id>/notes" in prompt
    assert "merge_requests/3997" in prompt


# --- AC #10: worktree, run-time re-fetch, python verification ---------------

def test_feedback_uses_its_own_worktree(tmp_path, worktree):
    """Never the shared magi clone: a `checkout -B` in there can yank the
    branch out from under a live 90-minute implement run."""
    from worksweep import checkouts
    assert "address-feedback" in checkouts._WORKTREE_EXECUTORS
    sub = _Subprocess(worktree, report=_report(replied=["t1"]))
    glab = _Glab(_payload(_thread("t1")),
                 _payload(_thread("t1", last=ME)))
    feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                     run_glab=glab)
    assert sub.ran("checkout", "-B", BRANCH)
    assert all(worktree in c for c in sub.ran("checkout", "-B"))
    assert str(tmp_path / "pb-www") not in [c[2] for c in sub.ran("checkout")]


def test_feedback_refetches_threads_at_run_time(tmp_path, worktree):
    """The sweep's snapshot is minutes to hours old -- the reviewer may have
    replied or closed the thread since. The run reads the threads itself."""
    sub = _Subprocess(worktree, report=_report(replied=["t1"]))
    glab = _Glab(_payload(_thread("t1"), _thread("t2", last=ME)),
                 _payload(_thread("t1", last=ME), _thread("t2", last=ME)))
    feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                     run_glab=glab)
    paths = [a[1] for a in [c[0] for c in glab.calls]]
    assert all("merge_requests/3997/discussions" in p for p in paths)
    assert len(glab.calls) == 2          # once before the run, once to verify
    # t2 was already answered at run time, so it never reached the prompt
    prompt = [c for c in sub.calls if c[0] == "claude"][0][2]
    assert "t1" in prompt and "t2" not in prompt


def test_feedback_verification_rejects_an_unpushed_commit_claim(tmp_path,
                                                                worktree):
    """A commit that never left the worktree is a commit the reviewer cannot
    see -- the reply pointing at its sha would be a lie."""
    sub = _Subprocess(worktree,
                      report=_report(addressed=[{"thread": "t1",
                                                 "sha": "deadbee"}]),
                      remote_shas=(PRE_SHA, PRE_SHA))
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1", last=ME)))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "never moved" in str(e.value)


def test_feedback_verification_accepts_a_pushed_commit_claim(tmp_path,
                                                             worktree):
    sub = _Subprocess(worktree,
                      report=_report(addressed=[{"thread": "t1",
                                                 "sha": "deadbee"}]),
                      remote_shas=(PRE_SHA, POST_SHA))
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1", last=ME)))
    result = feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                              run_glab=glab)
    assert result.addressed == 1
    assert result.result_sha == POST_SHA


def test_feedback_verification_rejects_a_reply_that_was_never_posted(
        tmp_path, worktree):
    """The report says it answered t1, but the thread's last word is still
    the reviewer's. Trust nothing until python re-reads it."""
    sub = _Subprocess(worktree, report=_report(replied=["t1"]))
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1")))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "t1" in str(e.value)
    assert "leyang" in str(e.value)


def test_feedback_rejects_a_claim_on_a_thread_it_was_never_given(tmp_path,
                                                                 worktree):
    sub = _Subprocess(worktree, report=_report(replied=["t1", "t-other"]))
    glab = _Glab(_payload(_thread("t1")),
                 _payload(_thread("t1", last=ME),
                          _thread("t-other", last=ME)))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "t-other" in str(e.value)


def test_a_missing_report_is_an_error(tmp_path, worktree):
    sub = _Subprocess(worktree, report=None)
    glab = _Glab(_payload(_thread("t1")))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "no report" in str(e.value)


def test_an_unparseable_report_is_an_error(tmp_path, worktree):
    sub = _Subprocess(worktree, report="{not json")
    glab = _Glab(_payload(_thread("t1")))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "report" in str(e.value)


def test_a_stale_report_from_a_previous_run_is_never_read(tmp_path, worktree):
    """The report is untracked in a reused worktree. If the claude run leaves
    none, yesterday's must not be mistaken for today's."""
    with open(os.path.join(worktree, feedback._REPORT_NAME), "w") as f:
        f.write(json.dumps(_report(replied=["t1"])))
    sub = _Subprocess(worktree, report=None)
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1", last=ME)))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "no report" in str(e.value)


# --- AC #17: the reviewer got there first -----------------------------------

def test_feedback_zero_unaddressed_at_run_time_is_a_normal_result(tmp_path,
                                                                  worktree):
    """The reviewer replied or closed the threads between the sweep and the
    run. That is a finished item, not an error -- and no claude run at all."""
    sub = _Subprocess(worktree, report=None)
    glab = _Glab(_payload(_thread("t1", last=ME)))
    result = feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                              run_glab=glab)
    assert result.already_answered is True
    assert (result.addressed, result.replied, result.escalated) == (0, 0, ())
    assert sub.ran("claude") == []
    assert feedback.tally(result) == (
        "0 addressed, 0 replied, 0 escalated — threads already answered")


# --- AC #18: the timeout comes from cfg -------------------------------------

def test_feedback_run_uses_the_cfg_timeout(tmp_path, worktree):
    from worksweep.runner import STALE_RUNNING_MINUTES
    sub = _Subprocess(worktree, report=_report(replied=["t1"]))
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1", last=ME)))
    cfg = _cfg(tmp_path)
    feedback.execute(_item(), cfg, run_subprocess=sub, run_glab=glab)
    assert sub.claude_kw["timeout"] == cfg.runner_timeout == 1800
    assert sub.claude_kw["cwd"] == worktree
    # inside the reap window, or healthy runs get killed mid-flight
    assert cfg.runner_timeout < STALE_RUNNING_MINUTES * 60


def test_a_shorter_configured_timeout_is_honoured(tmp_path, worktree):
    sub = _Subprocess(worktree, report=_report(replied=["t1"]))
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1", last=ME)))
    feedback.execute(_item(), _cfg(tmp_path, runner_timeout=600),
                     run_subprocess=sub, run_glab=glab)
    assert sub.claude_kw["timeout"] == 600


def test_a_timed_out_claude_run_is_a_clean_runner_error(tmp_path, worktree):
    import subprocess
    sub = _Subprocess(worktree,
                      claude_raises=subprocess.TimeoutExpired("claude", 1800))
    glab = _Glab(_payload(_thread("t1")))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "timed out" in str(e.value)


# --- AC #12: outcomes -------------------------------------------------------

def test_a_mixed_run_reports_an_honest_tally(tmp_path, worktree):
    sub = _Subprocess(worktree, remote_shas=(PRE_SHA, POST_SHA),
                      report=_report(
                          addressed=[{"thread": "t1", "sha": "deadbee"},
                                     {"thread": "t2", "sha": "deadbee"}],
                          replied=["t3"],
                          escalated=[{"thread": "t4",
                                      "reason": "wants a schema change"}]))
    before = _payload(*[_thread(t) for t in ("t1", "t2", "t3", "t4")])
    after = _payload(_thread("t1", last=ME), _thread("t2", last=ME),
                     _thread("t3", last=ME), _thread("t4"))
    result = feedback.execute(_item(), _cfg(tmp_path),
                              run_subprocess=sub, run_glab=_Glab(before, after))
    assert (result.addressed, result.replied) == (2, 1)
    assert len(result.escalated) == 1
    assert feedback.tally(result) == "2 addressed, 1 replied, 1 escalated"
    msg = feedback.done_message(result)
    assert msg.startswith("💬 !3997")
    assert "2 addressed, 1 replied, 1 escalated" in msg
    assert "wants a schema change" in msg          # actionable from a phone
    assert "leyang" in msg


def test_zero_handled_and_something_escalated_asks_instead_of_failing(
        tmp_path, worktree):
    """AC #12: a run that only found judgment calls has not failed -- it has a
    question. NeedsInputError, not RunnerError."""
    sub = _Subprocess(worktree, report=_report(
        escalated=[{"thread": "t1", "reason": "disagrees with the approach"},
                   {"thread": "t2", "reason": "needs a product call"}]))
    glab = _Glab(_payload(_thread("t1"), _thread("t2")),
                 _payload(_thread("t1"), _thread("t2")))
    with pytest.raises(NeedsInputError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "!3997" in str(e.value)
    assert "disagrees with the approach" in str(e.value)
    assert "needs a product call" in str(e.value)


def test_a_run_that_handled_and_escalated_nothing_is_a_failure(tmp_path,
                                                               worktree):
    """Silence is never an outcome: two threads went in, the report accounts
    for neither."""
    sub = _Subprocess(worktree, report=_report())
    glab = _Glab(_payload(_thread("t1"), _thread("t2")),
                 _payload(_thread("t1"), _thread("t2")))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "accounted for none" in str(e.value)


def test_a_failed_claude_run_is_a_runner_error(tmp_path, worktree):
    sub = _Subprocess(worktree, claude_rc=1, report=None)
    glab = _Glab(_payload(_thread("t1")))
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                         run_glab=glab)
    assert "the address-feedback run failed" in str(e.value)


# --- edge guards ------------------------------------------------------------

def test_the_executor_refuses_to_run_without_its_edges(tmp_path):
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(), _cfg(tmp_path))
    assert "wired without" in str(e.value)


def test_the_executor_refuses_an_item_with_no_branch(tmp_path, worktree):
    with pytest.raises(RunnerError) as e:
        feedback.execute(_item(branch=""), _cfg(tmp_path),
                         run_subprocess=_Subprocess(worktree),
                         run_glab=_Glab())
    assert "no source branch" in str(e.value)


def test_the_worktree_is_made_pristine_before_the_branch_is_touched(
        tmp_path, worktree):
    """A prior claim that died mid-run leaves this reused worktree dirty."""
    sub = _Subprocess(worktree, report=_report(replied=["t1"]))
    glab = _Glab(_payload(_thread("t1")), _payload(_thread("t1", last=ME)))
    feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                     run_glab=glab)
    first_status = next(i for i, c in enumerate(sub.calls)
                        if "status" in c and "--porcelain" in c)
    first_checkout = next(i for i, c in enumerate(sub.calls)
                          if "checkout" in c and "-B" in c)
    assert first_status < first_checkout


# --- the tally cannot double-count -----------------------------------------

def test_a_thread_listed_twice_is_counted_once(tmp_path, worktree):
    """A run that lists t1 under BOTH `addressed` and `replied` (or under
    `escalated` as well) would otherwise inflate its own tally past the number
    of threads it was even given -- and the tally is the whole report."""
    sub = _Subprocess(worktree, remote_shas=(PRE_SHA, POST_SHA),
                      report=_report(addressed=[{"thread": "t1",
                                                 "sha": "deadbee"}],
                                     replied=["t1", "t2"],
                                     escalated=[{"thread": "t1",
                                                 "reason": "also unsure"},
                                                {"thread": "t2",
                                                 "reason": "still unsure"}]))
    before = _payload(_thread("t1"), _thread("t2"))
    after = _payload(_thread("t1", last=ME), _thread("t2", last=ME))
    result = feedback.execute(_item(), _cfg(tmp_path), run_subprocess=sub,
                              run_glab=_Glab(before, after))
    assert (result.addressed, result.replied, len(result.escalated)) == (1, 1, 0)
    assert feedback.tally(result) == "1 addressed, 1 replied, 0 escalated"
