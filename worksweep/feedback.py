"""The `address-feedback` executor: answer the review threads waiting on you.

Worksweep has always been able to SEE that an authored MR had open threads, but
`feedback` was an inert `triage` row -- a nag that sat on the dashboard until
Chandler opened GitLab himself. This executor turns it into work: it takes the
threads where a reviewer had the last word, runs ONE unattended `claude -p` in
its own worktree, and then proves in Python what that run actually did.

    worktree -> preflight clean -> fetch -> checkout branch
    -> re-read the threads (NOT the sweep's snapshot)
    -> nothing waiting? done, no claude run at all
    -> one `claude -p` pass: fix + reply, reply, or escalate, per thread
    -> read its report, re-read the threads, verify every claim
    -> tally

Two things are deliberately absent, and both are load-bearing:

* It NEVER closes a thread. Whoever opened a thread decides when it is
  finished; a bot marking its own homework as done is exactly the kind of
  quiet overreach that makes a reviewer stop trusting the queue. The run is
  never told about that idea, and nothing here counts one.
* It is never auto-approved (models.RUNNABLE_EXECUTORS, but NOT
  config.auto_approve). The replies go out under Chandler's GitLab identity
  and a posted reply cannot be unsent, so consent is per-MR -- which is also
  why the prompt sends every judgment call, disagreement and shrug back to
  him instead of guessing.

Contract with the runner:

* `RunnerError`   -> the item goes `error`, ⚠️ posted, re-proposed next sweep.
                     Raised when the run cannot be trusted: no report, a
                     commit claimed but origin never moved, a reply claimed
                     but the thread's last word is still the reviewer's.
* `NeedsInputError` -> zero threads handled and at least one escalated. That
                     is a question, not a failure: the item goes
                     `needs-input` with a ❓ and waits for Chandler.
* a `FeedbackResult` -> `done`, with a tally naming what was addressed,
                     replied and escalated (and each escalation short enough
                     to act on from a phone).

Every edge is injected (`run_subprocess`, `run_glab`); this module never shells
out or reaches the network on its own, so the tests never do either.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Sequence

from . import checkouts, collectors
from .keepcurrent import _git, _preflight_clean, _run, iid_of
from .models import ReviewThread, WorkItem
from .runner import NeedsInputError, RunnerError

EXECUTOR = "address-feedback"

_FETCH_TIMEOUT = 120
_TAIL_LINES = 15
# The report the claude run leaves in the worktree root. Untracked, and the
# preflight clean wipes it before every run, so yesterday's can never be read
# as today's.
_REPORT_NAME = ".worksweep-feedback.json"
# How much of a reviewer's comment reaches the prompt / the Discord post. Long
# enough to carry the ask, short enough that ten threads still fit.
_NOTE_MAX = 600
_SHORT_NOTE_MAX = 90


@dataclass(frozen=True)
class FeedbackResult:
    iid: int                       # the authored MR whose threads were answered
    addressed: int = 0             # threads fixed with a commit + a reply
    replied: int = 0               # threads answered with words only
    escalated: tuple = ()          # short lines Chandler has to make a call on
    result_sha: str = ""           # origin/<branch> as this run left it
    already_answered: bool = False  # nothing was waiting by the time we ran


def _fetch_threads(run_glab: Callable, repo: str, iid: int) -> tuple:
    """Every page of this MR's discussions.

    Paged for the same reason the sweep probe is: GitLab files every system
    note as its own discussion, so on a busy MR the reviewer's actual question
    is not on page 1, and an unpaged read would decide nothing is waiting.
    """
    try:
        return collectors.discussions_pages(
            lambda page: run_glab(
                ["api", collectors.discussions_path(repo, iid, page)]))
    except Exception as e:
        raise RunnerError(f"could not read !{iid}'s threads: "
                          f"{type(e).__name__}: {e}")


# --- the prompt -------------------------------------------------------------
#
# Inline, like keepcurrent's conflict-resolver prompt: the contract an
# unattended claude run works under belongs in this repo's test suite, not in
# a skill file that nothing here can pin.

_PROMPT = """You are answering review feedback on merge request !{iid} in \
{repo}. Its branch `{branch}` is already checked out here and current with \
origin.

These are the threads where a reviewer had the last word. Nobody else is \
handling them:

{threads}

Take each thread on its own and pick exactly ONE of three outcomes:

1. FIXABLE -- you can see the change being asked for and it is not a judgment \
call. Make the change, commit it, and reply on that thread with \
`addressed in <short-sha>` (add one sentence if the change needs explaining).
2. QUESTION -- the reviewer asked something rather than asked for a change. \
Reply on the thread with the answer. No commit.
3. ESCALATE -- it is a judgment call, you disagree with the reviewer, or you \
cannot tell which of the three this is. Do NOT reply; record it as escalated \
with one line saying what the call is. Uncertainty always lands here: a weak \
reply posted under Chandler's name cannot be taken back, and a thread left \
for him costs nothing.

Reply to a thread with:

    glab api "projects/{project}/merge_requests/{iid}/discussions/<thread-id>/notes" \
-X POST -f body="<your reply>"

Before you push, pb-www hygiene:

- If your fix touched anything under `www/home/scss/*`, run \
`maintenance/compile-css` from the checkout root and commit the regenerated \
CSS. Never hand-edit compiled CSS.
- If your fix changed any CSS or JS the site serves, bump `$script_version` in \
`www/home/php/templates/tab_bar_common_logic.php` -- without it reviewers get \
the cached asset and see no change at all.
- Push the branch: `git push origin {branch}`.
- If the MR description carries an "Available on" dev link (read it with \
`glab api "projects/{project}/merge_requests/{iid}" --jq .description`), sync \
that dev box to the pushed branch, so the URL the reviewer was handed shows \
the fix.

Then write your report to `{report}` in the checkout root, and do NOT commit \
that file:

    {{"addressed": [{{"thread": "<thread-id>", "sha": "<short-sha>"}}],
     "replied": ["<thread-id>"],
     "escalated": [{{"thread": "<thread-id>", "reason": "<one line>"}}]}}

Every thread listed above must appear in exactly one of those three lists, and \
only list one under `addressed` or `replied` if you really did post the reply: \
afterwards Python re-reads the threads and checks that each thread you claim \
now carries Chandler's own reply as its last note, and that origin/{branch} \
moved whenever you claimed a commit. A report that does not match reality \
fails the run.

Do not touch any thread that is not listed above. Do not change the merge \
request's title, description, reviewers or state. Do not merge anything."""


def _thread_block(threads: Sequence[ReviewThread]) -> str:
    out = []
    for i, t in enumerate(threads, 1):
        body = (t.last_note or "").strip()[:_NOTE_MAX]
        indented = "\n".join(f"    {ln}" for ln in body.splitlines()) or "    (no text)"
        out.append(f"[{i}] thread-id `{t.id}` -- last word: {t.last_author}\n"
                   f"{indented}")
    return "\n\n".join(out)


def render_prompt(repo: str, iid: int, branch: str,
                  threads: Sequence[ReviewThread]) -> str:
    return _PROMPT.format(repo=repo, iid=int(iid), branch=branch,
                          project=collectors._project(repo),
                          threads=_thread_block(threads),
                          report=_REPORT_NAME)


# --- the run ----------------------------------------------------------------

def execute(item: WorkItem, cfg,
            run_subprocess: Callable = None,
            run_glab: Callable = None) -> FeedbackResult:
    """Answer the threads waiting on Chandler in one MR. See the module
    docstring for the three-outcome contract with the runner."""
    if run_subprocess is None or run_glab is None:
        raise RunnerError("address-feedback executor is wired without a "
                          "subprocess/glab edge")
    iid = iid_of(item)
    branch = item.branch
    if not branch:
        raise RunnerError(f"no source branch recorded for !{iid} "
                          f"(WorkItem.branch was not set by the assessor)")

    checkout = checkouts.worktree_for(cfg, item.repo, EXECUTOR, run_subprocess)
    # Reused worktree: a claim that timed out mid-run can leave it dirty, and
    # can leave its own report file behind. Both are wiped here, so nothing
    # from a previous run can be mistaken for this one's work.
    _preflight_clean(run_subprocess, checkout)
    report_path = os.path.join(checkout, _REPORT_NAME)
    _forget(report_path)

    _git(run_subprocess, checkout, ["fetch", "origin", branch],
         timeout=_FETCH_TIMEOUT)
    _git(run_subprocess, checkout, ["checkout", "-B", branch,
                                    f"origin/{branch}"])
    pre_remote = _remote_sha(run_subprocess, checkout, branch)

    # The sweep's snapshot is minutes to hours old. Ask GitLab again: the
    # reviewer may have answered themselves, or closed the thread, and running
    # a claude pass over stale threads would post replies nobody is waiting for.
    before = collectors.unaddressed_threads(
        _fetch_threads(run_glab, item.repo, iid), cfg.username)
    if not before:
        return FeedbackResult(iid=iid, result_sha=pre_remote,
                              already_answered=True)

    _claude(run_subprocess, cfg, checkout,
            render_prompt(item.repo, iid, branch, before))
    report = _read_report(report_path, iid)

    # A thread belongs to exactly one outcome. A run that lists the same one
    # twice (fixed it AND was unsure about it) would otherwise report a tally
    # bigger than the number of threads it was given, and the tally is the
    # whole report -- so the strongest claim wins and the rest are dropped.
    addressed = _ids(report.get("addressed"))
    replied = [t for t in _ids(report.get("replied")) if t not in addressed]
    handled = set(addressed) | set(replied)
    escalated = _escalations(report.get("escalated"), before, handled)
    _verify(run_subprocess, run_glab, cfg, item.repo, iid, branch, checkout,
            before, addressed, replied, pre_remote)

    if not addressed and not replied:
        if escalated:
            # Not a failure -- a question. The runner routes this to
            # `needs-input` with a ❓ instead of `error` with a ⚠️.
            raise NeedsInputError(
                f"!{iid}: {len(escalated)} thread"
                f"{'s' if len(escalated) != 1 else ''} need your call — "
                + "; ".join(escalated))
        raise RunnerError(
            f"the address-feedback run on !{iid} accounted for none of its "
            f"{len(before)} waiting thread"
            f"{'s' if len(before) != 1 else ''} — nothing was answered and "
            f"nothing was escalated")

    post_remote = (_remote_sha(run_subprocess, checkout, branch)
                   if addressed else pre_remote)
    return FeedbackResult(iid=iid, addressed=len(addressed),
                          replied=len(replied), escalated=tuple(escalated),
                          result_sha=post_remote)


def _claude(run_subprocess: Callable, cfg, checkout: str, prompt: str) -> None:
    """One unattended pass, on cfg.runner_timeout (1800s by default).

    That cap is not free-standing: the runner reaps a non-implement claim at
    45 minutes (runner.STALE_RUNNING_MINUTES), so a longer per-executor budget
    would get healthy runs killed mid-flight and leave half-posted replies.
    """
    timeout = int(getattr(cfg, "runner_timeout", 1800) or 1800)
    try:
        proc = _run([cfg.claude_bin, "-p", prompt], run_subprocess,
                    cwd=checkout, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"the address-feedback run timed out after "
                          f"{timeout}s")
    if proc.returncode != 0:
        out = f"{proc.stderr or ''}{proc.stdout or ''}"
        raise RunnerError(f"the address-feedback run failed: {_tail(out)}")


def _verify(run_subprocess: Callable, run_glab: Callable, cfg, repo: str,
            iid: int, branch: str, checkout: str,
            before: Sequence[ReviewThread], addressed: List[str],
            replied: List[str], pre_remote: str) -> None:
    """Trust nothing the run reported until GitLab and git agree with it.

    Three checks, in the order a lie is most likely: a thread it was never
    given, a reply it never posted, a commit it never pushed. Each one would
    otherwise reach Discord as a completed item Chandler had no reason to
    re-check.
    """
    known = {t.id for t in before}
    claimed = list(addressed) + list(replied)
    stray = [t for t in claimed if t not in known]
    if stray:
        raise RunnerError(
            f"the address-feedback run on !{iid} claims thread(s) it was "
            f"never given: {', '.join(stray)}")

    after = {t.id: t for t in collectors.parse_threads(
        _fetch_threads(run_glab, repo, iid))}
    for tid in claimed:
        thread = after.get(tid)
        if thread is None:
            raise RunnerError(f"the address-feedback run on !{iid} claims "
                              f"thread {tid}, which the MR no longer has")
        if thread.last_author != cfg.username:
            raise RunnerError(
                f"the address-feedback run on !{iid} claims it answered "
                f"thread {tid}, but its last word is still "
                f"{thread.last_author or 'nobody'}'s")

    if addressed:
        _git(run_subprocess, checkout, ["fetch", "origin", branch],
             timeout=_FETCH_TIMEOUT)
        if _remote_sha(run_subprocess, checkout, branch) == pre_remote:
            raise RunnerError(
                f"the address-feedback run on !{iid} claims {len(addressed)} "
                f"commit(s), but origin/{branch} never moved — the reply "
                f"points at a sha the reviewer cannot see")


def _remote_sha(run_subprocess: Callable, checkout: str, branch: str) -> str:
    return _git(run_subprocess, checkout,
                ["rev-parse", f"origin/{branch}"], allow_fail=True).strip()


def _read_report(path: str, iid: int) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RunnerError(f"the address-feedback run on !{iid} left no report "
                          f"at {_REPORT_NAME} — no way to tell what, if "
                          f"anything, it posted")
    except (OSError, ValueError) as e:
        raise RunnerError(f"the address-feedback run on !{iid} wrote an "
                          f"unreadable report: {e}")
    if not isinstance(data, dict):
        raise RunnerError(f"the address-feedback run on !{iid} wrote a report "
                          f"that is not an object")
    return data


def _forget(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _ids(raw) -> List[str]:
    """Thread ids from a report list, accepting `"t1"` or `{"thread": "t1"}`."""
    out = []
    for entry in (raw or []):
        tid = entry.get("thread") if isinstance(entry, dict) else entry
        if isinstance(tid, str) and tid and tid not in out:
            out.append(tid)
    return out


def _escalations(raw, before: Sequence[ReviewThread],
                 handled: set = frozenset()) -> List[str]:
    """One short line per escalation: who is waiting, roughly what they said,
    and the call this run refused to make. Chandler reads these on his phone,
    so they carry the ask, not a thread id he would have to go look up."""
    by_id = {t.id: t for t in before}
    out, seen = [], set()
    for entry in (raw or []):
        if isinstance(entry, dict):
            tid, reason = entry.get("thread"), entry.get("reason") or ""
        else:
            tid, reason = entry, ""
        if tid in handled or (tid and tid in seen):
            continue
        seen.add(tid)
        thread = by_id.get(tid)
        who = thread.last_author if thread else (tid or "a thread")
        quote = _squash((thread.last_note if thread else ""), _SHORT_NOTE_MAX)
        line = f"{who}: “{quote}”" if quote else f"{who}"
        if reason:
            line += f" — {_squash(reason, _SHORT_NOTE_MAX)}"
        out.append(line)
    return out


def _squash(text: str, limit: int) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit - 1] + "…"


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


# --- reporting --------------------------------------------------------------

def tally(result: FeedbackResult) -> str:
    """The one honest sentence: what was answered, what was not, and nothing
    about thread state -- this executor has no opinion on that."""
    base = (f"{result.addressed} addressed, {result.replied} replied, "
            f"{len(result.escalated)} escalated")
    if result.already_answered:
        return base + " — threads already answered"
    return base


def done_message(result: FeedbackResult) -> str:
    msg = f"💬 !{result.iid} — {tally(result)}"
    for line in result.escalated:
        msg += f"\nneeds you: {line}"
    return msg
