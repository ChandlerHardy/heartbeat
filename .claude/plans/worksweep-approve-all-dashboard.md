## Issue: (none — freeform work) — Worksweep `✅ all` blanket approval + mini approval dashboard
## Branch: feat/worksweep-approve-all-dashboard
## Repo: heartbeat (worksweep package) — /Users/chandlerhardy/repos/heartbeat
## Status: READY
## Source: decision-log (Round 4 — re-authored after the 2026-08-25 scope, layout-switcher, and branch-grouping amendments)

Gate attestation verified four times: `.planning/decisions/worksweep-approve-all-dashboard.md:1` carries `<!-- GATED: ExitPlanMode approved -->`, and each amendment carries its own scoped marker — Round 2 at `:97`, Round 3 at `:117`, Round 4 at `:128`. Authoring proceeds on the decision-log path (Steps D1–D2). **The whole file (134 lines, decisions 1-14, AC1-AC6 plus AC7-AC10 plus AC11 plus AC12) is the source. Round 2 supersedes Decision 4's read-only scope and the Critique Pass line "no approve-buttons/interactions endpoint"; Round 3 supersedes Decision 11's implicit "breakpoint decides the layout" with a manual switcher whose default the breakpoint sets; Round 4 widens Round 3's two-value switcher to three values.**

## Decision Log (gated)

### Gated in plan mode + amended three times by in-session user directive — `.planning/decisions/worksweep-approve-all-dashboard.md:1-134`, quoted bit-exact

```decision
<!-- GATED: ExitPlanMode approved -->
# Worksweep: `✅ all` blanket approval + mini status dashboard

## Context

Worksweep (seneschal's PLA work loop, `~/repos/heartbeat`, stdlib-only Python, 539 tests) now auto-handles keep-current merges and runs the full pla-pipeline for implement items — so on a well-curated digest, Chandler's remaining act is usually "approve everything." Typing `✅ 1,3,7,9` is friction; `✅ all` should do it. Separately, Discord is a great control/notification surface but a poor *view*: he wants a quick read-only web dashboard on the always-on Mac mini — MR links, work in progress/done, dev-slot picture, light telemetry — complementing Discord, not replacing it.

## Decisions (rationale · rejected alternative)

1. **`✅ all` approves `proposed` items ONLY — never `needs-input`.** A halted implement item is parked on an unanswered question; a blanket approval must not silently release it (numbered `✅ N` still can, as today). *Rejected:* including needs-input (would turn "yes to everything" into "ignore all questions").
2. **Explicit numbers always beat "all".** `✅ 1,3 all good` approves {1,3} — the word "all" is only a blanket when the message names no numbers. *Rejected:* any-mention-of-all wins (a chatty suffix could approve the whole queue).
3. **Dashboard = one new stdlib module `worksweep/dashboard.py` + `python3 -m worksweep dashboard` subcommand**, deployed by the existing rsync pipeline, tested by the existing pytest suite. *Rejected:* Flask/Node app (breaks the repo's stdlib-only discipline, new deploy surface).
4. **Read-only, render-per-request from `~/.worksweep/queue.json`.** No writes, no cache, no state of its own — the queue file is already the system's source of truth and is atomically replaced by writers. *Rejected:* live GitLab/ssh calls per request (slow, rate-limited, and the sweep already snapshots everything needed).
5. **Bind to the Tailscale IP** (resolved via `tailscale ip -4` at startup; fallback `127.0.0.1`), port `8787`, reachable as `http://mac-mini:8787` via MagicDNS. Tailnet-only exposure — PLA MR titles shouldn't sit on the open LAN. *Rejected:* `0.0.0.0` (LAN guests could browse it), auth layer (overkill for read-only on a private tailnet).
6. **launchd agent** `com.chandlerhardy.worksweep-dashboard` (KeepAlive, RunAtLoad) joining the three existing worksweep agents; plist source committed to `etc/mini/`.

## Implementation

### Part 1 — `✅ all` (worksweep/approvals.py, formatter footer)

- `parse_approve_all(text) -> bool`: has the `✅`/`approve` marker (reuse `_HAS_MARKER_RE`), contains `\ball\b` (case-insensitive), and `parse_approval(text)` is empty.
- `apply_approvals(...)`: collect author-gated messages as today; if any is approve-all, extend the approved set with every record number whose `item.status == "proposed"`. Numbered flips keep the existing `_APPROVABLE` (proposed + needs-input) semantics untouched.
- Footer (`formatter._FOOTER`): mention `✅ all`.
- Tests (`test_approvals.py` / `test_apply_approvals.py`): parse positives (`✅ all`, `approve ALL`), negatives (no marker; `✅ 1 all good` → {1} only), apply flips all proposed, leaves needs-input/running/done untouched, author gate holds, confirmation set contains the flipped numbers.

### Part 2 — dashboard (new worksweep/dashboard.py, __main__ wiring, plist)

- `render_page(records, now, queue_mtime) -> str` (pure, fully testable): HTML with inline CSS (dark, compact), auto-refresh `<meta http-equiv="refresh" content="60">`. Sections:
  - **Header/telemetry**: last sweep time (queue mtime), counts per status, done-this-week count (done records with `last_seen` in the past 7 days).
  - **Needs you**: proposed + needs-input items — number, executor badge, linked `!ref`/`#ref` (from `web_url`), title, why, age.
  - **In progress**: running/approved items, with `dev_box` and claimed time where set.
  - **Auto**: keep-current items, one compact row each.
  - **Recently done** (last ~20 by `last_seen` desc): title link, `done_reason`, verdict/`result_sha` short, dev URL when present.
  - **Errors**: error records with `error_summary`.
  - All titles/whys pass through `html.escape` (defense mirrors `formatter._sanitize_title`).
- `serve(cfg_path, port, bind)`: `ThreadingHTTPServer` + `BaseHTTPRequestHandler`, GET `/` only (404 otherwise), reads + parses queue.json per request via the existing `queue.load_queue`.
- `__main__.py`: `dashboard` subcommand (`--port 8787`, `--bind` default `auto` → `tailscale ip -4` → fallback `127.0.0.1`).
- `etc/mini/com.chandlerhardy.worksweep-dashboard.plist`: KeepAlive true, RunAtLoad true, same PATH/HOME env block as the runner plist, logs to `~/heartbeat-reports/worksweep-dashboard.{log,err}`.
- Tests (`test_dashboard.py`): render groups/section membership, done-this-week window math, escaping (a title with `<script>` renders inert), links built from `web_url`, empty queue renders an all-clear page; handler logic kept thin enough that render tests carry the weight.

## Files

- `worksweep/approvals.py`, `worksweep/formatter.py` (footer only)
- `worksweep/dashboard.py` (new), `worksweep/__main__.py` (subcommand)
- `etc/mini/com.chandlerhardy.worksweep-dashboard.plist` (new)
- `worksweep/tests/test_approvals.py`, `test_apply_approvals.py`, `test_dashboard.py` (new)

## Acceptance Criteria (EARS)

- AC1: WHEN the configured user posts a message containing the ✅/approve marker and the word "all" with no numeric tokens, the system SHALL flip every `proposed` queue record to `approved` and SHALL leave every `needs-input`, `running`, `done`, and `error` record unchanged.
- AC2: WHEN such a message also contains explicit numbers (e.g. `✅ 1,3 all good`), the system SHALL approve only the named numbers and SHALL NOT treat the message as a blanket approval.
- AC3 (falsifying): `test_approve_all_flips_every_proposed_item` SHALL pass with the change and SHALL fail when `parse_approve_all` is removed or `apply_approvals` ignores it (reverting the feature makes the asserted `approved` statuses read `proposed`).
- AC4: WHEN a GET request hits `/` on the dashboard, the server SHALL respond 200 with HTML whose sections partition the queue by status group, and any other path SHALL receive 404.
- AC5: WHEN a queue title contains HTML/script characters, the rendered page SHALL contain the escaped form and SHALL NOT contain the raw `<script>` sequence (falsifying: `test_dashboard_escapes_titles` fails if `html.escape` is dropped).
- AC6: WHILE the dashboard serves requests, it SHALL perform no writes to the queue file (read-only contract; asserted by the render/serve API taking records, never saving).

## Decision Coverage

| Decision | Covered by |
|---|---|
| 1. `✅ all` = proposed only | Part 1 `apply_approvals` extension + AC1/AC3 tests |
| 2. Numbers beat "all" | Part 1 `parse_approve_all` (empty-number precondition) + AC2 test |
| 3. stdlib module + subcommand | Part 2 `worksweep/dashboard.py` + `__main__` wiring |
| 4. Read-only render-per-request | Part 2 `render_page`/`serve` design + AC6 |
| 5. Tailscale-IP bind, port 8787 | Part 2 `--bind auto` resolution + plist args |
| 6. launchd agent | `etc/mini/com.chandlerhardy.worksweep-dashboard.plist` |

## Field Provenance

| Field | Value | Source |
|---|---|---|
| **Plan** | this file (gated decision-log + plan in one; handoff mirrors it) | plan-mode session 2026-08-25 |
| **Files** | `worksweep/approvals.py`, `worksweep/formatter.py`, `worksweep/dashboard.py` (new), `worksweep/__main__.py`, `etc/mini/com.chandlerhardy.worksweep-dashboard.plist` (new), tests | read this session: approvals.py:1-94, formatter.py:18-20, __main__.py subcommand table |
| **AC** | AC1–AC6 above | decisions 1–6 |
| **TDD Mode** | Full TDD (feature) | /do work-type table |
| **Owning layer** | approvals.py owns message→status flips (apply_approvals is the ONLY writer of `approved` from Discord input); dashboard is a NEW leaf module with no existing owner — it consumes `queue.load_queue` read-only | approvals.py:61-94 read verbatim this session |
| **Downstream consumers** | intake.py (calls apply_approvals, posts confirmation from the returned `newly` set); runner picks up `approved` items; formatter._FOOTER text is user-facing contract | grep `apply_approvals` in intake.py |
| **Sibling pattern** | `parse_approval` (approvals.py:34-58) for the parser shape; `keepcurrent.py` module layout + `_run`/`_git` edge discipline for the new module; runner plist in `etc/mini/` for the launchd shape | files read this session |
| **Verify** | `python3 -m pytest worksweep/tests/ -q` locally + on mini; `curl http://mac-mini:8787`; live `✅ all` round-trip | Verification section |
| **Plan provenance** | unhardened (claude-solo session — plan-checkpoint hook skip-modes) | mode banner Step 0 |

## Critique Pass

- *Sharpest risk:* `✅ all` misfire on a casual message like "✅ sounds good, that's all" — marker + "all", no numbers → would approve everything. Mitigation considered and accepted: the message must reach intake authored by Chandler in the worksweep channel, where bare ✅ chatter is already approval-intent; the falsifying test pins the semantics, and the digest footer documents `✅ all`. Residual risk acknowledged rather than adding a stricter grammar (`^✅ all$`) that would silently ignore near-misses — but the implementer SHOULD anchor the regex to require "all" adjacent to the marker (`✅ all` / `approve all`, i.e. `marker\s+all\b`), which kills the chatty-sentence false positive while keeping the ergonomic form. This tightening is part of the spec, not optional.
- *Tailscale-IP bind:* if `tailscale` CLI is absent/off at boot, fallback binds 127.0.0.1 and the page is unreachable remotely until restart — acceptable (KeepAlive restarts cheaply; the watchdog already alerts on tailnet loss).
- *Queue-file races:* writers atomically `os.replace`; a read mid-cycle sees the old complete file — no torn reads, no lock needed.
- *What this plan does NOT do:* no approve-buttons/interactions endpoint (separate decision), no historical telemetry store (mtime + queue-derived stats only), no auth.

## Verification

1. `python3 -m pytest worksweep/tests/ -q` green locally (expect ~560).
2. Deploy: push, rsync `worksweep/` to the mini, suite green there, scp + `launchctl bootstrap` the dashboard plist.
3. Live checks: `curl -s http://mac-mini:8787 | head` returns the page from the tailnet; page shows current queue state; send a real `✅ all` in Discord → intake confirmation names every proposed item; verify a needs-input item (if any) stays parked.

## Round 2 amendment — user-directed scope change (2026-08-25, in-chat directive)

<!-- GATED: user directive in session supersedes Decision 4's read-only scope and the Critique Pass "does NOT do approve-buttons" line for the dashboard; Discord `✅ all` (Part 1) is unchanged. -->

Chandler: "on mobile I want the buttons to be big, maybe like a scrolling checklist of the items I want to approve and then a big all button stickied to the bottom. then on desktop maybe like a panel layout with the same, with a little spacing between them so i don't get lost in a sea of text."

New decisions (rationale · rejected alternative):

7. **The dashboard gains approval actions** — `POST /approve` with a JSON body `{"numbers": [..]}` and `POST /approve-all`. Semantics mirror Discord exactly: explicit selections may flip `proposed` AND `needs-input` (a deliberately checked halted item = the human's "go again"); **Approve-all flips `proposed` only**, same as `✅ all`. *Rejected:* approve-all including needs-input (same reasoning as Decision 1).
8. **Write path mimics intake**: load fresh queue → flip → atomic `save_queue` (os.replace). Reuse `approvals`-layer semantics rather than duplicating status rules in the handler (extract or call a shared pure function so dashboard and Discord can never drift). *Rejected:* handler-local status logic (two divergent definitions of "approvable").
9. **Never-silent audit trail**: every dashboard approval also posts a Discord confirmation via the existing webhook ("✅ approved 12, 14 (dashboard)") so the channel remains the single history of who approved what. A failed Discord post must NOT fail the approval (log to stderr). *Rejected:* silent dashboard approvals (breaks the M3 never-silent contract).
10. **CSRF defense for the POSTs**: require a custom request header (e.g. `X-Worksweep: approve`) set by the page's `fetch()` — a cross-origin page cannot attach custom headers without a CORS preflight, and the server answers no preflight; additionally reject when an `Origin` header is present and does not match the Host. No cookies, no tokens to store. *Rejected:* no CSRF guard (any webpage open in a tailnet browser could POST to http://mac-mini:8787), session auth (overkill on a private tailnet).
11. **UI layout**: mobile-first responsive, no JS framework (vanilla fetch + DOM). Mobile: single-column scrolling checklist of actionable items with ≥44px touch targets and a checkbox per item; a bar **stickied to the bottom** (position:sticky/fixed) with two big buttons — "Approve selected (N)" and "Approve all". Desktop (`min-width: 900px` media query): the same content as a **panel/card grid** (sections as cards, generous gap/whitespace) so it reads as panels, not a wall of text. *Rejected:* separate mobile page (one responsive page is less drift).

AC amendments:
- AC7: WHEN `POST /approve` names queue numbers whose status is `proposed` or `needs-input`, the system SHALL persist them as `approved` and respond 200; numbers in other statuses SHALL be ignored (no error).
- AC8: WHEN `POST /approve-all` is received, the system SHALL approve exactly the `proposed` records (falsifying: a seeded `needs-input` record stays `needs-input`; the test fails if approve-all uses the numbered-approval status set).
- AC9: WHEN a POST arrives without the custom header, or with an `Origin` that does not match the Host, the system SHALL respond 403 and persist nothing.
- AC10: WHEN a dashboard approval persists, the system SHALL post a Discord confirmation naming the approved numbers with a "(dashboard)" marker; a Discord failure SHALL NOT roll back or fail the approval.

## Round 3 amendment — layout switcher (2026-08-25, in-chat directive)

<!-- GATED: user directive in session. -->

Chandler: "maybe a layout switcher also to switch between the views. just make it nice, make it easy to use and work from."

12. **Manual layout switcher** — a small toggle in the header switching between **Checklist** (mobile-style single column) and **Panels** (card grid) regardless of screen size; the responsive breakpoint only picks the DEFAULT (narrow → checklist, wide → panels). Choice persists in `localStorage` and survives the 60s auto-refresh. *Rejected:* separate URLs per layout (state in the URL breaks the pinned home-screen app default).
13. **Polish is in scope**: this is a daily driver — spend real care on typography, spacing, hover/active states, and one coherent dark palette; zero frameworks, still one self-contained page.

- AC11: WHEN the layout toggle is used, the page SHALL switch between checklist and panel layouts without reload and SHALL restore the chosen layout after refresh (localStorage).

## Round 4 amendment — branch/MR grouping view (2026-08-25, in-chat directive)

<!-- GATED: user directive in session. -->

Chandler: "add a view that groups the work items by their branch or MR, I think that would help to keep things in line mentally. the branch group should include the issue and MR links within their card/section."

14. **A third view: "Branches"** — added to the layout switcher (Checklist / Panels / Branches). Groups queue records by workstream: the grouping key is `item.branch` when set, else the MR iid derived from `web_url` (or `item.mr_iid` when set — an implemented issue record carries the MR it produced, which is exactly the mental link Chandler wants). Records with no branch/MR affinity land in an "Ungrouped" trailing card. Each branch card shows: the branch name as the card title, the MR link and the issue link(s) belonging to that workstream (from the grouped records' `web_url`s / `mr_iid`), and each record as a compact row (number, executor, status chip, why). Approval checkboxes still work inside this view for actionable rows. *Rejected:* querying GitLab live to resolve branch↔MR↔issue relations (the queue already carries enough affinity via branch + mr_iid + web_url; live calls would break render-per-request cheapness).

- AC12: WHEN two records share a branch (or one's `mr_iid` matches another's MR ref), the Branches view SHALL render them in one card whose header links the MR and the issue; a record with neither branch nor MR affinity SHALL appear under "Ungrouped".
```

## Sibling Patterns

Every sibling below was located by `rg`, Read-verified on disk this session, and quoted bit-exact. The implementer models the new code on these, not on memory.

### S1 — `worksweep/approvals.py:19-58` (parser constants + `parse_approval`) — the pure-parse shape `parse_approve_all` copies, and the module the shared flip function is extracted into

```sibling
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
```

### S2 — `worksweep/approvals.py:61-94` (`apply_approvals`) — the record-flip body that becomes the shared pure function both Discord and the dashboard call (Decision 8)

```sibling
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
```

### S3 — `worksweep/queue.py:47-72` (`load_queue`) — the tolerant reader the dashboard reuses UNCHANGED (GET render and the POST fresh-load)

```sibling
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
```

### S4 — `worksweep/queue.py:75-97` (`save_queue`) — the atomic temp-file + `os.replace` write the dashboard POST path reuses UNCHANGED (Decision 8)

```sibling
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

```

### S5 — `worksweep/__main__.py:212-225` — the confirmation-build + swallow-the-failure pattern the dashboard audit post mirrors (Decision 9)

```sibling
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
```

### S6 — `worksweep/__main__.py:104-116` (`_post_discord`) — the webhook poster injected into `serve`; note it RAISES `RuntimeError`, which Decision 9 requires the dashboard to swallow

```sibling
def _post_discord(webhook: str, content: str) -> None:
    """POST the digest to Discord. Raises RuntimeError on a bad host or network failure."""
    _validate_webhook(webhook)
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "WorksweepBot/1.0"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=15) as resp:
            resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"discord post failed: {e}")
```

### S7 — `worksweep/keepcurrent.py:17-22` (edge-injection discipline) — module-layout sibling for the new `worksweep/dashboard.py`

```sibling
`http_get`); this module never shells out or sshs on its own, matching
implementer.py's discipline. `run_ssh_probe` (fast, ~20s budget) is used only
for the branch-discovery fan-out over every configured box; `run_ssh` (the
longer sync budget, ~300s) is reserved for `sync_to_box`'s drift re-probe +
write against the ONE box that matched (review fix I5).

```

### S8 — `etc/mini/com.chandlerhardy.worksweep-runner.plist:1-30` — the launchd shape (PATH/HOME env block, log paths) the dashboard plist copies

```sibling
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Worksweep runner (mini): executes approved review actions every 10 min. -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.chandlerhardy.worksweep-runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/chandlerhardy/repos/heartbeat/bin/worksweep.sh</string>
        <string>run</string>
    </array>
    <key>StartInterval</key>
    <integer>600</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/chandlerhardy/bin:/Users/chandlerhardy/.pyenv/shims:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/chandlerhardy</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/chandlerhardy/heartbeat-reports/worksweep-runner.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/chandlerhardy/heartbeat-reports/worksweep-runner.err</string>
</dict>
</plist>
```

### S9 — `worksweep/__main__.py:433-440` (`main()` argparse) — the command surface the `dashboard` command extends (a POSITIONAL `choices` list, NOT `add_subparsers`)

```sibling
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("command", nargs="?", choices=["intake", "run"],
                    help="`intake` polls Discord for approval replies; "
                         "`run` executes one approved magi-review item")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, no Discord")
    ap.add_argument("--discord", action="store_true", help="post digest to Discord")
    args = ap.parse_args(argv)
```

### S10 — `worksweep/formatter.py:19` (`_FOOTER`) — the exact current string the footer edit replaces

```sibling
_FOOTER = "-# Reply e.g. `✅ 1,3` to approve (approved items run automatically; keep-current merges need no ✅)."
```

### S11 — `worksweep/tests/test_apply_approvals.py:1-25` — the test-module header (there is no `conftest.py` in this repo) and the record/message factories `test_dashboard.py` must copy

```sibling
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord, DiscordMessage  # noqa: E402
from worksweep.approvals import apply_approvals  # noqa: E402

USER = "chandler-123"
OTHER = "colleague-999"
T0 = "2026-06-23T08:00:00Z"
T1 = "2026-06-23T09:00:00Z"


def _rec(n, status="proposed"):
    return QueueRecord(number=n, first_seen=T0, last_seen=T0,
                       item=WorkItem(schema_version=1, id=f"id{n}", repo="pb-www",
                                     kind="mr", executor="magi-review", risk="low",
                                     why="w", web_url="u", sha="abc", status=status))


def _msg(author_id, content, mid="1"):
    return DiscordMessage(id=mid, author_id=author_id, content=content, timestamp=T1)


def _by_num(records):
    return {r.number: r for r in records}

```

### S12 — `worksweep/keepcurrent.py:92-99` (`iid_of`) — the existing `web_url` → MR-iid regex the Branches view reuses, and the reason it needs a tolerant wrapper (it RAISES)

```sibling
def iid_of(item: WorkItem) -> int:
    """MR iid from the item's web_url (`.../merge_requests/<iid>`). Raises
    rather than guessing -- a wrong iid would merge master into someone
    else's branch."""
    m = re.search(r"/merge_requests/(\d+)", item.web_url or "")
    if not m:
        raise RunnerError(f"cannot find MR iid in web_url: {item.web_url!r}")
    return int(m.group(1))
```

### S13 — `worksweep/models.py:74-79` — the three persisted affinity fields the Branches grouping key is built from (`branch`, `mr_iid`) plus `web_url`/`repo` scoping

```sibling
    error_summary: str = ""   # short failure text (status=error)
    title: str = ""           # mr.title / issue.title -- "" for todo items
    dev_box: str = ""         # name of the dev box claimed by an `implement` executor
    mr_iid: int = 0           # Draft MR iid opened by the `implement` executor
    branch: str = ""          # M4 Task H: mr.source_branch, set by assess_stale --
                              # the `keep-current` executor's checkout target
```

## Source vs Repository Mismatch

The gated decisions (1-11) are internally consistent once the amendment's explicit supersession is applied, and are implemented as written. Three *incidental factual claims* in the source's prose diverge from live repository truth read this session, and one supersession must be applied deliberately rather than silently. **Live repository truth wins on facts** (`planning-philosophy` Core Rule 2); the decisions are untouched. No HALT: none of these is an unresolved decision-vs-decision contradiction.

| # | Source claim | Repository truth / resolving source line (verified this session) | Resolution the plan implements |
|---|---|---|---|
| 1 | Field Provenance row: "Downstream consumers: **intake.py** (calls apply_approvals, posts confirmation from the returned `newly` set)" | **`worksweep/intake.py` does not exist.** Intake is `worksweep/__main__.py::_run_intake` (`__main__.py:180`), which imports `apply_approvals` at `__main__.py:39` and calls it at `__main__.py:201`; the confirmation is built at `__main__.py:212-225`. Exhaustive `rg -n 'apply_approvals'` returned zero hits in any `intake.py`. | Every Files / consumer / AC row names `worksweep/__main__.py`. The behavioral claim (a `newly`-driven confirmation post) is correct — only the file name was wrong. |
| 2 | §Implementation Part 2, "Recently done" bullet: "dev URL when present" | **`WorkItem` has no `dev_url` field** (`models.py:55-79`: the persisted fields are `schema_version, id, repo, kind, executor, risk, why, web_url, sha, status, claimed_at, done_reason, result_sha, report_path, error_summary, title, dev_box, mr_iid, branch`). `dev_url` exists only on the transient `KeepCurrentResult` (`keepcurrent.py:88`) and is never written into `queue.json`. | "Recently done" renders `dev_box` where set and renders **no** dev URL. The implementer does NOT add a field to `WorkItem` and does NOT synthesize a hostname. Persisting `dev_url` is listed under *Decisions deferred to orchestrator*. |
| 3 | §Implementation Part 2, "Recently done" bullet: "**verdict**/`result_sha` short" | **There is no `verdict` field** on `WorkItem`. The persisted done-state facts are `done_reason` (`models.py:71`) and `result_sha` (`models.py:72`). | "Recently done" renders `done_reason` plus the first 8 chars of `result_sha`. No `verdict` is invented. |
| 4 | Decision 4: "**Read-only**, render-per-request … No writes"; §Critique Pass: "What this plan does NOT do: no approve-buttons/interactions endpoint (separate decision)" | **Superseded in-source** by the Round 2 amendment's own gate marker (`:96`) and Decisions 7-11, which add `POST /approve` and `POST /approve-all`. This is an explicit, marked supersession, not a contradiction — Round 2 is the later gated statement and names exactly what it overrides. | Decision 4's read-only guarantee is narrowed to the **GET path** (AC #14 below asserts GET performs zero writes). The write path exists only under the two POST routes and is guarded by AC #18-#25. The plan states the narrowing explicitly so the implementer cannot read the two decisions as a live conflict. |
| 5 | Decision 11: "Mobile: single-column scrolling checklist … Desktop (`min-width: 900px` media query): the same content as a **panel/card grid**" — read in isolation, the breakpoint *decides* the layout | **Superseded in-source** by the Round 3 amendment (own gate marker at `:117`), decision 12: the toggle switches layouts "regardless of screen size" and "the responsive breakpoint only picks the DEFAULT". Another explicit, marked supersession. | The breakpoint is implemented as the *initial* value only (AC #30); an explicit stored choice always wins (AC #29, #31). The plan states the narrowing so the implementer cannot ship a pure media-query layout and believe decision 11 is satisfied. |
| 6 | Decision 14: "the MR iid derived from `web_url` (or `item.mr_iid` when set)" — implies a ready-made derivation | **A derivation already exists and RAISES.** `keepcurrent.iid_of` (`keepcurrent.py:92-99`) is exactly `re.search(r"/merge_requests/(\d+)", item.web_url or "")` but raises `RunnerError` when the URL has no MR segment — deliberately, so a keep-current merge never guesses. Every issue record and every todo record has a non-MR `web_url`, so on a real queue that raise would fire constantly under a `KeepAlive` agent. Separately, MR iids are per-project: `WorkItem.repo` (`models.py:58`) must scope the key or `pb-www!4821` and `pb-api!4821` would merge into one card. | The dashboard uses its OWN tolerant helper returning `0` on no match (the same regex, no raise), and the MR grouping token is repo-scoped (`(repo, iid)`). `keepcurrent.iid_of` is NOT modified — its raise is load-bearing for the merge path (AC #36, #37, #40). |

**Decisions deferred to orchestrator:**
1. Amend the decision-log Field Provenance to say `__main__.py::_run_intake` instead of `intake.py`.
2. Decide whether a follow-up should persist `dev_url` onto `WorkItem` (`test_models_v2.py:72-83` shows a queue written without a later-added key still loads) so the dashboard can link dev boxes. Out of scope here.
3. Confirm the dashboard's `href` hardening (escaping `web_url` with `html.escape(..., quote=True)` before attribute interpolation) — one step beyond the source line, which names only titles/whys. Specified in Tricky Parts, not as a locked AC, because it has no source span.
4. Confirm whether `bin/worksweep.sh:2-6`'s usage comment block should gain a `dashboard` line (docs-only; not required by any gated decision).
5. Confirm the audit-post wiring shape: Decision 9 requires a Discord post from the dashboard, but `_post_discord` lives in `__main__.py` (`:104`) which imports `dashboard` — so `dashboard.serve` must take an injected `post` callable (per S7's edge discipline) rather than importing `__main__`. The plan specifies injection; flagging it because the source does not name the mechanism.
8. Confirm the bare-`mr_iid` link case. Decision 14 asks each branch card to link "the MR link and the issue link(s)", but when a workstream's only MR evidence is an `item.mr_iid` integer (no record in the group carries a `/merge_requests/` `web_url`), there is no URL to link without CONSTRUCTING one from `repo` — which the plan declines to invent. AC #38 renders that case as unlinked text `!<iid>`. The orchestrator should decide whether a follow-up may construct MR URLs from `collectors._project(repo)`.
7. Confirm the aesthetic bar for decision 13. "Real care on typography, spacing, hover/active states" is a review obligation, not a machine-checkable one — AC #33 asserts only the falsifiable half (a single palette defined as CSS custom properties, `:hover`/`:active` rules present on every interactive control, zero external asset references). The orchestrator should decide whether a visual review gate is wanted before merge.
6. Confirm the queue-write concurrency stance: the runner and intake also write `~/.worksweep/queue.json`, and Decision 8's "load fresh → flip → atomic save" is a read-modify-write with no lock. A dashboard approval landing in the same instant as a sweep write can lose the sweep's delta (last-writer-wins on whole-file replace). See Tricky Parts; the source's original Critique Pass only analysed *read* races, not this new *write* race.

## Field Provenance

Every row traces to a numbered gated decision (1-11), a quoted Implementation / Critique / AC-amendment line inside the `## Decision Log (gated)` block, or an explicitly non-source-derived origin sanctioned by the agent contract (`Verify`, `TDD Mode`, `Branch`).

| Plan field | Derived from (quoted source reference) | Notes |
|---|---|---|
| **Plan** | this file; the gated decision-log is the source, the handoff mirrors this plan | `.planning/decisions/…:1-113` quoted verbatim above, both gate markers present |
| **Files** entry 1: `worksweep/approvals.py` | D-log §Files line 1, §Implementation Part 1 bullets 1-2, **decision 8** ("extract or call a shared pure function so dashboard and Discord can never drift") | new `parse_approve_all`, new shared `flip`/`approve_numbers`/`approve_all`, `apply_approvals` delegates |
| **Files** entry 2: `worksweep/formatter.py` | D-log §Files line 1 ("formatter.py (footer only)") + §Implementation Part 1 bullet 3 | `_FOOTER` mentions `✅ all`; scope is the one string at `formatter.py:19` |
| **Files** entry 3: `worksweep/dashboard.py` (NEW) | D-log decision 3, §Implementation Part 2 bullets 1-2, **decisions 7, 9, 10, 11** | render + GET + two POST routes + CSRF guard + injected audit poster |
| **Files** entry 4: `worksweep/__main__.py` | D-log §Files line 2, §Implementation Part 2 bullet 3, **decision 9** (webhook wiring) | `dashboard` command, `--port`/`--bind`, injects `_post_discord` + `cfg.discord_webhook`. No change to `_run_intake`. |
| **Files** entry 5: `etc/mini/com.chandlerhardy.worksweep-dashboard.plist` (NEW) | D-log decision 6 + §Implementation Part 2 bullet 4 | KeepAlive/RunAtLoad, runner-plist env block |
| **Files** entries 6-8: the three test modules | D-log §Files line 4 + §Implementation Part 1/2 "Tests" bullets + the AC7-AC10 amendments | two extended, one new |
| **AC #1** blanket flips every `proposed` | D-log decision 1 + §AC1 | "approves `proposed` items ONLY" |
| **AC #2** blanket leaves `needs-input` parked | D-log decision 1 ("never `needs-input`") + §AC1 | the load-bearing negative |
| **AC #3** explicit numbers beat "all" | D-log decision 2 + §AC2 | precondition: `parse_approval(text)` is empty |
| **AC #4** marker-adjacency regex | D-log §Critique Pass ("`marker\s+all\b` … part of the spec, not optional") | source explicitly overrides its own §Implementation `\ball\b` wording |
| **AC #5** author gate holds for blanket | D-log §Implementation Part 1 bullet 2 + Part 1 Tests ("author gate holds") | |
| **AC #6** confirmation names blanket numbers | D-log §Implementation Part 1 Tests ("confirmation set contains the flipped numbers") | drives `__main__.py:212-225` |
| **AC #7** footer advertises `✅ all` | D-log §Implementation Part 1 bullet 3 | |
| **AC #8 (falsifying)** approve-all mutation | D-log §AC3 (verbatim falsifying criterion) | mutation → asserted `approved` reads `proposed` |
| **AC #9** GET `/` 200, unknown path 404 | D-log §AC4 | amended: "any other path 404" now excludes the two POST routes |
| **AC #10** section partition of the queue | D-log §AC4 + §Implementation Part 2 bullet 1 sub-bullets | five sections + telemetry header |
| **AC #11** done-this-week window | D-log §Implementation Part 2 "Header/telemetry" sub-bullet | `done` records with `last_seen` in the past 7 days |
| **AC #12** empty queue → all-clear page | D-log §Implementation Part 2 Tests ("empty queue renders an all-clear page") | |
| **AC #13 (falsifying)** `html.escape` mutation | D-log §AC5 (verbatim falsifying criterion) | raw `<script>` must not appear |
| **AC #14** GET performs zero writes | D-log decision 4 ("Read-only, render-per-request … No writes") **as narrowed by the Round 2 supersession** | the read-only guarantee survives on the GET path; see mismatch row 4 |
| **AC #15** bind resolution + port | D-log decision 5 + §Implementation Part 2 bullet 3 | `auto` → `tailscale ip -4` → `127.0.0.1`; port 8787 |
| **AC #16** launchd plist | D-log decision 6 + §Implementation Part 2 bullet 4 | KeepAlive true, RunAtLoad true, log paths |
| **AC #17** malformed queue still serves 200 | D-log decision 4 ("via the existing `queue.load_queue`") + live repo truth `queue.py:47-72` | **added by the Critique Pass** (failure-path dimension); derived from a quoted decision line plus the tolerant reader that already exists |
| **AC #18** `POST /approve` flips `proposed` + `needs-input`, ignores others, 200 | D-log **decision 7** + **§AC7** | explicit selections deliberately include `needs-input` — the mirror of numbered `✅ N` |
| **AC #19 (falsifying)** `POST /approve-all` is `proposed`-only | D-log **decision 7** + **§AC8** (verbatim falsifying criterion) | "the test fails if approve-all uses the numbered-approval status set" |
| **AC #20** single shared status definition | D-log **decision 8** ("Reuse `approvals`-layer semantics rather than duplicating status rules in the handler") | asserted structurally: the handler holds no status tuple |
| **AC #21** fresh-load → flip → atomic save | D-log **decision 8** ("load fresh queue → flip → atomic `save_queue` (os.replace)") | the handler must not serve from a cached record list |
| **AC #22 (falsifying)** CSRF custom header | D-log **decision 10** + **§AC9** | missing `X-Worksweep` header ⇒ 403 and nothing persisted |
| **AC #23** CSRF Origin-vs-Host | D-log **decision 10** ("reject when an `Origin` header is present and does not match the Host") + **§AC9** | absent Origin is allowed; mismatched Origin is 403 |
| **AC #24** never-silent audit post | D-log **decision 9** + **§AC10** | confirmation names the numbers and carries the `(dashboard)` marker |
| **AC #25** Discord failure does not fail the approval | D-log **decision 9** ("A failed Discord post must NOT fail the approval (log to stderr)") + **§AC10** ("SHALL NOT roll back or fail") | the queue write is already durable when the post is attempted |
| **AC #26** mobile-first responsive UI | D-log **decision 11** + the quoted user directive at `:98` | checkbox per actionable item, ≥44px targets, sticky bottom bar, two big buttons |
| **AC #27** desktop panel grid at ≥900px | D-log **decision 11** ("Desktop (`min-width: 900px` media query) … panel/card grid") | same content, card layout, generous gap |
| **AC #28** vanilla JS, no framework | D-log **decision 11** ("no JS framework (vanilla fetch + DOM)") + decision 3 (stdlib-only discipline) | no CDN link, no bundler, no `<script src=…>` to a third party |
| **AC #29** manual layout switcher | D-log **decision 12** + **§AC11** + the quoted user directive at `:119` | toggle in the header; works at any screen size |
| **AC #30** breakpoint sets the DEFAULT only | D-log **decision 12** ("the responsive breakpoint only picks the DEFAULT (narrow → checklist, wide → panels)") | supersedes the Round 2 reading in which the breakpoint decided the layout outright |
| **AC #31** choice persists and survives auto-refresh | D-log **decision 12** ("persists in `localStorage` and survives the 60s auto-refresh") + **§AC11** ("SHALL restore the chosen layout after refresh") | the 60s `<meta http-equiv="refresh">` is a full reload; the restore must run before first paint |
| **AC #32** no layout state in the URL | D-log **decision 12** *Rejected* clause ("separate URLs per layout (state in the URL breaks the pinned home-screen app default)") | the rejected alternative is itself a locked constraint |
| **AC #33** polish bar: one dark palette, hover/active states, no frameworks | D-log **decision 13** ("real care on typography, spacing, hover/active states, and one coherent dark palette; zero frameworks, still one self-contained page") | the machine-checkable half is asserted; the aesthetic half is a review obligation, named as such |
| **AC #34** malformed POST body → 400 | D-log **decision 7** (the declared `{"numbers": [..]}` body contract) + **§AC7** ("numbers in other statuses SHALL be ignored (no error)", which covers unmatched numbers but not a malformed envelope) | **added by the Critique Pass** (failure-path dimension); derived from a quoted decision line, no invention |
| **AC #35** Branches is a third switcher value | D-log **decision 14** ("added to the layout switcher (Checklist / Panels / Branches)") + the quoted user directive at `:130` | widens Round 3's two-value toggle; AC #31 persistence and AC #32 no-URL-state apply to it unchanged |
| **AC #36** shared branch or matching MR ref ⇒ one card | D-log **decision 14** (grouping key) + **§AC12** ("WHEN two records share a branch (or one's `mr_iid` matches another's MR ref) … SHALL render them in one card") | AC12 makes this an equivalence over TWO signals, so the grouping is connected components, not a single-key bucket |
| **AC #37** no affinity ⇒ trailing "Ungrouped" card | D-log **decision 14** ("Records with no branch/MR affinity land in an \"Ungrouped\" trailing card") + **§AC12** second clause | |
| **AC #38** card header: branch title + MR link + issue link(s); compact rows | D-log **decision 14** ("the branch name as the card title, the MR link and the issue link(s) … each record as a compact row (number, executor, status chip, why)") | the bare-`mr_iid`-with-no-URL case renders unlinked text; see deferred item 8 |
| **AC #39** checkboxes still work inside Branches | D-log **decision 14** ("Approval checkboxes still work inside this view for actionable rows") | reuses AC #18/#19 routes unchanged |
| **AC #40** zero live GitLab calls | D-log **decision 14** *Rejected* clause ("querying GitLab live to resolve branch↔MR↔issue relations … live calls would break render-per-request cheapness") | the rejected alternative is itself a locked constraint; `render_page` stays pure |
| **TDD Mode** `Full TDD` | D-log §Field Provenance row "TDD Mode: Full TDD (feature)" — and independently the `pla-tdd` work-type matrix (new feature, new module, new write path) | sanctioned non-source-derived field; the source agrees |
| **Owning layer** `service` + `display` + `hook`, `data` reused-unchanged | D-log §Field Provenance row "Owning layer" ("apply_approvals is the ONLY writer of `approved` from Discord input"; "dashboard is a NEW leaf module") + **decision 8** (which makes the dashboard a SECOND writer through the same service layer) — confirmed on disk at `approvals.py:61-94`, `queue.py:47,75` | product Canon N/A (standalone stdlib Python app); layers named with file:line in the diagnostic block |
| **Downstream consumers** | D-log §Field Provenance row "Downstream consumers" — file name corrected per mismatch row 1, extended with the write-path and webhook consumers the amendment adds | enumerated with file:line in the diagnostic block |
| **Sibling pattern** S1-S11 | D-log §Field Provenance row "Sibling pattern" (`parse_approval`, `keepcurrent.py` layout/edge discipline, runner plist) plus decisions 8-9 (which name `save_queue` and the webhook) — each `rg`-located and Read-verified, quoted into `## Sibling Patterns` | S2/S4/S5/S6 added by the Round 2 discovery pass as the concrete templates the write path and audit post require |
| **Verify** | detected stack conventions (`python3 -m pytest worksweep/tests/ -q`, 539 green measured this session) + `testing-philosophy` § Falsification Shape | sanctioned non-source-derived field; the source's §Verification section corroborates |
| **Plan provenance** `unhardened` | D-log §Field Provenance row "Plan provenance: unhardened (claude-solo session — plan-checkpoint hook skip-modes)" | no cross-model harden checkpoint ran |
| **Branch** `feat/worksweep-approve-all-dashboard` | orchestrator dispatch prompt; confirmed checked out (`git branch --show-current`) | sanctioned non-source-derived field |
| **decision-verify** Decision Log (`.planning/decisions/worksweep-approve-all-dashboard.md:1-134`, 134 lines / 14 decision rows / 12 source AC rows) | diff against source-range: clean (0 bytes) | full-range diff re-verified after the Round 4 amendment; all four gate markers (`:1`, `:97`, `:117`, `:128`) present |
| **quote-verify** siblings S1-S11 | diff against each cited source-range: clean (0 bytes) | eleven full-range diffs against the on-disk files |
| **diff-against-current** `parse_approval` / `_HAS_MARKER_RE` / `_APPROVABLE` (`approvals.py:19-58` vs D-log Part 1) | 3 deltas: (1) NEW `_APPROVE_ALL_RE` (AC #4); (2) NEW `parse_approve_all` (AC #3, #4); (3) `_HAS_MARKER_RE` and `_APPROVABLE` unchanged — the source pins `_APPROVABLE` as "untouched" | zero silent drops; the "untouched" delta is itself asserted by AC #2 and AC #18 |
| **diff-against-current** `apply_approvals` (`approvals.py:61-94` vs D-log Part 1 + decision 8) | 3 deltas: (1) the record-flip loop (`approvals.py:84-94`) is EXTRACTED into a shared pure `flip(records, numbers, now, statuses)`; (2) `apply_approvals` gains the blanket branch and delegates to `approve_numbers` / `approve_all` (AC #1, #2, #20); (3) its `(records, messages, user_id, now) -> (List[QueueRecord], Set[int])` signature and return contract are UNCHANGED (AC #6 and all 20 call sites depend on it) | the extraction is the highest-risk edit in the plan — it touches the ONLY Discord→status writer; the 539-test floor plus behaviors 3-7 of the Behavioral Contract are the guard |
| **diff-against-current** `_FOOTER` (`formatter.py:19` vs D-log Part 1 bullet 3) | 1 delta: the string body gains a `✅ all` mention (AC #7). Every consumer references `_FOOTER` by symbol, never by literal, so no downstream text assertion breaks | verified: `rg -n '_FOOTER'` → `__main__.py:42`, `formatter.py:229,234`, `test_main_devslots.py:157,194` (all by-reference) |
| **diff-against-current** `main()` argparse (`__main__.py:433-440` vs D-log Part 2 bullet 3) | 2 deltas: (1) positional `choices=["intake","run"]` gains `"dashboard"` (AC #15); (2) NEW `--port` (default 8787) and `--bind` (default `auto`) optional args (AC #15). **`main()` uses a POSITIONAL `command`, not `add_subparsers`** — the source's word "subcommand" must not be read as an argparse subparser | the single highest silent-drop trap in the CLI edit; repeated in Tricky Parts |
| **diff-against-current** `save_queue` / `_post_discord` (`queue.py:75-97`, `__main__.py:104-116` vs decisions 8-9) | 0 deltas — both are reused UNCHANGED. `save_queue` already does temp-file + `os.replace` exactly as decision 8 requires; `_post_discord` already validates the webhook host and raises `RuntimeError`, which AC #25 requires the dashboard to swallow | no edit to either; the delta is entirely on the calling side (injection, AC #24/#25) |
| **diff-against-current** `iid_of` / `WorkItem` affinity fields (`keepcurrent.py:92-99`, `models.py:74-79` vs decision 14) | 2 deltas: (1) the dashboard adds its OWN tolerant `web_url` → iid helper (returns `0`, never raises) rather than calling `iid_of` — `keepcurrent.iid_of` is UNCHANGED because its raise guards the merge path (AC #36, #40); (2) the grouping token is repo-scoped using the existing `WorkItem.repo` field — NO new field is added to `WorkItem` | zero silent drops; the "do not modify `iid_of`" delta is asserted structurally by AC #40 |
| **impact-trace** `apply_approvals` callers | 20 call sites, exhaustive `rg -n 'apply_approvals'` sweep, each Read-verified. **Production (in scope — AC #6, #20):** `__main__.py:39` (import), `__main__.py:201` (call). **Tests (in scope — must stay green under the decision-8 extraction):** `test_apply_approvals.py:4,29,38,44,51,60,72,79`; `test_needs_input_lifecycle.py:8,69,76,86`; `test_loop_closure.py:17,85`. **Comment-only (no code impact):** `models.py:68`, `curator.py:214`, `queue.py:27`, `approvals.py:8` | signature unchanged ⇒ no caller edit required; the risk is behavioral regression from the extraction, covered by the test-surface row |
| **impact-trace** `parse_approval` callers | 2 non-test sites: `approvals.py:82` (the one production caller, inside `apply_approvals`) and `approvals.py:3` (docstring). `parse_approve_all` becomes its second production caller (it calls `parse_approval` to enforce the numbers-beat-all precondition, AC #3) | exhaustive `rg -n 'parse_approval'`, Read-verified |
| **impact-trace** `save_queue` / `_post_discord` callers (the amendment's new seam) | `save_queue`: `__main__.py:203` (intake), `__main__.py:459-461` (runner deps), `queue.py:75` (def) — the dashboard POST becomes a **third writer** of `~/.worksweep/queue.json` (deferred item 6 raises the unlocked read-modify-write). `_post_discord`: `__main__.py:221` (intake confirm), `:462`, `:485` (runner deps), plus 5 monkeypatch sites in `test_main.py:32,46,86,145` and `test_runner_execute.py:290` — the dashboard becomes a **fourth** poster, via injection rather than import | exhaustive `rg`, each Read-verified; the monkeypatch sites show the established test seam for the poster |
| **impact-trace** `_FOOTER` / `load_queue` consumers | `_FOOTER`: `__main__.py:42`, `formatter.py:229,234`, `test_main_devslots.py:157,194`. `load_queue`: `__main__.py` intake + runner deps — the dashboard becomes a NEW consumer on both the GET render and the POST fresh-load, changing nothing about it (AC #14, #21) | Read-verified |
| **sibling-pattern** D-log §Field Provenance "Sibling pattern" row + decisions 8-9 | S1-S11 quoted into `## Sibling Patterns`, `rg`-located + Read-verified, full-range-diff clean | the implementer reads S2/S4/S5/S7 as concrete templates, not name-drops |
| **test-surface** approval behavior | 5 modules, 29 approval assertions Read-verified: `test_approvals.py` (16 `parse_approval` tests, lines 6-72), `test_apply_approvals.py` (7 tests, lines 27-80), `test_needs_input_lifecycle.py` (lines 69,76,86), `test_loop_closure.py` (line 85), `test_intake.py` (6 tests, lines 33-137). **Exhaustive `rg -i` confirms no existing test message string contains the word "all"** ⇒ the blanket parser cannot regress any of them, and the decision-8 extraction is covered by all 29 | AC #1-#8, #20 add new tests; existing tests are the regression floor (539 green measured) |
| **test-surface** queue write path | `test_queue.py`, `test_queue_lifecycle.py`, `test_queue_reconcile.py`, `test_models_v2.py:64-83` already pin `save_queue`/`load_queue` round-tripping (including a queue missing a later-added key). The dashboard adds NO new assertions to them and must not change their subject | AC #21 asserts the dashboard's *use* of `save_queue`, not `save_queue` itself |
| **test-surface** Discord poster seam | `test_main.py:32,46,86,145` and `test_runner_execute.py:290` monkeypatch `_post_discord` — the established pattern for asserting posted content without network. `test_dashboard.py` uses an injected fake instead (the dashboard never imports `_post_discord`) | AC #24, #25 |
| **impact-trace** `branch` / `mr_iid` writers (the Round 4 affinity source) | `branch` is written at `assessor.py:234` (`mr.source_branch`, stale/keep-current items), `__main__.py:428` and `implementer.py:453,531` (`implementer.branch_name(iid, title)`, implement items); `mr_iid` at `implementer.py:142` (`mr.iid if mr else 0`), `runner.py:495` (`result.mr_iid`), and preserved across sweeps at `queue.py:167`. `iid_of` has TWO callers (`keepcurrent.py:114`, `__main__.py:418`) and `runner._iid_of` (`runner.py:207-211`) is an independent SECOND copy of the same regex, also raising — so the repo already carries two raising derivations and the dashboard's tolerant helper is a deliberate third, non-raising one | exhaustive `rg -n 'branch=|mr_iid='` + `rg -n 'iid_of'`, each Read-verified. The dashboard READS these fields and writes none of them, so no writer is impacted |
| **test-surface** branch/MR affinity | `test_models_v2.py:59-83` pins `dev_box`/absent-key tolerance on the same dataclass; `test_keepcurrent.py` pins `iid_of`. The Branches view adds NO assertions to either and must not change their subject — its grouping tests are new and live in `test_dashboard.py` | AC #35-#40 |
| **test-surface** dashboard | **Zero existing tests** — `worksweep/dashboard.py` is a new leaf module with no call sites. `test_dashboard.py` is entirely new | AC #9-#28; no migration impact |

## Decision Coverage

Every gated decision row (1-13) and every source AC (AC1-AC11) maps to at least one implementing AC and a concrete falsifiable test. No row is deferred.

| Gated decision | Implementing task (AC #) | Verification |
|---|---|---|
| Decision 1 — `✅ all` approves `proposed` items ONLY, never `needs-input` | AC #1, #2, #8 | `test_approve_all_flips_every_proposed_item` asserts all three `proposed` records read `approved`; `test_approve_all_leaves_needs_input_parked` asserts a `needs-input` record still reads `needs-input` and is absent from `newly`. Mutation: swap the blanket filter `("proposed",)` → `_APPROVABLE` ⇒ the second test fails. |
| Decision 2 — explicit numbers always beat "all" | AC #3 | `test_explicit_numbers_beat_all`: `apply_approvals` on `✅ 1,3 all good` over records 1-3 returns `newly == {1, 3}` and record 2 still reads `proposed`. Mutation: delete the `not parse_approval(text)` precondition ⇒ returns `{1,2,3}` and the test fails. |
| Decision 2 (Critique-Pass tightening) — "all" adjacent to the marker (`marker\s+all\b`) | AC #4 | `test_chatty_all_is_not_blanket`: `parse_approve_all("✅ sounds good, that's all")` is `False`; `parse_approve_all("✅ all")` and `("approve ALL")` are `True`. Mutation: relax to a bare `\ball\b` search ⇒ the chatty case flips to `True` and the test fails. |
| Decision 3 — stdlib module `worksweep/dashboard.py` + `python3 -m worksweep dashboard` | AC #15, #28 | `test_dashboard_command_is_accepted`: `main(["dashboard","--port","9999"])` reaches the dashboard branch with an injected serve fake (no socket bound) instead of exiting 2 on an argparse choice error. `test_dashboard_imports_are_stdlib_only` asserts every top-level import of `worksweep/dashboard.py` resolves inside the stdlib or the `worksweep` package. |
| Decision 4 — read-only render-per-request (as narrowed by Round 2 to the GET path) | AC #14, #21 | `test_get_never_writes_queue`: a render plus one handled GET against a `tmp_path` queue leaves the file's bytes and mtime unchanged. Mutation: call `save_queue` from the GET handler ⇒ the mtime assertion fails. |
| Decision 4 (render contract) — five sections + telemetry header | AC #10, #11, #12 | `test_render_partitions_by_status_group` builds one record per status and asserts each id lands in exactly one section; `test_done_this_week_window` asserts `last_seen` 6 days old counts and 8 days old does not; `test_empty_queue_renders_all_clear` asserts the all-clear page renders with no section tables. |
| Decision 4 (escaping) — every title/why through `html.escape` | AC #13 | `test_dashboard_escapes_titles`: a record titled `<script>alert(1)</script>` renders `&lt;script&gt;` and the page contains no raw `<script>alert` substring. Mutation: drop the `html.escape` call ⇒ the test fails. |
| Decision 4 (reader reuse) — "via the existing `queue.load_queue`" | AC #17 | `test_dashboard_survives_malformed_queue`: a `tmp_path` queue containing `{"nope": 1}` (and separately a nonexistent path) renders the all-clear page and the handler returns 200 without raising. Mutation: replace `load_queue` with a bare `json.load` ⇒ the test raises and fails. |
| Decision 5 — bind Tailscale IP (fallback `127.0.0.1`), port 8787 | AC #15 | `test_bind_auto_uses_tailscale_ip` (injected runner returns `100.x.y.z\n` ⇒ resolver returns `100.x.y.z`); `test_bind_auto_falls_back_to_loopback` (injected runner raises `FileNotFoundError`, returns non-zero, and returns empty stdout — three cases ⇒ `127.0.0.1`); `test_default_port_is_8787` asserts the argparse default. |
| Decision 6 — launchd agent `com.chandlerhardy.worksweep-dashboard` | AC #16 | `test_dashboard_plist_contract` parses the committed plist with `plistlib` and asserts `Label`, `KeepAlive is True`, `RunAtLoad is True`, `ProgramArguments` ending in `dashboard`, `EnvironmentVariables["PATH"]`/`["HOME"]` equal to the runner plist's, and both log paths. |
| **Decision 7** — `POST /approve` (proposed + needs-input) and `POST /approve-all` (proposed only) | AC #18, #19 | `test_post_approve_flips_selected_including_needs_input` seeds one `proposed`, one `needs-input`, one `running`, POSTs `{"numbers":[1,2,3]}`, asserts records 1 and 2 read `approved`, record 3 unchanged, response 200. `test_post_approve_all_is_proposed_only` seeds the same and asserts the `needs-input` record still reads `needs-input`. |
| **Decision 7 / §AC8 (falsifying)** — approve-all must not use the numbered status set | AC #19 | Mutation: point the approve-all route at `approve_numbers` (the `_APPROVABLE` path) ⇒ `test_post_approve_all_is_proposed_only` fails because the seeded `needs-input` record reads `approved`. This is the source's own named falsification. |
| **Decision 8** — shared pure function; no status rules in the handler | AC #20 | `test_status_rules_live_only_in_approvals`: `worksweep/dashboard.py`'s source contains no `"proposed"` / `"needs-input"` / `"approved"` status tuple and no `dataclasses.replace(..., status=...)`; the routes call `approvals.approve_numbers` / `approvals.approve_all`. Mutation: inline a status tuple in the handler ⇒ the test fails. Paired: `test_discord_and_dashboard_agree` asserts `approve_numbers` produces byte-identical records to `apply_approvals` on an equivalent `✅ 1,2` message. |
| **Decision 8** — load fresh → flip → atomic `save_queue` | AC #21 | `test_post_reloads_queue_before_flipping`: mutate the queue file on disk between the GET render and the POST; the POST result reflects the ON-DISK state, not the earlier render. `test_post_persists_via_save_queue` asserts the file round-trips through `load_queue` with the new statuses. Mutation: flip an in-memory cached list instead of reloading ⇒ the first test fails. |
| **Decision 9** — never-silent audit post with a `(dashboard)` marker | AC #24 | `test_post_approve_posts_discord_confirmation`: an injected `post` fake captures one call whose content names the approved numbers and contains `(dashboard)`. Mutation: remove the post call ⇒ the fake records zero calls and the test fails. |
| **Decision 9** — a failed Discord post must NOT fail the approval | AC #25 | `test_discord_failure_does_not_fail_approval`: the injected `post` raises `RuntimeError`; the response is still 200 AND the queue file on disk shows the flipped statuses. Mutation: let the exception propagate ⇒ the response becomes 500 and the test fails. |
| **Decision 10** — CSRF custom header | AC #22 | `test_post_without_custom_header_is_403`: a POST lacking `X-Worksweep` returns 403 and the queue file's bytes are unchanged. Mutation: delete the header check ⇒ the status becomes 200 and the bytes change; the test fails on both assertions. |
| **Decision 10** — Origin-vs-Host rejection | AC #23 | `test_post_with_foreign_origin_is_403` (Origin `http://evil.example` vs Host `mac-mini:8787` ⇒ 403, nothing persisted) and `test_post_without_origin_is_allowed` (no `Origin` header ⇒ 200), pinning "reject when an Origin header **is present** and does not match". |
| **Decision 11** — mobile-first checklist with ≥44px targets and a sticky bottom bar | AC #26 | `test_checklist_markup_contract`: for each actionable record the page emits one `<input type="checkbox">` carrying the record number; the rendered CSS declares a min touch dimension of at least 44px for the checkbox/label control and a `position: sticky` (or `fixed`) bottom bar containing exactly two buttons whose labels contain `Approve selected` and `Approve all`. |
| **Decision 11** — desktop panel/card grid at `min-width: 900px` | AC #27 | `test_desktop_breakpoint_declares_panel_grid`: the page's CSS contains a `@media (min-width: 900px)` block that sets a grid/flex panel layout with a non-zero `gap`. |
| **Decision 11 / 13** — vanilla fetch + DOM, zero frameworks, one self-contained page | AC #28, #33 | `test_page_is_self_contained`: the rendered HTML contains no `<script src=`, no `<link rel="stylesheet"`, and no `http`-scheme asset URL — all CSS and JS are inline. |
| **Decision 12** — manual layout switcher, any screen size | AC #29 | `test_layout_toggle_present`: the header contains a control with both `checklist` and `panels` values, and the inline JS binds a click/change handler that sets a layout attribute/class on the document root. Mutation: remove the handler binding ⇒ the test fails. |
| **Decision 12** — the breakpoint sets the DEFAULT only | AC #30 | `test_breakpoint_only_sets_default`: the layout is driven by a root-level attribute (for example `data-layout`) that the media query does not override — the CSS selects on the attribute, and the `@media` block supplies the initial value path only. Mutation: make the media query force the layout ⇒ the stored-choice test (AC #31) fails at narrow widths. |
| **Decision 12** — persists in `localStorage`, survives the 60s auto-refresh | AC #31 | `test_layout_persists_across_refresh`: the inline JS reads `localStorage` and applies the stored layout in a script that runs BEFORE the sections render (no flash of the wrong layout), and writes on toggle. Asserted on the rendered markup: a `localStorage.getItem` read appears ahead of the first section element and a `localStorage.setItem` appears in the toggle handler. |
| **Decision 12** *Rejected* — no separate URLs / no layout state in the URL | AC #32 | `test_layout_is_not_in_the_url`: the toggle markup contains no `href` carrying a layout value and the JS performs no `history.pushState` / `location.search` write; GET `/?layout=panels` renders identically to GET `/`. |
| **Decision 13** — polish: one coherent dark palette, hover/active states | AC #33 | `test_palette_is_single_source`: the CSS defines its colours as custom properties on a single `:root` block and every colour used elsewhere references a `var(--…)`; `test_interactive_controls_have_states`: each of the two buttons and the toggle has a `:hover` and an `:active` rule. The typography/spacing half of decision 13 is a human review obligation, recorded under *Decisions deferred to orchestrator* item 7. |
| Decision 7 (body contract) — `POST /approve` takes `{"numbers": [..]}` | AC #34 | `test_malformed_post_body_is_400`: three cases — empty body, `not json`, and `{"numbers": "1,2"}` — each returns 400 and leaves the queue file's bytes unchanged. Paired with `test_unmatched_numbers_are_ignored` (`{"numbers":[99]}` ⇒ 200, nothing changed), which pins §AC7's "no error" rule against over-strict validation. |
| **Decision 14** — a third view "Branches" on the switcher | AC #35 | `test_switcher_has_three_views`: the header control exposes `checklist`, `panels` and `branches`; `test_branches_layout_persists` reuses the AC #31 assertion with the stored value `branches`. Mutation: drop the third value ⇒ both tests fail. |
| **Decision 14 / §AC12** — shared branch or matching MR ref ⇒ ONE card | AC #36 | `test_same_branch_groups_into_one_card` (two records, same `item.branch` ⇒ one card) and `test_mr_iid_matches_web_url_ref_groups` (record A `web_url=.../merge_requests/4821`, record B `mr_iid=4821`, same `repo` ⇒ one card). Mutation: bucket by `item.branch` alone ⇒ the second test fails, producing two cards. Paired: `test_same_iid_different_repo_does_not_group` (pb-www!4821 vs pb-api!4821 ⇒ two cards). |
| **Decision 14 / §AC12** — no affinity ⇒ trailing "Ungrouped" | AC #37 | `test_no_affinity_goes_to_ungrouped`: a todo record with empty `branch`, `mr_iid == 0`, and a non-MR `web_url` renders under a single card titled `Ungrouped`, and that card is LAST. Mutation: make the tolerant iid helper raise (as `iid_of` does) ⇒ the test errors instead of rendering. |
| **Decision 14** — card header links MR + issue(s); compact record rows | AC #38 | `test_branch_card_header_links_mr_and_issues`: a group holding one MR record and two issue records renders a card whose title is the branch name, whose header contains the MR `web_url` and both issue `web_url`s as links, and whose rows each carry number, executor, a status chip and the why. Paired: `test_bare_mr_iid_renders_unlinked_ref` asserts the `!<iid>`-as-text fallback (deferred item 8). |
| **Decision 14** — approval checkboxes still work in this view | AC #39 | `test_branches_view_keeps_checkboxes`: every `proposed`/`needs-input` row inside a branch card still emits its `<input type="checkbox">` with the record number, and the sticky bar is present. Mutation: render branch rows read-only ⇒ the test fails. |
| **Decision 14** *Rejected* — no live GitLab calls for affinity | AC #40 | `test_branches_view_makes_no_network_calls`: `render_page` is called with `urllib.request.urlopen`, `subprocess.run` and `socket.socket` monkeypatched to raise; the page renders. Structural pair: `worksweep/dashboard.py` imports no `urllib`/`http.client` request machinery beyond `http.server`. |
| §Implementation Part 1 bullet 3 — footer mentions `✅ all` | AC #7 | `test_footer_mentions_approve_all` asserts `"✅ all"` is a substring of `formatter._FOOTER`. |
| §Implementation Part 1 Tests — author gate + confirmation set | AC #5, #6 | `test_approve_all_from_other_author_is_ignored` (a blanket message from `OTHER` changes nothing); `test_intake_confirms_blanket_approved_numbers` drives `_run_intake` with a `✅ all` message and asserts the posted confirmation names every flipped number. |
| §AC4 — GET `/` 200, any other path 404 | AC #9 | `test_get_root_is_200_html` and `test_unknown_path_is_404`; `test_unknown_method_on_known_path_is_404` pins that the two POST routes did not widen the 404 rule for other verbs. |

## Critique Pass

One inline self-critique pass over the six dimensions, re-run after the Round 2 and Round 3 amendments, with every fix applied in this file before the handoff mirror was written.

| Dimension | Result |
|---|---|
| Completeness | weakness: the pre-amendment draft covered decisions 1-6 but left two `§Files`-listed obligations (the `_FOOTER` edit, the plist) without an AC; the Round 2/3/4 amendments then added decisions 7-14 and AC7-AC12, none of which the draft touched → fix applied: AC #7 and AC #16 added, then AC #18-#33 for decisions 7-13 and AC #35-#40 for decision 14. `## Decision Coverage` now carries a row for all 14 decisions and all 12 source AC rows, each with a concrete falsifiable test. Zero deferrals inside the coverage table. |
| DAG order | weakness: the amendments introduced three new ordering hazards on top of the original three — the shared `flip`/`approve_numbers`/`approve_all` extraction must land before either POST route can call it (decision 8); the CSRF guard must be in the request path before any write is reachable (decision 10); the `localStorage` restore must execute before the first section renders or the 60s refresh shows a flash of the wrong layout (decision 12); and the affinity grouping must be computed before any card renders, because AC #36's two-signal equivalence means a record's card is not knowable from that record alone (decision 14) → fix applied: the **Files** list is ordered as a build DAG (1 approvals → 2 formatter → 3 dashboard → 4 `__main__` → 5 plist → 6-8 tests), and each dependent AC names its prerequisite AC inline (AC #18/#19 name AC #20; AC #21/#24 name AC #22; AC #31 names AC #29; AC #38/#39 name AC #36). |
| Pre/postconditions (EARS) | weakness: two source criteria bundled independent postconditions into one sentence, so a half-implementation could still pass a single test written against them — §AC1 bundled "flip every proposed" with "leave everything else unchanged", and §AC9 bundled the missing-header case with the bad-Origin case → fix applied: split into AC #1 / AC #2 and AC #22 / AC #23 respectively, each independently falsifiable, and AC #23 additionally pins the *absent*-Origin case that the source's wording ("when an `Origin` header is present") implies but does not state as a criterion. Every AC below is a `WHEN/IF/WHILE/THE … SHALL` sentence with an explicit trigger. |
| Failure path | weakness: the pre-amendment source had one unwanted-behavior criterion (the bare 404). The amendments added a write path whose failure surface was only partly specified: §AC9 covers CSRF rejection and §AC10 covers a Discord failure, but nothing covered a malformed queue under a `KeepAlive` agent, a malformed POST body, or numbers that match no record → fix applied: **AC #17 was added by this critique pass** (`IF ~/.worksweep/queue.json is missing or malformed THEN the dashboard SHALL serve 200 and SHALL NOT raise`, derived from decision 4's "via the existing `queue.load_queue`" plus `queue.py:47-72`), and **AC #34 was added by this critique pass** (`IF a POST body is absent, not JSON, or carries a non-list "numbers" value THEN the dashboard SHALL respond 400 and persist nothing`, derived from decision 7's declared `{"numbers": [..]}` contract plus §AC7's "numbers in other statuses SHALL be ignored (no error)" — which fixes the *unmatched-number* case but is silent on a malformed envelope). Both were inserted into `## Field Provenance` and `## Decision Coverage`. |
| Concrete layer-map | weakness: the source's Owning-layer row said "dashboard is a NEW leaf module with no existing owner" and, after decision 8, that is no longer even true — the dashboard becomes a second caller of the approvals service and a third writer of the queue file → fix applied: the **Owning layer** field names four layers with file:line (`service` = `approvals.py:61` and the extracted shared flip; `display` = `dashboard.render_page`; `hook` = `dashboard.serve` + handler + CSRF guard; `data` = `queue.py:47,75` reused unchanged but now with a NEW writer), declares product-Canon-N/A because worksweep is a standalone stdlib Python app, and names the order of changes. |
| Reversibility | weakness: two one-way doors, one of them created by the amendment and originally unflagged. **(a)** Verified on disk: worksweep has no un-approve path. `queue.reconcile` writes `status="proposed"` at only three places (`queue.py:151,157,169`), all requiring a changed sha or a prior `error`/`done` state; a same-sha `approved` record keeps its status (`queue.py:165-170`). A mistaken `✅ all` — or now a mistaken tap on "Approve all" — commits the whole `proposed` set to the runner on its next 10-minute pass, recoverable only by hand-editing `~/.worksweep/queue.json`. The amendment makes this door *one tap wide on a phone*, which is strictly worse than the Discord path. **(b)** The decision-8 extraction rewrites the only Discord→status writer; a silent behavioural drift there is not caught by any single AC, only by the 539-test floor. → fix applied: (a) is the top Tricky Part, with the mitigations named (author gate is absent on the dashboard, so CSRF + tailnet-only binding + the two-step select-then-confirm affordance are the ONLY guards; AC #19's falsifying test is what stops "Approve all" from also releasing parked `needs-input` work); (b) is pinned by AC #20's paired `test_discord_and_dashboard_agree` equivalence test plus the untouched-signature constraint in the `diff-against-current` row. The cheap doors are noted too: `launchctl bootout` reverses the agent, and no queue-schema change is introduced, so a plain `git revert` restores prior behaviour against an unmodified `queue.json`. |

<!-- spawn-contract Diagnostic Fields -->

**Plan:** `/Users/chandlerhardy/repos/heartbeat/.claude/plans/worksweep-approve-all-dashboard.md` — READ-FIRST

**Files:** (ordered as a build DAG — every entry's prerequisites appear above it)
- `worksweep/approvals.py` (EXTEND) — add `_APPROVE_ALL_RE` and `parse_approve_all(text) -> bool`; extract the record-flip loop (`approvals.py:84-94`) into a shared pure `flip(records, numbers, now, statuses) -> Tuple[List[QueueRecord], Set[int]]` plus two wrappers `approve_numbers(records, numbers, now)` (statuses `_APPROVABLE`) and `approve_all(records, now)` (statuses `_APPROVE_ALL_STATUSES = ("proposed",)`); `apply_approvals` keeps its exact signature and delegates. `_HAS_MARKER_RE` (line 21) and `_APPROVABLE` (line 31) are NOT modified.
- `worksweep/formatter.py` (EXTEND) — line 19 only: `_FOOTER` gains a `✅ all` mention. No other formatter change.
- `worksweep/dashboard.py` (NEW) — stdlib-only leaf module. `render_page(records, now, queue_mtime) -> str` (pure, includes the inline CSS and the inline vanilla JS); `resolve_bind(bind, run_subprocess=subprocess.run) -> str`; `group_by_workstream(records) -> Tuple[List[Group], List[QueueRecord]]` (pure — connected components over the branch and repo-scoped-MR affinity tokens, plus the ungrouped remainder) and a tolerant `mr_iid_of(item) -> int` returning `0` on no match; a `BaseHTTPRequestHandler` subclass routing GET `/` plus POST `/approve` and `/approve-all` through a CSRF guard; `serve(queue_path, port, bind, post=None, webhook="")` wiring `ThreadingHTTPServer`. Imports `approvals.approve_numbers` / `approvals.approve_all` and `queue.load_queue` / `queue.save_queue`; imports nothing from `__main__`.
- `worksweep/__main__.py` (EXTEND) — line 435: add `"dashboard"` to the positional `choices`; add `--port` (default `8787`) and `--bind` (default `"auto"`); add the `if args.command == "dashboard":` branch, which injects `_post_discord` and `cfg.discord_webhook` into `serve`. No change to `_run_intake`.
- `etc/mini/com.chandlerhardy.worksweep-dashboard.plist` (NEW) — modelled on S8; `KeepAlive` true, `RunAtLoad` true, no `StartInterval`, same `EnvironmentVariables` block, logs to `~/heartbeat-reports/worksweep-dashboard.{log,err}`.
- `worksweep/tests/test_approvals.py` (EXTEND) — `parse_approve_all` parser tests.
- `worksweep/tests/test_apply_approvals.py` (EXTEND) — blanket-apply, author-gate, precedence, and the shared-function equivalence test.
- `worksweep/tests/test_dashboard.py` (NEW) — render, escaping, window math, bind resolution, plist contract, the two POST routes, CSRF, audit post, the layout-switcher markup contract, and the Branches grouping (`group_by_workstream`) contract.
- `worksweep/queue.py` (REFERENCE) — `load_queue` (line 47) and `save_queue` (line 75) are reused unchanged; do not edit.
- `worksweep/models.py` (REFERENCE) — the persisted `WorkItem` field set (lines 55-79) bounds what the dashboard can render; do not add fields.
- `bin/worksweep.sh` (REFERENCE) — line 11 `exec python3 -m worksweep "$@"` is the plist's entry point; no code change needed.

**AC:**  (EARS shape — each line independently falsifiable; precondition = trigger clause, postcondition = SHALL response)

*Part 1 — Discord `✅ all` (unchanged by both amendments)*

1. WHEN the configured Discord user posts a message in which the approval marker is immediately followed by the word "all" and which contains no parsable item numbers, THE worksweep approvals layer SHALL flip every queue record whose `item.status` is `proposed` to `approved`, SHALL set each flipped record's `last_seen` to `now` while preserving its `number` and `first_seen`, and SHALL return every flipped number in the `newly` set.
2. WHEN that same blanket message is applied, THE worksweep approvals layer SHALL leave every record whose status is `needs-input`, `running`, `done`, or `error` byte-identical and SHALL NOT include any of their numbers in `newly`. (Prerequisite: AC #1. The blanket path uses `("proposed",)`, NOT `_APPROVABLE`.)
3. IF a message carries the approval marker AND `parse_approval(text)` returns a non-empty set, THEN THE worksweep approvals layer SHALL approve only the named numbers and SHALL NOT treat the message as a blanket approval — `✅ 1,3 all good` over records 1-3 yields `newly == {1, 3}` with record 2 still `proposed`.
4. THE `parse_approve_all` predicate SHALL require the literal "all" to be adjacent to the marker (pattern `(?:✅|approve)\s+all\b`, case-insensitive, composed from `_HAS_MARKER_RE.pattern` so the marker definition stays single-sourced): it SHALL return `True` for `✅ all` and `approve ALL`, and `False` for `✅ sounds good, that's all`.
5. IF a blanket message's `author_id` differs from the configured `user_id`, THEN THE worksweep approvals layer SHALL change no record and SHALL return an empty `newly` set. (Prerequisite: AC #1.)
6. WHEN `_run_intake` processes a sweep in which a blanket approval flipped N records, THE worksweep intake path (`__main__.py:180-226`) SHALL save the queue exactly once and SHALL post a confirmation naming all N flipped numbers with their executor and repo. (Prerequisite: AC #1 — `apply_approvals` keeps its `(updated_records, newly)` return contract.)
7. THE worksweep digest footer (`formatter._FOOTER`) SHALL contain the literal substring `✅ all`.
8. **Falsifying test:** `test_approve_all_flips_every_proposed_item` SHALL pass with the change in place and SHALL fail when `parse_approve_all` is deleted or `apply_approvals` ignores it — under that mutation the records asserted `approved` read `proposed` and `newly` is empty.

*Part 2 — dashboard: read surface*

9. WHEN a GET request arrives at path `/`, THE worksweep dashboard SHALL respond `200` with a `text/html` body; IF a request arrives at any path other than `/`, `/approve`, or `/approve-all`, THEN THE dashboard SHALL respond `404`; IF a request uses a method a known path does not implement, THEN THE dashboard SHALL respond `404` and SHALL NOT render queue content.
10. WHEN the dashboard renders a queue, THE `render_page` function SHALL partition the records into exactly five sections — **Needs you** (`proposed` + `needs-input`), **In progress** (`running` + `approved`), **Auto** (`keep-current` executor items), **Recently done** (the last 20 `done` records by `last_seen` descending), **Errors** (`error` records, each showing `error_summary`) — plus a telemetry header carrying the queue mtime, per-status counts, and the done-this-week count; and SHALL place each record in exactly one section.
11. WHEN the done-this-week count is computed against a given `now`, THE dashboard SHALL count a `done` record whose `last_seen` is 6 days before `now` and SHALL NOT count one whose `last_seen` is 8 days before `now`.
12. WHEN the loaded record list is empty, THE dashboard SHALL render an all-clear page containing no section tables and SHALL still respond `200`.
13. **Falsifying test:** WHEN a record's `title` is `<script>alert(1)</script>`, THE dashboard SHALL render `&lt;script&gt;alert(1)&lt;/script&gt;` and the page SHALL NOT contain the raw substring `<script>alert`. `test_dashboard_escapes_titles` SHALL fail when the `html.escape` call is removed.
14. WHILE the dashboard is serving a GET request, THE dashboard SHALL perform zero writes to the queue file — after a render plus one handled GET, the queue file's bytes and mtime SHALL be unchanged. (Decision 4's read-only guarantee, narrowed by Round 2 to the GET path; the POST paths write under AC #21.)
15. WHEN `main()` is invoked with the positional command `dashboard`, THE worksweep CLI SHALL accept it (argparse `choices` includes `"dashboard"`), SHALL default `--port` to `8787` and `--bind` to `"auto"`, and WHEN `--bind auto` is resolved THE dashboard SHALL use the first address printed by `tailscale ip -4`; IF that command is missing, exits non-zero, or prints nothing, THEN THE dashboard SHALL bind `127.0.0.1`.
16. THE committed plist `etc/mini/com.chandlerhardy.worksweep-dashboard.plist` SHALL parse under `plistlib` with `Label == "com.chandlerhardy.worksweep-dashboard"`, `KeepAlive is True`, `RunAtLoad is True`, `ProgramArguments` ending in `dashboard`, an `EnvironmentVariables` dict whose `PATH` and `HOME` equal the runner plist's (S8), and `StandardOutPath` / `StandardErrorPath` pointing at `~/heartbeat-reports/worksweep-dashboard.log` / `.err`.
17. IF `~/.worksweep/queue.json` is missing, is not valid JSON, is not a list, or contains an unparsable record, THEN THE dashboard SHALL still respond `200` (rendering the all-clear page or the surviving records) and SHALL NOT raise — tolerance is delegated to the unmodified `queue.load_queue` (`queue.py:47-72`). *(Added by the Critique Pass; load-bearing because the launchd agent runs `KeepAlive` and a raising handler would crash-loop.)*

*Part 3 — dashboard: write surface and mobile UI (Round 2 amendment, decisions 7-11). AC #34 was added later by the Critique Pass and is listed here with its subject matter rather than in strict numeric order; the checklist at the foot of this plan lists all 40 in numeric order.*

18. WHEN `POST /approve` arrives with a valid CSRF header and a body `{"numbers": [...]}`, THE dashboard SHALL persist every named record whose status is `proposed` or `needs-input` as `approved`, SHALL leave records in any other status unchanged, SHALL ignore numbers matching no record without erroring, and SHALL respond `200`. (Prerequisites: AC #20, AC #22.)
19. **Falsifying test:** WHEN `POST /approve-all` arrives with a valid CSRF header, THE dashboard SHALL persist exactly the `proposed` records as `approved` and SHALL leave a seeded `needs-input` record reading `needs-input`. `test_post_approve_all_is_proposed_only` SHALL fail when the route is pointed at the numbered-approval status set (`_APPROVABLE`). (Prerequisites: AC #20, AC #22.)
20. THE status rules SHALL exist in exactly one place: `worksweep/dashboard.py` SHALL contain no queue-status tuple and no `dataclasses.replace(..., status=…)` call, and both POST routes SHALL obtain their records from `approvals.approve_numbers` / `approvals.approve_all`; those same functions SHALL produce records byte-identical to `apply_approvals` for an equivalent Discord message.
21. WHEN either POST route persists an approval, THE dashboard SHALL re-read the queue from disk via `queue.load_queue` immediately before flipping (never from a cached render) and SHALL write via the unmodified `queue.save_queue`, so the write reaches disk through the existing temp-file + `os.replace` rename.
22. **Falsifying test:** IF a POST arrives without the required custom header `X-Worksweep`, THEN THE dashboard SHALL respond `403` and the queue file's bytes SHALL be unchanged. `test_post_without_custom_header_is_403` SHALL fail when the header check is deleted.
23. IF a POST arrives carrying an `Origin` header whose scheme+host+port does not match the request's `Host`, THEN THE dashboard SHALL respond `403` and persist nothing; WHEN a POST arrives with no `Origin` header at all, THE dashboard SHALL process it normally.
24. WHEN either POST route persists an approval and a webhook is configured, THE dashboard SHALL post exactly one Discord confirmation naming the approved numbers and containing the literal marker `(dashboard)`.
25. IF the Discord confirmation post raises, THEN THE dashboard SHALL log to stderr, SHALL still respond `200`, and SHALL NOT roll back the persisted approval — the queue file on disk SHALL show the flipped statuses. (Prerequisite: AC #21 — the write is durable before the post is attempted.)
26. THE rendered page SHALL emit, for every actionable record (`proposed` or `needs-input`), one `<input type="checkbox">` carrying that record's number; THE page CSS SHALL give that control a minimum touch dimension of at least 44px; and THE page SHALL contain a bottom bar declared `position: sticky` or `position: fixed` holding exactly two buttons whose labels contain `Approve selected` and `Approve all`.
27. WHERE the viewport is at least 900px wide, THE page SHALL default to a panel/card grid — its CSS SHALL contain a `@media (min-width: 900px)` block declaring a grid or flex panel layout with a non-zero `gap`.
28. THE rendered page SHALL be self-contained: it SHALL contain no `<script src=`, no `<link rel="stylesheet"`, and no `http`-scheme asset reference; all CSS and JavaScript SHALL be inline, and the JavaScript SHALL use only `fetch` and DOM APIs.
34. IF a POST body is absent, is not valid JSON, or carries a `"numbers"` value that is not a list of integers, THEN THE dashboard SHALL respond `400` and SHALL persist nothing. *(Added by the Critique Pass; §AC7 fixes the unmatched-number case as a non-error, so validation must reject the malformed envelope without rejecting unmatched numbers.)*

*Part 4 — dashboard: layout switcher and polish (Round 3 amendment, decisions 12-13)*

29. WHEN the header's layout toggle is used, THE page SHALL switch between the checklist and panel layouts without a reload, at any viewport width.
30. THE layout SHALL be driven by an explicit root-level attribute (for example `data-layout` on `<html>`), and the `@media (min-width: 900px)` block SHALL only supply that attribute's INITIAL value — the media query SHALL NOT override an explicit stored choice at any width. (Supersedes the Round 2 reading in which the breakpoint decided the layout outright.)
31. WHEN a layout is chosen, THE page SHALL write it to `localStorage`; and WHEN the page loads (including via the 60s `<meta http-equiv="refresh">` reload), THE page SHALL read `localStorage` and apply the stored layout in a script that executes BEFORE the first section element, so no flash of the non-chosen layout occurs. (Prerequisite: AC #29.)
32. THE layout choice SHALL NOT be carried in the URL — the toggle SHALL emit no `href` carrying a layout value, the JavaScript SHALL perform no `history.pushState` or `location.search` write, and GET `/?layout=panels` SHALL render identically to GET `/`. (Decision 12's rejected alternative, locked.)
33. THE page CSS SHALL define its colours as custom properties in a single `:root` block with every other colour referencing a `var(--…)`, and each of the two bottom-bar buttons and the layout toggle SHALL carry a `:hover` and an `:active` rule. *(The typography and spacing half of decision 13 is a human review obligation, recorded under Decisions deferred to orchestrator item 7.)*

*Part 5 — dashboard: Branches view (Round 4 amendment, decision 14)*

35. WHEN the header's layout toggle is used, THE page SHALL offer exactly three views — `checklist`, `panels` and `branches` — and selecting `branches` SHALL render the workstream-grouped view without a reload. The `localStorage` persistence of AC #31 and the no-URL-state rule of AC #32 SHALL apply to the `branches` value unchanged.
36. WHEN two records share a non-empty `item.branch`, OR when one record's `item.mr_iid` equals the MR iid another record's `web_url` refers to within the same `item.repo`, THE Branches view SHALL render both records inside exactly one card. Affinity SHALL be computed as connected components over two token kinds — `("branch", item.branch)` and `("mr", item.repo, iid)` where `iid` comes from the `/merge_requests/(\d+)` segment of `web_url` and, when that yields nothing, from a non-zero `item.mr_iid` — because AC12 makes branch-equality and MR-ref-equality two edges of the same equivalence, not a single bucketing key.
37. IF a record has an empty `item.branch`, a zero `item.mr_iid`, and a `web_url` with no `/merge_requests/<iid>` segment, THEN THE Branches view SHALL place it under a single card titled `Ungrouped`, and that card SHALL be rendered last. THE tolerant `mr_iid_of` helper SHALL return `0` rather than raising for such a record. (`keepcurrent.iid_of` at `keepcurrent.py:92-99` and `runner._iid_of` at `runner.py:207-211` both raise by design and SHALL NOT be modified or called from the dashboard.)
38. WHEN a workstream card renders, THE card SHALL use the group's branch name as its title (falling back to the MR ref when the group has no branch), THE card header SHALL link every `web_url` in the group matching `/merge_requests/` and every `web_url` matching `/-/issues/`, and each record SHALL render as a compact row carrying its number, executor, a status chip and its why. IF the group's only MR evidence is a bare non-zero `item.mr_iid` with no matching `/merge_requests/` URL among its records, THEN THE header SHALL render the reference as unlinked text `!<iid>` and SHALL NOT construct a URL. (Prerequisite: AC #36.)
39. WHILE the Branches view is active, THE approval controls SHALL remain functional — every `proposed` or `needs-input` row inside a card SHALL still emit its `<input type="checkbox">` carrying the record number, and the sticky bar with "Approve selected (N)" and "Approve all" SHALL still be present and SHALL post to the same `/approve` and `/approve-all` routes. (Prerequisites: AC #18, AC #19, AC #36.)
40. THE Branches view SHALL derive all affinity from the queue fields `item.branch`, `item.mr_iid`, `item.web_url` and `item.repo` alone, and SHALL make no network call of any kind — `render_page` and `group_by_workstream` SHALL remain pure functions of their arguments. (Decision 14's rejected alternative, locked.)

**TDD Mode:** `Full TDD` — new user-visible behaviour on three surfaces (a new Discord approval grammar, a new HTTP write path that mutates the shared queue, and a new UI), plus an extraction inside the only Discord→status writer. The falsifying tests AC #8, #13, #19 and #22 are written RED first, and the 539-test floor is the regression guard.

**Owning layer:** `service` + `display` + `hook` + `data` (worksweep is a standalone stdlib Python app — PLA product Canon N/A). `service` = `worksweep/approvals.py:61` (`apply_approvals`, today the ONLY writer of `approved` from Discord input, verified by exhaustive `rg`) plus the extracted shared `flip`/`approve_numbers`/`approve_all` that decision 8 makes the single definition of "approvable". `display` = `worksweep/dashboard.py::render_page` plus the pure `group_by_workstream` / `mr_iid_of` helpers (new; HTML+CSS+JS rendering and the workstream affinity computation, all pure). `hook` = `worksweep/dashboard.py::serve` and the `BaseHTTPRequestHandler` subclass, including the CSRF guard (new HTTP surface). `data` = `worksweep/queue.py:47,75` — `load_queue` / `save_queue` are reused UNCHANGED, but the dashboard becomes the queue file's third writer alongside intake and the runner. Order of changes: service (extraction first, suite green) → display → hook → CLI wiring → plist.

**Downstream consumers:**
- `worksweep/__main__.py:39` (imports `apply_approvals`) — no edit needed; the signature is unchanged by the decision-8 extraction.
- `worksweep/__main__.py:201` (`updated, approved = apply_approvals(...)`) — blanket-flipped numbers must arrive in `approved` or the confirmation is silent (AC #6).
- `worksweep/__main__.py:202-203` (`if approved != set(): save_queue(...)`) — a blanket flip must make this branch fire.
- `worksweep/__main__.py:212-225` (builds `✅ Approved: <n> (<executor> <repo>)` from `approved` + `updated`, and swallows a post failure at `:222-223`) — the user-facing confirmation (AC #6) and the exact swallow pattern AC #25 mirrors.
- `worksweep/__main__.py:104-116` (`_post_discord`) — injected into `serve` for the audit post (AC #24); it RAISES `RuntimeError`, which AC #25 requires the dashboard to catch. Never imported by `dashboard.py`.
- `worksweep/__main__.py:42` (imports `_FOOTER`), `worksweep/formatter.py:229,234` (appends `_FOOTER` to the last digest message or posts it standalone on overflow) — consume `_FOOTER` **by reference**, so the AC #7 text edit breaks nothing.
- `worksweep/__main__.py:435` (positional `choices=["intake","run"]`) — must gain `"dashboard"` (AC #15).
- `worksweep/queue.py:47` `load_queue` — a NEW consumer on both the GET render and the POST fresh-load (AC #14, #21); unchanged.
- `worksweep/queue.py:75` `save_queue` — a NEW third writer of `~/.worksweep/queue.json` alongside `__main__.py:203` (intake) and `__main__.py:459-461` (runner deps) (AC #21). See Tricky Parts for the unlocked read-modify-write.
- `worksweep/tests/test_apply_approvals.py:4,29,38,44,51,60,72,79` — 7 existing behaviour tests on `apply_approvals`; the decision-8 extraction must leave every one green.
- `worksweep/tests/test_needs_input_lifecycle.py:8,69,76,86` — pins that a numbered `✅ N` still releases a `needs-input` item; must stay green (AC #2 and AC #19 must not weaken it, and AC #18 deliberately preserves it on the dashboard's numbered route).
- `worksweep/tests/test_loop_closure.py:17,85` — end-to-end propose → approve → run closure; must stay green.
- `worksweep/tests/test_intake.py:33-137` — 6 tests driving `_run_intake`; must stay green (AC #6 adds a 7th).
- `worksweep/tests/test_main.py:32,46,86,145` and `worksweep/tests/test_runner_execute.py:290` — monkeypatch `_post_discord`; the established seam for asserting posted content without network (the dashboard uses injection instead).
- `worksweep/tests/test_main_devslots.py:157,194` — asserts the digest ends with `_FOOTER` **by reference**; unaffected by AC #7.
- `bin/worksweep.sh:11` — `exec python3 -m worksweep "$@"`; the new plist's `ProgramArguments` route through it (AC #16).
- `worksweep/models.py:76-79` (`dev_box`, `mr_iid`, `branch`) and `models.py:58` (`repo`) — READ by the Branches grouping (AC #36); written by `assessor.py:234`, `__main__.py:428`, `implementer.py:142,453,531`, `runner.py:495`, and preserved across sweeps at `queue.py:167`. The dashboard writes none of them.
- `worksweep/keepcurrent.py:92-99` (`iid_of`, callers `keepcurrent.py:114` and `__main__.py:418`) and `worksweep/runner.py:207-211` (`_iid_of`) — both raise by design; NEITHER is modified or called by the dashboard, which uses its own tolerant helper (AC #37).
- Comment-only references (no code impact): `worksweep/models.py:68`, `worksweep/curator.py:214`, `worksweep/queue.py:27`, `worksweep/approvals.py:8`.

**Sibling pattern:**
- `worksweep/approvals.py:34-58` (S1) — the pure-parse shape `parse_approve_all` copies: guard on `_HAS_MARKER_RE` first, return a plain value, never touch records.
- `worksweep/approvals.py:61-94` (S2) — the flip loop at lines 84-94 is the exact body extracted into the shared pure function decision 8 requires; the docstring at 63-76 is the behaviour contract that must survive the extraction.
- `worksweep/queue.py:47-72` (S3) — the tolerant reader reused unchanged (AC #17 depends on its existing degradation).
- `worksweep/queue.py:75-97` (S4) — the atomic temp-file + `os.replace` writer reused unchanged; decision 8's "atomic `save_queue` (os.replace)" is already exactly this, so the POST path adds no new write mechanics.
- `worksweep/__main__.py:212-225` (S5) — the confirmation-build plus `try/except → print to stderr` swallow that AC #24 and AC #25 mirror.
- `worksweep/__main__.py:104-116` (S6) — the webhook poster to inject; note the `_validate_webhook` host allowlist and the `RuntimeError` it raises.
- `worksweep/keepcurrent.py:17-22` (S7) — "every edge is injected … this module never shells out on its own"; `resolve_bind` takes a `run_subprocess` callable and `serve` takes a `post` callable for exactly this reason.
- `etc/mini/com.chandlerhardy.worksweep-runner.plist:1-30` (S8) — the launchd shape to copy: `ProgramArguments` through `bin/worksweep.sh`, the `PATH`/`HOME` `EnvironmentVariables` dict, and the `~/heartbeat-reports/*.{log,err}` paths. Swap `StartInterval` + `RunAtLoad false` for `KeepAlive true` + `RunAtLoad true`.
- `worksweep/__main__.py:433-440` (S9) — the argparse block to extend (positional `choices`, NOT subparsers).
- `worksweep/formatter.py:19` (S10) — the exact current `_FOOTER` string being edited.
- `worksweep/tests/test_apply_approvals.py:1-25` (S11) — the two-line `sys.path.insert` module header (there is no `conftest.py` in this repo) plus the `_rec` / `_msg` factories `test_dashboard.py` must copy.
- `worksweep/formatter.py:61-70` — `_sanitize_title` is the *conceptual* escaping sibling ONLY. It is **not reusable** here: it rewrites `http://` to `hxxp://`, which would destroy every dashboard link. The dashboard's defense is `html.escape`.
- `worksweep/keepcurrent.py:92-99` (S12) — the exact `re.search(r"/merge_requests/(\d+)", item.web_url or "")` the Branches grouping reuses. Copy the regex, NOT the `raise`: the dashboard's `mr_iid_of` returns `0` instead (AC #37). `runner._iid_of` (`runner.py:207-211`) is a second, independent copy of the same raising derivation.
- `worksweep/models.py:74-79` (S13) — the persisted affinity fields (`branch`, `mr_iid`) the grouping key is built from, with the field comments naming which executor writes each.
- No sibling exists for an HTTP server or an HTML page anywhere in this repo — `worksweep/dashboard.py` is the first instance of both. That is why decisions 3, 10, 11 and 13 carry so much of the specification weight, and why AC #20, #22, #26, #28 and #33 assert structure the codebase cannot demonstrate by example.

**Verify:**
```
# Targeted feedback loop (agent's inner loop):
python3 -m pytest worksweep/tests/test_approvals.py worksweep/tests/test_apply_approvals.py worksweep/tests/test_dashboard.py -q
# Run this immediately after the decision-8 extraction, BEFORE any new behaviour:
python3 -m pytest worksweep/tests/ -q     # must still be exactly 539 green
# Regression floor at the end (539 green measured on this branch before any edit):
python3 -m pytest worksweep/tests/ -q
```
- **Production paths:** (a) Discord message → `__main__._run_intake` (`__main__.py:180`) → `approvals.apply_approvals` (`approvals.py:61`) → `queue.save_queue`; (b) `queue.json` → `queue.load_queue` (`queue.py:47`) → `dashboard.render_page` → HTTP response; (c) browser `fetch` → CSRF guard → `queue.load_queue` → `approvals.approve_numbers` / `approve_all` → `queue.save_queue` → injected `_post_discord`.
- **Risky seams:** (1) message-text → approved-number-set, where `parse_approve_all` and `parse_approval` must compose with numbers-win precedence; (2) the blanket status filter `("proposed",)` versus the numbered filter `_APPROVABLE`, now duplicated across two entry points and deduplicated by decision 8; (3) untrusted `title`/`why`/`web_url` → HTML; (4) unauthenticated HTTP → queue mutation (the CSRF boundary); (5) the read-modify-write on a file two other processes also write.
- **Targeted mutations and expected failures:** delete the `not parse_approval(text)` precondition in `parse_approve_all` ⇒ `test_explicit_numbers_beat_all` fails (`{1,2,3}` != `{1,3}`). Swap the blanket filter to `_APPROVABLE` ⇒ `test_approve_all_leaves_needs_input_parked` fails. Relax the regex to a bare `\ball\b` search ⇒ `test_chatty_all_is_not_blanket` fails. Remove the `html.escape` call ⇒ `test_dashboard_escapes_titles` fails. Delete `parse_approve_all` ⇒ `test_approve_all_flips_every_proposed_item` fails (AC #8). Point `/approve-all` at `approve_numbers` ⇒ `test_post_approve_all_is_proposed_only` fails (AC #19). Delete the `X-Worksweep` header check ⇒ `test_post_without_custom_header_is_403` fails on both the status and the queue-bytes assertion (AC #22). Let the Discord `RuntimeError` propagate ⇒ `test_discord_failure_does_not_fail_approval` fails (AC #25). Make the `@media` block force the layout ⇒ `test_layout_persists_across_refresh` fails at narrow widths (AC #30/#31). Bucket the Branches view by `item.branch` alone ⇒ `test_mr_iid_matches_web_url_ref_groups` fails with two cards instead of one (AC #36). Call `keepcurrent.iid_of` instead of the tolerant helper ⇒ `test_no_affinity_goes_to_ungrouped` errors on a todo record (AC #37).
- **Live boundaries left unproved by pytest:** the real Discord fetch/POST round-trip, the real `tailscale ip -4` resolution on the mini, the `ThreadingHTTPServer` socket bind on port 8787, `launchctl bootstrap` of the new agent, real browser execution of the inline JavaScript (the tests assert the emitted markup and script text, never a running DOM), real `localStorage` persistence across a real refresh, real touch-target sizing on a physical phone, and whether the Branches grouping matches Chandler's actual mental workstreams on a real queue — the tests prove the affinity ALGORITHM, not that a real `~/.worksweep/queue.json` produces the cards he expects (check this in the first live pass). These are covered only by the deploy checklist in the source's §Verification plus a manual phone/desktop pass, not by the test suite. State this explicitly in the final report.

**Plan provenance:** `unhardened` (claude-solo session; no cross-model plan-checkpoint ran — the gated decision-log's own provenance row states the same)

### Behavioral Contract

Pre-change behaviours that must keep working (this list is the coverage denominator; each is Read-verified on disk this session). Behaviours 1-7 are the ones the decision-8 extraction puts at risk.

1. `parse_approval` returns numbers only when `✅` or `approve` is present; a bare number never approves (`approvals.py:41-42`; `test_approvals.py:36`).
2. `parse_approval` drops `0`, negatives, descending ranges, and ranges spanning more than 500, while keeping the remaining tokens (`approvals.py:49-57`; `test_approvals.py:52-72`).
3. `apply_approvals` consumes only messages whose `author_id == user_id`; a colleague's `✅ 1` is ignored (`approvals.py:79-82`; `test_apply_approvals.py:42`).
4. A numbered `✅ N` flips BOTH `proposed` and `needs-input` to `approved` via `_APPROVABLE` — this releases a halted implement item and must not be weakened (`approvals.py:31,87`; `test_needs_input_lifecycle.py:69`).
5. `apply_approvals` returns only freshly flipped numbers; an already-`approved` record stays approved and is absent from `newly` (`approvals.py:74-76`; `test_apply_approvals.py:56`).
6. A flipped record keeps its `number` and `first_seen` and gets `last_seen = now` (`approvals.py:88-90`; `test_apply_approvals.py:37`).
7. A number matching no record is a no-op (`approvals.py:87`; `test_apply_approvals.py:50`).
8. `_run_intake` saves the queue only when `approved != set()` (`__main__.py:202-203`).
9. `_run_intake` advances the Discord cursor to the newest message id seen, whether or not anything was approved (`__main__.py:205-210`; `test_intake.py:93`).
10. `_run_intake` posts `✅ Approved: <n> (<executor> <repo>)` to the webhook, or prints it when no webhook is configured, and a post failure is swallowed to stderr (`__main__.py:212-225`).
11. `_post_discord` refuses any webhook host outside the Discord allowlist and follows no redirects (`__main__.py:104-116`, `_ALLOWED_WEBHOOK_HOSTS` at `:31`).
12. `load_queue` is tolerant: missing file, bad JSON, non-list, or a bad record yields `[]` or a skip-with-stderr — it never raises (`queue.py:47-72`).
13. `save_queue` writes atomically via a same-directory temp file plus `os.replace` and creates the parent directory when needed (`queue.py:75-97`), so a concurrent reader always sees a complete file.
14. `queue.reconcile` never returns an `approved` record to `proposed` unless the sha changed or the prior state was `error`/`done` (`queue.py:151,157,165-170`) — there is no un-approve path.
15. The digest footer is appended to the last message, or posted as its own message when appending would exceed the byte cap (`formatter.py:229-234`).
16. `python3 -m worksweep` accepts the positional commands `intake` and `run` plus `--dry-run` / `--discord`, and `--dry-run` never persists a claim or posts (`__main__.py:433-440`, `:451-455`).
17. `keepcurrent.iid_of` and `runner._iid_of` RAISE on a `web_url` with no `/merge_requests/<iid>` segment — deliberate, so a keep-current merge or a runner claim never guesses an iid (`keepcurrent.py:92-99` pinned by `test_keepcurrent.py:51`; `runner.py:207-211`).
18. `item.branch` is set for stale/keep-current items (`assessor.py:234`) and implement items (`__main__.py:428`, `implementer.py:453,531`); `item.mr_iid` is set once an implement executor opens its Draft MR (`implementer.py:142`, `runner.py:495`) and is preserved across sweeps (`queue.py:167`). Both default empty and both must keep doing so.
19. `worksweep/tests/` is 539 tests green (`python3 -m pytest worksweep/tests/ -q`, measured on this branch this session).

### What I Discovered

- **`worksweep/intake.py` does not exist.** The dispatch brief and the gated log's provenance row both name it, but exhaustive `rg -n 'apply_approvals'` places the intake path in `worksweep/__main__.py::_run_intake` (`__main__.py:180`), importing at `:39` and calling at `:201`. Every Files / consumer / AC row uses the real path (mismatch row 1).
- **The amendment makes the dashboard the queue file's third writer.** `save_queue` is called today at `__main__.py:203` (intake) and `__main__.py:459-461` (runner deps). Decision 8's "load fresh → flip → atomic save" is a read-modify-write with no lock, so a dashboard approval landing inside a sweep's own read-modify-write window loses that sweep's delta (whole-file `os.replace`, last writer wins). The source's original Critique Pass analysed only *read* races ("no torn reads, no lock needed") — which remains true and is now insufficient. Raised as deferred item 6.
- **`_post_discord` cannot be imported by the dashboard.** It lives in `__main__.py:104`, and `__main__.py` imports the dashboard module — so decision 9's audit post must arrive by injection (`serve(..., post=..., webhook=...)`), matching S7's edge discipline. The existing tests monkeypatch `_post_discord` on the module (`test_main.py:32,46,86,145`); the dashboard tests use an injected fake instead.
- **`save_queue` already satisfies decision 8's atomicity requirement verbatim** (`queue.py:95-97`: temp file in the same directory, then `os.replace`). No new write mechanics are needed — only the calling discipline.
- **`WorkItem` has no `dev_url` field** (`models.py:55-79`); `dev_url` lives only on the transient `KeepCurrentResult` (`keepcurrent.py:88`) and is never persisted, so "dev URL when present" is unrenderable from the queue. There is likewise no `verdict` field — `done_reason` + `result_sha` are the persisted done-state facts (mismatch rows 2-3).
- **`main()` uses a positional `command` with `choices=["intake","run"]` (`__main__.py:435`), not `argparse.add_subparsers`.** Reading the source's word "subcommand" literally would send the implementer into a subparser refactor touching the existing `intake` and `run` wiring.
- **The footer edit is free.** `rg -n '_FOOTER'` shows every consumer references the symbol, never the literal text — including the only test assertion (`test_main_devslots.py:194`, `endswith(_FOOTER.rstrip())`).
- **The blanket parser cannot regress the existing approval tests.** An exhaustive case-insensitive `rg` over `worksweep/tests/` found no test message string containing the word "all", so all 29 existing approval assertions are orthogonal to the new grammar — which also means they form a clean regression net for the decision-8 extraction.
- **`formatter._sanitize_title` is not reusable for HTML** — it rewrites `http://` to `hxxp://` (`formatter.py:57,69`), a Discord-markdown-injection defense that would break every dashboard link.
- **There is no un-approve path anywhere in worksweep.** `queue.reconcile` writes `status="proposed"` at only three places (`queue.py:151,157,169`), all requiring a changed sha or a prior `error`/`done` state. The amendment therefore puts an irreversible bulk action one tap away on a phone.
- **The `web_url` → MR-iid derivation already exists twice, and both copies raise.** `keepcurrent.iid_of` (`keepcurrent.py:92-99`, pinned by `test_keepcurrent.py:51`) and `runner._iid_of` (`runner.py:207-211`) are independent copies of the same regex, each raising when the URL has no MR segment — correct for a merge or a claim, fatal for a dashboard that must render a queue full of issue and todo records under a `KeepAlive` agent. The Branches view copies the regex and returns `0` instead (AC #37).
- **MR iids are per-project, and the queue already carries the scope.** `WorkItem.repo` (`models.py:58`) is populated on every record, so the MR affinity token is `(repo, iid)`. Without it, `pb-www!4821` and `pb-api!4821` would collapse into one workstream card.
- **AC12 is an equivalence, not a bucketing key.** "two records share a branch (**or** one's `mr_iid` matches another's MR ref)" means a branch-keyed record and an MR-keyed record must land in the same card when they refer to the same workstream — so the grouping is connected components over two token kinds, and a naive `groupby(item.branch or mr_iid)` satisfies the wording only for the single-signal cases.
- **This repo has no HTTP server, no HTML template, and no browser-facing code of any kind.** `worksweep/dashboard.py` is the first instance; there is no sibling to copy for the handler, the CSRF check, or the page.

### Tricky Parts

- **The one-way door, now one tap wide.** A wrong `✅ all` — or a mis-tap on "Approve all" — commits the entire `proposed` set to the runner on its next 10-minute pass, and nothing in worksweep can walk it back; recovery means hand-editing `~/.worksweep/queue.json`. On the Discord path the guards are the author gate and the AC #4 adjacency regex. **The dashboard has no author gate at all** — its only guards are tailnet-only binding (decision 5), the CSRF header (AC #22), and the affordance itself. Do not add a one-tap confirm-free path beyond the two buttons decision 11 specifies.
- **Do the decision-8 extraction as its own step and re-run the full suite before writing any new behaviour.** `apply_approvals` is today the only Discord→status writer; behaviours 3-7 of the Behavioral Contract all live in the loop being moved. Extract, get 539 green, then add.
- **Blanket and numbered paths differ by exactly one status.** `_APPROVABLE` is `("proposed", "needs-input")`; the blanket set is `("proposed",)`. That single member is the entire content of decision 1 and decision 7's approve-all clause. Keep `_APPROVABLE` and the numbered path untouched — behaviour 4 still needs `needs-input` in it, and AC #18 deliberately preserves that on the dashboard's *numbered* route too. The two routes are NOT symmetric; this asymmetry is the most likely thing to be "cleaned up" by mistake.
- **The adjacency regex is load-bearing, not stylistic.** `_HAS_MARKER_RE` (`approvals.py:21`) is an unanchored `✅|approve`. A naive "marker present AND `\ball\b` present" predicate turns `✅ sounds good, that's all` into a full-queue approval. Build it as `re.compile(rf"(?:{_HAS_MARKER_RE.pattern})\s+all\b", re.I)` so the marker definition stays single-sourced. Consequence to accept deliberately: `✅all` (no space) does NOT match, because the source specifies `\s+`.
- **Precedence is an ordering constraint, not a filter.** `parse_approve_all` must call `parse_approval(text)` and return `False` when it is non-empty. Computing the blanket flag independently and unioning the two result sets silently violates decision 2.
- **The author gate must wrap the blanket check too.** The blanket flag is derived per-message inside the existing `if m.author_id == user_id:` loop (`approvals.py:80-82`), not from the raw message list.
- **Blanket-flipped numbers must land in `newly`.** `__main__.py:212-225` builds the user-facing confirmation from that set; a flip that bypasses it approves work silently.
- **The POST path must reload from disk, not reuse the rendered snapshot.** The page the user is looking at may be up to 60 seconds stale (the auto-refresh interval) and the runner may have claimed items since. Flipping a cached list would resurrect stale statuses over newer ones on `save_queue`'s whole-file replace (AC #21).
- **The unlocked read-modify-write is a real, accepted-for-now risk.** Three writers, no lock; the worst case is a lost sweep delta rather than a corrupt file (`os.replace` keeps the file atomic). Do not invent a locking scheme in this plan — surface it (deferred item 6) and keep the window as short as possible by loading immediately before the flip.
- **Order the write path so the queue is durable before the audit post.** AC #25 requires the approval to survive a Discord failure; that only holds if `save_queue` has returned before `post` is called, inside a `try/except` mirroring `__main__.py:220-223`.
- **CSRF: check the header AND the Origin, and answer no preflight.** The custom-header defense only works because a cross-origin page cannot set `X-Worksweep` without a CORS preflight the server never answers — so do NOT add an `OPTIONS` handler and do NOT emit any `Access-Control-Allow-*` header. Reject a *present* mismatched `Origin`; allow an absent one (same-origin `fetch` on a plain page may omit it).
- **Escape the `href`, not just the text.** `web_url` is interpolated into an attribute; use `html.escape(url, quote=True)` there as well as on titles and whys. One step beyond the source line; flagged as deferred item 3.
- **Do not import `formatter._sanitize_title` into the dashboard** — it rewrites `http://` to `hxxp://` in rendered text.
- **Do not add fields to `WorkItem`.** Render only what `queue.json` persists (`models.py:55-79`): no `dev_url`, no `verdict`.
- **`serve()` blocks forever — keep it out of the unit tests.** Test `render_page` (pure), `resolve_bind` (injected runner), and the handler's routing/CSRF/flip logic through a directly-driven fake request or a `ThreadingHTTPServer` on port 0 in a thread that is shut down in a `finally`. Never call `serve_forever()` on the configured port from pytest.
- **Inject the `tailscale` edge and the Discord poster.** `resolve_bind(bind, run_subprocess=subprocess.run)` and `serve(..., post=None, webhook="")` per S7's discipline, so no test shells out or reaches the network. Handle all three `tailscale` failure shapes: `FileNotFoundError`, non-zero return code, empty stdout.
- **`KeepAlive true` amplifies any raise into a crash loop.** Neither the GET handler nor either POST handler may raise (AC #17, #34); `load_queue` already degrades, so do not add a stricter parse on top of it.
- **The `localStorage` restore must run before the first section renders** or every 60s auto-refresh shows a visible flash of the wrong layout — put that script in `<head>`, before the body content, not in a `DOMContentLoaded` handler (AC #31).
- **The media query must not fight the stored choice.** Drive layout from a root attribute the JS sets, and scope the `@media` block so it only supplies the initial value. A media query that sets the layout directly will override the user's toggle at one width and pass a naive test at the other (AC #30).
- **Do NOT call `keepcurrent.iid_of` (or `runner._iid_of`) from the dashboard.** Both raise on a non-MR `web_url`, and under `KeepAlive` that is a crash loop the moment the queue holds an issue or todo record. Copy the regex into a tolerant `mr_iid_of` returning `0`. Do not "fix" the originals — their raise guards the merge and claim paths (behaviour 17).
- **Scope the MR affinity token by `item.repo`.** Iids are per-project; an unscoped `("mr", iid)` token merges unrelated pb-www and pb-api workstreams into one card (AC #36).
- **Group with connected components, not a dict keyed on one field.** A record can contribute BOTH a branch token and an MR token, and AC12 requires those to unify. Collect tokens per record, union records sharing any token, then render one card per component; records contributing zero tokens go to Ungrouped (AC #36, #37).
- **Card ordering must be deterministic** or every 60s refresh reshuffles the page under the user's thumb while they are checking boxes. Sort components by a stable key (for example the lowest record number in the group) and always render `Ungrouped` last (AC #37).
- **Do not construct MR URLs.** When a group's only MR evidence is a bare `mr_iid`, render `!<iid>` as text; building a URL from `repo` is invention the source did not authorise (AC #38, deferred item 8).
- **There is no `conftest.py`.** Every test module opens with the two-line `sys.path.insert` header (S11); `test_dashboard.py` must copy it verbatim or imports fail.
- **Port 8787 is unverified as free on the mini.** Check it before `launchctl bootstrap`; a bound port plus `KeepAlive` produces a silent restart loop in `worksweep-dashboard.err`.
- **Plist deltas from S8 are easy to half-apply:** the dashboard agent has NO `StartInterval`, sets `RunAtLoad` to `true` (the runner's is `false`), and adds `KeepAlive`. Copy the `EnvironmentVariables` block unchanged.
- **Decision 13's polish bar is a review obligation, not just a test.** AC #33 pins only the falsifiable half (single `:root` palette, `:hover`/`:active` on every control, zero external assets). Typography, spacing and the overall feel need a human look on both a phone and a desktop before merge.

### Relevant Patterns

- `worksweep/approvals.py:19-58` — parser constants plus `parse_approval`: the pure-parse-then-apply split the new predicate copies (quoted as S1).
- `worksweep/approvals.py:61-94` — `apply_approvals`, whose flip loop (84-94) is the body extracted into the shared pure function (quoted as S2).
- `worksweep/queue.py:47-72` — `load_queue`, the tolerant reader reused unchanged (quoted as S3).
- `worksweep/queue.py:75-97` — `save_queue`, the atomic writer reused unchanged (quoted as S4).
- `worksweep/__main__.py:212-225` — the confirmation-build plus swallow-the-failure pattern (quoted as S5).
- `worksweep/__main__.py:104-116` — `_post_discord`, the injected poster with its host allowlist (quoted as S6).
- `worksweep/keepcurrent.py:17-22` — the edge-injection discipline for a module that must never shell out or reach the network on its own (quoted as S7).
- `etc/mini/com.chandlerhardy.worksweep-runner.plist:1-30` — the launchd agent shape and env block (quoted as S8).
- `worksweep/__main__.py:433-440` — the argparse command surface to extend (quoted as S9).
- `worksweep/formatter.py:19` — the current `_FOOTER` string (quoted as S10).
- `worksweep/tests/test_apply_approvals.py:1-25` — test-module header and record/message factories (quoted as S11).
- `worksweep/formatter.py:61-70` — `_sanitize_title`, the conceptual escaping sibling that must NOT be imported into the dashboard.
- `worksweep/keepcurrent.py:92-99` — `iid_of`, the `web_url` → MR-iid regex to copy without its `raise` (quoted as S12); `worksweep/runner.py:207-211` is the second copy.
- `worksweep/models.py:74-79` — the persisted affinity fields and the comments naming their writers (quoted as S13).
- `worksweep/tests/test_keepcurrent.py:47-55` — the existing tests pinning both the parse and the raise; the shape the tolerant-helper tests mirror in the negative.
- `worksweep/tests/test_main.py:32,46,86,145` — the established monkeypatch seam for asserting Discord posts without network.
- `worksweep/tests/test_models_v2.py:72-83` — proof that a `queue.json` written without a later-added key still loads; the precedent to follow if the deferred `dev_url` persistence is ever approved.
- `bin/worksweep.sh:1-11` — the launcher the plist invokes (`exec python3 -m worksweep "$@"`).

<!-- Sprint-specific sections -->

## Layout (UI tasks only)

One self-contained page, three layouts selected by a root attribute (`data-layout="checklist" | "panels" | "branches"`). The `min-width: 900px` media query supplies the INITIAL value only (narrow → checklist, wide → panels; `branches` is never a breakpoint default, only an explicit choice); the header toggle and `localStorage` override it at any width (decisions 12, 14).

Checklist layout (mobile default) — single scrolling column, checkbox per actionable item, sticky bottom bar:

```
+------------------------------------------+
| 🔭 Worksweep  [Checklist|Panels|Branches]|   <- header + layout toggle
| last sweep <mtime> · done this week: N   |
| proposed N · needs-input N · running N   |
+------------------------------------------+
| Needs you                                |
| [x] #12 [implement] !4821                |
|     <title>                              |
|     <why>                    <age>       |   <- >=44px touch target per row
| [ ] #14 [magi-review] !4830               |
|     ...                                  |
+------------------------------------------+
| In progress / Auto / Recently done /     |
| Errors  (same single column, read-only)  |
+------------------------------------------+
|  [ Approve selected (2) ] [ Approve all ]|   <- position: sticky bottom bar
+------------------------------------------+
```

Panels layout (desktop default at >=900px) — same content and same checkboxes, as a spaced card grid:

```
+--------------------------------------------------------------+
| 🔭 Worksweep  last sweep <mtime>  [Checklist|Panels|Branches] |
| proposed N · needs-input N · running N · done N · error N     |
+--------------------------------------------------------------+
| +----------------------+   +----------------------+          |
| | Needs you            |   | In progress          |   <- gap  |
| | [x] #12 ...          |   | #9  dev2  claimed …  |          |
| | [ ] #14 ...          |   | #11 dev0  claimed …  |          |
| +----------------------+   +----------------------+          |
| +----------------------+   +----------------------+          |
| | Recently done        |   | Auto / Errors        |          |
| +----------------------+   +----------------------+          |
+--------------------------------------------------------------+
|          [ Approve selected (2) ]  [ Approve all ]           |
+--------------------------------------------------------------+
```

Branches layout (explicit choice only) — one card per workstream, `Ungrouped` last (decision 14):

```
+--------------------------------------------------------------+
| 🔭 Worksweep  last sweep <mtime>  [Checklist|Panels|Branches] |
+--------------------------------------------------------------+
| +----------------------------------------------------------+ |
| | chardy/1588-group-page-ranch-data                        | |  <- branch name = card title
| | !4821  ·  #1588  #1601                                   | |  <- MR link + issue link(s)
| |----------------------------------------------------------| |
| | [x] #12  implement    (needs-input)  awaiting answer      | |  <- compact rows, chip, why
| | [ ] #14  magi-review  (proposed)     no magi-review yet   | |
| |     #9   keep-current (done)         mr-merged            | |
| +----------------------------------------------------------+ |
| +----------------------------------------------------------+ |
| | chardy/1602-add-cost-validation                          | |
| | !4830                                                    | |
| | [ ] #17  implement    (proposed)     assigned issue       | |
| +----------------------------------------------------------+ |
| +----------------------------------------------------------+ |
| | Ungrouped                                                | |  <- always last
| |     #21  triage       (proposed)     todo assigned        | |
| +----------------------------------------------------------+ |
+--------------------------------------------------------------+
|          [ Approve selected (1) ]  [ Approve all ]           |
+--------------------------------------------------------------+
```

Empty queue renders the all-clear page instead of the sections (AC #12). Inline CSS and inline JS only; one `:root` dark palette (AC #33); `<meta http-equiv="refresh" content="60">` in the head, with the `localStorage` layout restore running ahead of the body (AC #31).

## Component Spec (UI tasks only)

No component framework (decision 11: vanilla `fetch` + DOM). The page's contract:

| Element | Contract |
|---|---|
| Layout toggle | Header control exposing `checklist`, `panels` and `branches`; sets `data-layout` on the document root and writes `localStorage`; no `href`, no URL state (AC #29, #31, #32). |
| Actionable row | One `<input type="checkbox">` carrying the record number, for every `proposed` or `needs-input` record; minimum 44px touch dimension (AC #26). |
| "Approve selected (N)" | Label carries the live count of checked boxes; `fetch('/approve', {method:'POST', headers:{'X-Worksweep':'approve','Content-Type':'application/json'}, body: JSON.stringify({numbers:[…]})})`; on 200, reload (AC #18, #22, #26). |
| "Approve all" | Same headers, `fetch('/approve-all', {method:'POST', …})`, empty or absent body; on 200, reload (AC #19, #22, #26). |
| Branch card | One card per affinity component; title = branch name (or the MR ref when the group has no branch); header links every `/merge_requests/` and `/-/issues/` `web_url` in the group; a bare `mr_iid` renders as unlinked `!<iid>` (AC #36, #38). |
| Branch record row | Compact: number, executor, status chip, why — plus the same checkbox on `proposed`/`needs-input` rows (AC #38, #39). |
| Ungrouped card | Trailing card holding every record with no branch and no MR ref; always rendered last (AC #37). |
| Palette | Colours declared once as custom properties on `:root`; every other colour a `var(--…)` (AC #33). |
| Interactive states | `:hover` and `:active` rules on both buttons and the toggle (AC #33). |
| External assets | None — no `<script src=`, no `<link rel="stylesheet"`, no `http`-scheme asset URL (AC #28). |

## Acceptance Criteria

Cross-referenced with the gated decision-log's own AC list. Source AC1-AC6 map to plan AC #1/#2, #3, #8, #9/#10, #13, #14; source AC7-AC10 map to #18, #19, #22/#23, #24/#25; source AC11 maps to #29/#31; source AC12 maps to #36/#37. Plan AC #4, #5, #6, #7, #11, #12, #15, #16, #20, #21, #26, #27, #28, #30, #32, #33 come from decisions and Implementation bullets that the source's own AC list does not enumerate; AC #35, #38, #39 and #40 come from decision 14's body and its rejected alternative, which source AC12 does not enumerate; AC #17 and #34 were added by the Critique Pass failure-path dimension.

- [ ] AC #1 — blanket flips every `proposed` to `approved`, `newly` carries them
- [ ] AC #2 — `needs-input` / `running` / `done` / `error` untouched and absent from `newly`
- [ ] AC #3 — explicit numbers beat "all"
- [ ] AC #4 — `(?:✅|approve)\s+all\b` adjacency; chatty "that's all" rejected
- [ ] AC #5 — author gate holds for blanket messages
- [ ] AC #6 — intake confirmation names every blanket-flipped number
- [ ] AC #7 — `_FOOTER` mentions `✅ all`
- [ ] AC #8 — falsifying: removing `parse_approve_all` fails `test_approve_all_flips_every_proposed_item`
- [ ] AC #9 — GET `/` is 200 HTML; unknown path or unimplemented method is 404
- [ ] AC #10 — five sections plus telemetry header; each record in exactly one section
- [ ] AC #11 — done-this-week window: 6 days counts, 8 days does not
- [ ] AC #12 — empty queue renders the all-clear page, still 200
- [ ] AC #13 — falsifying: removing `html.escape` fails `test_dashboard_escapes_titles`
- [ ] AC #14 — GET performs zero writes to the queue file
- [ ] AC #15 — `dashboard` command accepted; `--port` 8787, `--bind auto` → `tailscale ip -4` → `127.0.0.1`
- [ ] AC #16 — plist parses with `KeepAlive`/`RunAtLoad` true and the runner's env block
- [ ] AC #17 — malformed or missing queue still serves 200 and never raises
- [ ] AC #18 — `POST /approve` flips selected `proposed` AND `needs-input`; other statuses and unmatched numbers ignored; 200
- [ ] AC #19 — falsifying: `POST /approve-all` is `proposed`-only; fails if pointed at `_APPROVABLE`
- [ ] AC #20 — status rules live only in `approvals.py`; dashboard holds no status tuple; Discord and dashboard produce identical records
- [ ] AC #21 — POST reloads from disk, then writes through the unmodified atomic `save_queue`
- [ ] AC #22 — falsifying: missing `X-Worksweep` header ⇒ 403, nothing persisted
- [ ] AC #23 — mismatched `Origin` ⇒ 403; absent `Origin` ⇒ processed
- [ ] AC #24 — every dashboard approval posts one Discord confirmation carrying `(dashboard)`
- [ ] AC #25 — a Discord failure logs to stderr, still returns 200, does not roll back
- [ ] AC #26 — checkbox per actionable item, ≥44px targets, sticky bar with the two named buttons
- [ ] AC #27 — `@media (min-width: 900px)` panel/card grid with non-zero gap
- [ ] AC #28 — self-contained page: no external script, stylesheet, or asset URL
- [ ] AC #29 — layout toggle switches without reload at any width
- [ ] AC #30 — the breakpoint sets the DEFAULT only; it never overrides a stored choice
- [ ] AC #31 — layout persists in `localStorage` and is restored before first paint after the 60s refresh
- [ ] AC #32 — no layout state in the URL; `/?layout=panels` renders identically to `/`
- [ ] AC #33 — single `:root` palette via custom properties; `:hover` + `:active` on both buttons and the toggle
- [ ] AC #34 — malformed POST body ⇒ 400, nothing persisted (unmatched numbers still 200)
- [ ] AC #35 — switcher offers three views; `branches` persists like the others
- [ ] AC #36 — shared branch OR matching repo-scoped MR ref ⇒ one card (connected components)
- [ ] AC #37 — no affinity ⇒ trailing `Ungrouped` card; `mr_iid_of` returns 0, never raises
- [ ] AC #38 — card title = branch; header links MR + issue(s); bare `mr_iid` unlinked; compact rows
- [ ] AC #39 — checkboxes and the sticky bar still work inside the Branches view
- [ ] AC #40 — zero network calls; `render_page` and `group_by_workstream` stay pure
- [ ] Regression floor: `python3 -m pytest worksweep/tests/ -q` green, count strictly greater than 539; and exactly 539 green immediately after the decision-8 extraction, before any new behaviour
