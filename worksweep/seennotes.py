"""Notes Chandler has already read: `~/.worksweep/seen-notes.json`.

The plain-note sensor's first sweep produced three permanent proposed rows
whose entire content was "LGTM". A plain MR note can never be closed out, so
the reviewer's acknowledgment is the last word forever and the row re-fires on
every sweep -- and because an `address-feedback` row is runnable, it could not
even be dismissed.

Dismissal is keyed on EVIDENCE, not on the row's id: the pair
`(discussion id, last note id)`. A reviewer following up changes the note id,
the key stops matching, and the row comes back. "I have seen this note", never
"mute this thread" -- which is the difference between a dismissal a human can
trust and one that quietly swallows real feedback.

Its own file rather than a queue field, because a dismissal has to outlive the
row it dismissed: the queue row goes `done` and eventually compacts away, while
"Chandler read cmnoble's LGTM" stays true until cmnoble says something new.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from typing import Iterable, List

# Same window as queue compaction, for the same reason: a note nobody has seen
# in three months is not coming back, and this file should not grow forever.
SEEN_TTL_DAYS = 90


def load_seen(path: str, now: str = "") -> frozenset:
    """The `(discussion, note)` pairs already dismissed. Missing or malformed
    file -> empty, because failing to read a dismissal only costs a row
    reappearing, while failing loudly would take the whole sweep down."""
    return frozenset((e["discussion"], e["note"])
                     for e in prune_seen(_read(path), now))


def record_seen(path: str, pairs: Iterable, now: str) -> None:
    """Add `pairs` to the file, pruning expired entries on the way through.

    A pair with an empty half is dropped: an old queue row carries no note
    refs, and recording `("", "")` would dismiss every thread that has no id at
    once -- silently, and forever.
    """
    entries = prune_seen(_read(path), now)
    have = {(e["discussion"], e["note"]) for e in entries}
    for pair in (pairs or ()):
        try:
            discussion, note = (str(x or "") for x in tuple(pair)[:2])
        except (TypeError, ValueError):
            continue
        if not discussion or not note or (discussion, note) in have:
            continue
        have.add((discussion, note))
        entries.append({"discussion": discussion, "note": note, "seen": now})
    save_seen(path, entries)


def prune_seen(entries: List[dict], now: str = "") -> List[dict]:
    """Entries younger than the TTL. An unparseable timestamp is KEPT -- never
    drop a human's decision on bad data, mirroring the queue's own rule."""
    cutoff = _parse(now)
    if cutoff is None:
        return list(entries)
    cutoff -= datetime.timedelta(days=SEEN_TTL_DAYS)
    out = []
    for e in entries:
        seen = _parse(e.get("seen", ""))
        if seen is None or seen >= cutoff:
            out.append(e)
    return out


def save_seen(path: str, entries: List[dict]) -> None:
    """Atomically replace the file. Same discipline as save_queue: a UNIQUE
    temp name in the same directory, 0600, then os.replace -- this records what
    a human decided, and a half-written one would silently un-dismiss things."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".seen-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(list(entries), f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: str) -> List[dict]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        print(f"worksweep: seen-notes decode failed ({path}): {e}",
              file=sys.stderr)
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data
            if isinstance(e, dict) and e.get("discussion") and e.get("note")]


def _parse(value: str):
    try:
        ts = datetime.datetime.fromisoformat((value or "").replace("Z",
                                                                   "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts
