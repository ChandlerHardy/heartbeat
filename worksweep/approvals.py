"""Parse Discord approval replies and apply them to the queue.

`parse_approval` is pure: text -> the set of item numbers the message approves
(empty when the message is not an approval). An approval REQUIRES an explicit
`✅` or `approve` marker so a chat line that merely mentions a number ("3 looks
wrong") never flips anything.

`parse_approve_all` is the blanket-approval predicate: the marker immediately
followed by "all" and NO parsable numbers (explicit numbers always win).

`apply_approvals` (Task 5) is the author-gated bridge from messages to queue
status changes. The record-flip itself lives in the pure `flip` helper and its
two wrappers `approve_numbers` / `approve_all`, which are the single definition
of "approvable" shared by the Discord path and the dashboard's POST routes --
so the two entry points can never drift apart on status rules.
"""
from __future__ import annotations

import dataclasses
import re
from typing import List, Optional, Set, Tuple

from .models import RUNNABLE_EXECUTORS, DiscordMessage, QueueRecord

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
# A blanket `✅ all` flips `proposed` ONLY. `needs-input` is deliberately absent:
# a halted implement item is parked on an unanswered question, and "yes to
# everything" must never be read as "ignore all questions" (decision 1). The one
# member of difference from _APPROVABLE is the entire content of that decision.
_APPROVE_ALL_STATUSES = ("proposed",)
# The blanket marker must sit IMMEDIATELY before "all" -- `_HAS_MARKER_RE` is an
# unanchored `✅|approve`, so a bare "marker present AND `all` present" predicate
# would turn "✅ sounds good, that's all" into a full-queue approval. Composed
# from _HAS_MARKER_RE.pattern so the marker stays single-sourced. Deliberate
# consequence: `✅all` (no space) does not match.
_APPROVE_ALL_RE = re.compile(rf"(?:{_HAS_MARKER_RE.pattern})\s+all\b", re.I)


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


def parse_approve_all(text: str) -> bool:
    """True when `text` is a BLANKET approval (`✅ all` / `approve all`).

    Two preconditions, both load-bearing:

    1. Adjacency -- the marker must sit immediately before the word "all", so a
       casual sign-off like "✅ sounds good, that's all" is NOT a blanket
       approval. (`_HAS_MARKER_RE` is unanchored; a "marker present AND `all`
       present" test would approve the whole queue on that sentence.)
    2. Precedence -- explicit numbers always win. `✅ 1,3 all good` names
       numbers, so it is a numbered approval and this returns False. This is an
       ORDERING constraint, not a filter: callers must not compute the blanket
       flag independently and union the two result sets.
    """
    if not text or not _APPROVE_ALL_RE.search(text):
        return False
    return not parse_approval(text)


def is_blanket_eligible(item) -> bool:
    """True when a BLANKET approval may flip this item.

    Two conditions, and the executor one is a safety gate, not a nicety:
    `proposed` AND an executor the runner will actually claim.

    A blanket approve of a `triage`/`mr-hygiene`/`none` item would strand it
    permanently -- nothing ever claims it, reconcile preserves `approved`, and
    worksweep has no un-approve path, so the only way back is hand-editing
    queue.json. A numbered `✅ N` may still flip those items: naming one is a
    deliberate human choice, and the human can see what they typed.
    """
    return (item.status in _APPROVE_ALL_STATUSES
            and item.executor in RUNNABLE_EXECUTORS)


def flip(records: List[QueueRecord], numbers: Set[int], now: str,
         statuses: Tuple[str, ...]) -> Tuple[List[QueueRecord], Set[int]]:
    """Flip every record in `numbers` whose status is in `statuses` to `approved`.

    Pure. The ONE definition of the record-flip in worksweep: the Discord path
    (`apply_approvals`) and the dashboard's POST routes both reach the queue
    through this function, so "approvable" cannot be defined twice.

    A flipped record keeps its `number` and `first_seen` and gets `last_seen =
    now`. Returns (updated_records, newly_flipped_numbers) -- a record already
    `approved` is left byte-identical and is NOT in the returned set, so a
    confirmation names only freshly flipped items. Numbers matching no record,
    and records whose status is outside `statuses`, are no-ops.
    """
    out: List[QueueRecord] = []
    newly: Set[int] = set()
    for r in records:
        if r.number in numbers and r.item.status in statuses:
            out.append(QueueRecord(
                number=r.number, first_seen=r.first_seen, last_seen=now,
                item=dataclasses.replace(r.item, status="approved")))
            newly.add(r.number)
        else:
            out.append(r)
    return out, newly


def approve_numbers(records: List[QueueRecord], numbers: Set[int],
                    now: str) -> Tuple[List[QueueRecord], Set[int]]:
    """Numbered approval (`✅ 1,3`, or the dashboard's checked boxes).

    Uses `_APPROVABLE`, so an explicitly named `needs-input` item DOES flip --
    naming it is the human's deliberate "I've answered, go again".
    """
    return flip(records, numbers, now, _APPROVABLE)


def approve_all(records: List[QueueRecord], now: str,
                numbers: Optional[Set[int]] = None
                ) -> Tuple[List[QueueRecord], Set[int]]:
    """Blanket approval (`✅ all`, or the dashboard's "Approve all" button).

    Flips exactly the records `is_blanket_eligible` accepts: `proposed` AND
    runnable. NOT symmetric with `approve_numbers`, and must not be "cleaned up"
    into symmetry -- a blanket yes must never release a `needs-input` item
    parked on an unanswered question, nor strand a non-runnable one.

    `numbers` scopes the blanket to a caller-supplied set, intersected with
    what is eligible RIGHT NOW. The dashboard passes the numbers its page
    actually rendered, so the user approves the set they were shown and
    consented to: an item that landed between the render and the tap is not
    swept in silently (it shows up on the next refresh instead). The Discord
    path passes None, meaning "every eligible record".
    """
    eligible = {r.number for r in records if is_blanket_eligible(r.item)}
    if numbers is not None:
        eligible &= set(numbers)
    return flip(records, eligible, now, _APPROVE_ALL_STATUSES)


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

    A message that is a blanket approval (`✅ all`, see `parse_approve_all`)
    additionally flips every remaining `proposed` record -- `needs-input` is
    deliberately NOT swept (decision 1). Blanket-flipped numbers land in the
    returned set like any other, so the intake confirmation names them.

    Returns (updated_records, newly_approved_numbers). Already-`approved` records
    stay approved but are NOT in the returned set, so a confirmation message
    names only freshly flipped items. Numbers with no matching record are no-ops.
    """
    approved_numbers: Set[int] = set()
    blanket = False
    if user_id:
        for m in messages:
            if m.author_id == user_id:
                approved_numbers |= parse_approval(m.content)
                # Derived per-message INSIDE the author gate: a colleague's
                # `✅ all` must not sweep the queue.
                blanket = blanket or parse_approve_all(m.content)

    out, newly = approve_numbers(records, approved_numbers, now)
    if blanket:
        # `proposed` only, and applied to the already-flipped list so the two
        # paths compose (a numbered ✅ that released a needs-input item stays
        # released, and neither number is reported twice).
        out, blanket_newly = approve_all(out, now)
        newly |= blanket_newly
    return out, newly
