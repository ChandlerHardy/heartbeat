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

import contextlib
import dataclasses
import fcntl
import json
import os
import sys
import tempfile
import time
from typing import List, Optional, Tuple

from .models import RUNNABLE_EXECUTORS, QueueRecord, WorkItem

# f-007/f-028/f-029: queue.json is a whole-file replace written by FOUR
# independent processes -- the sweep, intake, each runner pass, and the live
# dashboard server. `save_queue`'s unique temp file plus os.replace makes each
# write atomic, which prevents a CORRUPT file but says nothing about a LOST
# one: two processes that both load, both mutate and both save leave only the
# second one's work, and the first one's ✅ or completion is gone with no trace.
#
# The lock is a sidecar (`<queue>.write.lock`), never the queue file itself:
# os.replace swaps the inode, so a lock held on the queue would follow the old
# file and protect nothing.
_LOCK_SUFFIX = ".write.lock"
# Short on purpose. A dashboard tap waits on this, so minutes would read as a
# hung page; a sweep's own read-modify-write is milliseconds once its network
# work is done, so anything longer than this means a genuinely wedged holder.
QUEUE_LOCK_TIMEOUT = 10.0
_LOCK_POLL_SECONDS = 0.02

_TERMINAL = ("done", "error")
# `needs-input` (M4 Task G) is terminal-ish: retained when it drops out of a
# sweep like an approved/running item, but deliberately NOT in _TERMINAL --
# it is never compacted away and never auto-re-proposed. The only path back
# to `approved` is a fresh Discord ✅ (approvals.apply_approvals).
_RETAIN_IF_GONE = ("approved", "running", "done", "error", "needs-input")
_NEEDS_INPUT = "needs-input"
# Executors whose approval is tied to the SIZE of the ask, not just the sha.
# An address-feedback ✅ covers the threads it named; three threads is not the
# consent that was given for two.
_WHY_SENSITIVE = ("address-feedback",)
# Bookkeeping worksweep appends to a why-string. Never part of the ask.
_PROBE_FAILED_MARKER = "(probe failed)"
# Resolution reasons strong enough to close an already-`error` row rather than
# retain it. Deliberately narrow: each one means "the work provably no longer
# exists" -- the signal cleared on its own, or the MR merged/closed and took
# the whole question with it. `error` is in _TERMINAL, so without this the
# generic branch below skips it and the row is retained forever: that is how a
# keep-current row for !3997 sat in `error` after its branch was deleted by
# the merge.
#
# `needs-input` needs no entry here and must not gain one: it is NOT in
# _TERMINAL, so the ordinary resolution branch below already closes it. That
# matters -- a question parked on threads that have since settled (a reviewer
# answered, Chandler answered by hand, or the bot filter landed) is asking
# something nobody can act on, and would otherwise sit on the dashboard
# forever. Adding `needs-input` to _TERMINAL would silently re-strand it,
# which is what test_a_parked_question_closes_when_its_signal_clears guards.
_CLOSES_AN_ERROR = ("signal-cleared", "mr-merged")
_COMPACT_AFTER_DAYS = 90


class QueueLockError(RuntimeError):
    """The queue write lock could not be taken. Never swallowed: a writer that
    cannot lock must report that it did not write, rather than write anyway
    and silently drop somebody else's update."""


@contextlib.contextmanager
def write_lock(queue_path: str, timeout: float = QUEUE_LOCK_TIMEOUT):
    """Hold the cross-process write lock for ONE load -> mutate -> save cycle.

    Scope matters as much as existence: this wraps the read-modify-write, never
    an executor run. A pass that held it across a 30-minute claude run would
    block the sweep, intake and every dashboard tap for the duration -- the
    same mistake the address-feedback pass had to be pulled out of the shared
    runner lock to avoid.

    Blocks up to `timeout`, then raises QueueLockError. Blocking (rather than
    failing instantly) because contention here is normal -- four timers and a
    web server share this file -- and giving up instantly would drop writes
    routinely; loud (rather than waiting forever) because a wedged holder must
    surface as a reported failure, not a launchd job that hangs until it is
    reaped.
    """
    lock_path = f"{queue_path}{_LOCK_SUFFIX}"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise QueueLockError(
                        f"could not take the queue write lock at {lock_path} "
                        f"within {timeout}s -- another worksweep process is "
                        f"holding it (or left it wedged)")
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield lock_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def null_lock(*args, **kwargs):
    """The inert stand-in: --dry-run (which never saves) and unit tests that
    exercise a single in-process writer."""
    yield None


def _consent_holds(prior: WorkItem, fresh: WorkItem) -> bool:
    """Whether `prior`'s status may carry onto `fresh` at an unchanged sha.

    For most executors the sha IS the ask, so an unchanged sha means unchanged
    consent. `address-feedback` is different: its sha is the MR head, but what
    was approved is a set of threads, and a reviewer can add a thread without
    anyone pushing a commit. Its why-string carries that count, so a changed
    why means a changed ask.
    """
    if prior.executor not in _WHY_SENSITIVE:
        return True
    return _ask_of(prior.why) == _ask_of(fresh.why)


def _ask_of(why: str) -> str:
    """The why-string with worksweep's own bookkeeping markers stripped.

    f-006: a probe failure appends "(probe failed)" to the carried-forward
    row. Comparing the raw strings would read that marker as "the ask changed"
    and reset the ✅ on the very next sweep -- turning a one-sweep blip into a
    lost approval anyway, one step later. The marker describes OUR failure to
    look, never a change in what was asked.
    """
    return (why or "").replace(_PROBE_FAILED_MARKER, "").strip()


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
    # A UNIQUE temp name per write, not a fixed `path + ".tmp"`. Three writers
    # touch this file (intake, runner, dashboard) and the dashboard is threaded:
    # with a shared fixed name, two concurrent writers interleave their bytes
    # into the SAME temp file and os.replace then publishes the resulting
    # mixture as the whole queue. Unique names make each writer's temp private,
    # so the worst case degrades to a lost update (last replace wins) instead of
    # a corrupt file.
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".queue-", suffix=".tmp")
    try:
        # mkstemp is 0600; queue.json holds private MR titles and whys, so keep
        # it that way rather than widening to the umask default.
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        # never leave a stray temp behind on a failed write
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def is_dismissable(item: WorkItem) -> bool:
    """True when a row may be dismissed from the dashboard.

    Non-terminal AND non-runnable. The executor half is the safety gate:
    anything the runner claims is approve-territory, and dismissing it would
    silently drop work the human meant to run. What is left -- triage,
    mr-hygiene, and any other executor nothing executes -- are FYI rows whose
    only resolution is "I looked at it".
    """
    return (item.status not in _TERMINAL
            and item.executor not in RUNNABLE_EXECUTORS)


def dismiss(records: List[QueueRecord], number: int,
            now: str) -> Tuple[List[QueueRecord], Optional[QueueRecord]]:
    """Retire record `number`: proposed -> done, done_reason "dismissed".

    Returns (updated_records, dismissed_record); the record is None when the
    number matches nothing or the record is not dismissable, so the caller can
    reject without a second pass.

    Lives here, beside reconcile, because this is a queue lifecycle transition
    and the queue owns those -- the dashboard writes no status of its own.
    Dismissal is durable across sweeps: reconcile retains `done` records that
    drop out of a sweep, and for a todo (sha "" on both sides) the
    same-sha branch keeps it `done` even if the todo is still pending in
    GitLab.
    """
    out: List[QueueRecord] = []
    dismissed: Optional[QueueRecord] = None
    for r in records:
        if r.number == number and is_dismissable(r.item):
            dismissed = QueueRecord(
                number=r.number, first_seen=r.first_seen, last_seen=now,
                item=dataclasses.replace(r.item, status="done",
                                         done_reason="dismissed"))
            out.append(dismissed)
        else:
            out.append(r)
    return out, dismissed


def reconcile(existing: List[QueueRecord], fresh: List[WorkItem],
              now: str, resolved: dict | None = None,
              resets: set | None = None) -> List[QueueRecord]:
    """Fold a sweep into the queue. M2 rules plus the M3 lifecycle:
    resolutions -> done; error+present -> retry; done+new-sha -> resurrect;
    terminal retained until 90-day compaction.

    `resets` is an optional OUT-PARAM: when a set is passed, the numbers of
    records that were `approved` and got re-proposed because the sha moved are
    added to it. Purely an observation -- no decision here changes -- so the
    caller can tell the human WHY their ✅ evaporated. Silence was the bug:
    an approved item whose author pushed simply reappeared as `proposed` with
    no explanation (2026-08-25).

    An out-param rather than a second return value only because `reconcile`
    has 30 call sites; the decision logic is untouched either way.
    """
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
        if ps == "running":
            # A live claim reconciles WHOLLY from the prior record. The
            # executor is mid-flight against exactly this item; merging fresh
            # content in would let a sweep rewrite the why, branch or executor
            # of work already in progress, and the claim would finish against
            # a description of itself that nobody consented to.
            out.append(QueueRecord(number=prior.number, item=prior.item,
                                   first_seen=prior.first_seen, last_seen=now))
            continue
        if prior.item.executor != it.executor:
            # The ARM changed under a stable id (the feedback row moves between
            # runnable `address-feedback` and informational `triage`). A ✅
            # given to one arm is not consent for the other -- "go look at
            # this" is not "reply to the reviewer in my name" -- so any
            # executor change re-proposes. This also un-strands a `needs-input`
            # row whose signal decayed: it becomes an ordinary proposed row
            # again, and therefore dismissable.
            if ps == "approved" and resets is not None:
                resets.add(prior.number)
            merged = dataclasses.replace(it, status="proposed")
        elif ps == _NEEDS_INPUT:
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
        elif prior.item.sha == it.sha and _consent_holds(prior.item, it):
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
            # Fresh wins: the sha moved, so whatever the queue thought is stale.
            # ONLY an approved->proposed reset is reported: `error`->proposed is
            # a retry, `done`+new-sha is a resurrection, and `proposed`->
            # proposed is a no-op -- none of those revoke a human decision.
            if ps == "approved" and resets is not None:
                resets.add(prior.number)
            merged = dataclasses.replace(it, status="proposed")
        out.append(QueueRecord(number=prior.number, item=merged,
                               first_seen=prior.first_seen, last_seen=now))

    for r in existing:
        if r.item.id in fresh_ids:
            continue
        reason = resolved.get(r.item.id)
        if reason in _CLOSES_AN_ERROR:
            if r.item.status == "error":
                # The run failed, and then the signal went away on its own
                # (the reviewer answered or closed everything themselves).
                # Retaining the error row leaves a permanent warning on the
                # dashboard for work that no longer exists.
                out.append(QueueRecord(
                    number=r.number, first_seen=r.first_seen, last_seen=now,
                    item=dataclasses.replace(r.item, status="done",
                                             done_reason=reason)))
                continue
            if r.item.status == "running":
                # A cleared signal RACES the live claim -- the run itself is
                # what clears it. Closing the claim from under the executor
                # would report the work finished before it is. Retain, and let
                # the executor (or the stale reap) settle it.
                out.append(r)
                continue
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
