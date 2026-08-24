"""Persistent queue store for Worksweep (`~/.worksweep/queue.json`).

The queue holds the current WorkItems with their stable digest `number` and
`status`. It is the source of truth for numbering: the formatter renders in
queue order so the digest number a user replies to (`✅ 3`) maps to the same
WorkItem the queue knows as #3.

Writes are atomic (temp file + os.replace) because both the sweep (--post) and
the intake poller touch the same file — a crash or concurrent read must never
see a half-written queue. Reads tolerate a missing or malformed file (→ []),
mirroring collectors._loads_list.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import List

from .models import QueueRecord, WorkItem

_TERMINAL = ("done", "error")
# `needs-input` (M4 Task G) is terminal-ish: retained when it drops out of a
# sweep like an approved/running item, but deliberately NOT in _TERMINAL --
# it is never compacted away and never auto-re-proposed. The only path back
# to `approved` is a fresh Discord ✅ (approvals.apply_approvals).
_RETAIN_IF_GONE = ("approved", "running", "done", "error", "needs-input")
_NEEDS_INPUT = "needs-input"
_COMPACT_AFTER_DAYS = 90


def _older_than_days(iso_ts: str, iso_now: str, days: int) -> bool:
    """True when iso_ts is more than `days` before iso_now. Unparseable -> False
    (never destroy a record on bad data)."""
    import datetime
    try:
        ts = datetime.datetime.fromisoformat(iso_ts)
        now = datetime.datetime.fromisoformat(iso_now)
    except (ValueError, TypeError):
        return False
    if (ts.tzinfo is None) != (now.tzinfo is None):   # naive/aware mix -> keep
        return False
    return (now - ts) > datetime.timedelta(days=days)


def load_queue(path: str) -> List[QueueRecord]:
    """Load queue records from `path`. Missing file or malformed JSON → []."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        print(f"worksweep: queue decode failed ({path}): {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"worksweep: queue expected a list, got {type(data).__name__}",
              file=sys.stderr)
        return []
    out: List[QueueRecord] = []
    for d in data:
        try:
            out.append(QueueRecord(
                number=int(d["number"]),
                first_seen=d.get("first_seen", ""),
                last_seen=d.get("last_seen", ""),
                item=WorkItem(**d["item"]),
            ))
        except (KeyError, TypeError, ValueError) as e:
            print(f"worksweep: queue skipping bad record: {e}", file=sys.stderr)
    return out


def save_queue(path: str, records: List[QueueRecord]) -> None:
    """Atomically write `records` to `path` (temp file + os.replace).

    Creates the parent directory if needed. The temp file lives in the same
    directory so os.replace is an atomic rename on the same filesystem.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = [
        {
            "number": r.number,
            "first_seen": r.first_seen,
            "last_seen": r.last_seen,
            "item": dataclasses.asdict(r.item),
        }
        for r in records
    ]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def auto_approve(records: List[QueueRecord],
                 executors: tuple) -> List[QueueRecord]:
    """Flip `proposed` records whose executor is in `executors` straight to
    `approved` -- no Discord ✅ needed (cfg.auto_approve; keep-current by
    default). Runs right after reconcile, before the queue is saved, so the
    digest and the runner both see the item already approved.

    Only `proposed` flips: `needs-input` stays a human question, and a failed
    item keeps the reconcile cadence (error -> proposed at the NEXT sweep),
    so an auto-approved executor that keeps failing retries at most daily,
    with a ⚠️ each time -- never a tight loop.
    """
    if not executors:
        return records
    out: List[QueueRecord] = []
    for r in records:
        if r.item.status == "proposed" and r.item.executor in executors:
            out.append(dataclasses.replace(
                r, item=dataclasses.replace(r.item, status="approved")))
        else:
            out.append(r)
    return out


def reconcile(existing: List[QueueRecord], fresh: List[WorkItem],
              now: str, resolved: dict | None = None) -> List[QueueRecord]:
    """Fold a sweep into the queue. M2 rules plus the M3 lifecycle:
    resolutions -> done; error+present -> retry; done+new-sha -> resurrect;
    terminal retained until 90-day compaction."""
    resolved = resolved or {}
    by_id = {r.item.id: r for r in existing}
    fresh_ids = {it.id for it in fresh}
    next_num = max((r.number for r in existing), default=0) + 1

    out: List[QueueRecord] = []
    for it in fresh:
        prior = by_id.get(it.id)
        if prior is None:
            out.append(QueueRecord(number=next_num, item=it,
                                   first_seen=now, last_seen=now))
            next_num += 1
            continue
        ps = prior.item.status
        if ps == _NEEDS_INPUT:
            # A halted item stays halted no matter what the sweep says (even
            # on a new sha): re-proposing it would let the runner re-claim
            # work the human was asked to unblock, and the question would
            # scroll away unanswered.
            merged = dataclasses.replace(it, status=_NEEDS_INPUT,
                                         error_summary=prior.item.error_summary,
                                         dev_box=prior.item.dev_box)
        elif ps == "error":
            merged = dataclasses.replace(it, status="proposed")
        elif ps == "done":
            if prior.item.sha == it.sha:
                out.append(QueueRecord(number=prior.number, item=prior.item,
                                       first_seen=prior.first_seen, last_seen=now))
                continue
            merged = dataclasses.replace(it, status="proposed")
        elif prior.item.sha == it.sha:
            # Carry the executor's own bookkeeping across the sweep. `dev_box`
            # in particular: issue items have sha="" so this branch fires
            # EVERY sweep, and rebuilding from the fresh item (dev_box="")
            # would wipe a live claim -- the claimed-box exclusion would go
            # empty and the next digest would offer an occupied box as free.
            merged = dataclasses.replace(it, status=ps,
                                         claimed_at=prior.item.claimed_at,
                                         dev_box=prior.item.dev_box,
                                         mr_iid=prior.item.mr_iid)
        else:
            merged = dataclasses.replace(it, status="proposed")
        out.append(QueueRecord(number=prior.number, item=merged,
                               first_seen=prior.first_seen, last_seen=now))

    for r in existing:
        if r.item.id in fresh_ids:
            continue
        reason = resolved.get(r.item.id)
        if reason and r.item.status not in _TERMINAL:
            out.append(QueueRecord(
                number=r.number, first_seen=r.first_seen, last_seen=now,
                item=dataclasses.replace(r.item, status="done",
                                         done_reason=reason)))
            continue
        if r.item.status in _RETAIN_IF_GONE:
            out.append(r)

    out = [r for r in out
           if not (r.item.status in _TERMINAL
                   and _older_than_days(r.last_seen, now, _COMPACT_AFTER_DAYS))]
    out.sort(key=lambda r: r.number)
    return out
