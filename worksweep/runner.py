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

from .models import QueueRecord, WorkItem

STALE_RUNNING_MINUTES = 45
# M4 Task G: an implement claim legitimately runs for `implement_timeout`
# (90 min default), so it gets its own, longer reap window: timeout + 15 min.
IMPLEMENT_REAP_GRACE_SECONDS = 900
_ERROR_SUMMARY_MAX = 500
_LOCK_DEFAULT = os.path.expanduser("~/.worksweep/runner.lock")
_IMPLEMENT_LOCK_NAME = "runner-implement.lock"

_MAGI = "magi-review"
_IMPLEMENT = "implement"
_ALL_EXECUTORS = (_MAGI, _IMPLEMENT)
# Executors that may have at most ONE claim in flight across the whole queue.
# magi-review is read-only and cheap; implement writes to GitLab and occupies
# a dev box, so a second one must wait even though it has its own lock file.
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
    """
    running_kinds = {r.item.executor for r in records
                     if r.item.status == "running"}
    candidates = [r for r in records
                  if r.item.status == "approved"
                  and r.item.executor in executors
                  and not (r.item.executor in _SINGLE_FLIGHT
                           and r.item.executor in running_kinds)]
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
             report_path: str, now: str, mr_iid: int = 0) -> List[QueueRecord]:
    changes = dict(status="done", done_reason="executor-completed",
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
               implement_timeout: int = 5400
               ) -> Tuple[List[QueueRecord], List[QueueRecord]]:
    """Flip `running` claims that outlived their executor's window to error.

    Two windows, because the two executors have very different runtimes: 45
    min for magi-review, `implement_timeout + 15 min` for implement. Using the
    magi window for both would reap healthy implement runs mid-flight (and
    then a second implement could claim the same dev box).
    """
    implement_limit = implement_timeout + IMPLEMENT_REAP_GRACE_SECONDS
    updated, reaped = [], []
    for r in records:
        limit = (implement_limit if r.item.executor == _IMPLEMENT
                 else STALE_RUNNING_MINUTES * 60)
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
    try:
        fetch = run_subprocess(["git", "-C", checkout, "fetch", "origin"],
                               capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RunnerError("git fetch timed out")
    if fetch.returncode != 0:
        raise RunnerError(f"git fetch failed: {(fetch.stderr or '').strip()[-300:]}")
    try:
        proc = run_subprocess(
            [cfg.claude_bin, "-p", f"/magi:magi-review !{iid}"],
            cwd=checkout, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,  # claude -p blocks/exits 1 waiting on a non-TTY stdin
            timeout=cfg.runner_timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"magi-review !{iid} exceeded {cfg.runner_timeout}s")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RunnerError(f"magi-review !{iid} exited {proc.returncode}: {tail}")
    report = find_report(checkout, iid)
    if report is None:
        raise RunnerError(f"magi-review !{iid} produced no tribunal report")
    return item.sha, report


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
    fresh = deps["load"]()
    if not any(r.number == number for r in fresh):
        _post(deps, cfg, f"⚠️ Worksweep runner: #{number} vanished from the "
                         f"queue before its result could be recorded")
        return None
    updated = apply_fn(fresh)
    deps["save"](updated)
    return updated


def run_once(cfg, deps: Dict[str, Callable], lock_path: str = _LOCK_DEFAULT,
             implement_lock_path: Optional[str] = None) -> int:
    """One runner pass: reap stale claims, then run at most ONE magi-review
    item and at most ONE implement item.

    The two executors hold separate lock files so a 90-minute implement run
    never starves the (much shorter) magi-review queue, and an overlapping
    launchd fire still can't double-run either kind. `implement_lock_path`
    defaults to a sibling of `lock_path` (so a test passing a tmp lock path
    keeps BOTH locks inside its tmp dir, never touching ~/.worksweep).
    """
    if implement_lock_path is None:
        implement_lock_path = os.path.join(os.path.dirname(lock_path) or ".",
                                           _IMPLEMENT_LOCK_NAME)
    rc = _guarded_pass(cfg, deps, _MAGI, _run_magi_pass, lock_path)
    rc_implement = _guarded_pass(cfg, deps, _IMPLEMENT, _run_implement_pass,
                                 implement_lock_path)
    return rc or rc_implement


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
    if not acquire_lock(lock_path):
        return 0    # another runner is live — that's fine, not an error
    try:
        now = deps["now"]()
        records = deps["load"]()
        records, reaped = reap_stale(
            records, now, implement_timeout=_implement_timeout(cfg))
        if reaped:
            deps["save"](records)
            for r in reaped:
                _post(deps, cfg, f"⚠️ Worksweep runner: reaped stale claim "
                                 f"#{r.number} ({r.item.repo} {r.item.id})")
        target = pick_claim(records, (_MAGI,))
        if target is None:
            return 0
        records = claim(records, target.number, now)
        deps["save"](records)
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


def _run_implement_pass(cfg, deps: Dict[str, Callable], lock_path: str) -> int:
    """At most one implement item. Every exit below is either a no-op (nothing
    approved / lock held) or ends in BOTH a queue status and a Discord post —
    this executor writes to GitLab and occupies a dev box, so a silent failure
    would leave a claimed box and a half-open branch with nobody told."""
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
        from . import implementer      # local: implementer imports runner
        now = deps["now"]()
        records = deps["load"]()
        target = pick_claim(records, (_IMPLEMENT,))
        if target is None:
            return 0                   # raced with another pass — fine
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
        deps["save"](claim(records, number, now, dev_box=slot.name))
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
    finally:
        release_lock(lock_path)


def _implement_timeout(cfg) -> int:
    return int(getattr(cfg, "implement_timeout", 5400) or 5400)


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


def _fail_and_post(deps, cfg, number: int, summary: str, kind: str) -> None:
    """The ONLY way an executor failure is recorded: queue -> error AND a ⚠️
    naming the item, so no failure path can end in silence."""
    if _apply_to_fresh(
            deps, cfg, number,
            lambda fresh: fail(fresh, number, summary, deps["now"]())) is not None:
        _post(deps, cfg, f"⚠️ Worksweep runner: #{number} {kind} failed — "
                         f"{summary}")


def _post(deps, cfg, content: str) -> None:
    try:
        if cfg.discord_webhook:
            deps["post"](cfg.discord_webhook, content)
        else:
            print(content)
    except Exception as e:
        print(f"worksweep runner: discord post failed: {e}", file=sys.stderr)
