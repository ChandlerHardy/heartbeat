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
| **Downstream consumers** | __main__._run_intake (calls apply_approvals, posts confirmation from the returned `newly` set); runner picks up `approved` items; formatter._FOOTER text is user-facing contract | grep `apply_approvals` in intake.py |
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
