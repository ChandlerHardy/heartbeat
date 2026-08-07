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
_ERROR_SUMMARY_MAX = 500
_LOCK_DEFAULT = os.path.expanduser("~/.worksweep/runner.lock")


class RunnerError(RuntimeError):
    """Executor failure with a human-postable summary."""


def _replace(rec: QueueRecord, now: str, **item_changes) -> QueueRecord:
    return QueueRecord(number=rec.number, first_seen=rec.first_seen,
                       last_seen=now,
                       item=dataclasses.replace(rec.item, **item_changes))


def pick_claim(records: List[QueueRecord]) -> Optional[QueueRecord]:
    candidates = [r for r in records
                  if r.item.status == "approved" and r.item.executor == "magi-review"]
    return min(candidates, key=lambda r: r.number) if candidates else None


def claim(records: List[QueueRecord], number: int, now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="running", claimed_at=now)
            if r.number == number else r for r in records]


def complete(records: List[QueueRecord], number: int, result_sha: str,
             report_path: str, now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="done", done_reason="executor-completed",
                     result_sha=result_sha, report_path=report_path)
            if r.number == number else r for r in records]


def fail(records: List[QueueRecord], number: int, error_summary: str,
         now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="error",
                     error_summary=(error_summary or "")[:_ERROR_SUMMARY_MAX])
            if r.number == number else r for r in records]


def reap_stale(records: List[QueueRecord],
               now: str) -> Tuple[List[QueueRecord], List[QueueRecord]]:
    updated, reaped = [], []
    for r in records:
        if r.item.status == "running" and _stale(r.item.claimed_at, now):
            nr = _replace(r, now, status="error",
                          error_summary="stale claim reaped")
            updated.append(nr)
            reaped.append(nr)
        else:
            updated.append(r)
    return updated, reaped


def _stale(claimed_at: str, now: str) -> bool:
    try:
        t = datetime.datetime.fromisoformat(claimed_at)
        n = datetime.datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return True   # unparseable claim time -> reap (running must be provable)
    if (t.tzinfo is None) != (n.tzinfo is None):
        return True
    return (n - t) > datetime.timedelta(minutes=STALE_RUNNING_MINUTES)


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
            timeout=cfg.runner_timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"magi-review !{iid} exceeded {cfg.runner_timeout}s")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RunnerError(f"magi-review !{iid} exited {proc.returncode}: {tail}")
    report = find_report(checkout, iid) or ""
    return item.sha, report


def run_once(cfg, deps: Dict[str, Callable], lock_path: str = _LOCK_DEFAULT) -> int:
    """One runner pass: reap stale claims, then run at most one approved item."""
    if not acquire_lock(lock_path):
        return 0    # another runner is live — that's fine, not an error
    try:
        now = deps["now"]()
        records = deps["load"]()
        records, reaped = reap_stale(records, now)
        if reaped:
            deps["save"](records)
            for r in reaped:
                _post(deps, cfg, f"⚠️ Worksweep runner: reaped stale claim "
                                 f"#{r.number} ({r.item.repo} {r.item.id})")
        target = pick_claim(records)
        if target is None:
            return 0
        records = claim(records, target.number, now)
        deps["save"](records)
        try:
            result_sha, report_path = deps["execute"](target.item, cfg)
        except RunnerError as e:
            records = fail(records, target.number, str(e), deps["now"]())
            deps["save"](records)
            _post(deps, cfg, f"⚠️ Worksweep runner: #{target.number} "
                             f"magi-review failed — {e}")
            return 1
        records = complete(records, target.number, result_sha, report_path,
                           deps["now"]())
        deps["save"](records)
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


def _post(deps, cfg, content: str) -> None:
    try:
        if cfg.discord_webhook:
            deps["post"](cfg.discord_webhook, content)
        else:
            print(content)
    except Exception as e:
        print(f"worksweep runner: discord post failed: {e}", file=sys.stderr)
