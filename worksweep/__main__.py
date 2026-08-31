"""Worksweep entry point: collect -> assess -> dedupe -> format -> output.

M1: read-only digest. `--dry-run` prints to stdout; `--discord` posts.
M2: `--discord`/`--dry-run` reconcile the sweep into a persistent queue and
render the digest from the queue's stable numbers; the `intake` subcommand
polls Discord for Chandler's approval replies and flips matched items to
`approved` (no executors — approval is status-only).
M3: the sweep runs one GraphQL query (mirroring the GitLab dashboard) instead
of the old per-repo REST calls; `run_sweep(cfg, deps)` is the dependency-
injected core so tests never shell out, and `main()` just wires real deps
(`glab`, Discord, the queue file) around it. Message contract: every sweep
posts exactly one digest (or 🔍 heartbeat, or ⚠️ error) — never silence.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

# Only ever POST the digest to a Discord-owned host. The webhook comes from
# ~/etc/heartbeat.json (semi-trusted); this allowlist + no-redirect opener stop
# a tampered/typo'd webhook from exfiltrating the digest elsewhere.
_ALLOWED_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")

_SSH_TIMEOUT_SECONDS = 20
# M4 Task G: syncing a branch onto a dev box does a real `git fetch` over the
# network on the far side — the 20s read-only probe budget is far too short.
_SSH_SYNC_TIMEOUT_SECONDS = 300
_HTTP_PROBE_TIMEOUT_SECONDS = 20
# The launchd agent that runs a digest sweep (etc/mini/com.chandlerhardy.worksweep.plist).
# The dashboard's Sync button kickstarts this rather than sweeping in-process.
_SWEEP_AGENT_LABEL = "com.chandlerhardy.worksweep"
# kickstart returns as soon as launchd has started the job, so this is a
# generous ceiling on a call that normally takes milliseconds.
_KICKSTART_TIMEOUT_SECONDS = 15
# Marking a GitLab todo done is a WRITE, unlike collectors._run_glab's reads.
_GLAB_WRITE_TIMEOUT_SECONDS = 30
# A Discord 503 is a delivery blip, not a sweep failure. Without a retry the
# whole sweep reported ⚠️ for work it had already completed and saved
# (2026-08-25, a Sync-triggered sweep). Three attempts total, so a blip costs
# ~7s and a real outage still fails fast enough to be seen.
_POST_ATTEMPTS = 3
_POST_BACKOFF_SECONDS = (2, 5)      # waited after attempt 1, then after 2
# Discord's own 429 wait is authoritative, but a pathological value must not
# park the sweep -- 15s is well under the runner's cadence.
_POST_RETRY_AFTER_CAP_SECONDS = 15

from . import assessor, collectors, curator, devslots, implementer, keepcurrent
from .approvals import apply_approvals
from .config import WorksweepConfig, load_config
from .discord_read import fetch_messages
from . import models
from .models import WorkItem
from .formatter import (
    DISCORD_MAX_CHARS, _FOOTER, _HEADER, _truncate_bytes,
    format_messages_from_records, format_reproposed,
)
from . import reviewedstate
from . import seennotes
from .queue import (QueueLockError, auto_approve, load_queue, null_lock,
                    reconcile, save_queue, write_lock)

_QUEUE_DEFAULT = os.path.expanduser("~/.worksweep/queue.json")
# Notes a human has dismissed. Its own file, because a dismissal has to
# outlive the row it dismissed (see seennotes).
_SEEN_DEFAULT = os.path.expanduser("~/.worksweep/seen-notes.json")
_CURSOR_DEFAULT = os.path.expanduser("~/.worksweep/intake-cursor")


def _queue_path() -> str:
    """Path to the persistent queue (overridable in tests)."""
    return _QUEUE_DEFAULT


def _seen_path() -> str:
    """Path to the dismissed-notes sidecar (overridable in tests)."""
    return _SEEN_DEFAULT


def _reviewed_path() -> str:
    """Path to the re-review sensor's reviewed-state sidecar."""
    return os.path.join(os.path.dirname(_SEEN_DEFAULT), "reviewed-state.json")


def _cursor_path() -> str:
    """Path to the intake last-seen-message-id cursor (overridable in tests)."""
    return _CURSOR_DEFAULT


def _now() -> str:
    """Current UTC timestamp (ISO8601). The CLI edge — pure fns take `now` in."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read_cursor() -> Optional[str]:
    try:
        with open(_cursor_path()) as f:
            val = f.read().strip()
            return val or None
    except FileNotFoundError:
        return None


def _write_cursor(message_id: str) -> None:
    path = _cursor_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(message_id)
    os.replace(tmp, path)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so a 30x can't bounce the POST off-host."""
    def redirect_request(self, *args, **kwargs):
        return None


def _validate_webhook(webhook: str) -> None:
    """Raise RuntimeError unless the webhook is an https Discord-host URL."""
    parts = urllib.parse.urlparse(webhook)
    if parts.scheme != "https":
        raise RuntimeError(f"discord webhook must be https, got {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_WEBHOOK_HOSTS and not any(
            host.endswith("." + h) for h in _ALLOWED_WEBHOOK_HOSTS):
        raise RuntimeError(f"refusing to post to non-Discord host: {host!r}")


def _retry_after_seconds(error) -> Optional[float]:
    """Discord's `Retry-After` in seconds, capped, or None when unusable.

    Discord sends a number of seconds (sometimes fractional). Anything else --
    an HTTP-date, junk, a missing header -- yields None so the caller falls back
    to its own backoff rather than guessing.

    f-004: `float("nan")` parses without complaint and then poisons every check
    downstream -- `nan < 0` is False and `min(nan, cap)` is nan, so a nan
    header sailed through to `sleep(nan)`. `inf` survives the cap but is not a
    wait anybody sent on purpose. Both are junk, so both fall back.
    """
    try:
        raw = error.headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        wait = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(wait) or wait < 0:
        return None
    return min(wait, _POST_RETRY_AFTER_CAP_SECONDS)


def _post_discord(webhook: str, content: str,
                  sleep: Callable[[float], None] = time.sleep) -> None:
    """POST the digest to Discord, retrying transient failures.

    Raises RuntimeError on a bad host, a permanent HTTP error, or exhausted
    retries -- the same shape as before, so every caller's handling is
    unchanged.

    Retries a 5xx or a URLError (the network blip case): the sweep has already
    done its work and saved the queue by the time it posts, so failing the
    whole run over one 503 reports a delivery problem as a work problem.
    A 429 waits Discord's own `Retry-After` when it sends one, and counts as an
    attempt. Any OTHER 4xx raises immediately: a malformed request or a revoked
    webhook never heals by being sent again.

    ACCEPTED RESIDUAL (f-005): a retry can post the digest TWICE. If Discord
    receives and processes the POST but the acknowledgement never gets back to
    us -- a read timeout, a reset after send -- the retry sends the same
    content again. There is no clean fix available: Discord's webhook API takes
    no client-supplied idempotency key or nonce, so a caller cannot mark a
    resend as a duplicate, and the underlying error does not reliably say
    whether the bytes arrived (a refused connection proves they did not; a
    timeout proves nothing either way).

    The direction is chosen deliberately rather than left open. Worksweep's
    top-level contract is that silence is never an outcome, and a duplicate
    digest is cosmetic noise a human reads once and ignores, while a dropped
    one loses the entire sweep's report. So the ambiguous case retries.

    `sleep` is injected so the tests never actually wait.
    """
    _validate_webhook(webhook)
    # `allowed_mentions: {"parse": []}` is global on purpose. Worksweep quotes
    # text other people wrote -- MR titles, and now review thread bodies -- and
    # a quoted "@everyone" must render as characters rather than ring every
    # phone on the server. Setting it at the one place every post funnels
    # through means no future caller can forget it.
    data = json.dumps({"content": content,
                       "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "WorksweepBot/1.0"})
    opener = urllib.request.build_opener(_NoRedirect())

    last_error = None
    for attempt in range(1, _POST_ATTEMPTS + 1):
        wait: Optional[float] = None
        try:
            with opener.open(req, timeout=15) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            # MUST be caught before URLError -- HTTPError is a subclass of it,
            # so the reverse order would treat every 4xx as retryable.
            last_error = e
            if e.code == 429:
                wait = _retry_after_seconds(e)
            elif not 500 <= e.code < 600:
                raise RuntimeError(f"discord post failed: {e}")
        except urllib.error.URLError as e:
            last_error = e

        if attempt == _POST_ATTEMPTS:
            break
        if wait is None:
            # f-001: indexed by attempt, so bumping _POST_ATTEMPTS without
            # extending the table would raise IndexError from inside the very
            # handler that exists to survive failures. Fall back to the longest
            # known wait instead.
            wait = _POST_BACKOFF_SECONDS[min(attempt - 1,
                                             len(_POST_BACKOFF_SECONDS) - 1)]
        print(f"worksweep: discord post attempt {attempt} failed "
              f"({last_error}); retrying in {wait}s", file=sys.stderr)
        sleep(wait)
    raise RuntimeError(f"discord post failed: {last_error}")


def _kickstart_sweep(run_subprocess: Callable = subprocess.run) -> None:
    """Ask launchd to run the sweep agent now. Raises RuntimeError on failure.

    This is the dashboard's "Sync" edge, injected into `dashboard.serve` so the
    dashboard module never learns about launchctl (and its tests never spawn a
    process).

    `kickstart` runs the job under its OWN agent -- its environment, its log
    files, its Discord digest -- and returns as soon as launchd has started it,
    so this call is fast even though the sweep itself takes ~90s. That
    out-of-process property is the point: the sweep writes the queue, and
    running it inside the dashboard would mean two writers on one file plus a
    blocked request thread.
    """
    target = f"gui/{os.getuid()}/{_SWEEP_AGENT_LABEL}"
    try:
        result = run_subprocess(
            ["launchctl", "kickstart", target],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_KICKSTART_TIMEOUT_SECONDS)
    except Exception as e:
        raise RuntimeError(f"launchctl kickstart {target}: {e}")
    if getattr(result, "returncode", 1) != 0:
        detail = ((getattr(result, "stderr", "") or
                   getattr(result, "stdout", "") or "").strip()[:200])
        raise RuntimeError(
            f"launchctl kickstart {target} exited "
            f"{result.returncode}: {detail or 'no output'}")


def _mark_todo_done(todo_id: int,
                    run_subprocess: Callable = subprocess.run) -> None:
    """Mark GitLab todo `todo_id` done. Raises RuntimeError on failure.

    The dashboard's Dismiss edge, injected into `dashboard.serve` so the
    dashboard never learns about glab. A failure here is logged and swallowed by
    the caller: clearing the GitLab todo is a courtesy on top of the local
    dismiss, not the point of the action.

    NOTE: this cannot fire today. Worksweep never captures a todo's numeric id
    (`collectors.parse_todos` reads only target_type/action_name/target_url), so
    `dashboard.todo_id_of` always returns 0. The edge is wired and tested so it
    works the moment an id IS carried -- see the report.
    """
    args = ["api", f"todos/{int(todo_id)}/mark_as_done", "-X", "POST"]
    try:
        result = run_subprocess(
            ["glab", *args], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=_GLAB_WRITE_TIMEOUT_SECONDS)
    except Exception as e:
        raise RuntimeError(f"glab {' '.join(args)}: {e}")
    if getattr(result, "returncode", 1) != 0:
        detail = ((getattr(result, "stderr", "") or
                   getattr(result, "stdout", "") or "").strip()[:200])
        raise RuntimeError(f"glab {' '.join(args)} exited "
                           f"{result.returncode}: {detail or 'no output'}")


def run_ssh(host: str, command: str, timeout: int = _SSH_TIMEOUT_SECONDS) -> str:
    """M4 Task F ssh edge (read-only): run `command` on `host` via the `ssh`
    binary and return stdout. Raises RuntimeError on timeout, a missing ssh
    binary, or a non-zero exit -- mirrors collectors._run_glab. Callers
    (devslots.probe) catch this per-box and degrade to an unknown branch/sha
    rather than losing the whole sweep to one unreachable box."""
    try:
        # stdin=DEVNULL (the `ssh -n` equivalent): without it ssh hands the
        # parent's stdin to the remote command, which under launchd is the
        # same non-TTY hazard fixed for `claude -p` in c0e7791.
        result = subprocess.run(["ssh", host, command],
                                stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ssh {host} timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("ssh not found on PATH")
    if result.returncode != 0:
        raise RuntimeError(f"ssh {host} failed: {(result.stderr or '').strip()}")
    return result.stdout


def http_status(url: str, timeout: int = _HTTP_PROBE_TIMEOUT_SECONDS) -> int:
    """M4 Task G http edge: GET `url` and return its status code. Used only to
    prove a dev box still serves 200 after a sync, so an HTTP error response
    is a RESULT (its code), not an exception; anything that isn't an HTTP
    response at all raises and implementer.sync_to_box turns it into a
    RunnerError."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "WorksweepBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _implement_boxes(cfg: WorksweepConfig) -> list:
    """Probe + classify the dev boxes for one implement claim.

    Deliberately re-runs the GraphQL sweep instead of trusting the queue: the
    tier of a box depends on the CURRENT state of the MR sitting on its branch
    (merged? approved and handed off? under live review?), and the runner
    fires on its own cadence, minutes to hours after the digest that proposed
    the item. Raises on failure — the runner's implement pass catches it and
    turns it into an error status + ⚠️ rather than guessing a box is free.
    """
    if not cfg.dev_boxes:
        return []
    boxes = devslots.probe(list(cfg.dev_boxes), run_ssh)
    review_mrs, authored, assigned = collectors.parse_graphql_sweep(
        collectors.run_graphql_sweep(), cfg.username, cfg.repos)
    records = load_queue(_queue_path())
    claimed = frozenset(r.item.dev_box for r in records
                        if r.item.dev_box and r.item.executor == "implement"
                        and r.item.status in ("running", "approved"))
    return implementer.annotate_boxes(boxes, review_mrs + authored + assigned,
                                      cfg.username, claimed=claimed)


def _run_intake(cfg: WorksweepConfig) -> int:
    """Poll Discord for approval replies and flip matched queue items.

    Read-only Discord (one GET) + an optional confirmation POST via the M1
    webhook. No executor runs — approval is status-only.
    """
    if not cfg.bot_token or not cfg.channel_id:
        print("worksweep: intake needs a discord bot_token + channel_id "
              "(set the `discord` block in ~/etc/heartbeat.json)", file=sys.stderr)
        return 1

    qpath = _queue_path()
    records = load_queue(qpath)
    after = _read_cursor()
    try:
        messages = fetch_messages(cfg.channel_id, cfg.bot_token, after=after)
    except Exception as e:
        print(f"worksweep: discord fetch failed: {e}", file=sys.stderr)
        return 1

    now = _now()
    try:
        with write_lock(qpath):
            # Re-load INSIDE the lock: the Discord fetch above can take
            # seconds, and a sweep or a dashboard tap may have written since
            # the snapshot at the top of this function.
            records = load_queue(qpath)
            updated, approved = apply_approvals(records, messages,
                                                cfg.discord_user_id, now)
            if approved != set():
                save_queue(qpath, updated)
    except QueueLockError as e:
        print(f"worksweep: intake could not lock the queue: {e}",
              file=sys.stderr)
        return 1

    # Advance the cursor to the newest message id seen so the next poll only
    # reads newer messages (Discord ids are monotonically increasing snowflakes).
    if messages:
        newest = max(messages, key=lambda m: int(m.id) if m.id.isdigit() else 0)
        if newest.id:
            _write_cursor(newest.id)

    if approved:
        nums = sorted(approved)
        by_num = {r.number: r for r in updated}
        details = ", ".join(
            f"{n} ({by_num[n].item.executor} {by_num[n].item.repo})".strip()
            for n in nums if n in by_num)
        confirm = f"✅ Approved: {details}"
        if cfg.discord_webhook:
            try:
                _post_discord(cfg.discord_webhook, confirm)
            except Exception as e:
                print(f"worksweep: confirmation post failed: {e}", file=sys.stderr)
        else:
            print(confirm)
    return 0


def _with_unaddressed(mr, cfg: WorksweepConfig,
                      discussions: Callable[[str, int], str],
                      seen=()) -> tuple:
    """`(mr rebound with its unaddressed-thread count, probe_ok)`. Never raises.

    Skipped (count stays 0, probe_ok True) for an MR with nothing unresolved --
    there is nothing for the probe to find -- and for one already handed off,
    whose threads belong to the maintainer who will merge it.

    A probe failure prints and returns the MR untouched with probe_ok False.
    That flag matters twice: the caller must not DROP a feedback row it can no
    longer derive (a freed number gets reused, and the highest is reused
    first, so a stale `✅ 12` would approve something else entirely), and it
    must not claim the signal is CLEAR when it simply could not look.
    """
    # Two preconditions, either of which can produce work, so the gate is the
    # union rather than the old resolvable-only half:
    #   * unresolved threads  -> a resolvable thread may be waiting on us;
    #   * listed reviewers    -> a plain MR note from one of them is review
    #     feedback (!4084), and it creates NO resolvable discussion at all, so
    #     `unresolved_count` is 0 and the old gate skipped it entirely.
    # With neither, nothing this probe could find would qualify.
    if not (mr.unresolved_count > 0 or mr.reviewers):
        return mr, True
    if assessor.is_handed_off(mr, cfg.username):
        return mr, True
    try:
        raw = discussions(mr.repo, mr.iid)
    except Exception as e:
        print(f"worksweep: discussions probe for {mr.repo}!{mr.iid} failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return mr, False
    threads = collectors.unaddressed_threads(raw, cfg.username, mr.reviewers,
                                             seen)
    # Carry the evidence onto the MR so the emitted row can carry it too: the
    # dashboard only has the row, and a dismissal keys on (discussion, note).
    return dataclasses.replace(
        mr, unaddressed_count=len(threads),
        note_refs=tuple((t.id, t.last_note_id) for t in threads)), True


PROBE_FAILED_MARKER = "(probe failed)"
# Statuses reconcile RETAINS when an id stops being emitted. Only these can
# strand, so only these are ever worth an MR-state probe: `proposed` is dropped
# outright, and `done`/`running` are already settled or still live.
_STRANDABLE = ("error", "needs-input", "approved")
_MR_IID_RE = re.compile(r"/merge_requests/(\d+)")


def _mr_ref(item) -> tuple:
    """(repo, iid) for a row that refers to a merge request, else None.

    Read off the row itself rather than parsed out of its id: the four id
    shapes an MR can strand (`stale:`, `feedback:`, `magi:...@sha`,
    `hygiene-devurl:`) have nothing in common but the MR they point at, and
    enumerating them here would go stale the next time one is added.
    """
    m = _MR_IID_RE.search(item.web_url or "")
    if not m or not item.repo:
        return None
    return (item.repo, int(m.group(1)))


def _merged_mr_resolutions(records, emitted: set,
                           probe: Callable[[str, int], str]) -> dict:
    """{item id: "mr-merged"} for every row stranded by a finished MR.

    The sweep only ever queries OPEN merge requests, so the moment one merges
    its rows stop being emitted and anything retained sits there forever --
    which is exactly how a keep-current row for !3997 stayed `error` after the
    merge deleted its branch.

    Bounded by construction: only retained-and-gone rows are candidates, and
    each (repo, iid) is asked at most once, so the normal sweep does zero
    probes. Reads only -- the caller runs this OUTSIDE the queue lock and
    applies the result inside.
    """
    stranded: dict = {}
    for r in records:
        if r.item.status not in _STRANDABLE or r.item.id in emitted:
            continue
        ref = _mr_ref(r.item)
        if ref is not None:
            stranded.setdefault(ref, []).append(r.item.id)
    out: dict = {}
    for ref, ids in stranded.items():
        try:
            state = probe(*ref)
        except Exception as e:
            # Fail-safe, like the discussions probe: not knowing an MR is
            # finished is not the same as knowing it is not.
            print(f"worksweep: MR state probe for {ref[0]}!{ref[1]} failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            continue
        if collectors.is_closed_state(state):
            for ident in ids:
                out[ident] = "mr-merged"
    return out


def _retained_feedback(prior: WorkItem) -> WorkItem:
    """The prior feedback row, carried forward because the probe could not
    re-derive it. Marked so the digest says why it looks stale.

    f-006: the STATUS is preserved, not reset. This used to hard-set
    "proposed", so a transient network error silently spent a human's ✅ --
    approved work stopped happening and the only signal was the row quietly
    reappearing as unapproved. A probe blip is worksweep failing to look, not
    the human changing their mind, and it may not revoke consent.
    """
    why = prior.why or ""
    if PROBE_FAILED_MARKER not in why:
        why = f"{why} {PROBE_FAILED_MARKER}".strip()
    return dataclasses.replace(prior, why=why)


def run_sweep(cfg: WorksweepConfig, deps: Dict[str, Callable]) -> int:
    """One sweep under the message contract: digest, 🔍 heartbeat, or ⚠️ error —
    never silence. All I/O arrives via `deps` so tests stay hermetic."""
    post = deps["post"]

    def _post_all(messages) -> None:
        if cfg.discord_webhook:
            for m in messages:
                post(cfg.discord_webhook, m)
        else:
            for m in messages:
                print(m)

    try:
        raw = deps["graphql"]()
        review_mrs, authored, assigned = collectors.parse_graphql_sweep(
            raw, cfg.username, cfg.repos)

        # Which of the authored MRs' unresolved threads are actually waiting
        # on Chandler? The GraphQL sweep only knows the COUNT, so the answer
        # comes from one targeted REST call per authored MR that has any
        # unresolved thread (typically two or three across the whole queue),
        # and the MergeRequest is REBOUND with the answer right here -- before
        # bootstrap_magi_records, the assess loop, the stale loop and
        # resolutions all read the same `authored` list, so none of them can
        # disagree about it. Opt-in via deps["discussions"] (absent -> skipped,
        # matching the Task H diverged-commits pattern) and never fatal: a bad
        # call degrades that ONE MR to zero unaddressed threads, which costs a
        # sweep of address-feedback work, not the digest.
        records0 = deps["load"]()
        prior_by_id = {r.item.id: r.item for r in records0}
        discussions_edge = deps.get("discussions")
        # Notes a human has already dismissed. Fail-safe in the NOISY
        # direction: not knowing what was dismissed shows a row again, which
        # is recoverable, while losing the sweep is not.
        seen_notes = frozenset()
        seen_edge = deps.get("seen_notes")
        if seen_edge is not None:
            try:
                seen_notes = frozenset(seen_edge())
            except Exception as e:
                print(f"worksweep: could not read dismissed notes: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
        probe_failed = set()
        if discussions_edge is not None:
            probed = []
            for mr in authored:
                mr, ok = _with_unaddressed(mr, cfg, discussions_edge,
                                           seen_notes)
                probed.append(mr)
                if not ok:
                    probe_failed.add(f"feedback:{mr.repo}!{mr.iid}")
            authored = probed

        items = []
        for mr in review_mrs:
            items += assessor.assess_review_request(mr, cfg.username)
        # Re-review sensing (the reverse-direction twin of the plain-note
        # sensor). The sidecar remembers the head each reviewed MR was at;
        # first sight SEEDS quietly, a later head fires a row. Edges are
        # opt-in like every other side channel: absent -> sensor off.
        reviewed_load = deps.get("reviewed_state")
        reviewed_record = deps.get("record_reviewed")
        if reviewed_load is not None:
            reviewed = {}
            try:
                reviewed = dict(reviewed_load())
            except Exception as e:
                print(f"worksweep: could not read reviewed-state: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
            for mr in review_mrs:
                if (cfg.username in mr.reviewers
                        and mr.my_review_state in assessor.RE_REVIEW_WAITING_STATES):
                    key = f"{mr.repo}!{mr.iid}"
                    if key not in reviewed:
                        if reviewed_record is not None:
                            try:
                                reviewed_record(key, mr.sha)
                            except Exception as e:
                                print(f"worksweep: could not seed reviewed-state "
                                      f"for {key}: {type(e).__name__}: {e}",
                                      file=sys.stderr)
                        continue
                    items += assessor.assess_re_review(mr, cfg.username,
                                                       reviewed[key])
        records0 = assessor.bootstrap_magi_records(
            records0, authored, deps["now"]())
        for mr in authored:
            items += assessor.assess_own_mr(
                mr, cfg.username,
                has_magi=lambda r, i, s: assessor.has_magi_done(records0, r, i, s))
        # M4 Task H: stale-branch sensing — one REST call per authored MR
        # that isn't already handed off (the maintainer will merge those;
        # Chandler doesn't need a keep-current nag for someone else's merge).
        # Entirely opt-in via deps["diverged_commits"] (absent -> skipped,
        # matching the Task F ssh-dep pattern) and never fatal to the sweep:
        # one bad glab call just degrades that MR's check to "unknown".
        diverged_edge = deps.get("diverged_commits")
        if diverged_edge is not None:
            for mr in authored:
                if assessor.is_handed_off(mr, cfg.username):
                    continue
                try:
                    diverged = diverged_edge(mr.repo, mr.iid)
                except Exception as e:
                    print(f"worksweep: diverged-commits check for "
                          f"{mr.repo}!{mr.iid} failed: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    continue
                items += assessor.assess_stale(mr, diverged, cfg.stale_threshold)
        # An assigned-to-me MR not already surfaced as a review-requested or
        # authored item this sweep gets a lightweight triage item.
        tracked_mr_ids = {(mr.repo, mr.iid) for mr in review_mrs + authored}
        for mr in assigned:
            items += assessor.assess_assigned_mr(mr, cfg.username, tracked_mr_ids)
        covered_issues = assessor.covered_issue_iids(authored)
        for repo in cfg.repos:
            try:
                for iss in deps["issues"](repo, cfg.username):
                    items += assessor.assess_issue(iss, covered=covered_issues)
            except Exception as e:
                print(f"worksweep: issues for {repo} failed: {e}", file=sys.stderr)
        items = assessor.dedupe(items)

        # GitLab todos are noisier than the GraphQL sweep's authoritative
        # buckets (review/authored/assigned) -- drop a todo whose action is
        # already covered by a bucket, or whose (fragment/slash-normalized)
        # URL matches an item or MR already surfaced this sweep, so the
        # digest doesn't show the same thing twice under two executors.
        try:
            todos_raw = deps["todos"]()
        except Exception as e:
            print(f"worksweep: todos collection failed: {e}", file=sys.stderr)
            todos_raw = []
        surviving_todos = assessor.filter_todos(
            todos_raw, items, review_mrs + authored + assigned)
        for td in surviving_todos:
            items += assessor.assess_todo(td)
        items = assessor.dedupe(items)

        # A feedback row the probe could not re-derive is carried forward
        # rather than dropped. Only when the assessor produced nothing for it:
        # `changes_requested` is known without the probe, so that arm is still
        # derivable and must not be shadowed by a stale row.
        emitted = {it.id for it in items}
        for fid in sorted(probe_failed - emitted):
            if fid in prior_by_id:
                items.append(_retained_feedback(prior_by_id[fid]))

        resolved = assessor.resolutions(review_mrs, cfg.username, authored)
        if reviewed_load is not None:
            resolved.update(assessor.re_review_resolutions(
                review_mrs, cfg.username, reviewed))
        # An MR that merged or closed takes every row it stranded with it.
        # Probing happens HERE, outside the lock, because it is a read; the
        # closures are applied by reconcile inside it.
        state_probe = deps.get("mr_state")
        if state_probe is not None:
            resolved.update(_merged_mr_resolutions(records0, emitted,
                                                   state_probe))
        # A feedback id whose signal is PROVABLY gone -- nothing unaddressed,
        # no changes requested, and the probe actually looked. This is the one
        # reason strong enough to close an `error` row instead of retaining it
        # forever (see queue._CLOSES_AN_ERROR), so the bar is evidence, not
        # absence of evidence: with no probe wired every MR reads
        # unaddressed_count 0, which is ignorance, and acting on it would close
        # errored rows across the whole queue on no information at all.
        if discussions_edge is not None:
            for mr in authored:
                fid = f"feedback:{mr.repo}!{mr.iid}"
                if (fid not in resolved and fid not in probe_failed
                        and mr.unaddressed_count == 0
                        and not mr.changes_requested):
                    resolved[fid] = "signal-cleared"
        # Observe (not change) reconcile's fresh-wins rule so a revoked ✅ can
        # be explained instead of the item just quietly reappearing.
        reproposed: set = set()
        # Everything above is sensing (GraphQL, probes, todos) and touches no
        # state. The lock goes around the read-modify-write ONLY -- holding it
        # across ~90s of network work would stall every dashboard tap.
        try:
            with deps.get("queue_lock", null_lock)():
                # Re-load inside the lock: `records0` was read before all that
                # network work, and intake or the dashboard may have written
                # since. Bootstrap is idempotent, so re-running it here is
                # cheap and keeps the seeded rows.
                fresh_records = deps["load"]()
                fresh_records = assessor.bootstrap_magi_records(
                    fresh_records, authored, deps["now"]())
                records = reconcile(fresh_records, items, deps["now"](),
                                    resolved=resolved, resets=reproposed)
                records = auto_approve(records, cfg.auto_approve)
                deps["save"](records)
        except QueueLockError as e:
            print(f"worksweep: could not lock the queue: {e}", file=sys.stderr)
            records = records0
        except OSError as e:
            print(f"worksweep: could not persist queue: {e}", file=sys.stderr)

        # done/error records are terminal — they've already been reported (or
        # failed and are excluded from re-proposal) so they'd only clutter the
        # digest. The actionable check must use the same filtered set the
        # formatter renders, or "nothing needs you" and an empty digest could
        # disagree.
        actionable = [r for r in records if r.item.status not in ("done", "error")]

        # M4 Task F: dev-slot sensing. Entirely opt-in (cfg.dev_boxes empty
        # -> deps["ssh"] is never touched, matching every pre-M4 caller/test)
        # and never fatal to the sweep — a probe failure just means no
        # preamble line this round, not a failed digest.
        slot_line: Optional[str] = None
        if cfg.dev_boxes:
            ssh_edge = deps.get("ssh")
            if ssh_edge is None:
                print("worksweep: runner.dev_boxes configured but no ssh dep "
                      "provided — skipping dev-slot sensing", file=sys.stderr)
            else:
                try:
                    boxes = devslots.probe(list(cfg.dev_boxes), ssh_edge)
                    claimed = frozenset(
                        r.item.dev_box for r in records
                        if r.item.dev_box and r.item.executor == "implement"
                        and r.item.status in ("running", "approved"))
                    tiers = devslots.classify(
                        boxes, review_mrs + authored + assigned, cfg.username,
                        claimed=claimed)
                    slot_line = devslots.summary_line(tiers)
                except Exception as e:
                    print(f"worksweep: dev-slot sensing failed: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    slot_line = None

        if actionable:
            curated = None
            run_llm = deps.get("llm")
            if cfg.curate and run_llm is not None:
                curated = curator.curate(actionable, deps["now"](), run_llm,
                                         preamble=slot_line)
            if curated is not None:
                curated = curator.linkify(curated, actionable)  # deterministic, post-validation
                n, m = curator.partition_counts(actionable)
                head = (f"{_HEADER}\n**{n} actionable** · {m} held · curated\n"
                        + (f"-# {slot_line}\n" if slot_line else ""))
                tail = f"\n{_FOOTER}"
                # Fixed parts (header, slot line, footer) must always survive:
                # give the LLM body whatever budget remains and truncate ONLY
                # the body. Truncating the assembled string from the end would
                # silently eat the ✅-instructions footer once a multi-box slot
                # line is present (Task F review finding, 2026-08-18).
                fixed = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
                body_budget = max(200, DISCORD_MAX_CHARS - fixed)
                curated = curator.fit_links(curated, body_budget)   # drop links before bytes
                _post_all([head + _truncate_bytes(curated, body_budget) + tail])
            else:
                _post_all(format_messages_from_records(
                    actionable, now=deps["now"](), preamble=slot_line))
        else:
            _post_all([f"🔍 Worksweep: nothing needs you "
                       f"(checked {len(review_mrs)} review requests, "
                       f"{len(authored)} authored MRs)"])

        # Immediately after the digest, so the explanation sits next to the
        # items it explains. Never fatal: the digest is the contract, and a
        # failed footnote must not turn a good sweep into a ⚠️ error post.
        if reproposed:
            try:
                by_num = {r.number: r.item for r in records}
                line = format_reproposed(
                    [(n, by_num[n]) for n in sorted(reproposed) if n in by_num])
                if line:
                    _post_all([line])
            except Exception as e:
                print(f"worksweep: re-proposed notice failed: {e}",
                      file=sys.stderr)
        return 0
    except Exception as e:
        msg = f"⚠️ Worksweep sweep failed: {type(e).__name__}: {e}"
        try:
            if cfg.discord_webhook:
                post(cfg.discord_webhook, msg)
        except Exception as post_err:
            print(f"worksweep: error post also failed: {post_err}", file=sys.stderr)
        print(msg, file=sys.stderr)
        return 1


def _execute_implement(item, cfg, boxes):
    """Real implement edge: subprocess + a longer-budget ssh + an http probe."""
    return implementer.execute(
        item, cfg, boxes, run_subprocess=subprocess.run,
        run_ssh=lambda host, command: run_ssh(
            host, command, timeout=_SSH_SYNC_TIMEOUT_SECONDS),
        http_get=http_status, run_glab=_run_glab_api)


def _run_glab_api(args, body=None, run_subprocess: Callable = subprocess.run):
    """The park executor's glab edge: one `glab api` call, optionally with a
    raw JSON request body on stdin.

    `--input -` rather than `-f key=value` because glab's own help is explicit
    that "neither --field nor --raw-field parses JSON arrays or objects" -- an
    MR description full of newlines and markdown must go over as a JSON body or
    it arrives mangled (the 2026-08 array bug).
    """
    try:
        result = run_subprocess(
            ["glab", *args],
            input=body if body is not None else None,
            stdin=None if body is not None else subprocess.DEVNULL,
            capture_output=True, text=True,
            timeout=_GLAB_WRITE_TIMEOUT_SECONDS)
    except Exception as e:
        raise RuntimeError(f"glab {' '.join(args)}: {e}")
    if result.returncode != 0:
        raise RuntimeError(f"glab {' '.join(args)} exited "
                           f"{result.returncode}: "
                           f"{(result.stderr or '').strip()[:200] or 'no output'}")
    return result.stdout


def _execute_park(item, cfg):
    """Real park edge: probe/classify the boxes, then ssh + http + glab.

    Boxes are re-probed here (not trusted from the digest) for the same reason
    the implement pass does it: the runner fires minutes to hours after the
    sweep that proposed the item, and a box that was free then may not be now.
    """
    from . import park
    return park.execute(
        item, cfg, _implement_boxes(cfg),
        run_ssh=lambda host, command: run_ssh(
            host, command, timeout=_SSH_SYNC_TIMEOUT_SECONDS),
        http_get=http_status,
        run_glab=_run_glab_api)


def _dry_run_park(item, cfg):
    """--dry-run must never take a box or rewrite an MR."""
    from . import park
    return park.ParkResult(iid=0, box_name="(dry-run)", dev_url="",
                           result_sha=item.sha, description_updated=False)


def _execute_address_feedback(item, cfg):
    """Real address-feedback edge: subprocess (git + the claude pass) and a
    read-only glab GET of the MR's threads.

    The replies themselves are posted by the claude run, inside the worktree,
    under Chandler's own glab credentials -- this edge only ever reads, so
    everything Python asserts afterwards is independent of what the run says
    it did.
    """
    from . import feedback
    return feedback.execute(item, cfg, run_subprocess=subprocess.run,
                            run_glab=_run_glab_api, now=_now)


def _dry_run_address_feedback(item, cfg):
    """--dry-run must never post a reply under Chandler's name."""
    from . import feedback
    return feedback.FeedbackResult(iid=0, result_sha=item.sha,
                                   already_answered=True)


def _execute_keep_current(item, cfg):
    """Real keep-current edge: subprocess + two ssh budgets + an http probe.
    `cfg.dev_boxes` is the raw box-config list — keepcurrent.execute probes
    it itself (devslots.probe) to find whichever box, if any, currently has
    the stale branch checked out.

    review fix I5: that probe is a fan-out over EVERY configured box, so it
    gets the plain (20s) `run_ssh` edge — the same one `_implement_boxes`
    uses for its own probing. Only `sync_to_box`, which touches exactly ONE
    box once it's found, gets the longer 300s-wrapped edge (mirrors
    `_execute_implement`'s single sync-budget edge above).
    """
    return keepcurrent.execute(
        item, cfg, list(cfg.dev_boxes), run_subprocess=subprocess.run,
        run_ssh_probe=run_ssh,
        run_ssh=lambda host, command: run_ssh(
            host, command, timeout=_SSH_SYNC_TIMEOUT_SECONDS),
        http_get=http_status)


def _dry_run_keep_current(item, cfg):
    return keepcurrent.KeepCurrentResult(
        iid=keepcurrent.iid_of(item), ahead_count=0, box_name="",
        scss_recompiled=False, result_sha=item.sha, dev_url="")


def _dry_run_implement(item, cfg, boxes):
    box = boxes[0] if boxes else None
    iid = implementer.issue_iid(item)
    return implementer.ImplementResult(
        iid=iid, mr_iid=0, mr_url="", dev_url=getattr(box, "url", ""),
        dev_box=getattr(box, "name", ""),
        branch=implementer.branch_name(iid, item.title or ""),
        report_path="(dry-run)", verdict="", result_sha=item.sha,
        magi_note="dry-run: nothing was pushed, opened or synced")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("command", nargs="?", choices=["intake", "run", "dashboard"],
                    help="`intake` polls Discord for approval replies; "
                         "`run` executes one approved magi-review item; "
                         "`dashboard` serves the queue view + approval buttons")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, no Discord")
    ap.add_argument("--discord", action="store_true", help="post digest to Discord")
    ap.add_argument("--port", type=int, default=8787,
                    help="dashboard port (default 8787)")
    ap.add_argument("--bind", default="auto",
                    help="dashboard bind address; `auto` resolves the Tailscale "
                         "IP and falls back to 127.0.0.1")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except RuntimeError as e:
        print(f"worksweep: {e}", file=sys.stderr)
        return 1

    # Resolve the domain gate ONCE per process, from the configured registry
    # (or the default ferdinand-checkout path). Every later
    # touches_domain_gate() call uses this resolution; a missing or invalid
    # registry leaves the baked-in fallback in force.
    models.refresh_domain_gate(cfg.domain_registry_path
                               or models.DEFAULT_DOMAIN_REGISTRY)

    if args.command == "intake":
        return _run_intake(cfg)

    if args.command == "dashboard":
        from . import dashboard as _dashboard
        # The Discord poster is INJECTED: dashboard.py must not import this
        # module (this one imports it), and the injection also keeps the
        # dashboard tests off the network entirely.
        return _dashboard.serve(_queue_path(), port=args.port, bind=args.bind,
                                post=_post_discord, webhook=cfg.discord_webhook,
                                sweep=_kickstart_sweep,
                                mark_todo_done=_mark_todo_done,
                                seen_path=_seen_path(),
                                record_reviewed=lambda key, sha:
                                    reviewedstate.record_state(
                                        _reviewed_path(), key, sha, _now()))

    if args.command == "run":
        from . import runner as _runner
        # --dry-run must be a preview, never a mutation: it may READ the live
        # queue but never persists a claim/done nor posts to Discord. (2026-08-18:
        # a diagnostic dry-run consumed a real ✅ and posted a fake 🧙 verdict.)
        deps = {
            "load": lambda: load_queue(_queue_path()),
            "save": ((lambda records: print("worksweep: dry-run — queue NOT saved"))
                     if args.dry_run else
                     (lambda records: save_queue(_queue_path(), records))),
            "post": ((lambda hook, content: print(f"[dry-run post] {content}"))
                     if args.dry_run else _post_discord),
            "now": _now,
            "execute": (lambda item, c: (item.sha, "(dry-run)"))
                       if args.dry_run else _runner.execute,
            # M4 Task G. Box probing stays real under --dry-run (read-only ssh,
            # nothing to preview around); the executor itself does not, since
            # it pushes, opens an MR and rewrites a dev box.
            "boxes": lambda: _implement_boxes(cfg),
            "execute_implement": (_dry_run_implement if args.dry_run
                                  else _execute_implement),
            "execute_keep_current": (_dry_run_keep_current if args.dry_run
                                     else _execute_keep_current),
            "execute_park": (_dry_run_park if args.dry_run else _execute_park),
            "execute_address_feedback": (_dry_run_address_feedback
                                         if args.dry_run
                                         else _execute_address_feedback),
            # --dry-run never saves, so it never needs to exclude anyone.
            "queue_lock": (null_lock if args.dry_run
                           else (lambda: write_lock(_queue_path()))),
        }
        return _runner.run_once(cfg, deps)

    if args.discord and not args.dry_run and not cfg.discord_webhook:
        print("worksweep: no discord_webhook configured", file=sys.stderr)
        return 1

    deps = {
        "graphql": collectors.run_graphql_sweep,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
        "post": _post_discord,
        "load": lambda: load_queue(_queue_path()),
        "save": lambda records: save_queue(_queue_path(), records),
        "now": _now,
    }
    if args.dry_run or not args.discord:
        deps["post"] = lambda hook, content: print(content)
    # --dry-run must never shell out to the curator LLM (it's meant to be a
    # side-effect-free preview); every other invocation gets the real edge.
    if not args.dry_run:
        deps["llm"] = curator.make_run_llm(cfg)
    # M4 Task F: dev-slot ssh probing is read-only (git branch/rev-parse on
    # the box), so --dry-run still runs it for real -- unlike the LLM/post
    # edges above, there's no side effect to preview around. run_sweep only
    # ever calls this when cfg.dev_boxes is non-empty.
    deps["ssh"] = run_ssh
    # M4 Task H: stale-branch REST probe is read-only (one GET per authored
    # MR), so --dry-run still runs it for real -- same reasoning as "ssh"
    # above. run_sweep only ever calls it for authored MRs not handed off.
    deps["diverged_commits"] = collectors.collect_diverged_commits_count
    # Same reasoning: one read-only GET per authored MR with unresolved
    # threads, so --dry-run runs it for real too.
    deps["discussions"] = collectors.collect_discussions
    # One read-only GET per stranded MR, and normally none at all -- same
    # reasoning as the probes above, so --dry-run runs it for real too.
    deps["mr_state"] = collectors.collect_mr_state
    deps["seen_notes"] = lambda: seennotes.load_seen(_seen_path(), _now())
    deps["reviewed_state"] = lambda: reviewedstate.load_state(
        _reviewed_path(), _now())
    deps["record_reviewed"] = lambda key, sha: reviewedstate.record_state(
        _reviewed_path(), key, sha, _now())
    if not args.dry_run:
        deps["queue_lock"] = lambda: write_lock(_queue_path())
    return run_sweep(cfg, deps)


if __name__ == "__main__":
    raise SystemExit(main())
