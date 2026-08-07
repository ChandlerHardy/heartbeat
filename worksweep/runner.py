"""M3 executor runner: drain approved magi-review items, one at a time.

Pure state-machine functions (pick/claim/complete/fail/reap) are unit-tested;
the subprocess edge (execute) and the CLI glue live in run_once/execute. The
lockfile guarantees single-flight across overlapping launchd fires."""
from __future__ import annotations

import dataclasses
import datetime
import os
from typing import List, Optional, Tuple

from .models import QueueRecord

STALE_RUNNING_MINUTES = 45
_ERROR_SUMMARY_MAX = 500


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
