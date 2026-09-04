"""M3 executor runner: drain approved magi-review items, one at a time.

Pure state-machine functions (pick/claim/complete/fail/reap) are unit-tested;
the subprocess edge (execute) and the CLI glue live in run_once/execute. The
lockfile guarantees single-flight across overlapping launchd fires."""
from __future__ import annotations

import dataclasses
import datetime
import glob as _glob
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

from .formatter import DISCORD_MAX_CHARS, _truncate_bytes
from .models import QueueRecord, WorkItem, magi_item_id
from .queue import null_lock as _null_lock

STALE_RUNNING_MINUTES = 45
# M4 Task G: an implement claim legitimately runs for `implement_timeout`
# (90 min default), so it gets its own, longer reap window: timeout + 15 min.
IMPLEMENT_REAP_GRACE_SECONDS = 900
# 2026-08-26 (magi 0.2.4): the rebuttal round is no longer skipped for
# unattended runs -- it is performed mechanically, adding up to ~36 min. A full
# tribunal now legitimately takes 40-60 minutes, which does not fit the generic
# 45-minute window, and reaping a healthy one both loses the work and
# re-proposes it to burn another hour. Same treatment implement already gets:
# its own budget, and a reap window that is that budget plus a grace margin.
MAGI_TIMEOUT_SECONDS = 4500
MAGI_REAP_GRACE_SECONDS = 900
# 2026-09-04 (tiered feedback): a substantive feedback fix runs the implement
# lane's ceremony, so address-feedback gets its own budget and reap window too.
FEEDBACK_TIMEOUT_SECONDS = 3600
FEEDBACK_REAP_GRACE_SECONDS = 900
_ERROR_SUMMARY_MAX = 500
_LOCK_DEFAULT = os.path.expanduser("~/.worksweep/runner.lock")
_IMPLEMENT_LOCK_NAME = "runner-implement.lock"
_ADDRESS_FEEDBACK_LOCK_NAME = "runner-address-feedback.lock"
_CONSULT_LOCK_NAME = "runner-consult.lock"
# The `why` on a row worksweep approved for itself. The trailing
# "(auto)" is the marker the digest and the dashboard both render, so a
# human can see the row was never ✅'d by hand.
AUTO_MAGI_WHY = "post-feedback re-review (auto)"

_MAGI = "magi-review"
_IMPLEMENT = "implement"
_KEEP_CURRENT = "keep-current"
_PARK = "park"
_ADDRESS_FEEDBACK = "address-feedback"
_ALL_EXECUTORS = (_MAGI, _IMPLEMENT, _KEEP_CURRENT, _PARK,
                  _ADDRESS_FEEDBACK)
# Executors that may have at most ONE claim in flight across the whole queue.
# magi-review is read-only and cheap; implement writes to GitLab and occupies
# a dev box, so a second one must wait even though it has its own lock file.
# keep-current is also a write (a merge + push), but it's a short git op --
# it shares the magi-review pass/lock (see _run_magi_pass) rather than
# getting its own lock file, and that pass only ever runs ONE claim per
# invocation regardless, so no separate single-flight entry is needed here.
_SINGLE_FLIGHT = (_IMPLEMENT,)


class RunnerError(RuntimeError):
    """Executor failure with a human-postable summary."""


class NeedsInputError(RunnerError):
    """The executor stopped to ask the human a question (M4 Task G).

    A subclass of RunnerError so any handler that only knows about failures
    still catches it (never uncaught), but run_once checks for it FIRST and
    routes it to the `needs-input` status + a ❓ post instead of ⚠️/error:
    a question is not a failure and must not be silently retried.
    """


def _replace(rec: QueueRecord, now: str, **item_changes) -> QueueRecord:
    return QueueRecord(number=rec.number, first_seen=rec.first_seen,
                       last_seen=now,
                       item=dataclasses.replace(rec.item, **item_changes))


def pick_claim(records: List[QueueRecord],
               executors: Tuple[str, ...] = _ALL_EXECUTORS
               ) -> Optional[QueueRecord]:
    """Lowest-numbered approved record whose executor is in `executors`.

    Single-flight kinds (implement) are skipped entirely while one of their
    own is already `running` — a second implement must not claim a second dev
    box or a second `/rubric:do` while the first is mid-flight. magi-review is
    unaffected by a running implement (and vice versa): the two kinds hold
    separate lock files and may run one each per pass.

    A BRANCH already being worked by any running record is also skipped, no
    matter which executor holds it. The lock files make each executor
    single-flight against itself; they say nothing about two DIFFERENT
    executors targeting the same branch, and since address-feedback got its
    own lock those two can genuinely overlap. Two runs on one branch means the
    second one's `checkout -B` lands in a worktree the first is still working
    in. Items with no branch (magi-review, triage) are unaffected -- an empty
    branch is the absence of one, not a shared resource.
    """
    running_kinds = {r.item.executor for r in records
                     if r.item.status == "running"}
    running_branches = {r.item.branch for r in records
                        if r.item.status == "running" and r.item.branch}
    candidates = [r for r in records
                  if r.item.status == "approved"
                  and r.item.executor in executors
                  and not (r.item.executor in _SINGLE_FLIGHT
                           and r.item.executor in running_kinds)
                  and r.item.branch not in running_branches]
    return min(candidates, key=lambda r: r.number) if candidates else None


def claim(records: List[QueueRecord], number: int, now: str,
          dev_box: str = "") -> List[QueueRecord]:
    """Flip `number` to running. `dev_box` stamps the claimed dev slot BEFORE
    any long work starts, so a concurrent sweep's classify() already sees the
    box as taken (devslots.classify treats a claimed box as `live`)."""
    changes = {"status": "running", "claimed_at": now}
    if dev_box:
        changes["dev_box"] = dev_box
    return [_replace(r, now, **changes) if r.number == number else r
            for r in records]


def complete(records: List[QueueRecord], number: int, result_sha: str,
             report_path: str, now: str, mr_iid: int = 0,
             done_reason: str = "executor-completed") -> List[QueueRecord]:
    """Flip `number` to done. `done_reason` says WHY it is finished -- usually
    because the executor did the work, but "mr-merged" when the MR ended and
    took the work with it (models.WorkItem documents the enum)."""
    changes = dict(status="done", done_reason=done_reason,
                   result_sha=result_sha, report_path=report_path)
    if mr_iid:
        changes["mr_iid"] = mr_iid
    return [_replace(r, now, **changes) if r.number == number else r
            for r in records]


def needs_input(records: List[QueueRecord], number: int, question: str,
                now: str) -> List[QueueRecord]:
    """Park `number` on the human's answer. Terminal-ish: reconcile keeps it
    and never re-proposes it; only a Discord ✅ flips it back to approved."""
    return [_replace(r, now, status="needs-input",
                     error_summary=(question or "")[:_ERROR_SUMMARY_MAX])
            if r.number == number else r for r in records]


def fail(records: List[QueueRecord], number: int, error_summary: str,
         now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="error",
                     error_summary=(error_summary or "")[:_ERROR_SUMMARY_MAX])
            if r.number == number else r for r in records]


def reap_stale(records: List[QueueRecord], now: str,
               implement_timeout: int = 5400,
               magi_timeout: int = MAGI_TIMEOUT_SECONDS,
               feedback_timeout: int = FEEDBACK_TIMEOUT_SECONDS
               ) -> Tuple[List[QueueRecord], List[QueueRecord]]:
    """Flip `running` claims that outlived their executor's window to error.

    Four windows, because the executors have very different runtimes:

      implement         `implement_timeout` + 15 min  (a 90-min pipeline run)
      magi-review       `magi_timeout` + 15 min       (a 40-60 min full tribunal)
      address-feedback  `feedback_timeout` + 15 min   (a tiered fix round that
                                                       may dispatch the
                                                       implementer + a tribunal)
      everything else            45 min               (short ops, one claude pass)

    Each long-running executor gets a window WIDER than its own budget, so the
    reap only ever catches a claim that is genuinely stuck rather than one
    that is merely slow. Reaping a healthy run loses the work AND re-proposes
    it, which for magi means burning another hour of tokens on the next fire.

    The 45-minute default is deliberately kept for the rest (keep-current,
    park, consult): short ops, and a stuck one should not sit on its branch
    for an extra half hour.
    """
    implement_limit = implement_timeout + IMPLEMENT_REAP_GRACE_SECONDS
    magi_limit = magi_timeout + MAGI_REAP_GRACE_SECONDS
    feedback_limit = feedback_timeout + FEEDBACK_REAP_GRACE_SECONDS
    updated, reaped = [], []
    for r in records:
        if r.item.executor == _IMPLEMENT:
            limit = implement_limit
        elif r.item.executor == _MAGI:
            limit = magi_limit
        elif r.item.executor == _ADDRESS_FEEDBACK:
            limit = feedback_limit
        else:
            limit = STALE_RUNNING_MINUTES * 60
        if r.item.status == "running" and _stale(r.item.claimed_at, now, limit):
            nr = _replace(r, now, status="error",
                          error_summary="stale claim reaped")
            updated.append(nr)
            reaped.append(nr)
        else:
            updated.append(r)
    return updated, reaped


def _stale(claimed_at: str, now: str,
           limit_seconds: int = STALE_RUNNING_MINUTES * 60) -> bool:
    try:
        t = datetime.datetime.fromisoformat(claimed_at)
        n = datetime.datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return True   # unparseable claim time -> reap (running must be provable)
    if (t.tzinfo is None) != (n.tzinfo is None):
        return True
    return (n - t) > datetime.timedelta(seconds=limit_seconds)


def _lock_holder_pid(path: str) -> Optional[int]:
    """Read PID from lock file. Returns None if file missing or PID unparseable."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def acquire_lock(path: str) -> bool:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if attempt == 2:
                return False
            pid = _lock_holder_pid(path)
            if pid is None:
                # Lock file vanished (or was unreadable) between FileExistsError and
                # the read — another process called release_lock. Loop to retry.
                continue
            try:
                os.kill(pid, 0)
                return False           # holder alive
            except ProcessLookupError:
                # TOCTOU: Two concurrent stale-breakers can race; one removes, the
                # other fails. Accepted for single mini + 10-min launchd cadence.
                try:
                    os.remove(path)    # stale -> break it, retry once
                except FileNotFoundError:
                    pass
            except PermissionError:
                return False           # alive under another uid
    return False


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _iid_of(item: WorkItem) -> int:
    m = re.search(r"/merge_requests/(\d+)", item.web_url)
    if not m:
        raise RunnerError(f"cannot find MR iid in web_url: {item.web_url!r}")
    return int(m.group(1))


def find_report(checkout: str, iid: int) -> Optional[str]:
    hits = _glob.glob(os.path.join(checkout, ".magi",
                                   f"tribunal-report-mr-{iid}-*.md"))
    return max(hits, key=os.path.getmtime) if hits else None


def extract_verdict(report_path: str) -> str:
    try:
        with open(report_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    out, capturing = [], False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = line.lower().startswith("## verdict")
            continue
        if capturing and line.strip():
            out.append(line)
        if len(out) >= 12:
            break
    return "\n".join(out)


def execute(item: WorkItem, cfg,
            run_subprocess: Callable = subprocess.run) -> Tuple[str, str]:
    """Fetch + run `claude -p "/magi:magi-review !<iid>"` in the repo checkout.
    Returns (result_sha, report_path). Raises RunnerError on any failure."""
    checkout = os.path.join(cfg.checkouts_root, item.repo)
    if not os.path.isdir(checkout):
        raise RunnerError(f"no checkout for {item.repo} at {checkout}")
    iid = _iid_of(item)
    timeout = _magi_timeout(cfg)
    try:
        fetch = run_subprocess(["git", "-C", checkout, "fetch", "origin"],
                               capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RunnerError("git fetch timed out")
    if fetch.returncode != 0:
        raise RunnerError(f"git fetch failed: {(fetch.stderr or '').strip()[-300:]}")
    try:
        # `--draft-findings` stages pending review comments -- a channel to
        # OTHER authors, so it applies only to review-requested MRs
        # (kind="review_request"). On an AUTHORED MR (kind="mr": assessed or
        # post-feedback-chained) the findings' consumer is us -- the report
        # alone is the output, and the fixes happen in a fix round (Chandler,
        # 2026-08-26: "we don't review our own MRs, we just fix the
        # problems"). `--no-rebuttal` is gone as of magi 0.2.4 -- the rebuttal
        # round is performed mechanically now, and passing a flag it no longer
        # defines is an unknown-argument error, not a no-op.
        drafts = (" --draft-findings"
                  if item.kind in ("review_request", "re_review") else "")
        proc = run_subprocess(
            [cfg.claude_bin, "-p", f"/magi:magi-review !{iid}{drafts}"],
            cwd=checkout, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,  # claude -p blocks/exits 1 waiting on a non-TTY stdin
            timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"magi-review !{iid} exceeded {timeout}s")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RunnerError(f"magi-review !{iid} exited {proc.returncode}: {tail}")
    report = find_report(checkout, iid)
    if report is None:
        raise RunnerError(f"magi-review !{iid} produced no tribunal report")
    return item.sha, report


def _queue_lock(deps):
    """The cross-process queue lock, or an inert one when unwired.

    Scope is the point: it wraps a load -> mutate -> save cycle and NEVER an
    executor run. A pass holding it across a 30-minute claude run would stall
    the sweep, intake and every dashboard tap -- the same mistake that pulled
    address-feedback out of the shared runner lock in the first place.
    """
    return deps.get("queue_lock", _null_lock)()


def _apply_to_fresh(deps, cfg, number: int,
                    apply_fn: Callable[[List[QueueRecord]], List[QueueRecord]]
                    ) -> Optional[List[QueueRecord]]:
    """Re-load the queue fresh and apply `apply_fn` (complete/fail) to `number`
    in that fresh state, then save it.

    execute() can run for up to 30 minutes; the in-memory `records` captured
    before execute() started is a stale snapshot by the time it returns, and
    saving it back would clobber any concurrent write (e.g. intake approving
    a different item) that landed mid-run. Re-loading right before the
    post-execute save closes that window.

    If `number` no longer exists in the fresh load (the queue was rewritten
    out from under us), nothing is saved for it and a ⚠️ names the lost
    record; returns None so the caller can skip any follow-up post that would
    otherwise claim a result was recorded.
    """
    with _queue_lock(deps):
        fresh = deps["load"]()
        if not any(r.number == number for r in fresh):
            lost = True
            updated = None
        else:
            lost = False
            updated = apply_fn(fresh)
            deps["save"](updated)
    if lost:
        # Posted outside the lock: Discord is a network call, and nothing else
        # should wait on it.
        _post(deps, cfg, f"⚠️ Worksweep runner: #{number} vanished from the "
                         f"queue before its result could be recorded")
    return updated


# The pass families one runner invocation may serve, in the order they run.
# "short" = magi-review/keep-current/park plus the stale-claim reap.
RUN_FAMILIES = ("short", "feedback", "consult", "implement")


def run_once(cfg, deps: Dict[str, Callable], lock_path: str = _LOCK_DEFAULT,
             implement_lock_path: Optional[str] = None,
             address_feedback_lock_path: Optional[str] = None,
             consult_lock_path: Optional[str] = None,
             families: Optional[Tuple[str, ...]] = None) -> int:
    """One runner pass: reap stale claims, then run at most ONE item from each
    of the three executor families.

    Each family holds its own lock file, so a long run never starves a short
    queue and an overlapping launchd fire still can't double-run any of them.
    The short-op pass (magi-review, keep-current, park) also owns the stale
    reap, which is precisely why address-feedback was moved OUT of it: that
    pass would otherwise sit on the lock for the length of a claude run, and
    nothing — not even the reap that exists to clean up stuck claims — could
    make progress for 30 minutes.

    Both extra lock paths default to siblings of `lock_path`, so a test
    passing a tmp path keeps ALL THREE inside its tmp dir, never touching
    ~/.worksweep.
    """
    def _sibling(name: str) -> str:
        return os.path.join(os.path.dirname(lock_path) or ".", name)

    if implement_lock_path is None:
        implement_lock_path = _sibling(_IMPLEMENT_LOCK_NAME)
    if address_feedback_lock_path is None:
        address_feedback_lock_path = _sibling(_ADDRESS_FEEDBACK_LOCK_NAME)
    if consult_lock_path is None:
        consult_lock_path = _sibling(_CONSULT_LOCK_NAME)
    families = tuple(families or RUN_FAMILIES)
    rcs = []
    # Short passes FIRST, implement LAST: passes run sequentially inside one
    # invocation and launchd will not start a new instance while this one
    # lives, so an hours-long implement drain placed earlier would starve
    # every other family for its whole duration (2026-09-01: a queued consult
    # sat "requested" for an hour behind the first drain). The real decoupling
    # is running implement as its OWN launchd job (`--families implement`,
    # etc/mini has the plist); the ordering protects the combined default.
    if "short" in families:
        rcs.append(_guarded_pass(cfg, deps, _MAGI, _run_magi_pass, lock_path))
    if "feedback" in families:
        rcs.append(_guarded_pass(cfg, deps, _ADDRESS_FEEDBACK,
                                 _run_address_feedback_pass,
                                 address_feedback_lock_path))
    if "consult" in families:
        rcs.append(_guarded_pass(cfg, deps, "consult", _run_consult_pass,
                                 consult_lock_path))
    if "implement" in families:
        rcs.append(_guarded_pass(cfg, deps, _IMPLEMENT, _run_implement_pass,
                                 implement_lock_path))
    return next((rc for rc in rcs if rc), 0)


def _guarded_pass(cfg, deps: Dict[str, Callable], kind: str,
                  fn: Callable, lock_path: str) -> int:
    """Last-resort net: a pass may not take the runner down with it.

    Every *expected* failure is already handled inside the passes; this
    catches the unexpected (a queue write that fails with ENOSPC, a dep that
    isn't callable) so the OTHER executor still gets its turn and Discord
    still hears about it. Silence is the one outcome that is never allowed.
    """
    try:
        return fn(cfg, deps, lock_path)
    except Exception as e:
        _post(deps, cfg, f"⚠️ Worksweep runner: the {kind} pass crashed — "
                         f"{type(e).__name__}: {e}")
        return 1


def _run_magi_pass(cfg, deps: Dict[str, Callable], lock_path: str) -> int:
    """One claim from magi-review, keep-current, park OR address-feedback
    (lowest number wins across all four — see pick_claim). They share this
    pass/lock deliberately: each is a short op (a git fetch/merge/push, a
    branch sync plus one API write, or one bounded claude pass over an MR's
    threads), not worth its own lock file, and this pass only ever runs one
    claim per invocation either way."""
    if not acquire_lock(lock_path):
        return 0    # another runner is live — that's fine, not an error
    try:
        now = deps["now"]()
        with _queue_lock(deps):
            records = deps["load"]()
            records, reaped = reap_stale(
                records, now, implement_timeout=_implement_timeout(cfg),
                magi_timeout=_magi_timeout(cfg),
                feedback_timeout=_feedback_timeout(cfg))
            if reaped:
                deps["save"](records)
            target = pick_claim(records, (_MAGI, _KEEP_CURRENT, _PARK))
            if target is not None:
                records = claim(records, target.number, now)
                deps["save"](records)
        # Discord posts and the executor itself run OUTSIDE the lock.
        for r in reaped:
            _post(deps, cfg, f"⚠️ Worksweep runner: reaped stale claim "
                             f"#{r.number} ({r.item.repo} {r.item.id})")
        if target is None:
            return 0
        if target.item.executor == _KEEP_CURRENT:
            return _run_keep_current_claim(cfg, deps, target)
        if target.item.executor == _PARK:
            return _run_park_claim(cfg, deps, target)
        try:
            result_sha, report_path = deps["execute"](target.item, cfg)
        except RunnerError as e:
            _fail_and_post(deps, cfg, target.number, str(e), _MAGI)
            return 1
        except Exception as e:
            # Non-RunnerError failures (e.g. FileNotFoundError when `claude`/git
            # is missing from launchd's minimal PATH) must still flip the claim
            # to error and post — otherwise the item is stuck `running` silently
            # until the 45-min reap, with no signal anything went wrong.
            _fail_and_post(deps, cfg, target.number,
                           f"{type(e).__name__}: {e}", _MAGI)
            return 1
        updated = _apply_to_fresh(
            deps, cfg, target.number,
            lambda fresh: complete(fresh, target.number, result_sha, report_path,
                                   deps["now"]()))
        if updated is not None and target.item.kind == "re_review":
            # The re-review is done at this head: record it so the sensor's
            # sha comparison resolves the row instead of re-proposing it.
            record_edge = deps.get("record_reviewed")
            if record_edge is not None:
                try:
                    record_edge(f"{target.item.repo}!{_iid_of(target.item)}",
                                result_sha or target.item.sha)
                except Exception as e:
                    print(f"worksweep: could not record reviewed sha for "
                          f"#{target.number}: {type(e).__name__}: {e}",
                          file=sys.stderr)
        if updated is not None:
            verdict = extract_verdict(report_path) if report_path else ""
            msg = (f"🧙 magi-review done — #{target.number} {target.item.repo} "
                   f"<{target.item.web_url}>\n"
                   + (f"```\n{verdict}\n```\n" if verdict else "")
                   + (f"report: `{report_path}`" if report_path
                      else "(no report file found)"))
            _post(deps, cfg, msg)
        return 0
    finally:
        release_lock(lock_path)


def _run_keep_current_claim(cfg, deps: Dict[str, Callable],
                            target: QueueRecord) -> int:
    """The keep-current half of the shared magi/keep-current pass. Called
    with the claim already saved as `running` (by _run_magi_pass, same as
    the magi-review path) -- every exit below ends in a queue status AND a
    Discord post, since this executor pushes to GitLab and rewrites a dev
    box's checkout."""
    number = target.number
    if "execute_keep_current" not in deps:
        _fail_and_post(deps, cfg, number,
                       "keep-current executor is not wired into this runner "
                       "(no execute_keep_current dep)", _KEEP_CURRENT)
        return 1
    try:
        result = deps["execute_keep_current"](target.item, cfg)
    except RunnerError as e:
        _fail_and_post(deps, cfg, number, str(e), _KEEP_CURRENT)
        return 1
    except Exception as e:
        _fail_and_post(deps, cfg, number, f"{type(e).__name__}: {e}",
                       _KEEP_CURRENT)
        return 1
    merged = bool(getattr(result, "mr_merged", False))
    updated = _apply_to_fresh(
        deps, cfg, number,
        lambda fresh: complete(fresh, number, result.result_sha, "",
                               deps["now"](),
                               done_reason=("mr-merged" if merged
                                            else "executor-completed")))
    if updated is not None:
        _post(deps, cfg, _keep_current_done_message(result))
    return 0


def _run_park_claim(cfg, deps: Dict[str, Callable],
                    target: QueueRecord) -> int:
    """The park half of the shared magi/keep-current/park pass.

    Called with the claim already saved as `running`. Like keep-current, every
    exit ends in BOTH a queue status and a Discord post: this executor takes
    over a dev box and rewrites an MR description, so a silent failure would
    leave a box occupied and nobody told.
    """
    number = target.number
    if "execute_park" not in deps:
        _fail_and_post(deps, cfg, number,
                       "park executor is not wired into this runner "
                       "(no execute_park dep)", _PARK)
        return 1
    try:
        result = deps["execute_park"](target.item, cfg)
    except RunnerError as e:
        _fail_and_post(deps, cfg, number, str(e), _PARK)
        return 1
    except Exception as e:
        _fail_and_post(deps, cfg, number, f"{type(e).__name__}: {e}", _PARK)
        return 1
    updated = _apply_to_fresh(
        deps, cfg, number,
        lambda fresh: complete(fresh, number, result.result_sha, "",
                               deps["now"]()))
    if updated is not None:
        from . import park as _park       # local: park imports implementer
        _post(deps, cfg, _park.done_message(result))
    return 0


def _run_address_feedback_pass(cfg, deps: Dict[str, Callable],
                               lock_path: str) -> int:
    """At most one address-feedback item, on its own lock.

    It shared the short-op pass at first, on the theory that it was another
    quick git op. It is not: it runs an unattended claude pass that can take
    half an hour, and holding the short-op lock that long would block
    magi-review, keep-current, park and the stale-claim reap behind it.
    Mirrors _run_implement_pass: cheap pre-check, then the lock.
    """
    try:
        if pick_claim(deps["load"](), (_ADDRESS_FEEDBACK,)) is None:
            return 0
    except Exception as e:
        _post(deps, cfg, f"⚠️ Worksweep runner: could not read the queue for "
                         f"the address-feedback pass — {type(e).__name__}: {e}")
        return 1
    if not acquire_lock(lock_path):
        return 0    # another address-feedback run is live under this lock
    try:
        with _queue_lock(deps):
            records = deps["load"]()
            target = pick_claim(records, (_ADDRESS_FEEDBACK,))
            if target is not None:
                deps["save"](claim(records, target.number, deps["now"]()))
        if target is None:
            return 0                   # raced with another pass — fine
        return _run_address_feedback_claim(cfg, deps, target)
    finally:
        release_lock(lock_path)


def _run_consult_pass(cfg, deps: Dict[str, Callable], lock_path: str) -> int:
    """At most one Send-to-Fable consult, on its own lock.

    A consult is a bounded read-only claude pass over a PARKED question — the
    row stays `needs-input` throughout, so this pass deliberately bypasses
    pick_claim (which only sees `approved`) and never calls claim(): there is
    no status to flip, only the consult fields to advance. requested -> done
    (rec on the row + a 🔮 post) or requested -> error (⚠️ post, and the
    dashboard re-offers the button). A crash mid-run leaves "requested", which
    simply retries next pass — a consult is advisory and idempotent, so a
    retry costs tokens, never correctness.
    """
    execute = deps.get("execute_consult")
    if execute is None:
        return 0                       # not wired (old caller/tests) — inert
    try:
        parked = [r for r in deps["load"]()
                  if r.item.status == _NEEDS_INPUT_STATUS
                  and r.item.consult == "requested"]
    except Exception as e:
        _post(deps, cfg, f"⚠️ Worksweep runner: could not read the queue for "
                         f"the consult pass — {type(e).__name__}: {e}")
        return 1
    if not parked:
        return 0
    if not acquire_lock(lock_path):
        return 0                       # another consult is live — fine
    try:
        target = min(parked, key=lambda r: r.number)
        try:
            rec_text = execute(target.item, cfg)
        except RunnerError as e:
            _set_consult(deps, target.number, "error", "")
            _post(deps, cfg, _clamped(
                f"⚠️ Consult on #{target.number} failed — {e}"))
            return 1
        except Exception as e:
            _set_consult(deps, target.number, "error", "")
            _post(deps, cfg, _clamped(
                f"⚠️ Consult on #{target.number} crashed — "
                f"{type(e).__name__}: {e}"))
            return 1
        if _set_consult(deps, target.number, "done", rec_text):
            _post(deps, cfg, _clamped(
                f"🔮 Consult ready — #{target.number} {target.item.repo} "
                f"{target.item.id}\n> {rec_text}\n"
                f"Accept it on the dashboard to hand the ruling to the "
                f"executor."))
        return 0
    finally:
        release_lock(lock_path)


_NEEDS_INPUT_STATUS = "needs-input"


def _set_consult(deps, number: int, state: str, rec: str) -> bool:
    """Advance a row's consult fields, on the FRESH queue and only while the
    row is still parked: a question answered (approved, dismissed, signal-
    cleared) mid-consult must not grow a stale recommendation the human could
    accept against a row that no longer asks it."""
    with _queue_lock(deps):
        records = deps["load"]()
        hit = False
        out = []
        for r in records:
            if (r.number == number
                    and r.item.status == _NEEDS_INPUT_STATUS):
                out.append(_replace(r, deps["now"](), consult=state,
                                    consult_rec=rec))
                hit = True
            else:
                out.append(r)
        if hit:
            deps["save"](out)
        return hit


def _run_address_feedback_claim(cfg, deps: Dict[str, Callable],
                                target: QueueRecord) -> int:
    """The body of the address-feedback pass.

    Called with the claim already saved as `running`. Unlike its siblings this
    one has THREE outcomes, not two, and the middle one is why it cannot just
    copy _run_park_claim: `NeedsInputError` subclasses `RunnerError`, so an
    `except RunnerError` alone would record "I found only judgment calls" as a
    hard failure with a warning, instead of a question. It is caught FIRST,
    exactly as the implement pass does it.

    Every exit still ends in BOTH a queue status and a Discord post: this
    executor posts replies under Chandler's identity, so a silent outcome
    would leave him not knowing what went out in his name.
    """
    number = target.number
    if "execute_address_feedback" not in deps:
        _fail_and_post(deps, cfg, number,
                       "address-feedback executor is not wired into this "
                       "runner (no execute_address_feedback dep)",
                       _ADDRESS_FEEDBACK)
        return 1
    try:
        result = deps["execute_address_feedback"](target.item, cfg)
    except NeedsInputError as e:
        if _apply_to_fresh(
                deps, cfg, number,
                lambda fresh: needs_input(fresh, number, str(e),
                                          deps["now"]())) is not None:
            _post(deps, cfg, _clamped(f"❓ #{number} needs your input: {e}"))
        return 0        # a question is a handled outcome, not a failure
    except RunnerError as e:
        _fail_and_post(deps, cfg, number, str(e), _ADDRESS_FEEDBACK)
        return 1
    except Exception as e:
        _fail_and_post(deps, cfg, number, f"{type(e).__name__}: {e}",
                       _ADDRESS_FEEDBACK)
        return 1
    merged = bool(getattr(result, "mr_merged", False))
    updated = _apply_to_fresh(
        deps, cfg, number,
        lambda fresh: _chain_magi_review(
            complete(fresh, number, result.result_sha, "", deps["now"](),
                     done_reason=("mr-merged" if merged
                                  else "executor-completed")),
            target.item, result, deps["now"]()))
    if updated is not None:
        from . import feedback as _feedback   # local: feedback imports runner
        _post(deps, cfg, _clamped(_feedback.done_message(result)))
    return 0


def _chain_magi_review(records: List[QueueRecord], item: WorkItem, result,
                       now: str) -> List[QueueRecord]:
    """Queue a magi review of the commits this feedback run just pushed.

    Trigger is `addressed`, NOT a non-empty result_sha. Every completion
    carries a sha -- reply-only runs report the head they read, and the
    already-answered shortcut reports it before doing anything at all -- so
    keying on the sha would queue a review of untouched code on every pass.
    `addressed` counts threads fixed WITH a commit, and the executor's own
    verification already refused to report those unless origin/<branch>
    actually moved.

    Pre-approved, which is the one sanctioned bypass of the ✅ gate: the
    trigger is scoped to commits worksweep itself made, and magi-review is
    read-only plus draft comments. Being a RUNNABLE executor, it is claimable
    without a human step, so it cannot strand as an approved zombie.

    Appended to the SAME list the completion just produced, so both reach disk
    in one write: two saves would leave a window where the feedback row reads
    `done` and the review it earned does not exist yet.
    """
    if not result.addressed or not result.result_sha:
        return records
    magi_id = magi_item_id(item.repo, result.iid, result.result_sha)
    if any(r.item.id == magi_id for r in records):
        return records      # same head already queued -- never stack a second
    number = max((r.number for r in records), default=0) + 1
    return list(records) + [QueueRecord(
        number=number, first_seen=now, last_seen=now,
        item=WorkItem(schema_version=1, id=magi_id, repo=item.repo, kind="mr",
                      executor=_MAGI, risk="low", why=AUTO_MAGI_WHY,
                      web_url=item.web_url, sha=result.result_sha,
                      status="approved", title=item.title))]


# How many implement claims one pass may DRAIN back-to-back. A bound, not a
# budget: each claim is already time-boxed by its own executor, and the pass
# stops the moment nothing is approved. Before this, a nine-item batch spent
# up to 80 minutes purely waiting on the 10-minute launchd cadence between
# claims (2026-09-01).
_IMPLEMENT_DRAIN_MAX = 9


def _run_implement_pass(cfg, deps: Dict[str, Callable], lock_path: str) -> int:
    """Drain approved implement items back-to-back, one at a time, under the
    one lock. Every claim's exit is either a no-op (nothing approved / lock
    held) or ends in BOTH a queue status and a Discord post — this executor
    writes to GitLab and occupies a dev box, so a silent failure would leave
    a claimed box and a half-open branch with nobody told. A failed claim
    does not stop the drain: the next item deserves its run, and the failure
    already posted."""
    # Cheap pre-check before taking the lock (and before probing dev boxes over
    # ssh): the overwhelmingly common case is "nothing approved".
    try:
        if pick_claim(deps["load"](), (_IMPLEMENT,)) is None:
            return 0
    except Exception as e:
        _post(deps, cfg, f"⚠️ Worksweep runner: could not read the queue for "
                         f"the implement pass — {type(e).__name__}: {e}")
        return 1
    if not acquire_lock(lock_path):
        return 0    # an implement run is already live under this lock
    try:
        worst = 0
        for _ in range(_IMPLEMENT_DRAIN_MAX):
            rc = _run_one_implement_claim(cfg, deps)
            if rc is None:             # nothing approved any more — done
                break
            worst = worst or rc
        return worst
    finally:
        release_lock(lock_path)


def _run_one_implement_claim(cfg, deps: Dict[str, Callable]) -> Optional[int]:
    """One implement claim, called WITH the implement lock held. Returns the
    claim's rc, or None when nothing is approved (the drain's stop signal)."""
    from . import implementer      # local: implementer imports runner
    now = deps["now"]()
    records = deps["load"]()
    target = pick_claim(records, (_IMPLEMENT,))
    if target is None:
        return None                # raced with another pass — fine
    number = target.number

    if "boxes" not in deps or "execute_implement" not in deps:
        _fail_and_post(deps, cfg, number,
                       "implement executor is not wired into this runner "
                       "(no boxes/execute_implement dep)", _IMPLEMENT)
        return 1
    try:
        iid = implementer.issue_iid(target.item)
        branch = implementer.branch_name(iid, target.item.title or "")
    except RunnerError as e:
        _fail_and_post(deps, cfg, number, str(e), _IMPLEMENT)
        return 1

    try:
        slot = implementer.select_slot(deps["boxes"]())
        reason = "no dev slot available — free one or reclaim"
    except Exception as e:
        slot, reason = None, (f"dev-slot probe failed: "
                              f"{type(e).__name__}: {e}")
    if slot is None:
        _fail_and_post(deps, cfg, number, reason, _IMPLEMENT)
        return 1

    # Claim the box on disk BEFORE the long work: a concurrent sweep's
    # devslots.classify reads dev_box off running/approved records, so an
    # unstamped claim could hand the same box to the next implement item.
    with _queue_lock(deps):
        # Re-load: selecting a dev slot probes every box over ssh, so the
        # `records` read at the top of this pass is seconds old by now.
        deps["save"](claim(deps["load"](), number, now,
                           dev_box=slot.name))
    _post(deps, cfg, _implement_claim_message(iid, slot, branch))

    try:
        result = deps["execute_implement"](target.item, cfg, [slot])
    except NeedsInputError as e:
        if _apply_to_fresh(
                deps, cfg, number,
                lambda fresh: needs_input(fresh, number, str(e),
                                          deps["now"]())) is not None:
            _post(deps, cfg, f"❓ #{iid} needs your input: {e}")
        return 0        # a question is a handled outcome, not a failure
    except RunnerError as e:
        _fail_and_post(deps, cfg, number, str(e), _IMPLEMENT)
        return 1
    except Exception as e:
        _fail_and_post(deps, cfg, number, f"{type(e).__name__}: {e}",
                       _IMPLEMENT)
        return 1

    updated = _apply_to_fresh(
        deps, cfg, number,
        lambda fresh: complete(fresh, number, result.result_sha,
                               result.report_path, deps["now"](),
                               mr_iid=result.mr_iid))
    if updated is not None:
        _post(deps, cfg, _implement_done_message(result))
    return 0


def _implement_timeout(cfg) -> int:
    return int(getattr(cfg, "implement_timeout", 5400) or 5400)


def _magi_timeout(cfg) -> int:
    return int(getattr(cfg, "magi_timeout", MAGI_TIMEOUT_SECONDS)
               or MAGI_TIMEOUT_SECONDS)


def _feedback_timeout(cfg) -> int:
    return int(getattr(cfg, "feedback_timeout", FEEDBACK_TIMEOUT_SECONDS)
               or FEEDBACK_TIMEOUT_SECONDS)


def _implement_claim_message(iid: int, slot, branch: str) -> str:
    """`🛠️ implementing #1775 on dev1 (branch feat/1775-…)`, prefixed with the
    reassignment note when a handed-off box is reclaimed so the owner of the
    displaced MR sees where their dev site went."""
    prefix = ""
    if getattr(slot, "tier", "") == "handed_off" and getattr(slot, "mr_iid", 0):
        prefix = (f"{slot.name} reassigned from !{slot.mr_iid} "
                  f"(approved, awaiting merge)\n")
    return f"{prefix}🛠️ implementing #{iid} on {slot.name} (branch {branch})"


def _implement_done_message(result) -> str:
    verdict_line = (result.verdict or "").strip().splitlines()
    magi = verdict_line[0] if verdict_line else "no report"
    msg = (f"🛠️ implemented #{result.iid} → Draft !{result.mr_iid} "
           f"({result.dev_url}) · magi: {magi} · branch {result.branch}")
    if result.mr_url:
        msg += f"\n<{result.mr_url}>"
    if result.magi_note:
        msg += f"\nmagi note: {result.magi_note}"
    return msg


def _keep_current_done_message(result) -> str:
    if getattr(result, "mr_merged", False):
        # Good news, and it must READ as good news. This outcome used to
        # arrive as a ⚠️ fetch failure, and a warning for the ordinary end of
        # an MR's life is how people learn to ignore warnings.
        return (f"✅ !{result.iid} merged — branch gone, nothing to keep "
                f"current")
    return _keep_current_merge_message(result)


def _keep_current_merge_message(result) -> str:
    """`🔄 !4020 merged master (+7 commits, scss recompiled) · dev4 verified
    200` or `... · no dev box serving branch` when nothing currently has the
    branch checked out (a done outcome, not an error -- the merge+push
    already succeeded)."""
    scss = "recompiled" if result.scss_recompiled else "unchanged"
    sync_part = (f"{result.box_name} verified 200" if result.box_name
                else "no dev box serving branch")
    msg = (f"🔄 !{result.iid} merged master (+{result.ahead_count} commits, "
           f"scss {scss}) · {sync_part}")
    resolved = getattr(result, "conflicts_resolved", ())
    if resolved:
        names = ", ".join(f.rsplit("/", 1)[-1] for f in resolved)
        msg += f" · auto-resolved conflicts: {names}"
    return msg


def _clamped(message: str) -> str:
    """Discord REJECTS an over-long post outright, so an unclamped one turns a
    reported outcome back into silence. The address-feedback messages quote
    arbitrary third-party thread text, up to twenty threads at a time, so they
    are the ones most likely to get there."""
    return _truncate_bytes(message or "", DISCORD_MAX_CHARS - 100)


def _fail_and_post(deps, cfg, number: int, summary: str, kind: str) -> None:
    """The ONLY way an executor failure is recorded: queue -> error AND a ⚠️
    naming the item, so no failure path can end in silence."""
    if _apply_to_fresh(
            deps, cfg, number,
            lambda fresh: fail(fresh, number, summary, deps["now"]())) is not None:
        # A stderr tail can be kilobytes; Discord rejects an over-length post
        # outright, which would turn a reported failure back into silence.
        _post(deps, cfg, f"⚠️ Worksweep runner: #{number} {kind} failed — "
                         + _truncate_bytes(summary or "", DISCORD_MAX_CHARS - 100))


def _post(deps, cfg, content: str) -> None:
    try:
        if cfg.discord_webhook:
            deps["post"](cfg.discord_webhook, content)
        else:
            print(content)
    except Exception as e:
        print(f"worksweep runner: discord post failed: {e}", file=sys.stderr)
