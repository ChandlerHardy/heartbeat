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
from .models import RUNNABLE_EXECUTORS, QueueRecord, WorkItem
from .queue import load_queue, save_queue

DEFAULT_PORT = 8787
_LOOPBACK = "127.0.0.1"
# The CSRF defense (decision 10): a cross-origin page cannot attach a custom
# header without a CORS preflight, and this server answers none -- so the mere
# PRESENCE of this header proves the request came from our own page. Do not add
# do_OPTIONS and do not emit any Access-Control-* header, or the guard evaporates.
_CSRF_HEADER = "X-Worksweep"
_MAX_BODY_BYTES = 64 * 1024
_REFRESH_SECONDS = 60
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
_ISSUE_URL_RE = re.compile(r"/-/issues/(\d+)")
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

        if branch:
            title = branch
        elif mr_links:
            title = f"!{int(_MR_URL_RE.search(mr_links[0]).group(1))}"
        elif bare:
            title = f"!{bare[0]}"
        else:
            title = "Workstream"

        groups.append(Group(
            title=title, branch=branch,
            records=tuple(sorted(recs, key=lambda r: _as_int(r.number))),
            mr_links=tuple(mr_links), issue_links=tuple(issue_links),
            bare_mr_refs=tuple(bare)))

    groups.sort(key=lambda g: min(_as_int(r.number) for r in g.records))
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
.sweep{color:var(--ink-2);font-size:13px;font-variant-numeric:tabular-nums}
.counts{display:flex;flex-wrap:wrap;gap:6px;width:100%;margin-top:2px}
.cnt{
  font-size:12px;font-variant-numeric:tabular-nums;color:var(--ink-2);
  background:var(--panel-2);border:1px solid var(--line-soft);
  border-radius:999px;padding:3px 10px;
}
.cnt-week{color:var(--ink);border-color:var(--accent-2)}

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

_BODY_SCRIPT = """
(function(){
  var root=document.documentElement,KEY='%(key)s',RELOAD_MS=%(reload)d;
  var inflight=false;
  function scope(){
    return document.querySelector(root.getAttribute('data-layout')==='branches'?'.branches':'.sections');
  }
  function boxes(sel){
    var s=scope();
    return s?Array.prototype.slice.call(s.querySelectorAll(sel)):[];
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
  function refresh(){
    var n=selected().length,c=document.getElementById('sel-count'),
        go=document.getElementById('approve-selected'),
        all=document.getElementById('approve-all');
    if(c){c.textContent=n;}
    // Both buttons are re-enabled here: send() disables them for the round
    // trip, and a failed POST must not leave the page permanently inert.
    if(go){go.disabled=inflight||n===0;}
    if(all){all.disabled=inflight||blanket().length===0;}
  }
  function setLayout(v){
    root.setAttribute('data-layout',v);
    try{localStorage.setItem(KEY,v);}catch(e){}
    marks();refresh();
  }
  function marks(){
    var cur=root.getAttribute('data-layout'),
        b=document.querySelectorAll('[data-set-layout]');
    for(var i=0;i<b.length;i++){
      b[i].setAttribute('aria-pressed',b[i].getAttribute('data-set-layout')===cur?'true':'false');
    }
  }
  function send(url,list){
    inflight=true;refresh();
    fetch(url,{method:'POST',headers:{'X-Worksweep':'approve','Content-Type':'application/json'},body:JSON.stringify({numbers:list})})
      .then(function(r){
        if(r.status===200){location.reload();return;}
        inflight=false;alert('Approval failed ('+r.status+')');refresh();
      })
      .catch(function(e){inflight=false;alert('Approval failed: '+e);refresh();});
  }
  // A timed reload instead of <meta refresh>: never reload mid-POST (which
  // would tear an approval) and never reload while boxes are ticked (which
  // would silently discard the selection under the user's thumb). Reschedule
  // and try again later instead.
  function tick(){
    if(inflight||selected().length){setTimeout(tick,RELOAD_MS);return;}
    location.reload();
  }
  setTimeout(tick,RELOAD_MS);
  document.addEventListener('click',function(e){
    var t=e.target&&e.target.closest?e.target.closest('[data-set-layout]'):null;
    if(t){setLayout(t.getAttribute('data-set-layout'));}
  });
  document.addEventListener('change',function(e){
    var b=e.target;
    if(b&&b.type==='checkbox'&&b.hasAttribute('data-view')){
      // the same record has a checkbox in each view: keep them in step so a
      // selection survives a layout switch
      var twins=document.querySelectorAll('input[type=checkbox][value="'+b.value+'"]');
      for(var i=0;i<twins.length;i++){twins[i].checked=b.checked;}
      refresh();
    }
  });
  var sel=document.getElementById('approve-selected');
  if(sel){sel.addEventListener('click',function(){
    var n=selected();
    if(!n.length){return;}
    send('/approve',n);
  });}
  var all=document.getElementById('approve-all');
  if(all){all.addEventListener('click',function(){
    // There is NO un-approve path anywhere in worksweep, and this is one tap
    // wide on a phone -- so the bulk action confirms with its blast radius,
    // counting exactly the set it is about to send.
    var n=blanket();
    if(!n.length){alert('Nothing is proposed right now.');return;}
    if(!confirm('Approve all '+n.length+' proposed items?')){return;}
    send('/approve-all',n);
  });}
  marks();refresh();
})();
""" % {"key": _LAYOUT_STORAGE_KEY, "reload": _REFRESH_SECONDS * 1000}


def _link(url: str, label: str) -> str:
    """An anchor when the URL is really a web URL, escaped text otherwise.

    Refusing to linkify anything else keeps a `javascript:` value in a queue
    record from becoming a clickable payload.
    """
    if url and url.startswith(_LINKABLE_SCHEMES):
        return f'<a href="{_e(url)}">{_e(label)}</a>'
    return f"<span>{_e(label)}</span>"


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
    if item.executor not in RUNNABLE_EXECUTORS:
        return ('<span class="manual" title="no runner claims this executor '
                '- handle it by hand">manual</span>')
    blanket = ' data-blanket="1"' if is_blanket_eligible(item) else ''
    return (f'<label class="check">'
            f'<input type="checkbox" data-view="{_e(view)}" '
            f'value="{_e(record.number)}"{blanket} '
            f'aria-label="approve item {_e(record.number)}"></label>')


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

    age = _age(record, now)
    meta = f'<div class="meta">{_e(age)}</div>' if age else ""
    return (f'<div class="row">{_checkbox(record, "sections")}'
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
    return (f'<div class="row"><span class="spacer"></span>'
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
    return (f'<div class="row">{_checkbox(record, "branches")}'
            f'<div class="cell"><div class="line1">'
            f'<span class="num">#{_e(record.number)}</span>'
            f'<span class="exec">{_e(item.executor)}</span>{_chip(record)}'
            f'</div><div class="why">{_e(item.why)}</div></div></div>')


def _card(title: str, refs_html: str, records: Sequence[QueueRecord]) -> str:
    rows = "".join(_safe(_card_row, r) for r in records)
    refs = f'<div class="card-refs">{refs_html}</div>' if refs_html else ""
    return (f'<section class="card"><div class="card-h">'
            f'<div class="card-t">{_e(title)}</div>{refs}</div>'
            f'<div class="rows">{rows}</div></section>')


def _branches_html(groups: Sequence[Group],
                   ungrouped: Sequence[QueueRecord]) -> str:
    cards = []
    for g in groups:
        refs = []
        for url in g.mr_links:
            refs.append(_link(url, f"!{int(_MR_URL_RE.search(url).group(1))}"))
        for iid in g.bare_mr_refs:
            # No URL to link -- render the reference as plain text rather than
            # constructing a URL we do not actually have.
            refs.append(f'<span class="ref-bare">!{_e(iid)}</span>')
        for url in g.issue_links:
            refs.append(_link(url, f"#{int(_ISSUE_URL_RE.search(url).group(1))}"))
        cards.append(_card(g.title, "".join(refs), g.records))
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
    pills = "".join(f'<span class="cnt">{_e(st)} {counts[st]}</span>'
                    for st in ordered)
    if queue_mtime:
        stamp = datetime.datetime.fromtimestamp(
            queue_mtime).strftime("%Y-%m-%d %H:%M")
        sweep = f"last sweep {stamp}"
    else:
        sweep = "last sweep unknown"
    week = done_this_week(records, now)
    return (f'<div class="sweep">{_e(sweep)}</div>'
            f'<div class="counts">{pills}'
            f'<span class="cnt cnt-week">done this week: {week}</span></div>')


def _bar_html(records: Sequence[QueueRecord]) -> str:
    """The sticky action bar.

    Deliberately carries NO server-side count: the page computes both the
    confirm-dialog count and the numbers it POSTs from the same rendered
    `data-blanket` rows, so the two can never disagree. A server-rendered count
    would go stale the moment the queue changed and would tell the user they
    were approving N items while sending a different set.
    """
    del records                      # the page derives the set from the DOM
    return (
        '<div class="bar">'
        '<button type="button" class="btn" id="approve-selected" disabled>'
        'Approve selected (<span id="sel-count">0</span>)</button>'
        '<button type="button" class="btn btn-go" id="approve-all" disabled>'
        'Approve all</button>'
        '</div>')


def render_page(records: Sequence[QueueRecord], now: str,
                queue_mtime: Optional[float] = None) -> str:
    """Render the whole dashboard as one self-contained HTML page.

    Pure: a function of its arguments only. No I/O, no network, no clock -- the
    caller supplies `now` and the queue file's mtime.

    All three views are emitted into the DOM and selected by the `data-layout`
    attribute on <html>, so switching is instant and needs no reload and no
    round trip. The layout is never carried in the URL (decision 12's rejected
    alternative): a pinned home-screen app must keep its stored default.
    """
    records = list(records)
    sections = partition_sections(records)
    toggle = "".join(
        f'<button type="button" class="toggle-btn" data-set-layout="{_e(v)}" '
        f'aria-pressed="false">{_e(v.capitalize())}</button>'
        for v in ("checklist", "panels", "branches"))

    if records:
        groups, ungrouped = group_by_workstream(records)
        content = (_sections_html(sections, now)
                   + _branches_html(groups, ungrouped))
        bar = _bar_html(records)
        telemetry = _telemetry_html(records, sections, now, queue_mtime)
    else:
        content = ('<div class="clear"><span class="clear-i">🔭</span>'
                   'Nothing needs you right now.</div>')
        bar = ""
        telemetry = _telemetry_html(records, sections, now, queue_mtime)

    return (
        '<!doctype html>\n'
        '<html lang="en" data-layout="checklist">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'viewport-fit=cover">\n'
        '<title>Worksweep</title>\n'
        f'<style>{_CSS}</style>\n'
        f'<script>{_HEAD_SCRIPT}</script>\n'
        '</head>\n<body>\n'
        '<div class="wrap">\n'
        '<header class="head">'
        '<div class="brand">🔭 Worksweep</div>'
        f'<div class="toggle" role="group" aria-label="Layout">{toggle}</div>'
        f'{telemetry}</header>\n'
        f'{content}\n'
        '</div>\n'
        f'{bar}\n'
        f'<script>{_BODY_SCRIPT}</script>\n'
        '</body>\n</html>\n')


_ERROR_PAGE = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
               '<title>Worksweep</title></head><body>'
               '<h1>Worksweep</h1><p>The queue could not be rendered. '
               'See worksweep-dashboard.err.</p></body></html>\n')


# =============================================================================
# HTTP surface
# =============================================================================

def _queue_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


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
                   updated: Sequence[QueueRecord]) -> str:
    """Build the Discord confirmation, clamped under the Discord byte cap.

    "Approve all" is the highest-blast-radius action in worksweep, so it must
    ALWAYS leave a channel record. An unclamped message naming 200 items would
    be rejected by Discord and the audit trail would silently vanish for exactly
    the approval that most needed one -- so overflow is summarised instead.
    """
    by_num = {r.number: r for r in updated}
    parts = [f"{n} ({by_num[n].item.executor} {by_num[n].item.repo})".strip()
             for n in numbers if n in by_num]
    prefix, suffix = "✅ Approved: ", " (dashboard)"

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


class DashboardHandler(BaseHTTPRequestHandler):
    """GET `/` renders; POST `/approve` and `/approve-all` persist.

    Nothing in here may raise: the launchd agent is KeepAlive, so an escaping
    exception is a restart loop.
    """

    server_version = "worksweep-dashboard/1"

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

    # -- plumbing --------------------------------------------------------
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page is a local view of private PLA work: never let it be framed
        # or sniffed, and never let a referrer leak an MR title off the tailnet.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, message: str) -> None:
        self._send(code, "text/plain; charset=utf-8",
                   message.encode("utf-8"))

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
        if self._path() != "/":
            self._text(404, "not found")
            return
        qpath = self.server.queue_path
        try:
            body = render_page(load_queue(qpath), self.server.now(),
                               _queue_mtime(qpath)).encode("utf-8",
                                                            errors="replace")
        except Exception as e:                     # never crash the agent
            print(f"worksweep: dashboard render failed: {e}", file=sys.stderr)
            body = _ERROR_PAGE.encode("utf-8")
        self._send(200, "text/html; charset=utf-8", body)

    def do_POST(self) -> None:                                 # noqa: N802
        path = self._path()
        if path not in ("/approve", "/approve-all"):
            self._text(404, "not found")
            return
        if not self._csrf_ok():
            self._text(403, "forbidden")
            return

        raw = self._body_bytes()
        if raw is None:
            self._text(400, "bad request")
            return

        # BOTH routes now carry `{"numbers": [...]}`. For /approve-all those
        # are the proposed+runnable numbers the page actually displayed: the
        # server flips that set INTERSECTED with what is still eligible, so the
        # user approves exactly what they were shown and consented to. An item
        # that landed between render and tap is not swept in silently.
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._text(400, "bad request")
            return
        numbers = _valid_numbers(payload)
        if numbers is None:
            self._text(400, "bad request")
            return

        try:
            self._approve(path, numbers)
        except Exception as e:                     # never crash the agent
            print(f"worksweep: dashboard approval failed: {e}", file=sys.stderr)
            self._text(500, "approval failed")

    def _approve(self, path: str, numbers: Optional[List[int]]) -> None:
        qpath = self.server.queue_path
        now = self.server.now()
        with _WRITE_LOCK:
            # Load FRESH from disk, never from the rendered snapshot: the page
            # may be stale and the runner may have claimed items since.
            # Flipping a cached list would resurrect stale statuses over newer
            # ones on save_queue's whole-file replace. The lock keeps two
            # concurrent taps in THIS process from interleaving their
            # read-modify-write; the cross-process race stays accepted.
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
            self._audit(sorted(newly), updated)
        self._send(200, "application/json",
                   json.dumps({"approved": sorted(newly)}).encode("utf-8"))

    def _audit(self, numbers: Sequence[int],
               updated: Sequence[QueueRecord]) -> None:
        """Never-silent: the channel stays the single history of what was
        approved. A failed post is logged and swallowed -- the approval already
        reached disk and must not be undone."""
        confirm = _audit_message(numbers, updated)
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
                now: Optional[Callable[[], str]] = None) -> _DashboardServer:
    """Build (but do not start) the dashboard server.

    Split out of `serve` so tests can bind port 0 on a thread and shut it down;
    `serve_forever` on the configured port never runs under pytest.
    """
    httpd = _DashboardServer(address, DashboardHandler)
    httpd.queue_path = queue_path
    httpd.post = post
    httpd.webhook = webhook
    httpd.now = now or _now
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
                                    post=post, webhook=webhook)
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
