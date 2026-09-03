"""Worksweep mini dashboard: a self-contained HTML view of `~/.worksweep/queue.json`
with an approval write surface, served on the tailnet from the always-on mini.

Discord is a great control and notification surface but a poor *view*. This
module renders the queue as one page -- what needs Chandler, what is in flight,
what ran itself, what just landed, what broke -- and lets him approve from a
phone with big touch targets instead of typing `✅ 1,3`.

Design constraints, all load-bearing:

* **Stdlib only, one module, no framework.** Matches the repo's discipline; the
  page carries its own CSS and JS inline so there is no asset pipeline and no
  second deploy surface.
* **Every edge is injected** (`resolve_bind(..., run_subprocess=...)`,
  `serve(..., post=...)`), mirroring keepcurrent.py: this module never shells
  out or reaches the network on its own, so the tests never do either. The
  Discord poster in particular arrives by injection from the CLI -- the module
  that owns it imports this one, so importing it back would be circular.
* **The status rules live in approvals.py, never here.** Both POST routes go
  through `approvals.approve_numbers` / `approvals.approve_all`, so a dashboard
  approval and a Discord ✅ can never mean different things. This module holds
  no status tuple and never writes a status itself.
* **Nothing here may raise.** The launchd agent runs `KeepAlive`, so an
  exception escaping a handler is a crash loop. `queue.load_queue` already
  degrades on a missing/garbage queue; the handlers add a belt-and-braces catch
  rather than a stricter parse.
* **The GET path never writes.** The POST paths re-read the queue from disk
  immediately before flipping (never from the rendered snapshot, which may be
  60s stale) and write through the unmodified atomic `queue.save_queue`.
"""
from __future__ import annotations

import datetime
import html
import ipaddress
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .approvals import (_APPROVABLE, approve_all, approve_numbers,
                         is_blanket_eligible)
from .formatter import DISCORD_MAX_CHARS, _truncate_bytes
from .seennotes import record_seen
from .models import RUNNABLE_EXECUTORS, QueueRecord, WorkItem
from .queue import (_TERMINAL, accept_rec as accept_rec_record,
                    dismiss as dismiss_record, is_dismissable, load_queue,
                    request_consult as request_consult_record, save_queue,
                    write_lock)

DEFAULT_PORT = 8787
_LOOPBACK = "127.0.0.1"
# The CSRF defense (decision 10): a cross-origin page cannot attach a custom
# header without a CORS preflight, and this server answers none -- so the mere
# PRESENCE of this header proves the request came from our own page. Do not add
# do_OPTIONS and do not emit any Access-Control-* header, or the guard evaporates.
_CSRF_HEADER = "X-Worksweep"
_MAX_BODY_BYTES = 64 * 1024
_DONE_WINDOW_DAYS = 7
_RECENT_DONE_LIMIT = 20
_LAYOUT_STORAGE_KEY = "worksweep-layout"
# Tailnet addresses live in the CGNAT range; anything else (a LAN address, or
# 0.0.0.0) would put private PLA MR titles on a network this page has no auth
# for. Loopback is allowed for local testing.
_TAILNET_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# The mini does not always have `tailscale` on PATH under launchd.
_TAILSCALE_FALLBACK_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_BIND_RETRY_SECONDS = 30
# The dashboard renders queue.json, which only changes when a sweep runs
# (9am/1pm CT). "Sync" kicks a sweep so a freshly-assigned review request shows
# up in seconds instead of hours. The sweep runs OUT-OF-PROCESS under its own
# launchd agent -- see the injected `sweep` edge on the server -- because it
# needs that agent's env, writes the queue itself, posts its own digest, and
# takes ~90s, which would block one of this server's request threads.
# Every sync posts a normal Discord digest (the standard sweep contract), so
# the throttle is what bounds channel noise, not an accident of timing.
_SWEEP_MIN_INTERVAL_SECONDS = 60
# Give up waiting and reload anyway: a sweep that errors out may never move the
# mtime, and the user should not be left staring at a spinner.
_SYNC_FALLBACK_SECONDS = 120
# The page polls the queue mtime continuously, not just after a Sync tap, so a
# runner completion / intake approval / keep-current merge shows up within one
# interval instead of waiting for the next timed reload.
_POLL_SECONDS = 10
# GET /events stats the queue file itself on its own thread rather than sharing
# a broadcaster: thread-per-connection makes per-client polling correct and
# trivial, and at the two or three viewers this page ever has, a registry with
# condition variables would be shared mutable state bought for nothing.
_EVENT_STAT_SECONDS = 1.0
# Quiet enough to be invisible, frequent enough that a client which went away
# surfaces as a write error and its thread exits within the minute.
_EVENT_HEARTBEAT_SECONDS = 15.0
# Every stream costs a thread and a socket for as long as it is held, so the
# ceiling is set in code rather than by trusting that only one person ever opens
# tabs. Generous for the two or three real viewers; a hard stop for a loop.
# EventSource retries on its own, so a client refused here comes back.
_MAX_EVENT_STREAMS = 8
_EVENT_SLOTS = threading.BoundedSemaphore(_MAX_EVENT_STREAMS)
# A workstream card is named after a human-readable thing, not an iid. Long MR
# titles are truncated so a card header stays one scannable line.
_CARD_TITLE_LIMIT = 60
# Control bytes from a request line must never reach a log file raw -- they can
# forge log lines or drive a terminal that later tails the file.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WIDE_BREAKPOINT = "(min-width: 900px)"

# Section names in render order. These are DISPLAY buckets, not status rules.
_NEEDS_YOU = "Needs you"
_IN_PROGRESS = "In progress"
_AUTO = "Auto"
_RECENTLY_DONE = "Recently done"
_ERRORS = "Errors"
SECTION_NAMES = (_NEEDS_YOU, _IN_PROGRESS, _AUTO, _RECENTLY_DONE, _ERRORS)

# S12: the same `web_url` -> MR iid derivation keepcurrent.iid_of uses. The
# regex is copied; its `raise` deliberately is NOT (see mr_iid_of).
_MR_URL_RE = re.compile(r"/merge_requests/(\d+)")
# GitLab serves the same issue under BOTH spellings and has started handing
# back `/-/work_items/<iid>` from the API (live queue records confirm). Every
# `implement` row is issue-kind (assessor.py:216), so matching only
# `/-/issues/` silently dropped the link from every one of them on the
# deployed page. Tolerant of both, mirroring curator.py:377; the rendered
# label stays `#<iid>` either way, since it is the same issue.
_ISSUE_URL_RE = re.compile(r"/(?:issues|work_items)/(\d+)")
_LINKABLE_SCHEMES = ("https://", "http://")


# Serializes THIS process's load -> flip -> save. ThreadingHTTPServer handles
# each request on its own thread, so two taps could otherwise interleave their
# read-modify-write and lose one. The cross-PROCESS race with intake/runner
# stays accepted (documented): os.replace keeps the file atomic, so the worst
# case there is a lost update, not corruption.
_WRITE_LOCK = threading.Lock()


def _now() -> str:
    """Current UTC timestamp (ISO8601). The CLI edge -- pure fns take `now` in."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# =============================================================================
# Pure helpers
# =============================================================================

def _e(value) -> str:
    """Escape untrusted text for HTML. `quote=True` because MR titles, whys and
    web_urls all ride into ATTRIBUTES as well as text nodes -- escaping only the
    text would leave `href="..."` wide open.

    NOTE: formatter._sanitize_title is the conceptual sibling but is NOT
    reusable here -- it rewrites `http://` to `hxxp://`, which would destroy
    every link on the page.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _as_int(value) -> int:
    """Best-effort int for SORT KEYS only. A hand-edited record must not be able
    to raise TypeError out of a sort and blank the whole page."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _days_between(iso_ts: str, iso_now: str) -> Optional[int]:
    """Whole days between two ISO timestamps. Unparseable or naive/aware mix ->
    None (never guess on bad data)."""
    try:
        ts = datetime.datetime.fromisoformat(iso_ts)
        now = datetime.datetime.fromisoformat(iso_now)
    except (ValueError, TypeError):
        return None
    if (ts.tzinfo is None) != (now.tzinfo is None):
        return None
    return (now - ts).days


def mr_iid_of(item: WorkItem) -> int:
    """MR iid for `item`, or 0 when it refers to no MR.

    TOLERANT BY DESIGN, unlike keepcurrent.iid_of and runner._iid_of, which both
    raise on a `web_url` with no MR segment. Their raise is correct -- guessing
    an iid would merge master into someone else's branch, or claim the wrong MR.
    Here the queue is full of issue and todo records with no MR at all, and the
    dashboard runs under KeepAlive: a raise would be a crash loop. So neither of
    those helpers is called (or modified) from this module.

    The `web_url` wins over the `mr_iid` field: the URL is what the record IS,
    the field is what an implement executor later opened for it.
    """
    m = _MR_URL_RE.search(getattr(item, "web_url", "") or "")
    if m:
        return int(m.group(1))
    try:
        return int(getattr(item, "mr_iid", 0) or 0)
    except (TypeError, ValueError):
        return 0


def issue_iid_of(item: WorkItem) -> int:
    """Issue iid from the item's `web_url`, or 0. Tolerant, same reasoning."""
    m = _ISSUE_URL_RE.search(getattr(item, "web_url", "") or "")
    return int(m.group(1)) if m else 0


def ref_of(item: WorkItem) -> str:
    """The short GitLab reference a record renders as: `!4821`, `#1588`, or ""."""
    iid = issue_iid_of(item)
    if iid:
        return f"#{iid}"
    iid = mr_iid_of(item)
    return f"!{iid}" if iid else ""


def is_actionable(item: WorkItem) -> bool:
    """True when a ✅ (or a checked box) may flip this record.

    Single-sourced from approvals._APPROVABLE so the page can never offer a
    checkbox for something the approval layer would refuse, or hide one it
    would accept.
    """
    return item.status in _APPROVABLE


def todo_id_of(item) -> int:
    """The GitLab todo id for a record, or 0 when it carries none.

    Read from the persisted `todo_id` field (captured by `collectors.parse_todos`
    and threaded on by `assessor.assess_todo`). Returns 0 -- never raises -- for
    every non-todo kind and for todo records written before the field existed;
    those refresh on the next sweep, and until then Dismiss falls back to a
    local-only dismiss with a loud stderr note.
    """
    try:
        return int(getattr(item, "todo_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def done_this_week(records: Sequence[QueueRecord], now: str) -> int:
    """Count `done` records last seen within the past 7 days.

    A record 6 days old counts; 8 days does not. An unparseable timestamp is not
    counted (and never raises).
    """
    n = 0
    for r in records:
        try:
            if r.item.status != "done":
                continue
            age = _days_between(r.last_seen, now)
        except Exception:
            continue            # an unreadable record is simply not counted
        if age is not None and 0 <= age < _DONE_WINDOW_DAYS:
            n += 1
    return n


def partition_sections(
        records: Sequence[QueueRecord]) -> Dict[str, List[QueueRecord]]:
    """Assign every record to exactly ONE display section.

    Precedence, and why it is in this order:

    1. `error` -> Errors. A broken record is the most urgent thing on the page.
    2. actionable (`is_actionable`) -> Needs you. Deliberately ABOVE the
       keep-current rule so every approvable record keeps its checkbox -- a
       keep-current item still sitting `proposed` is approvable and must be
       offered, not filed under "Auto" where it would be read-only.
    3. `done` -> Recently done.
    4. `keep-current` executor -> Auto. What is left here is in flight and needs
       no human, so it renders read-only.
    5. `running` / `approved` -> In progress.
    6. anything else -> Errors. An unknown status means a hand-edited queue;
       surfacing it beats silently dropping the record off the page.

    Recently done is capped at the most recent `_RECENT_DONE_LIMIT` by
    `last_seen` descending -- the section IS "recently done", so older records
    fall off the page by definition.
    """
    out: Dict[str, List[QueueRecord]] = {name: [] for name in SECTION_NAMES}
    for r in records:
        try:
            st = r.item.status
            executor = r.item.executor
            actionable = is_actionable(r.item)
        except Exception as e:
            # Unreadable record: file it under Errors so it still reaches the
            # page as a degraded row instead of taking the whole render down.
            print(f"worksweep: dashboard cannot classify a record: {e}",
                  file=sys.stderr)
            out[_ERRORS].append(r)
            continue
        if st == "error":
            out[_ERRORS].append(r)
        elif actionable:
            out[_NEEDS_YOU].append(r)
        elif st == "done":
            out[_RECENTLY_DONE].append(r)
        elif executor == "keep-current":
            out[_AUTO].append(r)
        elif st == "running" or st == "approved":
            out[_IN_PROGRESS].append(r)
        else:
            out[_ERRORS].append(r)
    out[_RECENTLY_DONE].sort(
        key=lambda r: (str(r.last_seen or ""), _as_int(r.number)), reverse=True)
    del out[_RECENTLY_DONE][_RECENT_DONE_LIMIT:]
    return out


@dataclass(frozen=True)
class Group:
    """One workstream card: a connected component of branch/MR affinity."""
    title: str
    branch: str
    records: Tuple[QueueRecord, ...]
    mr_links: Tuple[str, ...]       # web_urls that ARE merge requests
    issue_links: Tuple[str, ...]    # web_urls that ARE issues
    bare_mr_refs: Tuple[int, ...]   # iids with no URL among the group's records
    active: bool = True             # any member still not done/error
    last_activity: str = ""         # max member last_seen, for recency ordering


def _card_name(records: Sequence[QueueRecord], branch: str) -> str:
    """The human name for a workstream card.

    Only stale/keep-current records carry `branch`, so naming cards after it
    alone left most of them reading "!4078" -- an iid is an index, not a name.
    Falls back to a member title, preferring an MR's (it describes the change
    under way) over an issue's or anything else.
    """
    if branch:
        return branch
    titles = []
    for r in records:
        try:
            title = (r.item.title or "").strip()
            kind = r.item.kind
        except Exception:
            continue
        if title:
            titles.append((0 if kind == "mr" else 1, _as_int(r.number), title))
    if titles:
        titles.sort()
        name = titles[0][2]
        if len(name) > _CARD_TITLE_LIMIT:
            name = name[:_CARD_TITLE_LIMIT - 1].rstrip() + "…"
        return name
    return ""


def group_by_workstream(
        records: Sequence[QueueRecord]
) -> Tuple[List[Group], List[QueueRecord]]:
    """Group records into workstreams by branch and MR affinity.

    Returns (groups, ungrouped). Pure: derived from `item.branch`,
    `item.mr_iid`, `item.web_url` and `item.repo` alone, with no network call.

    Affinity is CONNECTED COMPONENTS over two token kinds, not a dict keyed on
    one field: "two records share a branch OR one's mr_iid matches another's MR
    ref" is an equivalence with two edge types, and a single record can carry
    both tokens -- which is exactly what unifies a branch-keyed record with an
    MR-keyed one. Bucketing by `item.branch` alone would split that workstream
    into two cards.

    The MR token is scoped by `item.repo` because iids are per-project:
    an unscoped token would merge pb-www!4821 and pb-api!4821 into one card.

    Ordering is deterministic (lowest record number in the group) so a 60s
    auto-refresh does not reshuffle the page under the user's thumb.
    """
    parent: Dict[tuple, tuple] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    tokenised: List[Tuple[QueueRecord, List[tuple]]] = []
    for r in records:
        toks: List[tuple] = []
        try:
            repo = r.item.repo or ""
            branch = (r.item.branch or "").strip()
        except Exception:
            tokenised.append((r, []))     # unreadable -> Ungrouped
            continue
        if branch:
            # Repo-scoped like the MR token: `chardy/fix-login` in pb-www and
            # the same branch name in pb-api are two different workstreams, and
            # short conventional names collide across repos easily.
            toks.append(("branch", repo, branch))
        try:
            iid = mr_iid_of(r.item)
        except Exception:
            iid = 0
        # No repo means no namespace to scope the iid to (todo-derived records
        # carry none), and an unscoped iid would merge unrelated workstreams.
        # Such a record groups by branch or lands in Ungrouped.
        if iid and repo:
            toks.append(("mr", repo, iid))
        for t in toks:
            find(t)
        for t in toks[1:]:
            union(toks[0], t)
        tokenised.append((r, toks))

    ungrouped = [r for r, toks in tokenised if not toks]

    buckets: Dict[tuple, List[QueueRecord]] = {}
    for r, toks in tokenised:
        if toks:
            buckets.setdefault(find(toks[0]), []).append(r)

    groups: List[Group] = []
    for recs in buckets.values():
        branches = sorted({b for b in (_branch_of(r) for r in recs) if b})
        branch = branches[0] if branches else ""

        mr_links, issue_links = [], []
        mr_iids_by_repo: Dict[str, set] = {}
        linked_iids_by_repo: Dict[str, set] = {}
        for r in recs:
            try:
                repo = r.item.repo or ""
                url = r.item.web_url or ""
            except Exception:
                continue
            m = _MR_URL_RE.search(url)
            if m:
                mr_links.append(url)
                linked_iids_by_repo.setdefault(repo, set()).add(int(m.group(1)))
            elif _ISSUE_URL_RE.search(url):
                issue_links.append(url)
            try:
                iid = mr_iid_of(r.item)
            except Exception:
                iid = 0
            if iid:
                mr_iids_by_repo.setdefault(repo, set()).add(iid)
        mr_links = sorted(set(mr_links))
        issue_links = sorted(set(issue_links))
        # An iid with no matching URL is "bare" -- rendered as text, never as a
        # constructed URL, because guessing the host/namespace would be
        # invention. The comparison is WITHIN one repo: a branch-joined group
        # can span repos, and pb-api!4821 must not be counted as "already
        # linked" just because pb-www!4821 has a URL.
        bare = sorted({iid
                       for repo, iids in mr_iids_by_repo.items()
                       for iid in iids
                       if iid not in linked_iids_by_repo.get(repo, ())})

        # Name the card after something a human recognises; refs stay as
        # secondary metadata in the header rather than standing in as the name.
        title = _card_name(recs, branch)
        if not title:
            if mr_links:
                title = f"!{int(_MR_URL_RE.search(mr_links[0]).group(1))}"
            elif bare:
                title = f"!{bare[0]}"
            else:
                title = "Workstream"

        active, last_activity = False, ""
        for r in recs:
            try:
                if r.item.status not in _TERMINAL:
                    active = True
            except Exception:
                active = True          # unreadable -> surface it, never bury it
            last_activity = max(last_activity, str(getattr(r, "last_seen", "") or ""))

        groups.append(Group(
            title=title, branch=branch,
            records=tuple(sorted(recs, key=lambda r: _as_int(r.number))),
            mr_links=tuple(mr_links), issue_links=tuple(issue_links),
            bare_mr_refs=tuple(bare),
            active=active, last_activity=last_activity))

    # Three stable passes, least significant first. Ordering by iid put long
    # -merged workstreams at the top of the page; what matters is "what is
    # live, and what moved most recently".
    groups.sort(key=lambda g: min(_as_int(r.number) for r in g.records))
    groups.sort(key=lambda g: str(g.last_activity or ""), reverse=True)
    groups.sort(key=lambda g: 0 if g.active else 1)
    return groups, ungrouped


# =============================================================================
# Rendering
# =============================================================================

_CSS = """
:root{
  --bg:#0d1117;
  --panel:#151b23;
  --panel-2:#1b222c;
  --line:#262e3a;
  --line-soft:#1e2530;
  --ink:#e7edf4;
  --ink-2:#a3b0c0;
  --ink-3:#6f7d8f;
  --accent:#58a6ff;
  --accent-ink:#08131f;
  --accent-2:#1f3a5c;
  --ok:#3fb950;
  --warn:#d29922;
  --danger:#f85149;
  --violet:#a371f7;
  --teal:#39c5bb;
  --shadow:rgba(0,0,0,.38);
  --focus:rgba(88,166,255,.45);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg);color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,"SF Pro Text",Segoe UI,Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
  -webkit-tap-highlight-color:transparent;
  padding-bottom:96px;
}
.wrap{max-width:1180px;margin:0 auto;padding:16px 14px 0}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* ---- header + telemetry ---- */
.head{
  display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;
  padding:14px 16px;margin-bottom:18px;
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  box-shadow:0 1px 3px var(--shadow);
}
.brand{font-size:17px;font-weight:650;letter-spacing:-.01em;margin-right:auto}
.brand-logo{width:22px;height:22px;border-radius:50%;vertical-align:-5px;margin-right:2px}
.sweep{color:var(--ink-2);font-size:13px;font-variant-numeric:tabular-nums}
.counts{display:flex;flex-wrap:wrap;gap:6px;width:100%;margin-top:2px}
.cnt{
  font-size:12px;font-variant-numeric:tabular-nums;color:var(--ink-2);
  background:var(--panel-2);border:1px solid var(--line-soft);
  border-radius:999px;padding:3px 10px;
}
/* Filter pills are buttons; the week pill is a plain span with NO hover or
   pointer affordance, so it never looks tappable (it is informational). */
button.cnt{appearance:none;font:inherit;cursor:pointer}
button.cnt:hover{background:var(--line-soft);color:var(--ink);border-color:var(--ink-3)}
button.cnt:active{transform:translateY(1px);background:var(--line)}
button.cnt[aria-pressed="true"]{
  background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:650;
}
button.cnt:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.cnt-week{color:var(--ink);border-color:var(--accent-2);cursor:default}

/* ---- layout toggle ---- */
.toggle{display:flex;gap:2px;padding:3px;background:var(--panel-2);
        border:1px solid var(--line);border-radius:11px}
.toggle-btn{
  appearance:none;border:0;background:transparent;color:var(--ink-2);
  font:inherit;font-size:13px;font-weight:550;
  padding:7px 13px;border-radius:8px;cursor:pointer;
  transition:background .13s ease,color .13s ease;
}
.toggle-btn:hover{background:var(--line-soft);color:var(--ink)}
.toggle-btn:active{background:var(--line);transform:translateY(1px)}
.toggle-btn[aria-pressed="true"]{background:var(--accent);color:var(--accent-ink)}
.toggle-btn:focus-visible{outline:2px solid var(--focus);outline-offset:1px}

/* ---- sync ---- */
.btn-sync{
  appearance:none;font:inherit;font-size:13px;font-weight:600;
  min-height:34px;padding:7px 14px;border-radius:10px;cursor:pointer;
  border:1px solid var(--line);background:var(--panel-2);color:var(--ink-2);
  transition:background .13s ease,color .13s ease,border-color .13s ease;
}
.btn-sync:hover{background:var(--line-soft);color:var(--ink);border-color:var(--ink-3)}
.btn-sync:active{transform:translateY(1px);background:var(--line)}
.btn-sync:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.btn-sync[disabled]{opacity:.55;cursor:progress}

/* ---- sections ---- */
.section{
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  margin:0 0 16px;overflow:hidden;box-shadow:0 1px 3px var(--shadow);
}
.section-h{
  display:flex;align-items:baseline;gap:8px;
  padding:12px 16px;border-bottom:1px solid var(--line-soft);
  font-size:13px;font-weight:650;text-transform:uppercase;letter-spacing:.07em;
  color:var(--ink-2);
}
.section-n{font-weight:500;color:var(--ink-3);letter-spacing:0}
.rows{display:flex;flex-direction:column}
.row{
  display:flex;align-items:flex-start;gap:10px;
  min-height:44px;padding:11px 14px;
  border-top:1px solid var(--line-soft);
}
.rows .row:first-child{border-top:0}
.row:hover{background:var(--panel-2)}

/* ---- the >=44px touch target (AC #26) ---- */
.check{
  display:flex;align-items:center;justify-content:center;
  min-width:44px;min-height:44px;margin:-6px 0 -6px -6px;
  cursor:pointer;flex:0 0 auto;
}
.check input{width:21px;height:21px;accent-color:var(--accent);cursor:pointer}
.spacer{flex:0 0 auto;min-width:20px}
/* The resolution for a row no runner will ever claim. */
.btn-dismiss{
  appearance:none;font:inherit;font-size:10px;font-weight:650;
  letter-spacing:.04em;text-transform:uppercase;
  display:flex;align-items:center;justify-content:center;
  min-width:44px;min-height:44px;margin:-6px 0 -6px -6px;flex:0 0 auto;
  padding:0 6px;border-radius:9px;cursor:pointer;
  border:1px solid var(--line);background:transparent;color:var(--ink-3);
  transition:background .13s ease,color .13s ease,border-color .13s ease;
}
.btn-dismiss:hover{background:var(--panel-2);color:var(--ink);border-color:var(--ink-3)}
.btn-dismiss:active{transform:translateY(1px);background:var(--line)}
.btn-dismiss:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
/* Send-to-Fable: the parked question, the recommendation, and its controls */
.ask{font-size:12px;color:var(--violet);margin-top:2px;white-space:pre-wrap}
.rec{font-size:12px;color:var(--ink-2);margin-top:4px;padding:6px 8px;
  border:1px solid var(--line);border-left:2px solid var(--violet);
  border-radius:6px;background:var(--panel-2);white-space:pre-wrap}
.rec-failed{font-size:11px;color:var(--danger)}
.btn-consult{
  appearance:none;font:inherit;font-size:10px;font-weight:650;
  letter-spacing:.04em;text-transform:uppercase;display:inline-flex;
  align-items:center;justify-content:center;min-height:28px;margin-top:4px;
  padding:0 10px;border-radius:9px;cursor:pointer;
  border:1px solid var(--violet);background:transparent;color:var(--violet);
  transition:background .13s ease,color .13s ease;
}
.btn-consult:hover{background:var(--violet);color:var(--accent-ink)}
.btn-consult:active{transform:translateY(1px)}
.btn-consult:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
/* An actionable row no runner will ever claim: it needs a human, but not this
   button, so it gets a label where the checkbox would be. */
.manual{
  display:flex;align-items:center;justify-content:center;
  min-width:44px;min-height:44px;margin:-6px 0 -6px -6px;flex:0 0 auto;
  font-size:10px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;
  color:var(--ink-3);
}

.cell{min-width:0;flex:1 1 auto}
.line1{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin-bottom:2px}
.num{font-variant-numeric:tabular-nums;font-weight:650;color:var(--ink-3)}
.exec{
  font-size:11px;font-weight:600;letter-spacing:.03em;
  padding:2px 7px;border-radius:6px;
  background:var(--panel-2);border:1px solid var(--line);color:var(--ink-2);
}
.title{font-weight:500;overflow-wrap:anywhere}
.why{color:var(--ink-2);font-size:13px;overflow-wrap:anywhere}
.meta{color:var(--ink-3);font-size:12px;font-variant-numeric:tabular-nums;
      white-space:nowrap;padding-top:2px}
.err{color:var(--danger);font-size:13px;overflow-wrap:anywhere}

.chip{
  font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;
  border:1px solid var(--line);color:var(--ink-2);background:var(--panel-2);
}
.chip[data-st="proposed"]{color:var(--warn);border-color:var(--warn)}
.chip[data-st="needs-input"]{color:var(--violet);border-color:var(--violet)}
.chip[data-st="running"]{color:var(--accent);border-color:var(--accent)}
.chip[data-st="approved"]{color:var(--teal);border-color:var(--teal)}
.chip[data-st="done"]{color:var(--ok);border-color:var(--ok)}
.chip[data-st="error"]{color:var(--danger);border-color:var(--danger)}

/* ---- branch cards ---- */
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  margin:0 0 16px;overflow:hidden;box-shadow:0 1px 3px var(--shadow);
}
.card-h{padding:12px 16px;border-bottom:1px solid var(--line-soft)}
.card-t{font-weight:650;letter-spacing:-.01em;overflow-wrap:anywhere}
.card-refs{display:flex;flex-wrap:wrap;gap:10px;margin-top:5px;
           font-size:13px;font-variant-numeric:tabular-nums}
.ref-bare{color:var(--ink-3)}
/* Refs are secondary metadata under the card's name, never the name itself. */
.card-refs{color:var(--ink-3)}
/* Finished workstreams stay on the page but never compete with live work. */
.divider{
  grid-column:1/-1;margin:22px 0 12px;padding-top:14px;
  border-top:1px solid var(--line);
  font-size:11px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;
  color:var(--ink-3);
}
.card-done{opacity:.62}
.card-done:hover{opacity:1}

/* ---- all clear ---- */
.clear{
  text-align:center;padding:64px 20px;color:var(--ink-2);
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
}
.clear-i{font-size:38px;display:block;margin-bottom:10px}

/* ---- sticky action bar ---- */
.bar{
  position:sticky;bottom:0;z-index:20;
  display:flex;gap:12px;
  padding:12px 14px calc(12px + env(safe-area-inset-bottom,0px));
  margin-top:8px;
  background:var(--panel);border-top:1px solid var(--line);
  box-shadow:0 -3px 14px var(--shadow);
}
/* A class rule outranks the UA's [hidden]{display:none}, so say it here or a
   hidden bar would still lay out. */
.bar[hidden]{display:none}

/* ---- the five out-of-band swap targets ---- */
/* They exist only to give every dynamic region a stable id in EVERY queue
   state, so `display:contents`: .head is a flex container whose .brand uses
   margin-right:auto and whose .counts uses width:100% to force its own line,
   and .bar is position:sticky. A real wrapper box would quietly change both. */
.oob{display:contents}

/* "queue changed - update pending": shown when an update arrives while a
   selection or a dialog is live. A held update must be VISIBLE -- a silent
   hold is the bug this whole refresh path exists to kill -- and tapping it
   applies the update immediately. */
.pending{
  appearance:none;font:inherit;font-size:12px;font-weight:600;
  padding:5px 10px;border-radius:999px;cursor:pointer;
  border:1px solid var(--warn);background:var(--panel-2);color:var(--warn);
}
.pending:hover{background:var(--line-soft)}
.pending:active{transform:translateY(1px)}
.pending:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
.pending[hidden]{display:none}
.bar .btn{
  appearance:none;flex:1 1 0;min-height:52px;
  font:inherit;font-size:15px;font-weight:650;
  border-radius:12px;cursor:pointer;
  border:1px solid var(--line);background:var(--panel-2);color:var(--ink);
  transition:background .13s ease,border-color .13s ease,transform .06s ease;
}
.bar .btn:hover{background:var(--line-soft);border-color:var(--ink-3)}
.bar .btn:active{transform:translateY(1px);background:var(--line)}
.bar .btn:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.bar .btn-go{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.bar .btn-go:hover{background:var(--teal);border-color:var(--teal)}
.bar .btn-go:active{transform:translateY(1px);background:var(--accent-2);color:var(--ink)}
.bar .btn[disabled]{opacity:.45;cursor:not-allowed}

/* ---- view switching: driven ONLY by the data-layout attribute ---- */
.branches{display:none}
.sections{display:block}
[data-layout="panels"] .sections{display:grid;grid-template-columns:1fr;gap:16px;align-items:start}
[data-layout="panels"] .section{margin:0}
[data-layout="branches"] .sections{display:none}
[data-layout="branches"] .branches{display:block}

/* The breakpoint supplies the DEFAULT view (via the head script) and widens
   the grids. Every rule here is scoped by [data-layout], so the media query can
   never override an explicit stored choice at any width (AC #30). */
@media (min-width: 900px){
  [data-layout="panels"] .sections{grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
  [data-layout="branches"] .branches{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;align-items:start}
  [data-layout="branches"] .card{margin:0}
  [data-layout="checklist"] .wrap{max-width:860px}
}
"""

# Runs in <head>, BEFORE any section renders: a stored layout must be applied
# ahead of first paint or the 60s auto-refresh flashes the wrong layout every
# minute. The breakpoint is consulted ONLY when there is no stored choice.
_HEAD_SCRIPT = """
(function(){var d=document.documentElement;
try{var v=localStorage.getItem('%(key)s');
if(v==='checklist'||v==='panels'||v==='branches'){d.setAttribute('data-layout',v);return;}}catch(e){}
try{d.setAttribute('data-layout',window.matchMedia('%(bp)s').matches?'panels':'checklist');}catch(e){}})();
""" % {"key": _LAYOUT_STORAGE_KEY, "bp": _WIDE_BREAKPOINT}

# ---- vendored htmx ---------------------------------------------------------
# A repo file, not a CDN tag and not a `<script src>`: the page is one
# self-contained document (AC #28), so the library rides inline like the CSS
# and the rest of the JS do. Pinned by sha256 in the sibling `htmx.version`.
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_HTMX_PATH = os.path.join(_STATIC_DIR, "htmx.min.js")


def _read_vendored_js(path: str) -> str:
    """Read a vendored asset, LOUDLY.

    Deliberately the one place in this module that is allowed to raise, and it
    raises at import rather than per-request. A deploy that forgot `static/`
    would otherwise serve a page that looks perfect and never updates -- the
    worst failure mode for a page whose whole job is showing what changed.
    Under KeepAlive an import that raises is a restart loop with a real
    traceback in the .err file, which is exactly the visibility that needs.
    """
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    if not source.strip():
        raise RuntimeError(f"vendored asset is empty: {path}")
    if "</script" in source:
        # It is emitted inline; a closing tag inside it would end the element
        # early and spray the remainder of the library into the document.
        raise RuntimeError(f"vendored asset cannot be inlined safely: {path}")
    return source


_HTMX_JS = _read_vendored_js(_HTMX_PATH)

# Seneschal roundel (96px JPEG from the seneschal repo, docs/brand/
# seneschal-plate.jpg), inlined so the dashboard stays a single-file
# server with no static routes. Placed AFTER the head scripts in the
# rendered page: AC #31 needs those within the first few hundred bytes.
# Served at /apple-touch-icon.png: iOS ignores data: URIs when adding a
# page to the home screen and fetches this URL out-of-band instead.
_APPLE_ICON_PATH = Path(__file__).resolve().parent / "assets" / "apple-touch-icon.png"
_LOGO_URI = "data:image/jpeg;base64,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"


_BODY_SCRIPT = """
(function(){
  var root=document.documentElement,KEY='%(key)s';
  var POLL_MS=%(poll)d,SYNC_MAX_MS=%(sync_max)d;
  var inflight=false,syncing=false,confirming=false;
  // Script-scoped, NOT a load-time DOM read: the token is re-seeded from the
  // freshly rendered Sync button after every swap. A value captured once dies
  // with the region it was read from, and the poll would then either fire on
  // every pass forever or never fire again.
  var lastMtime=(function(){
    var s=document.getElementById('sync');
    return s?(s.getAttribute('data-mtime')||'').trim():'';
  })();
  // An update arrived while the page was busy and is waiting to be applied.
  var pending=false;
  // The 10s poll is the DEGRADED path now and is armed at most once, by the
  // event stream failing terminally -- never by a user action, and never twice.
  var polling=false;
  // The selection captured just before a swap, re-applied just after.
  var carried=[];
  // Single-flight bookkeeping. `cycle` counts refreshes issued, `settled`
  // counts refreshes dealt with; they differ exactly while one is in the air.
  // Counters rather than a boolean so a LATE settle from an old request can
  // never release the guard belonging to a newer one.
  var cycle=0,settled=0;

  // Defense in depth, set before this page can make a single request. Our
  // fragments carry no script tags and everything we fetch is same-origin, so
  // say so: htmx 2.0.7 ships allowScriptTags:true, and a malformed or tampered
  // response should not be able to smuggle one past a default we do not need.
  try{htmx.config.allowScriptTags=false;htmx.config.selfRequestsOnly=true;}catch(e){}

  function scope(){
    return document.querySelector(root.getAttribute('data-layout')==='branches'?'.branches':'.sections');
  }
  // Only rows the user can actually SEE. A row filtered out by a status pill
  // is not on offer: it must not be counted, submitted, or keep the bar up.
  function boxes(sel){
    var s=scope();
    if(!s){return [];}
    return Array.prototype.slice.call(s.querySelectorAll(sel)).filter(function(b){
      var row=b.closest?b.closest('.row'):null;
      return !row||row.style.display!=='none';
    });
  }
  function nums(list){
    var seen={},out=[];
    list.forEach(function(b){
      if(!seen[b.value]){seen[b.value]=1;out.push(parseInt(b.value,10));}
    });
    return out;
  }
  function selected(){
    return nums(boxes('input[type=checkbox]').filter(function(b){return b.checked;}));
  }
  // The set "Approve all" may sweep: exactly the proposed+runnable rows THIS
  // page rendered. Sending it (rather than letting the server decide) means the
  // user approves the set they were actually shown.
  function blanket(){
    return nums(boxes('input[type=checkbox][data-blanket="1"]'));
  }
  // Reloading now would lose a selection, tear a POST, or yank the page out
  // from under an open confirm dialog. Every auto-reload path checks this.
  function busy(){return inflight||confirming||selected().length>0;}

  function refresh(){
    var n=selected().length,c=document.getElementById('sel-count'),
        go=document.getElementById('approve-selected'),
        all=document.getElementById('approve-all');
    if(c){c.textContent=n;}
    if(go){go.disabled=inflight||n===0;}
    if(all){all.disabled=inflight||blanket().length===0;}
    // Hide the whole bar, not merely disable it: under a `running`/`done`
    // filter none of the visible rows is approvable, so the bar is dead chrome
    // covering the bottom of a phone screen. applyFilter() and setLayout() both
    // end here, so this recomputes on every view and filter change.
    var bar=document.querySelector('.bar');
    if(bar){bar.hidden=boxes('input[type=checkbox]').length===0;}
  }
  function setLayout(v){
    root.setAttribute('data-layout',v);
    try{localStorage.setItem(KEY,v);}catch(e){}
    marks();applyFilter();
  }
  function marks(){
    var cur=root.getAttribute('data-layout'),
        b=document.querySelectorAll('[data-set-layout]');
    for(var i=0;i<b.length;i++){
      b[i].setAttribute('aria-pressed',b[i].getAttribute('data-set-layout')===cur?'true':'false');
    }
  }

  // ---- status filter: tap a pill to show only that status ----
  function applyFilter(){
    var f=(root.getAttribute('data-filter')||'').trim();
    var rows=document.querySelectorAll('.row[data-st]');
    for(var i=0;i<rows.length;i++){
      rows[i].style.display=(!f||rows[i].getAttribute('data-st')===f)?'':'none';
    }
    // A section or card with nothing left to show is noise, so hide it too.
    var groups=document.querySelectorAll('.section,.card');
    for(var j=0;j<groups.length;j++){
      groups[j].style.display=(!f||groups[j].querySelector('.row[data-st="'+f+'"]'))?'':'none';
    }
    var div=document.querySelector('.divider'),shown=false;
    if(div){
      var doneCards=document.querySelectorAll('.card-done');
      for(var k=0;k<doneCards.length;k++){
        if(doneCards[k].style.display!=='none'){shown=true;break;}
      }
      div.style.display=shown?'':'none';
    }
    var pills=document.querySelectorAll('[data-filter]');
    for(var m=0;m<pills.length;m++){
      pills[m].setAttribute('aria-pressed',
        pills[m].getAttribute('data-filter')===f&&f?'true':'false');
    }
    refresh();
  }
  function toggleFilter(v){
    // Exactly one active at a time; tapping the active one clears it. Never
    // persisted -- a filter that survived a reload would hide work on a page
    // whose whole job is showing it.
    root.setAttribute('data-filter',
      (root.getAttribute('data-filter')||'')===v?'':v);
    applyFilter();
  }

  function send(url,body){
    inflight=true;refresh();
    fetch(url,{method:'POST',headers:{'X-Worksweep':'approve','Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){
        // The rows just acted on are no longer on offer, so the selection
        // that submitted them is spent. Clear it BEFORE refreshing or the
        // deferred-swap guard would hold the very update just asked for.
        //
        // Wrapped, because by this point the write is DURABLE on the server. A
        // DOM error in the follow-up would otherwise fall into the .catch below
        // and tell the user their approval failed -- and the honest reaction to
        // that message is to approve it again. Degrade to the poll instead and
        // put the chip up: slower, visible, and true.
        if(r.status===200){
          inflight=false;
          try{clearSelection();applyFragments();}catch(err){pending=true;chip(true);armPoll();}
          return;
        }
        inflight=false;alert('Request failed ('+r.status+')');refresh();
      })
      .catch(function(e){inflight=false;alert('Request failed: '+e);refresh();});
  }

  function clearSelection(){
    var b=document.querySelectorAll('input[type=checkbox]');
    for(var i=0;i<b.length;i++){b[i].checked=false;}
    refresh();
  }

  // ---- live refresh: swap the five dynamic regions, never reload ----------
  // A reload discards a selection, tears an open dialog and repaints the whole
  // document to change one row. One GET returns the five regions marked for an
  // out-of-band swap, so the page updates atomically and in place.
  function chip(on){
    var c=document.getElementById('pending');
    if(c){c.hidden=!on;}
  }
  function swapInFlight(){return cycle!==settled;}
  // Never silent. The chip is both the notification and the retry: tapping it
  // asks again. Without this the single-flight guard would also stay claimed,
  // and one failed fetch would lock the page out of refreshing forever.
  function swapFailed(){
    if(!swapInFlight()){return;}
    settled=cycle;pending=true;chip(true);
  }
  htmx.on('htmx:responseError',swapFailed);
  htmx.on('htmx:sendError',swapFailed);
  function applyFragments(force){
    // Rows shifting under a half-built selection, an open confirm dialog or an
    // in-flight POST is exactly what the old busy() guard rightly prevented --
    // but holding SILENTLY is the bug this build exists to kill. Hold, and say
    // so on the chip. `force` is the chip's own tap: asking for it explicitly.
    // An in-flight POST is never forced past; it ends in its own refresh.
    if(inflight||(!force&&busy())){pending=true;chip(true);return;}
    // Single-flight. Two requests in the air race, and the page would settle
    // on whichever RESPONSE landed last -- not necessarily the one that read
    // the queue last. A burst (stream event, then a poll, then an action)
    // could therefore leave the page showing the older of two states,
    // permanently. Queue it instead; the swap hook drains it one round trip
    // later, so no chip: this is not a hold the user needs to know about.
    if(swapInFlight()){pending=true;return;}
    var mine=++cycle;
    // Captured BEFORE the request: rows that come back are re-checked by
    // value, rows that vanished simply drop out. The server re-validates every
    // number on POST, so consent enforcement never rests on this.
    carried=selected();
    // Settles the guard however the request ends, including the case where
    // htmx returns a response it never turns into an event. Only ever moves
    // FORWARD, so a late settle cannot release a newer request's guard.
    var settle=function(){if(settled<mine){settled=mine;}};
    htmx.ajax('GET','/fragments',{target:'body',swap:'none'}).then(settle,settle);
  }
  // htmx 2.0.7 fires this exactly once, on the ajax target, even for
  // swap:'none' -- its settle info is {tasks:[],elts:[target]}. That is the
  // contract this hook is built on. The guard on the first line is what makes
  // being WRONG about it merely wasteful instead of silently corrupting: a
  // second pass would re-apply a now-stale `carried` over a page the user has
  // since touched, and drain `pending` twice.
  htmx.on('htmx:afterSwap',function(){
    if(!swapInFlight()){return;}settled=cycle;
    pending=false;chip(false);
    // The header that just arrived is server truth, and its Sync button is
    // fresh. A `syncing` flag left over from a sweep that has since landed
    // would keep re-disabling it for up to SYNC_MAX_MS after the fact.
    syncing=false;
    for(var i=0;i<carried.length;i++){
      var twins=document.querySelectorAll('input[type=checkbox][value="'+carried[i]+'"]');
      for(var j=0;j<twins.length;j++){twins[j].checked=true;}
    }
    // Re-seeded from the button that just arrived, not from the one that left.
    var s=document.getElementById('sync');
    lastMtime=s?(s.getAttribute('data-mtime')||'').trim():lastMtime;
    // The rows changed, so every derived piece of state is stale: which rows
    // the active filter hides, and therefore what the bar may offer.
    applyFilter();refresh();marks();
  });

  // ---- the 10s mtime poll: the fallback for when the stream will not run --
  function armPoll(){if(polling){return;}polling=true;setTimeout(poll,POLL_MS);}
  function poll(){
    fetch('/mtime',{cache:'no-store'})
      .then(function(r){return r.text();})
      .then(function(t){
        t=(t||'').trim();
        if(t){
          if(lastMtime&&t!==lastMtime){applyFragments();}
          // Reassigned on EVERY response: a held update must not re-trigger
          // on the next pass, it drains through `pending` instead.
          lastMtime=t;
        }
        setTimeout(poll,POLL_MS);
      })
      .catch(function(){setTimeout(poll,POLL_MS);});
  }

  // ---- the event stream: the queue says when it changed, once, when it did --
  // The poll asked "anything yet?" every ten seconds per open tab forever and
  // was told "no" almost every time. One held connection replaces all of that.
  // EventSource reconnects by itself, which covers the common case of the
  // dashboard agent being restarted under the page.
  (function(){
    if(!window.EventSource){armPoll();return;}
    var es=new EventSource('/events');
    htmx.on(es,'queue',function(){applyFragments();});
    // CLOSED (2) only. EventSource retries CONNECTING on its own, and arming
    // the poll on every transient blip would leave the page with two refresh
    // paths racing each other. This is the terminal case -- a proxy that will
    // not stream, a browser quirk -- where the page must degrade rather than
    // silently freeze.
    es.onerror=function(){if(es.readyState===2){armPoll();}};
  })();

  function approveSelected(){
    var n=selected();
    if(!n.length){return;}
    send('/approve',{numbers:n});
  }
  function approveAll(){
    // There is NO un-approve path anywhere in worksweep, and this is one tap
    // wide on a phone -- so the bulk action confirms with its blast radius,
    // counting exactly the set it is about to send.
    var n=blanket();
    if(!n.length){alert('Nothing is proposed right now.');return;}
    confirming=true;
    var ok=confirm('Approve all '+n.length+' proposed items?');
    // A BELT, not a live drain: confirm() blocks this thread, so no stream
    // event, poll or timer can run between the two lines above and `pending`
    // cannot become true while the dialog is open. Kept because that is a
    // property of confirm(), not of this code -- the day the dialog becomes a
    // non-blocking one, this is the line that stops the hold being permanent.
    confirming=false;
    if(pending){applyFragments();}
    if(!ok){return;}
    send('/approve-all',{numbers:n});
  }

  // ---- Sync: kick a sweep; the always-on poll picks up the result ----
  function syncDone(label){
    syncing=false;
    var sync=document.getElementById('sync');
    if(!sync){return;}
    sync.textContent=label;
    setTimeout(function(){
      // Re-queried again: 3s is long enough for the header to be re-rendered.
      var b=document.getElementById('sync');
      if(b&&!syncing){b.disabled=false;b.textContent='Sync';}
    },3000);
  }
  function kickSweep(){
    // Re-queried per click, and guarded: the button may be gone (a re-render
    // landed between click and dispatch). Return quietly rather than throwing,
    // and above all do not POST /sweep.
    var sync=document.getElementById('sync');
    if(!sync||syncing||sync.disabled){return;}
    syncing=true;sync.disabled=true;sync.textContent='syncing…';
    fetch('/sweep',{method:'POST',headers:{'X-Worksweep':'approve'}})
      .then(function(r){
        if(r.status===202){
          // No private poll: the always-on one reloads when the sweep lands.
          // This only stops the button spinning if the sweep dies silently.
          setTimeout(function(){syncDone('Sync');},SYNC_MAX_MS);
          return;
        }
        if(r.status===429){syncDone('just synced');return;}
        syncDone('sync failed');
      })
      .catch(function(){syncDone('sync failed');});
  }

  // ---- one delegated click listener: every control survives a re-render ----
  document.addEventListener('click',function(e){
    if(!e.target||!e.target.closest){return;}
    var t=e.target.closest('[data-set-layout]');
    if(t){setLayout(t.getAttribute('data-set-layout'));return;}
    var f=e.target.closest('[data-filter]');
    if(f){toggleFilter(f.getAttribute('data-filter'));return;}
    var d=e.target.closest('[data-dismiss]');
    if(d){
      if(!inflight){send('/dismiss',{number:parseInt(d.getAttribute('data-dismiss'),10)});}
      return;
    }
    var c=e.target.closest('[data-consult]');
    if(c){
      if(!inflight){send('/consult',{number:parseInt(c.getAttribute('data-consult'),10)});}
      return;
    }
    var a=e.target.closest('[data-accept-rec]');
    if(a){
      if(!inflight){send('/accept-rec',{number:parseInt(a.getAttribute('data-accept-rec'),10)});}
      return;
    }
    if(e.target.closest('#approve-selected')){approveSelected();return;}
    if(e.target.closest('#approve-all')){approveAll();return;}
    if(e.target.closest('#sync')){kickSweep();return;}
    // Drain 2 of 2: tapping the chip IS the request to update now, so it
    // forces past the selection guard the other drain waits out.
    if(e.target.closest('#pending')){applyFragments(true);return;}
  });
  document.addEventListener('change',function(e){
    var b=e.target;
    if(b&&b.type==='checkbox'&&b.hasAttribute('data-view')){
      // the same record has a checkbox in each view: keep them in step so a
      // selection survives a layout switch
      var twins=document.querySelectorAll('input[type=checkbox][value="'+b.value+'"]');
      for(var i=0;i<twins.length;i++){twins[i].checked=b.checked;}
      refresh();
      // A change made WHILE a refresh is in the air must survive the swap
      // exactly as the user left it, so record it against the captured set at
      // the moment of the click rather than reconstructing it afterwards from
      // a DOM that has since been replaced. A DESELECTION above all:
      // re-applying a stale capture over one would put a row the user just
      // refused back on offer, and back into the next submitted set.
      if(swapInFlight()){
        var v=parseInt(b.value,10);
        carried=carried.filter(function(x){return x!==v;});
        if(b.checked){carried.push(v);}
      }
      // Drain 1 of 2: nothing is selected any more, so a held update can land
      // without moving a single row under the user's thumb.
      if(pending&&selected().length===0){applyFragments();}
    }
  });
  marks();applyFilter();refresh();
})();
""" % {"key": _LAYOUT_STORAGE_KEY, "poll": _POLL_SECONDS * 1000,
       "sync_max": _SYNC_FALLBACK_SECONDS * 1000}


def _link(url: str, label: str) -> str:
    """An anchor when the URL is really a web URL, escaped text otherwise.

    Refusing to linkify anything else keeps a `javascript:` value in a queue
    record from becoming a clickable payload.
    """
    if url and url.startswith(_LINKABLE_SCHEMES):
        return f'<a href="{_e(url)}">{_e(label)}</a>'
    return f"<span>{_e(label)}</span>"


def has_checkbox(item) -> bool:
    """True when a row renders an approval checkbox.

    Single-sourced so the sticky bar's initial visibility cannot disagree with
    which rows actually got a control -- a bar offering "Approve selected" over
    a page with nothing selectable is the bug this exists to prevent.
    """
    return is_actionable(item) and item.executor in RUNNABLE_EXECUTORS


def _checkbox(record: QueueRecord, view: str) -> str:
    """The approval control for a row, or a reason there isn't one.

    A non-runnable actionable record (`triage`/`mr-hygiene`/`none`) gets NO
    checkbox: nothing in worksweep would ever execute it, so approving it would
    strand it as a permanently-`approved` zombie with no un-approve path. It
    still renders -- labelled "manual" -- because it genuinely does need a human,
    just not through this button.

    `data-blanket` marks the rows "Approve all" is allowed to sweep, so the
    page sends exactly the set it displayed and the confirm dialog counts the
    same set (never a server-side count the user never saw).
    """
    item = record.item
    if not is_actionable(item):
        return '<span class="spacer"></span>'
    if not has_checkbox(item):
        # Nothing executes these, so the only resolution is "I looked at it".
        # A Dismiss button is that resolution; an approve checkbox here would
        # strand the record as a permanently-approved zombie.
        return _dismiss_button(record.number, "mark as handled")
    blanket = ' data-blanket="1"' if is_blanket_eligible(item) else ''
    check = (f'<label class="check">'
             f'<input type="checkbox" data-view="{_e(view)}" '
             f'value="{_e(record.number)}"{blanket} '
             f'aria-label="approve item {_e(record.number)}"></label>')
    if not is_dismissable(item):
        return check
    # BOTH controls. An address-feedback row can be runnable AND have nothing
    # to run -- a reviewer's "LGTM" on a plain note is exactly that. Approve
    # runs it; dismiss says "I read this note". Rendering only the checkbox
    # made the server-side dismiss this round added unreachable from the page.
    return (check + _dismiss_button(record.number, "seen it"))


def _dismiss_button(number: int, title: str) -> str:
    return (f'<button type="button" class="btn-dismiss" '
            f'data-dismiss="{_e(number)}" '
            f'aria-label="dismiss item {_e(number)}" '
            f'title="{_e(title)}">Dismiss</button>')


def _consult_html(record: QueueRecord) -> str:
    """The Send-to-Fable strip on a parked row.

    Four states, one element each: offer the button ("" / "error"), show the
    hold ("requested"), or show the recommendation plus Accept ("done"). The
    checkbox next to it stays the PLAIN re-approve — accepting is the path
    that carries content, so it gets its own verb rather than overloading ✅.
    """
    item = record.item
    n = record.number
    if item.consult == "requested":
        return '<div class="rec">🔮 consult pending — the runner picks it up within 10 min</div>'
    if item.consult == "done" and item.consult_rec:
        return (f'<div class="rec">🔮 {_e(item.consult_rec)}</div>'
                f'<button type="button" class="btn-consult" '
                f'data-accept-rec="{_e(n)}" '
                f'aria-label="accept recommendation for item {_e(n)}" '
                f'title="approve this row with the recommendation as the '
                f'operator ruling">Accept → run</button>')
    failed = ('<span class="rec-failed">consult failed — </span>'
              if item.consult == "error" else "")
    return (f'<div>{failed}<button type="button" class="btn-consult" '
            f'data-consult="{_e(n)}" '
            f'aria-label="consult fable on item {_e(n)}" '
            f'title="run an unattended second opinion on this question">'
            f'🔮 Consult Fable</button></div>')


def _chip(record: QueueRecord) -> str:
    st = record.item.status
    return f'<span class="chip" data-st="{_e(st)}">{_e(st)}</span>'


def _age(record: QueueRecord, now: str) -> str:
    days = _days_between(record.first_seen, now)
    return "" if days is None else (f"{days}d" if days else "today")


def _section_row(record: QueueRecord, now: str, section: str) -> str:
    item = record.item
    ref = ref_of(item)
    bits = [f'<span class="num">#{_e(record.number)}</span>',
            f'<span class="exec">{_e(item.executor)}</span>']
    if ref:
        bits.append(_link(item.web_url, ref))
    if section != _NEEDS_YOU:
        bits.append(_chip(record))

    detail = []
    if item.title:
        detail.append(f'<div class="title">{_e(item.title)}</div>')
    if section == _ERRORS:
        summary = item.error_summary or f"unknown status: {item.status}"
        detail.append(f'<div class="err">{_e(summary)}</div>')
    elif section == _RECENTLY_DONE:
        done_bits = [b for b in (item.done_reason,
                                 (item.result_sha or "")[:8]) if b]
        if done_bits:
            detail.append(f'<div class="why">{_e(" · ".join(done_bits))}</div>')
    else:
        if item.why:
            detail.append(f'<div class="why">{_e(item.why)}</div>')
        if section == _IN_PROGRESS and item.dev_box:
            detail.append(f'<div class="why">{_e(item.dev_box)}</div>')
        if item.status == "needs-input":
            # The QUESTION is the whole point of a parked row; rendering only
            # the why left the human reading Discord scrollback to learn what
            # was asked (#238, 2026-09-01).
            if item.error_summary:
                detail.append(f'<div class="ask">{_e(item.error_summary)}</div>')
            detail.append(_consult_html(record))

    age = _age(record, now)
    meta = f'<div class="meta">{_e(age)}</div>' if age else ""
    # `data-st` (not `data-status`) matches the chip attribute and keeps the
    # module free of the literal `status="`, which the AC #20 invariant forbids.
    return (f'<div class="row" data-st="{_e(item.status)}">'
            f'{_checkbox(record, "sections")}'
            f'<div class="cell"><div class="line1">{"".join(bits)}</div>'
            f'{"".join(detail)}</div>{meta}</div>')


def _status_of(record) -> str:
    try:
        return record.item.status
    except Exception:
        return "unreadable"


def _branch_of(record) -> str:
    try:
        return (record.item.branch or "").strip()
    except Exception:
        return ""


def _degraded_row(record, error: Exception) -> str:
    """Fallback for a record that could not be rendered.

    One malformed record costs one ugly row, never the whole page -- a blank
    dashboard under KeepAlive gives the human nothing to act on.
    """
    print(f"worksweep: dashboard row render failed: {error}", file=sys.stderr)
    number = _e(getattr(record, "number", "?"))
    return (f'<div class="row" data-st="error"><span class="spacer"></span>'
            f'<div class="cell"><div class="line1">'
            f'<span class="num">#{number}</span>'
            f'<span class="chip" data-st="error">unrenderable</span>'
            f'</div></div></div>')


def _safe(render, record, *args) -> str:
    try:
        return render(record, *args)
    except Exception as e:
        return _degraded_row(record, e)


def _sections_html(sections: Dict[str, List[QueueRecord]], now: str) -> str:
    out = []
    for name, records in sections.items():
        if not records:
            continue
        rows = "".join(_safe(_section_row, r, now, name) for r in records)
        out.append(
            f'<section class="section"><div class="section-h">{_e(name)}'
            f'<span class="section-n">{len(records)}</span></div>'
            f'<div class="rows">{rows}</div></section>')
    return f'<div class="sections">{"".join(out)}</div>'


def _card_row(record: QueueRecord) -> str:
    item = record.item
    return (f'<div class="row" data-st="{_e(item.status)}">'
            f'{_checkbox(record, "branches")}'
            f'<div class="cell"><div class="line1">'
            f'<span class="num">#{_e(record.number)}</span>'
            f'<span class="exec">{_e(item.executor)}</span>{_chip(record)}'
            f'</div><div class="why">{_e(item.why)}</div></div></div>')


def _card(title: str, refs_html: str, records: Sequence[QueueRecord],
          done: bool = False) -> str:
    rows = "".join(_safe(_card_row, r) for r in records)
    refs = f'<div class="card-refs">{refs_html}</div>' if refs_html else ""
    cls = "card card-done" if done else "card"
    return (f'<section class="{_e(cls)}"><div class="card-h">'
            f'<div class="card-t">{_e(title)}</div>{refs}</div>'
            f'<div class="rows">{rows}</div></section>')


def _branches_html(groups: Sequence[Group],
                   ungrouped: Sequence[QueueRecord]) -> str:
    cards = []
    divided = False
    for g in groups:
        if not g.active and not divided:
            # Finished workstreams stay on the page but never above live work.
            divided = True
            cards.append('<div class="divider">Completed</div>')
        refs = []
        for url in g.mr_links:
            refs.append(_link(url, f"!{int(_MR_URL_RE.search(url).group(1))}"))
        for iid in g.bare_mr_refs:
            # No URL to link -- render the reference as plain text rather than
            # constructing a URL we do not actually have.
            refs.append(f'<span class="ref-bare">!{_e(iid)}</span>')
        for url in g.issue_links:
            refs.append(_link(url, f"#{int(_ISSUE_URL_RE.search(url).group(1))}"))
        cards.append(_card(g.title, "".join(refs), g.records,
                           done=not g.active))
    if ungrouped:
        cards.append(_card("Ungrouped", "",
                           sorted(ungrouped, key=lambda r: _as_int(r.number))))
    return f'<div class="branches">{"".join(cards)}</div>'


def _telemetry_html(records: Sequence[QueueRecord],
                    sections: Dict[str, List[QueueRecord]],
                    now: str, queue_mtime: Optional[float]) -> str:
    counts: Dict[str, int] = {}
    for r in records:
        counts[_status_of(r)] = counts.get(_status_of(r), 0) + 1
    # Ordered by the section a status lands in (so the things needing Chandler
    # come first), then alphabetically inside a section -- deterministic, and
    # without declaring a status ordering of its own.
    ordered, seen = [], set()
    for recs in sections.values():
        for st in sorted({_status_of(r) for r in recs}, key=str):
            if st not in seen:
                seen.add(st)
                ordered.append(st)
    # Tap-to-filter. Deliberately NOT persisted: a filter that survived a
    # reload would silently hide work on a page whose whole job is showing it.
    pills = "".join(
        f'<button type="button" class="cnt" data-filter="{_e(st)}" '
        f'aria-pressed="false">{_e(st)} {counts[st]}</button>'
        for st in ordered)
    if queue_mtime:
        stamp = datetime.datetime.fromtimestamp(
            queue_mtime).strftime("%Y-%m-%d %H:%M")
        # Relative first because that is the question being asked at a glance
        # ("is this page stale?"); the absolute stamp stays for precision.
        sweep = f"{relative_age(queue_mtime, now)} · {stamp}"
    else:
        sweep = "never synced"
    week = done_this_week(records, now)
    return (f'<div class="sweep">{_e(sweep)}</div>'
            f'<div class="counts">{pills}'
            f'<span class="cnt cnt-week">done this week: {week}</span></div>')


def _sync_html(queue_mtime: Optional[float],
               queue_size: Optional[int] = None) -> str:
    """The header's Sync control.

    Carries the mtime token the page was rendered from, so the page can tell a
    /mtime response apart from the state it is already showing -- rather than
    guessing a duration or refreshing into the same view. Lives in the header,
    so it is present in all three layouts.

    The update-pending chip rides along in this region deliberately: it is the
    one piece of chrome that must be re-rendered (and so re-hidden) by the same
    swap that applies the update it was announcing.
    """
    return (f'<button type="button" class="btn-sync" id="sync" '
            f'data-mtime="{_e(mtime_token(queue_mtime, queue_size))}" '
            f'title="run a sweep now">Sync</button>'
            f'<button type="button" class="pending" id="pending" hidden '
            f'title="apply the update now">'
            f'queue changed \u2014 update pending</button>')


def _bar_html(records: Sequence[QueueRecord]) -> str:
    """The sticky action bar.

    Deliberately carries NO server-side count: the page computes both the
    confirm-dialog count and the numbers it POSTs from the same rendered
    `data-blanket` rows, so the two can never disagree. A server-rendered count
    would go stale the moment the queue changed and would tell the user they
    were approving N items while sending a different set.
    """
    # Hidden from the first paint when nothing on the page is approvable (an
    # all running/done queue). The page recomputes this on every filter and
    # layout change; rendering it server-side too avoids a flash of a bar that
    # cannot be used.
    hidden = "" if any(has_checkbox(r.item) for r in records) else " hidden"
    return (
        f'<div class="bar"{hidden}>'
        '<button type="button" class="btn" id="approve-selected" disabled>'
        'Approve selected (<span id="sel-count">0</span>)</button>'
        '<button type="button" class="btn btn-go" id="approve-all" disabled>'
        'Approve all</button>'
        '</div>')


# The five regions of the page that change when the queue changes, in
# document order. Every one carries a stable id in EVERY queue state -- an
# out-of-band swap needs a target that exists on both sides of the transition,
# and the empty queue is exactly the transition that would otherwise have none.
_REGION_IDS = ("telemetry", "sync-region", "sections", "branches", "bar")


def _regions(records: Sequence[QueueRecord], now: str,
             queue_mtime: Optional[float],
             queue_size: Optional[int] = None) -> Dict[str, str]:
    """Inner HTML of each dynamic region, keyed by container id.

    The single producer for BOTH the full page and the fragment response, so
    the two cannot drift: a fragment endpoint that renders a region differently
    from the page is a page that changes appearance every time it updates.

    Pure, like `render_page`: a function of its arguments only.
    """
    records = list(records)
    sections = partition_sections(records)
    if records:
        groups, ungrouped = group_by_workstream(records)
        content = _sections_html(sections, now)
        branches = _branches_html(groups, ungrouped)
        bar = _bar_html(records)
    else:
        # The all-clear lives INSIDE #sections rather than replacing it, so the
        # swap that empties the queue also removes the rows it replaces. When
        # this was a sibling of .sections, approving the last item left the
        # page showing rows that no longer existed.
        content = ('<div class="clear"><span class="clear-i">🔭</span>'
                   'Nothing needs you right now.</div>')
        branches = ""
        bar = ""
    return {"telemetry": _telemetry_html(records, sections, now, queue_mtime),
            "sync-region": _sync_html(queue_mtime, queue_size),
            "sections": content,
            "branches": branches,
            "bar": bar}


def _container(rid: str, inner: str, oob: bool = False) -> str:
    """One region, in its stable-id container.

    Emitted by the page without the swap marker and by the fragment response
    with it, from the same call, so the container markup cannot disagree either.
    """
    marker = ' hx-swap-oob="true"' if oob else ""
    return f'<div id="{_e(rid)}" class="oob"{marker}>{inner}</div>'


def render_fragments(records: Sequence[QueueRecord], now: str,
                     queue_mtime: Optional[float] = None,
                     queue_size: Optional[int] = None) -> str:
    """The five dynamic regions, each marked for an out-of-band swap.

    One response, not five endpoints: the whole page updates in one round trip
    and cannot tear between regions that were rendered from different reads of
    the queue.
    """
    regions = _regions(records, now, queue_mtime, queue_size)
    return "".join(_container(rid, regions[rid], oob=True)
                   for rid in _REGION_IDS) + "\n"


def render_page(records: Sequence[QueueRecord], now: str,
                queue_mtime: Optional[float] = None,
                queue_size: Optional[int] = None) -> str:
    """Render the whole dashboard as one self-contained HTML page.

    Pure: a function of its arguments only. No I/O, no network, no clock -- the
    caller supplies `now` and the queue file's mtime.

    All three views are emitted into the DOM and selected by the `data-layout`
    attribute on <html>, so switching is instant and needs no reload and no
    round trip. The layout is never carried in the URL (decision 12's rejected
    alternative): a pinned home-screen app must keep its stored default.
    """
    regions = _regions(records, now, queue_mtime, queue_size)
    part = {rid: _container(rid, regions[rid]) for rid in _REGION_IDS}
    toggle = "".join(
        f'<button type="button" class="toggle-btn" data-set-layout="{_e(v)}" '
        f'aria-pressed="false">{_e(v.capitalize())}</button>'
        for v in ("checklist", "panels", "branches"))

    return (
        '<!doctype html>\n'
        '<html lang="en" data-layout="checklist">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        '<title>Worksweep</title>\n'
        f'<style>{_CSS}</style>\n'
        f'<script>{_HEAD_SCRIPT}</script>\n'
        # AFTER the layout-restore script: that script must sit within the
        # first few hundred characters before <body> for AC #31's lookback, and
        # 51KB of library in front of it would push it out of that window.
        f'<script>{_HTMX_JS}</script>\n'
        f'<link rel="icon" type="image/jpeg" href="{_e(_LOGO_URI)}">\n'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        '</head>\n<body>\n'
        '<div class="wrap">\n'
        '<header class="head">'
        f'<div class="brand"><img class="brand-logo" src="{_e(_LOGO_URI)}" '
        'alt="" width="22" height="22"> Worksweep</div>'
        f'<div class="toggle" role="group" aria-label="Layout">{toggle}</div>'
        f'{part["sync-region"]}{part["telemetry"]}</header>\n'
        f'{part["sections"]}\n'
        f'{part["branches"]}\n'
        '</div>\n'
        f'{part["bar"]}\n'
        f'<script>{_BODY_SCRIPT}</script>\n'
        '</body>\n</html>\n')


_ERROR_PAGE = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
               '<title>Worksweep</title></head><body>'
               '<h1>Worksweep</h1><p>The queue could not be rendered. '
               'See worksweep-dashboard.err.</p></body></html>\n')


# =============================================================================
# HTTP surface
# =============================================================================

def mtime_token(queue_mtime: Optional[float],
                queue_size: Optional[int] = None) -> str:
    """Stable string form of the queue's identity, for change detection.

    The page embeds the token it was rendered from and polls GET /mtime for a
    different one. A string compare needs no clock maths on the client and no
    agreement about float formatting between the two ends -- only that the same
    function produced both.

    The SIZE rides along because mtime alone is not a change detector on every
    filesystem: APFS keeps nanoseconds, but HFS+ and most network filesystems
    round to the second, and two sweeps landing inside one second is exactly
    the busy moment the page most needs to keep up with. Size is not a hash --
    a same-second rewrite to an identical length still hides -- but it costs
    one field of the stat already being made and removes the common case.
    """
    if not queue_mtime:
        return "0"
    if queue_size is None:
        return "%.6f" % queue_mtime
    return "%.6f:%d" % (queue_mtime, queue_size)


def relative_age(queue_mtime: Optional[float], now: str) -> str:
    """Human "how stale is this page" text, computed server-side.

    Rendered per request so the client needs no clock maths -- and so a phone
    in a different timezone, or with a skewed clock, still reads the same
    truth. Deliberately coarse: the point is glanceable staleness, not
    precision.
    """
    if not queue_mtime:
        return "never synced"
    try:
        seconds = datetime.datetime.fromisoformat(now).timestamp() - queue_mtime
    except (ValueError, TypeError):
        return "synced"
    if seconds < 90:
        # covers small clock skew (a negative age) as well as a fresh sweep
        return "synced just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"synced {minutes} min ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"synced {hours} hr ago"
    days = int(seconds // 86400)
    return f"synced {days} day{'' if days == 1 else 's'} ago"


def _queue_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _queue_stamp(path: str) -> Tuple[Optional[float], Optional[int]]:
    """(mtime, size) from ONE stat.

    One call, not two: reading the mtime and the size separately could straddle
    a rewrite and mint a token for a pairing that never existed on disk.
    """
    try:
        st = os.stat(path)
    except OSError:
        return (None, None)
    return (st.st_mtime, st.st_size)


def _valid_numbers(payload) -> Optional[List[int]]:
    """Validate a POST envelope: {"numbers": [int, ...]}.

    Returns the list, or None when the ENVELOPE is malformed (-> 400). Numbers
    matching no record are NOT an error -- they are a no-op at the approvals
    layer -- so validation rejects only the shape, never the contents.
    """
    if not isinstance(payload, dict):
        return None
    nums = payload.get("numbers")
    if not isinstance(nums, list):
        return None
    out = []
    for n in nums:
        # `isinstance(True, int)` is True, so bools are excluded explicitly.
        if isinstance(n, bool) or not isinstance(n, int):
            return None
        out.append(n)
    return out


def _audit_message(numbers: Sequence[int],
                   updated: Sequence[QueueRecord], actor: str = "") -> str:
    """Build the Discord confirmation, clamped under the Discord byte cap.

    "Approve all" is the highest-blast-radius action in worksweep, so it must
    ALWAYS leave a channel record. An unclamped message naming 200 items would
    be rejected by Discord and the audit trail would silently vanish for exactly
    the approval that most needed one -- so overflow is summarised instead.

    `actor` is attribution, not authorisation: Chandler sometimes has Claude
    press the button on his say-so, and the channel should be able to tell the
    two hands apart afterwards. `"claude"` is the only value that renders --
    anything else, including nothing at all, keeps the line byte-identical to
    what a browser tap has always posted. The suffix is chosen BEFORE the
    message is measured, so the longer one is inside the clamp rather than
    appended past it. `actor` is keyword-with-default because both existing
    call sites pass positionally.
    """
    by_num = {r.number: r for r in updated}
    parts = [f"{n} ({by_num[n].item.executor} {by_num[n].item.repo})".strip()
             for n in numbers if n in by_num]
    prefix = "✅ Approved: "
    suffix = " (dashboard · claude)" if actor == _ACTOR else " (dashboard)"

    full = prefix + ", ".join(parts) + suffix
    if len(full.encode("utf-8")) <= DISCORD_MAX_CHARS:
        return full

    kept = list(parts)
    while kept:
        kept.pop()
        candidate = (prefix + ", ".join(kept)
                     + f" … (+{len(parts) - len(kept)} more)" + suffix)
        if len(candidate.encode("utf-8")) <= DISCORD_MAX_CHARS:
            return candidate
    # Even one entry does not fit (absurd repo/executor strings) -- still say
    # something rather than nothing.
    return _truncate_bytes(f"{prefix}{len(parts)} items{suffix}",
                           DISCORD_MAX_CHARS)


# The one actor value that renders. A whitelist rather than a sanitiser
# because this string lands in a Discord post: anything broader means thinking
# about mentions, links and length every time someone touches the endpoint.
_ACTOR = "claude"


def _valid_actor(payload) -> str:
    """The attributed actor from an approve envelope, or "" for everyone else.

    Never raises and never rejects the request: an unrecognised actor is an
    unattributed approval, not a failed one -- the approval itself was already
    consented to by whoever held the ✅. The submitted text is never echoed
    anywhere, so a 5000-character actor or one carrying an @everyone simply
    renders the ordinary "(dashboard)".
    """
    if not isinstance(payload, dict):
        return ""
    actor = payload.get("actor")
    if not isinstance(actor, str) or actor != _ACTOR:
        return ""
    return actor


def _valid_number(payload) -> Optional[int]:
    """Validate a `{"number": N}` envelope. None when malformed (-> 400).

    Bools are excluded explicitly: `isinstance(True, int)` is True in Python,
    so `{"number": true}` would otherwise dismiss record 1.
    """
    if not isinstance(payload, dict):
        return None
    number = payload.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return number


class DashboardHandler(BaseHTTPRequestHandler):
    """GET `/` renders; POST `/approve` and `/approve-all` persist.

    Nothing in here may raise: the launchd agent is KeepAlive, so an escaping
    exception is a restart loop.
    """

    server_version = "worksweep-dashboard/1"
    # Required by GET /events: HTTP/1.0 closes the connection after every
    # response, so a stream could never stay open. Class-wide, so every route
    # became keep-alive at the same moment -- which is safe only because _send
    # always sets a Content-Length, and because a refusal that skipped reading
    # the request body now closes rather than leaving it on the wire.
    protocol_version = "HTTP/1.1"
    # ...which also made every connection reusable, and a reused connection
    # with nobody on it holds a thread and a socket until the process restarts.
    # A tab left open all weekend is not an attack, it is Tuesday.
    # BaseHTTPRequestHandler applies this as a SOCKET timeout and turns the
    # resulting read timeout into a close. Comfortably longer than the event
    # stream's 15s heartbeat, so a live stream is never the thing it reaps.
    timeout = 65

    def _log_line(self, fmt, args) -> str:
        """Render a log line with control bytes stripped.

        The request line is attacker-controlled and lands in a file a human
        later tails: raw \r / \x1b would let a request forge log lines or drive
        the reader's terminal.
        """
        try:
            body = fmt % args if args else str(fmt)
        except Exception:
            body = repr((fmt, args))
        line = f"worksweep-dashboard: {self.address_string()} - {body}"
        return _CTRL_RE.sub("?", line)

    def log_message(self, fmt, *args):   # noqa: A003
        # Access logs to stdout (the .log file) so .err stays meaningful for
        # actual failures under launchd.
        sys.stdout.write(self._log_line(fmt, args) + "\n")
        sys.stdout.flush()

    def log_error(self, fmt, *args):
        # BaseHTTPRequestHandler routes malformed-request errors here and would
        # otherwise echo the raw request line to stderr unsanitised.
        sys.stderr.write(self._log_line(fmt, args) + "\n")

    def handle_one_request(self):
        """As the base class, minus the traceback for a client going away.

        Keep-alive means the read for a NEXT request always happens, so a
        closed tab or a phone leaving the tailnet arrives here as a
        ConnectionResetError -- which socketserver turns into a full traceback
        in the .err file. A client disappearing is the ordinary end of a
        connection, not a server failure, and .err staying meaningful for real
        failures is the whole reason access logs go to stdout.

        The base class already handles the read TIMEOUT (one sanitised line);
        this adds only the reset family, and closes rather than swallowing.
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    # -- plumbing --------------------------------------------------------
    def _send(self, code: int, ctype: str, body: bytes,
              headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        # The page is a local view of private PLA work: never let it be framed
        # or sniffed, and never let a referrer leak an MR title off the tailnet.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict,
              headers: Optional[Dict[str, str]] = None) -> None:
        self._send(code, "application/json",
                   json.dumps(payload).encode("utf-8"), headers=headers)

    def _text(self, code: int, message: str) -> None:
        self._send(code, "text/plain; charset=utf-8",
                   message.encode("utf-8"))

    def _reject(self, code: int, message: str) -> None:
        """A refusal issued WITHOUT reading the request body.

        Under HTTP/1.1 the connection is reused, so an unread body would be
        parsed as the next request line: a CSRF rejection would be followed by
        a mystery 400 on a socket the browser believed was clean. Closing is
        the correct answer to "I am not going to read what you sent".

        Every refusal goes through here, including the ones issued AFTER the
        body was read. A refusal is the one moment the two ends most disagree
        about what was actually sent, which makes it the worst possible moment
        to keep the socket and hope.
        """
        self._send(code, "text/plain; charset=utf-8", message.encode("utf-8"),
                   headers={"Connection": "close"})

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    # -- CSRF ------------------------------------------------------------
    def _csrf_ok(self) -> bool:
        """Custom header REQUIRED, Origin checked when present.

        The custom header is the whole defense: a cross-origin page cannot set
        it without a preflight this server never answers. The Origin check is
        the belt to that braces. An ABSENT Origin is allowed -- a same-origin
        fetch on a plain page may legitimately omit it.
        """
        if not (self.headers.get(_CSRF_HEADER) or "").strip():
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            netloc = urllib.parse.urlsplit(origin).netloc
            if not netloc or netloc != (self.headers.get("Host") or ""):
                return False
        return True

    def _body_bytes(self) -> Optional[bytes]:
        """The request body, or None for anything this server will not read.

        None means "refuse and close", never "empty body".
        """
        if (self.headers.get("Transfer-Encoding") or "").strip():
            # We read exactly Content-Length bytes, so a chunked request would
            # read ZERO and leave its whole body on a connection HTTP/1.1 says
            # is reusable -- where it becomes the next request line. Our own
            # page never sends chunked, so refuse it rather than half-implement
            # a framing we have no use for.
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            return None
        try:
            return self.rfile.read(length) if length else b""
        except OSError:
            return None

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:                                  # noqa: N802
        path = self._path()
        if path == "/mtime":
            # Read-only and side-effect free, so no CSRF guard: it leaks only
            # "when did the queue last change", which the page already shows.
            # The Sync flow polls this to know when a kicked sweep has landed.
            self._text(200, mtime_token(*_queue_stamp(self.server.queue_path)))
            return
        if path in ("/apple-touch-icon.png",
                    "/apple-touch-icon-precomposed.png"):
            # Read-only, side-effect free; served from a repo asset so the
            # phone home-screen icon survives page redesigns.
            try:
                icon = _APPLE_ICON_PATH.read_bytes()
            except OSError:
                self._text(404, "icon asset missing")
                return
            self._send(200, "image/png", icon)
            return
        if path == "/fragments":
            # Same rationale as /mtime, and the same absence of a CSRF guard:
            # a read that leaks only what the page it is refreshing already
            # shows. This is the whole live-update path -- SSE only says
            # "something changed", this says what it changed to.
            qpath = self.server.queue_path
            try:
                body = render_fragments(load_queue(qpath), self.server.now(),
                                        *_queue_stamp(qpath)).encode(
                                            "utf-8", errors="replace")
            except Exception as e:             # never crash the agent
                print(f"worksweep: dashboard fragment render failed: {e}",
                      file=sys.stderr)
                self._text(500, "fragment render failed")
                return
            self._send(200, "text/html; charset=utf-8", body)
            return
        if path == "/events":
            self._events()
            return
        if path != "/":
            self._text(404, "not found")
            return
        qpath = self.server.queue_path
        try:
            body = render_page(load_queue(qpath), self.server.now(),
                               *_queue_stamp(qpath)).encode("utf-8",
                                                            errors="replace")
        except Exception as e:                     # never crash the agent
            print(f"worksweep: dashboard render failed: {e}", file=sys.stderr)
            body = _ERROR_PAGE.encode("utf-8")
        self._send(200, "text/html; charset=utf-8", body)

    def _events(self) -> None:
        """Server-sent events: one frame whenever queue.json's mtime moves.

        Holds NO lock, deliberately and permanently. This connection stays open
        for hours; the write side takes _WRITE_LOCK and the queue file lock for
        the shortest possible window and must never end up queued behind a
        reader that never finishes. All this route ever does is stat a path.

        Bypasses _send, which is body-first with a fixed Content-Length -- the
        one thing a stream by definition cannot supply.

        Like every other handler it may not raise: the only way out is a failed
        write to a client that went away, and that is the intended exit.
        """
        qpath = self.server.queue_path
        # Bound to a local: the slot released at the end must be the very
        # object the slot was taken from, whatever the module global says by
        # then. BoundedSemaphore.release() RAISES if it over-releases, and this
        # one runs in a `finally` where nothing is allowed to.
        slots = _EVENT_SLOTS
        if not slots.acquire(blocking=False):
            # A well-framed refusal, not a dropped connection: the pool being
            # full is a temporary condition and EventSource will come back.
            self._text(503, "too many event streams")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # No Content-Length is possible, so the connection cannot be
            # reused; say so rather than leaving a keep-alive client guessing.
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()

            def emit(payload: str) -> None:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()

            last = mtime_token(*_queue_stamp(qpath))
            # Announce the CURRENT token immediately, ahead of the loop.
            #
            # Without this, `last` is seeded from the file as it is right now
            # and everything that changed while this client was NOT connected
            # is never announced. EventSource reconnects by itself after an
            # agent restart or a tailnet blip, so that window is routine -- and
            # swallowing it is the silent-stale bug this whole build exists to
            # kill, reappearing at the worst possible moment. It also closes
            # the smaller gap between a page being rendered and its stream
            # opening. Immediately, not inside the loop, or every page load
            # would be one stat interval behind for no reason.
            #
            # Announcing a token the client already holds costs one 3KB GET,
            # because the refresh it triggers is idempotent. Missing a change
            # costs a page that is quietly wrong until somebody touches it.
            #
            # "0" is the absence of a queue file, which is not a change:
            # announcing it would make every page opened before the first sweep
            # flash the all-clear at itself.
            if last != "0":
                emit(f"event: queue\ndata: {last}\n\n")
            beat = time.monotonic()
            while True:
                time.sleep(_EVENT_STAT_SECONDS)
                token = mtime_token(*_queue_stamp(qpath))
                now = time.monotonic()
                if token != last:
                    last = token
                    emit(f"event: queue\ndata: {token}\n\n")
                    beat = now
                elif now - beat >= _EVENT_HEARTBEAT_SECONDS:
                    # Invisible to EventSource, but it is still a WRITE: a
                    # client that vanished fails here and this thread ends,
                    # instead of spinning on a socket nobody is reading.
                    emit(": heartbeat\n\n")
                    beat = now
        except Exception:
            # Almost always the client going away, which is the normal end of
            # a stream and not worth a log line.
            return
        finally:
            slots.release()

    def do_POST(self) -> None:                                 # noqa: N802
        path = self._path()
        if path not in ("/approve", "/approve-all", "/sweep", "/dismiss",
                        "/consult", "/accept-rec"):
            self._reject(404, "not found")
            return
        if not self._csrf_ok():
            self._reject(403, "forbidden")
            return

        raw = self._body_bytes()
        if raw is None:
            self._reject(400, "bad request")
            return

        if path == "/sweep":
            # No body: the sweep takes no arguments. `raw` was still read so
            # the connection is left in a sane state.
            try:
                self._sweep()
            except Exception as e:                 # never crash the agent
                print(f"worksweep: dashboard sweep failed: {e}", file=sys.stderr)
                self._json(500, {"started": False, "error": "sweep failed"})
            return

        # BOTH routes now carry `{"numbers": [...]}`. For /approve-all those
        # are the proposed+runnable numbers the page actually displayed: the
        # server flips that set INTERSECTED with what is still eligible, so the
        # user approves exactly what they were shown and consented to. An item
        # that landed between render and tap is not swept in silently.
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._reject(400, "bad request")
            return

        if path == "/dismiss":
            number = _valid_number(payload)
            if number is None:
                self._reject(400, "bad request")
                return
            try:
                self._dismiss(number, _valid_actor(payload))
            except Exception as e:                 # never crash the agent
                print(f"worksweep: dashboard dismiss failed: {e}", file=sys.stderr)
                self._json(500, {"dismissed": False, "error": "dismiss failed"})
            return

        if path in ("/consult", "/accept-rec"):
            number = _valid_number(payload)
            if number is None:
                self._reject(400, "bad request")
                return
            try:
                if path == "/consult":
                    self._consult_request(number)
                else:
                    self._accept_rec(number, _valid_actor(payload))
            except Exception as e:                 # never crash the agent
                print(f"worksweep: dashboard {path} failed: {e}",
                      file=sys.stderr)
                self._json(500, {"ok": False, "error": f"{path} failed"})
            return

        numbers = _valid_numbers(payload)
        if numbers is None:
            self._reject(400, "bad request")
            return

        try:
            self._approve(path, numbers, _valid_actor(payload))
        except Exception as e:                     # never crash the agent
            print(f"worksweep: dashboard approval failed: {e}", file=sys.stderr)
            self._text(500, "approval failed")

    def _sweep(self) -> None:
        """Kick a sweep out-of-process, throttled.

        Deliberately does NOT run the sweep here: it belongs to its own launchd
        agent with its own environment, log files and Discord digest, it writes
        the queue itself (running it in-process would mean two writers racing on
        one file), and it takes ~90s, which would pin one of this server's
        request threads for the duration.

        The throttle bounds Discord noise -- every sweep posts a digest -- and
        is per-server rather than module-global so the state cannot leak between
        tests. The lock is held only across the check-and-set, never across the
        subprocess, so a sync never blocks an approval.
        """
        started_at = time.time()
        with _WRITE_LOCK:
            elapsed = started_at - getattr(self.server, "sweep_last", 0.0)
            if elapsed < _SWEEP_MIN_INTERVAL_SECONDS:
                retry_after = int(_SWEEP_MIN_INTERVAL_SECONDS - elapsed) + 1
                self._json(429, {"started": False, "retry_after": retry_after},
                           headers={"Retry-After": str(retry_after)})
                return
            self.server.sweep_last = started_at

        sweep = getattr(self.server, "sweep", None)
        if sweep is None:
            with _WRITE_LOCK:                      # nothing was started
                self.server.sweep_last = 0.0
            self._json(500, {"started": False, "error": "sweep is not wired"})
            return

        try:
            sweep()
        except Exception as e:
            # Nothing started, so do not hold the user off for a minute over a
            # failure that produced no digest -- let them retry immediately.
            with _WRITE_LOCK:
                self.server.sweep_last = 0.0
            print(f"worksweep: dashboard could not start a sweep: {e}",
                  file=sys.stderr)
            self._json(500, {"started": False, "error": str(e)[:200]})
            return
        self._json(202, {"started": True})

    def _dismiss(self, number: int, actor: str = "") -> None:
        """Retire a non-runnable row: flip it to done/`dismissed`.

        Ordering is deliberate. The local flip happens first, under the lock and
        durable, and the GitLab call happens after and outside the lock:

        * a glab failure must not block the dismiss (it is a courtesy that also
          clears the todo in GitLab, not the point of the action);
        * the glab call has a 30s timeout, and holding the write lock across it
          would stall every approval on the page for that long -- the same
          mistake the sweep route avoids.
        """
        with _WRITE_LOCK, write_lock(self.server.queue_path):
            records = load_queue(self.server.queue_path)
            target = None
            for r in records:
                if r.number == number:
                    target = r
                    break
            if target is None:
                self._json(400, {"dismissed": False,
                                 "error": f"no record #{number}"})
                return
            # The flip itself lives in queue.py: the dashboard writes no
            # status of its own, exactly as with approvals.
            updated, flipped = dismiss_record(records, number, self.server.now())
            if flipped is None:
                # Runnable items are approve-territory; already-terminal ones
                # have nothing to dismiss.
                self._json(400, {"dismissed": False,
                                 "error": f"#{number} is not dismissable"})
                return
            save_queue(self.server.queue_path, updated)

        self._mark_todo_done(target.item, number)
        self._record_seen_notes(target.item, number)
        self._record_reviewed_sha(target.item, number)
        self._audit_dismiss(number, target.item, actor)
        self._json(200, {"dismissed": True, "number": number})

    def _consult_request(self, number: int) -> None:
        """Queue a Send-to-Fable consult on a parked row. The flip itself
        lives in queue.request_consult — the dashboard holds no status rules
        of its own (AC #20), it only maps outcomes to HTTP."""
        with _WRITE_LOCK, write_lock(self.server.queue_path):
            records = load_queue(self.server.queue_path)
            updated, outcome = request_consult_record(records, number,
                                                      self.server.now())
            if outcome == "requested":
                save_queue(self.server.queue_path, updated)
        if outcome in ("requested", "already"):
            self._json(200, {"ok": True, "consult": "requested"})
        elif outcome == "has-rec":
            self._json(400, {"ok": False,
                             "error": f"#{number} already has a recommendation "
                                      f"— accept it or handle it in a session"})
        else:
            self._json(400, {"ok": False,
                             "error": f"#{number} is not a parked question"})

    def _accept_rec(self, number: int, actor: str = "") -> None:
        """Accept a consult recommendation: queue.accept_rec flips the row to
        approved with the rec as the operator RULING. This is the one approval
        path that carries content, so its audit line says so."""
        with _WRITE_LOCK, write_lock(self.server.queue_path):
            records = load_queue(self.server.queue_path)
            updated, accepted = accept_rec_record(records, number,
                                                  self.server.now())
            if accepted is None:
                self._json(400, {"ok": False,
                                 "error": f"#{number} has no recommendation "
                                          f"to accept"})
                return
            save_queue(self.server.queue_path, updated)
        suffix = (" (dashboard · claude · accepted fable rec)"
                  if actor == _ACTOR else " (dashboard · accepted fable rec)")
        confirm = (f"✅ Approved: #{number} {accepted.item.repo} "
                   f"{accepted.item.id}{suffix}")
        post, webhook = self.server.post, self.server.webhook
        if post and webhook:
            try:
                post(webhook, confirm)
            except Exception as e:
                print(f"worksweep: dashboard accept-rec post failed: {e}",
                      file=sys.stderr)
        else:
            print(confirm)
        self._json(200, {"ok": True, "approved": number})

    def _record_reviewed_sha(self, item, number: int) -> None:
        """Dismissing a re_review row means "I re-reviewed this head myself":
        record the row's sha so the sensor resolves instead of re-proposing.
        A later push changes the head and the row comes back — evidence-keyed,
        exactly like seen-notes."""
        if getattr(item, "kind", "") != "re_review" or not getattr(item, "sha", ""):
            return
        record = getattr(self.server, "record_reviewed", None)
        if record is None:
            return
        iid = mr_iid_of(item)
        if not iid:
            return
        try:
            record(f"{item.repo}!{iid}", item.sha)
        except Exception as e:
            print(f"worksweep: dismissing #{number} could not record reviewed "
                  f"sha: {type(e).__name__}: {e}", file=sys.stderr)

    def _record_seen_notes(self, item, number: int) -> None:
        """Remember which notes this dismissal covered, so the next sweep does
        not re-derive the same row from the same note.

        Best-effort, exactly like the todo edge above it: the row is already
        `done` on disk by the time this runs. Losing the durable half means
        the row comes back next sweep -- annoying, not wrong -- while raising
        here would report a dismissal that DID happen as having failed.

        Keyed on (discussion, last note), so a reviewer's follow-up changes
        the key and the row returns. A row carrying no refs (written before
        the field existed) simply dismisses without durability and picks it up
        on the next sweep.
        """
        refs = getattr(item, "note_refs", ())
        path = getattr(self.server, "seen_path", "")
        if not refs or not path:
            return
        try:
            record_seen(path, refs, self.server.now())
        except Exception as e:
            print(f"worksweep: dismissed #{number} but could not record its "
                  f"notes ({path}): {e}", file=sys.stderr)

    def _mark_todo_done(self, item, number: int) -> None:
        """Best-effort: clear the matching GitLab todo. Never fatal."""
        todo_id = todo_id_of(item)
        if not todo_id:
            if item.kind == "todo":
                print(f"worksweep: dismissed #{number} locally; the GitLab todo "
                      f"was NOT marked done (the queue record carries no todo "
                      f"id: {item.id!r})", file=sys.stderr)
            return
        edge = getattr(self.server, "mark_todo_done", None)
        if edge is None:
            return
        try:
            edge(todo_id)
        except Exception as e:
            print(f"worksweep: could not mark GitLab todo {todo_id} done: {e}",
                  file=sys.stderr)

    def _audit_dismiss(self, number: int, item, actor: str = "") -> None:
        # f-015: same attribution as an approve. Dismissing is the other half
        # of the same decision -- "I handled this" -- and the channel should be
        # able to tell Chandler's hand from Claude's on both.
        suffix = " (dashboard · claude)" if actor == _ACTOR else " (dashboard)"
        confirm = (f"🗑️ dismissed {number} "
                   f"({item.executor} {item.repo})".rstrip() + suffix)
        post, webhook = self.server.post, self.server.webhook
        if post and webhook:
            try:
                post(webhook, confirm)
            except Exception as e:
                print(f"worksweep: dismiss confirmation post failed: {e}",
                      file=sys.stderr)
        else:
            print(confirm)

    def _approve(self, path: str, numbers: Optional[List[int]],
                 actor: str = "") -> None:
        qpath = self.server.queue_path
        now = self.server.now()
        with _WRITE_LOCK, write_lock(qpath):
            # Load FRESH from disk, never from the rendered snapshot: the page
            # may be stale and the runner may have claimed items since.
            # Flipping a cached list would resurrect stale statuses over newer
            # ones on save_queue's whole-file replace.
            #
            # TWO locks, different scopes: `_WRITE_LOCK` keeps two concurrent
            # taps in THIS process from interleaving, and `write_lock` keeps
            # the sweep, intake and the runner out of the same read-modify-write
            # (f-007/f-028/f-029). Neither subsumes the other -- one is a
            # threading lock, the other an flock other processes can see.
            records = load_queue(qpath)
            if path == "/approve":
                updated, newly = approve_numbers(records, set(numbers or []), now)
            else:
                updated, newly = approve_all(records, now,
                                             numbers=set(numbers or []))
            if newly:
                # Durable BEFORE the audit post: a Discord failure must not be
                # able to roll this back.
                save_queue(qpath, updated)
        if newly:
            self._audit(sorted(newly), updated, actor)
        self._send(200, "application/json",
                   json.dumps({"approved": sorted(newly)}).encode("utf-8"))

    def _audit(self, numbers: Sequence[int],
               updated: Sequence[QueueRecord], actor: str = "") -> None:
        """Never-silent: the channel stays the single history of what was
        approved. A failed post is logged and swallowed -- the approval already
        reached disk and must not be undone."""
        confirm = _audit_message(numbers, updated, actor)
        post, webhook = self.server.post, self.server.webhook
        if post and webhook:
            try:
                post(webhook, confirm)
            except Exception as e:
                print(f"worksweep: dashboard confirmation post failed: {e}",
                      file=sys.stderr)
        else:
            print(confirm)


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(address: Tuple[str, int], queue_path: str,
                post: Optional[Callable[[str, str], None]] = None,
                webhook: str = "",
                now: Optional[Callable[[], str]] = None,
                sweep: Optional[Callable[[], None]] = None,
                mark_todo_done: Optional[Callable[[int], None]] = None,
                seen_path: str = "",
                record_reviewed: Optional[Callable[[str, str], None]] = None
                ) -> _DashboardServer:
    """Build (but do not start) the dashboard server.

    Split out of `serve` so tests can bind port 0 on a thread and shut it down;
    `serve_forever` on the configured port never runs under pytest.
    """
    httpd = _DashboardServer(address, DashboardHandler)
    httpd.queue_path = queue_path
    httpd.post = post
    httpd.webhook = webhook
    httpd.now = now or _now
    # Injected edge: kicking the sweep agent is the CLI's job (it owns the
    # launchd knowledge), so this module never learns about launchctl and the
    # tests never spawn anything.
    httpd.sweep = sweep
    httpd.sweep_last = 0.0
    httpd.mark_todo_done = mark_todo_done
    httpd.seen_path = seen_path
    # Injected edge: dismissing a re_review row records "reviewed at sha"
    # into the reviewedstate sidecar; None -> recording silently off (tests,
    # older callers).
    httpd.record_reviewed = record_reviewed
    return httpd


def is_allowed_bind(address: str) -> bool:
    """True for loopback or a Tailscale CGNAT address, False for anything else.

    Tailnet-only exposure IS the security model here (decision 5): there is no
    auth layer, and the page shows private PLA MR titles and whys. Binding a LAN
    address or 0.0.0.0 would publish all of it to anyone on the network.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or ip in _TAILNET_NETWORK


def _tailscale_ipv4(run_subprocess: Callable) -> str:
    """First IPv4 address tailscale reports, or "" if it cannot be asked.

    Tries the PATH binary first, then the app bundle: under launchd the mini's
    PATH does not always include the CLI shim.
    """
    for binary in ("tailscale", _TAILSCALE_FALLBACK_BIN):
        try:
            result = run_subprocess([binary, "ip", "-4"],
                                    capture_output=True, text=True, timeout=10)
        except Exception:
            continue          # not installed at this path -- try the next
        if getattr(result, "returncode", 1) != 0:
            continue
        for line in (getattr(result, "stdout", "") or "").splitlines():
            address = line.strip()
            if address:
                return address
    return ""


def resolve_bind(bind: str,
                 run_subprocess: Callable = subprocess.run) -> str:
    """Resolve `--bind` to an address, or "" when `auto` cannot resolve YET.

    An explicit bind is VALIDATED and rejected hard if it is not loopback or
    tailnet -- a typo must never quietly publish the queue to the LAN.

    `auto` returns "" rather than falling back to loopback: falling back would
    strand the dashboard unreachable until someone noticed and restarted it.
    The caller retries instead (see `serve`), so a dashboard that starts before
    tailscaled simply picks up the address a few seconds later.
    """
    if bind and bind != "auto":
        if not is_allowed_bind(bind):
            raise ValueError(
                f"refusing to bind {bind!r}: the dashboard has no auth and "
                f"serves private PLA work, so it may only bind loopback or a "
                f"Tailscale address (100.64.0.0/10)")
        return bind
    address = _tailscale_ipv4(run_subprocess)
    if address and is_allowed_bind(address):
        return address
    if address:
        print(f"worksweep: tailscale reported {address!r}, which is not a "
              f"tailnet address; ignoring", file=sys.stderr)
    return ""


def serve(queue_path: str, port: int = DEFAULT_PORT, bind: str = "auto",
          post: Optional[Callable[[str, str], None]] = None,
          webhook: str = "",
          sweep: Optional[Callable[[], None]] = None,
          mark_todo_done: Optional[Callable[[int], None]] = None,
          seen_path: str = "",
          record_reviewed: Optional[Callable[[str, str], None]] = None,
          run_subprocess: Callable = subprocess.run,
          sleep: Callable[[float], None] = time.sleep,
          max_attempts: int = 0) -> int:
    """Serve the dashboard until interrupted. Blocks forever -- CLI only.

    Resolution AND bind live in one retry loop. Both of the failures that
    actually happen on the mini are transient: the agent starts at boot before
    tailscaled has an address, and the port is briefly held after a restart.
    Retrying in-process beats the alternatives -- crash-looping under KeepAlive
    (which buries the reason in a restart storm) or falling back to loopback
    permanently (silently unreachable).

    `post` and `webhook` are injected: this module must not import the CLI that
    owns the Discord poster, because that module imports this one.
    `max_attempts` is for tests; 0 means retry forever.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            address = resolve_bind(bind, run_subprocess=run_subprocess)
        except ValueError as e:
            # A bad EXPLICIT bind is a configuration error, not a transient
            # one: retrying would never fix it and would hide the message.
            print(f"worksweep: {e}", file=sys.stderr)
            return 1

        httpd = None
        if address:
            try:
                httpd = make_server((address, port), queue_path,
                                    post=post, webhook=webhook, sweep=sweep,
                                    mark_todo_done=mark_todo_done,
                                    seen_path=seen_path,
                                    record_reviewed=record_reviewed)
            except OSError as e:
                print(f"worksweep: dashboard cannot bind {address}:{port} "
                      f"({e})", file=sys.stderr)
        else:
            print("worksweep: no tailscale address yet", file=sys.stderr)

        if httpd is not None:
            print(f"worksweep: dashboard serving http://{address}:{port} "
                  f"({queue_path})", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                httpd.server_close()
            return 0

        if max_attempts and attempt >= max_attempts:
            return 1
        print(f"worksweep: dashboard retrying in {_BIND_RETRY_SECONDS}s "
              f"(attempt {attempt})", file=sys.stderr)
        sleep(_BIND_RETRY_SECONDS)
