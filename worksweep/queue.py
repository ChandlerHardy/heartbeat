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

# Statuses whose records survive vanishing from a sweep: an approved/running
# item is mid-flight toward an executor (M3) and must not disappear before it is
# handled. A still-`proposed` item that's gone upstream is dropped.
_RETAIN_IF_GONE = ("approved", "running")


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


def reconcile(existing: List[QueueRecord], fresh: List[WorkItem],
              now: str) -> List[QueueRecord]:
    """Fold a fresh sweep into the queue, preserving stable numbers + status.

    Rules (see plan Task 2):
    - New id -> next number (max existing + 1), status from the item, first/last
      seen = now.
    - Same id + same sha -> keep number, keep status (approved stays approved),
      keep first_seen, bump last_seen = now.
    - Same id + different sha -> keep number, update sha, reset status to
      proposed (the prior approval was for the old SHA), bump last_seen = now.
    - Queued id absent from the sweep -> drop, UNLESS its status is approved or
      running (retained mid-flight, last_seen unchanged).

    `now` is injected so callers/tests are deterministic.
    """
    by_id = {r.item.id: r for r in existing}
    fresh_ids = {it.id for it in fresh}
    next_num = max((r.number for r in existing), default=0) + 1

    out: List[QueueRecord] = []
    # Retained-but-gone records first would scramble order; instead walk fresh in
    # order, then append surviving-gone records, then sort by number so the queue
    # is always rendered in stable number order.
    for it in fresh:
        prior = by_id.get(it.id)
        if prior is None:
            out.append(QueueRecord(number=next_num, item=it,
                                   first_seen=now, last_seen=now))
            next_num += 1
            continue
        if prior.item.sha == it.sha:
            # Same proposal: keep number + status + first_seen, refresh the item
            # (its non-status metadata may have changed) but force-keep status.
            merged = dataclasses.replace(it, status=prior.item.status)
            out.append(QueueRecord(number=prior.number, item=merged,
                                   first_seen=prior.first_seen, last_seen=now))
        else:
            # New commits on the same MR: the old approval no longer applies.
            merged = dataclasses.replace(it, status="proposed")
            out.append(QueueRecord(number=prior.number, item=merged,
                                   first_seen=prior.first_seen, last_seen=now))

    for r in existing:
        if r.item.id in fresh_ids:
            continue
        if r.item.status in _RETAIN_IF_GONE:
            out.append(r)   # retained mid-flight, last_seen untouched

    out.sort(key=lambda r: r.number)
    return out
