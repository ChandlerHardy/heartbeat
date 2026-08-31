"""The head sha each reviewed MR was at when Chandler dealt with it:
`~/.worksweep/reviewed-state.json`.

The re-review sensor's memory. GitLab's reviewState stays "REVIEWED" when an
author pushes fixes without clicking re-request review, so state alone cannot
say "the branch moved under a finished review" — ck-www !401 sat in exactly
that mutual wait (2026-08-31), and pb-www !4076's version-7 fixes surfaced
only because the author mentioned them in standup.

Semantics mirror seen-notes deliberately: the record is EVIDENCE-keyed — "I
have dealt with THIS head" (reviewed it, dismissed it, or the executor
re-reviewed it), never "mute this MR". A later push changes the head, the
recorded sha stops matching, and the row comes back.

First sight of a reviewed MR seeds the file quietly, so turning the sensor on
does not fire a retroactive storm over every MR reviewed before it existed.

Its own file rather than a queue field for the seen-notes reason: the record
must outlive the row (rows go done and compact away; "Chandler reviewed
!401 at abc123" stays true until the branch moves).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from typing import Dict

# Same window as queue compaction and seen-notes, for the same reason: an MR
# untouched for three months is settled, and this file should not grow forever.
STATE_TTL_DAYS = 90


def load_state(path: str, now: str = "") -> Dict[str, str]:
    """{"repo!iid": sha} for every live entry. Missing or malformed file ->
    {}, because losing this state only re-seeds quietly (one silent sweep),
    while failing loudly would take the whole sweep down."""
    return {e["key"]: e["sha"] for e in _read(path, now)}


def record_state(path: str, key: str, sha: str, now: str) -> None:
    """Remember that `key` (repo!iid) has been dealt with at `sha`. Replaces
    any prior entry for the key. Atomic write, same discipline as the queue."""
    entries = [e for e in _read(path, now) if e["key"] != key]
    entries.append({"key": key, "sha": sha, "noted_at": now})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=".reviewed-state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: str, now: str = "") -> list:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    cutoff = _cutoff(now)
    for e in raw:
        if not isinstance(e, dict):
            continue
        key, sha = e.get("key"), e.get("sha")
        if not (isinstance(key, str) and key and isinstance(sha, str) and sha):
            continue
        if cutoff and str(e.get("noted_at", "")) and str(e["noted_at"]) < cutoff:
            continue
        out.append({"key": key, "sha": sha,
                    "noted_at": str(e.get("noted_at", ""))})
    return out


def _cutoff(now: str) -> str:
    if not now:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (dt - datetime.timedelta(days=STATE_TTL_DAYS)).isoformat()
