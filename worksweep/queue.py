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
