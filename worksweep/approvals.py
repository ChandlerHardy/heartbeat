"""Parse Discord approval replies and apply them to the queue.

`parse_approval` is pure: text -> the set of item numbers the message approves
(empty when the message is not an approval). An approval REQUIRES an explicit
`✅` or `approve` marker so a chat line that merely mentions a number ("3 looks
wrong") never flips anything.

`apply_approvals` (Task 5) is the author-gated bridge from messages to queue
status changes.
"""
from __future__ import annotations

import dataclasses
import re
from typing import List, Set, Tuple

from .models import DiscordMessage, QueueRecord

# Require an approval marker: the ✅ emoji or the word "approve" (any case).
# Without it, numbers in the message are ignored.
_HAS_MARKER_RE = re.compile(r"✅|approve", re.I)
# A token is either a `lo-hi` range or a single number. The single branch
# captures an optional leading `-` so a negative like `-1` is recognised and
# dropped (rather than read as a bare `1`).
_TOKEN_RE = re.compile(r"(\d+)\s*-\s*(\d+)|(-?)(\d+)")
# Cap the span of a range so `✅ 1-100000` can't expand into a giant set.
_MAX_RANGE_SPAN = 500
# Statuses a ✅ may flip to `approved`. `needs-input` is included so the human's
# answer un-parks a halted implement item; `running`/`done`/`error` are not (a
# ✅ must never re-enter a live claim, and `error` re-proposes itself).
_APPROVABLE = ("proposed", "needs-input")


def parse_approval(text: str) -> Set[int]:
    """Return the set of item numbers an approval message references.

    Not an approval (no `✅`/`approve` marker) -> empty set. `0`, negatives, and
    ranges whose span exceeds _MAX_RANGE_SPAN (or that descend) are ignored; the
    rest of the tokens still parse.
    """
    if not text or not _HAS_MARKER_RE.search(text):
        return set()
    out: Set[int] = set()
    for lo_s, hi_s, sign, single in _TOKEN_RE.findall(text):
        if single:
            if sign == "-":
                continue      # negative -> ignore
            n = int(single)
            if n >= 1:
                out.add(n)
            continue
        lo, hi = int(lo_s), int(hi_s)
        if lo < 1 or hi < lo:
            continue          # bad/descending range -> ignore this token
        if hi - lo > _MAX_RANGE_SPAN:
            continue          # absurd span -> ignore this token (keep the rest)
        out.update(range(lo, hi + 1))
    return out


def apply_approvals(records: List[QueueRecord], messages: List[DiscordMessage],
                    user_id: str, now: str) -> Tuple[List[QueueRecord], Set[int]]:
    """Flip queue records the configured user approved, proposed -> approved.

    M4 Task G: `needs-input` also flips to `approved`. A halted implement item
    is parked on the human's answer; their ✅ is the explicit "go again" that
    releases it (reconcile never re-proposes it on its own).

    Author gate: only messages whose author_id == user_id contribute numbers (a
    colleague typing `✅ 1` is ignored). The union of those messages' parsed
    numbers is matched against record numbers; each matching record currently
    `proposed` becomes `approved` (last_seen bumped to `now`).

    Returns (updated_records, newly_approved_numbers). Already-`approved` records
    stay approved but are NOT in the returned set, so a confirmation message
    names only freshly flipped items. Numbers with no matching record are no-ops.
    """
    approved_numbers: Set[int] = set()
    if user_id:
        for m in messages:
            if m.author_id == user_id:
                approved_numbers |= parse_approval(m.content)

    out: List[QueueRecord] = []
    newly: Set[int] = set()
    for r in records:
        if r.number in approved_numbers and r.item.status in _APPROVABLE:
            out.append(QueueRecord(
                number=r.number, first_seen=r.first_seen, last_seen=now,
                item=dataclasses.replace(r.item, status="approved")))
            newly.add(r.number)
        else:
            out.append(r)
    return out, newly
