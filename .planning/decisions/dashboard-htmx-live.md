<!-- GATED: ExitPlanMode approved -->
# Worksweep dashboard: htmx live updates (fragment swaps + SSE)

## Context

The dashboard's "live" mechanism is a 10s `/mtime` poll that does a full `location.reload()`, suppressed by a `busy()` guard whenever anything is selected or in flight — which is why Chandler had to hand-refresh to see row 216 close. Full-page reload is the wrong primitive: it loses scroll/filter/selection state, so it must be guarded, so it goes stale. Decision (2026-08-27, after a Preact/htmx/Alpine/Lit survey): **htmx now** — keep the server-renders-HTML architecture the 1211-test suite pins, swap fragments instead of pages, push via SSE. Long-term this is the platform for the estate dashboard (work + personal contexts = server-side routing + more fragment trees).

## Verified constraints (explorer report, 2026-08-27)

- `render_page` (dashboard.py:1221) is the only HTML entry, but the per-section renderers it calls are already pure functions: `_sections_html`:1091, `_branches_html`:1124, `_telemetry_html`:1150, `_sync_html`:1185, `_bar_html`:1198 — fragment endpoints are a composition change, not a rewrite.
- `test_page_is_self_contained` (test_dashboard.py:866-878) forbids `<script src=` and any external href/src → htmx must be **inlined**, not CDN'd and not even same-origin `src`'d (unless the test is relaxed — we won't).
- Handler default is HTTP/1.0 (`Connection: close`) → SSE needs `protocol_version = "HTTP/1.1"` on the handler class; `_send` (body-first, fixed Content-Length) cannot serve SSE — the route needs its own write loop.
- Direct listeners (`#approve-selected`:904, `#approve-all`:910, `#sync`:933) and the load-time `baseMtime` capture (:753) break under innerHTML swaps; the delegated document-level listeners (:885-903) survive swaps fine.
- Consent scoping is DOM state: per-row `input[value=N]` + `data-blanket` ARE the numbers POSTed; every record renders twice (sections + branches) with checkbox twin-mirroring; filter state is inline `style.display` re-derived by `applyFilter()`.
- `_WRITE_LOCK` must never be held while streaming; tests drive real HTTP on port 0 via the `_Server` harness (test_dashboard.py:119-175, 5s client timeout — SSE tests need their own read handling).
- Dead constants `_REFRESH_SECONDS`:63, `_MTIME_POLL_SECONDS`:84 — delete in passing.

## Decision Log (decision · rationale · rejected alternative)

1. **htmx core only, vendored as a repo file, inlined at import.** `worksweep/static/htmx.min.js` (pinned release, version + sha256 recorded in a sibling `htmx.version` file), read ONCE at module import into a constant, emitted inline in the page `<head>`. Keeps `test_page_is_self_contained` intact verbatim, keeps dashboard.py readable, keeps the repo's no-CDN/no-npm discipline (a vendored asset is not a dependency). *Rejected:* CDN (external host, violates self-containment); `<script src="/static/...">` (needs the test relaxed for zero benefit); pasting 48KB into dashboard.py (unreadable, breaks the AST-based tests' file hygiene).
2. **Native `EventSource` + ~15 lines of glue, no htmx SSE extension.** The client opens `/events`; on a `queue` event it triggers the fragment refresh. EventSource auto-reconnects natively. *Rejected:* the sse extension (second vendored file to solve a problem 15 lines solve).
3. **One fragment endpoint, out-of-band swaps: `GET /fragments`** (CSRF-free read, like `/mtime`) returns one response containing the four dynamic regions — telemetry, sync button, `.sections` content, `.branches` content, bar — each tagged `hx-swap-oob`, so one round trip updates the whole page atomically. New pure composer `render_fragments(records, now, queue_mtime) -> str` reusing the existing per-section renderers verbatim; `render_page` calls the same composer so full page and fragments cannot drift. *Rejected:* per-section endpoints (N round trips, tearing between sections); replacing `<body>` (nukes `<html data-layout>`, filter state, scroll).
4. **Refresh triggers:** (a) SSE `queue` event; (b) immediately after any 200 from `/approve`, `/approve-all`, `/dismiss` (replacing today's `location.reload()` in `send()` — actions feel instant); (c) a degraded fallback: if `EventSource` errors terminally, fall back to the existing 10s mtime-poll loop BUT reloading fragments, not the page. The 5-min `tick()` full reload dies. *Rejected:* keeping the poll as primary (SSE is the point); no fallback (a proxy or browser quirk would silently freeze the page).
5. **Selection-aware deferred swap replaces the silent `busy()` hold.** On a refresh trigger with `selected().length > 0 || confirming || inflight`: do NOT swap; set a visible "queue changed — update pending" chip on the header; apply the deferred refresh the moment the selection clears (checkbox change → count 0), the confirm dialog closes, or the user clicks the chip. After EVERY swap: re-run `applyFilter(); refresh(); marks()` and re-apply checkbox state for values captured before the swap (checked numbers whose inputs still exist; vanished rows drop naturally — the server re-validates numbers on POST anyway, so consent enforcement stays server-side). *Rejected:* swap-under-selection with state re-application only (the row list visibly shifting under a half-built selection is the UX the busy guard rightly exists to prevent); today's silent stale hold (the bug that prompted this build).
6. **All direct listeners become delegated** (approve-selected, approve-all, sync join the document-level click handler; `syncDone` re-queries `#sync` instead of closing over it; `baseMtime` is deleted with the poll). Pure pre-htmx refactor, shipped first, zero behavior change. *Rejected:* htmx `hx-preserve` on the buttons (preserves DOM nodes but not the bound-once-listener architecture problem).
7. **SSE endpoint `GET /events`:** handler sets `protocol_version = "HTTP/1.1"` (class-wide — safe: `_send` always sets Content-Length); the route writes `text/event-stream` headers then loops: stat queue mtime every 1s (own stat, no shared broadcaster, no locks — thread-per-connection makes per-client polling correct and simple), on change emit `event: queue\ndata: {token}\n\n`, plus a `: heartbeat` comment every 15s so dead clients surface as write errors and the thread exits. Never touches `_WRITE_LOCK`. Server gains no new state. *Rejected:* a broadcaster registry with condition variables (shared mutable state + lock discipline for zero user-visible gain at ≤3 concurrent viewers); inotify/kqueue (non-portable, stdlib-awkward).
8. **CSRF/actions unchanged.** POSTs keep the `X-Worksweep` header via the existing `send()`; `/fragments` and `/events` are CSRF-free reads like `/mtime`; approve/dismiss/sweep handlers untouched.

## Scope / files

`worksweep/dashboard.py` (fragment composer, /fragments + /events routes, JS rework, listener delegation, dead-constant removal), `worksweep/static/htmx.min.js` + `worksweep/static/htmx.version` (new, vendored), `worksweep/tests/test_dashboard.py` (JS-string tests reworked for the new blocks; new fragment/SSE endpoint tests via the `_Server` harness with raw-read SSE handling), `etc/mini/` unchanged (same agent). Single repo, L2, ships via the personal-repo green pipeline.

## Acceptance Criteria (EARS)

- AC1: WHEN `queue.json`'s mtime changes while a client holds `/events` open, the server SHALL emit a `queue` SSE event within 2s, and the page SHALL apply updated fragments without a page reload (falsifying: `test_events_emits_on_mtime_change` fails if the stat loop or event framing is removed).
- AC2: WHEN `GET /fragments` is requested, the response SHALL contain the telemetry, sync, sections, branches, and bar regions each marked for out-of-band swap, byte-identical in content to the same regions of `render_page` for the same records (falsifying: `test_fragments_match_page_regions` fails if the composers drift).
- AC3: WHEN a refresh trigger fires while boxes are checked (or a confirm dialog is open), the page SHALL NOT swap, SHALL show the update-pending chip, and SHALL apply the deferred refresh when the selection count returns to zero (JS-source assertions pin the guard, chip, and drain path).
- AC4: WHEN any of `/approve`, `/approve-all`, `/dismiss` returns 200, the client SHALL request `/fragments` instead of calling `location.reload()` (falsifying: JS-source test fails if `location.reload` reappears in `send()`).
- AC5: The rendered page SHALL remain self-contained — `test_page_is_self_contained` passes UNCHANGED (no `<script src=`, no external refs) with htmx inlined from the vendored file, and the vendored file's sha256 SHALL match `htmx.version` (falsifying: `test_vendored_htmx_integrity`).
- AC6: WHEN the SSE connection errors terminally, the client SHALL fall back to a 10s fragment-refresh poll (never a page reload), and the 5-minute full-reload `tick()` SHALL be gone (falsifying: JS-source test fails if `tick()`/full-reload fallback survives).
- AC7: WHILE an SSE stream is open, the handler SHALL hold no queue lock and other routes SHALL remain responsive (test: concurrent `/approve` during an open stream completes within the harness timeout).
- AC8: WHEN fragments swap, checkbox selections for still-present rows SHALL survive, `applyFilter`/`refresh`/`marks` SHALL re-run, and the approve bar's disabled/hidden/count state SHALL be recomputed (JS-source assertions).

## Decision Coverage

| Decision | Covered by |
|---|---|
| 1. Vendored inline htmx | AC5 + integrity test |
| 2. Native EventSource glue | AC1/AC6 JS assertions |
| 3. One oob fragment endpoint | AC2 + composer-sharing test |
| 4. Trigger set incl. post-action refresh | AC4, AC1 |
| 5. Deferred swap + pending chip | AC3, AC8 |
| 6. Delegated listeners first | phase-1 refactor tests (existing suite green, JS-source updated) |
| 7. HTTP/1.1 + per-thread SSE loop | AC1, AC7 |
| 8. CSRF unchanged | existing CSRF tests untouched |

## Field Provenance

| Field | Value | Source |
|---|---|---|
| **Plan** | this file (gated decision log; `@plan-author` authors the per-issue plan from it) | plan-mode session 2026-08-27 |
| **Files** | dashboard.py, static/htmx.min.js + htmx.version (new), tests/test_dashboard.py | explorer structural map, this session (line-precise) |
| **AC** | AC1–AC8 | decisions 1–8 |
| **TDD Mode** | Full TDD (feature), phase 1 = Test-After refactor | /do work-type table |
| **Owning layer** | dashboard.py owns all rendering + serving; the per-section renderers (:1091,:1124,:1150,:1185,:1198) are the reused seam; no other module changes | explorer map |
| **Downstream consumers** | launchd agent (unchanged invocation); rsync deploy must include worksweep/static/; the JS-string tests in test_dashboard.py:1469-2212 are the churn surface | explorer map §5 |
| **Sibling pattern** | `/mtime` route (CSRF-free read, :1527) for /fragments; `_Server` harness (test:119-175) for endpoint tests; `_HEAD_SCRIPT`/`_BODY_SCRIPT` constants for the JS blocks | explorer map |
| **Verify** | full suite local + mini; live: two browsers on the tailnet, ✅ an item in one, watch the other update in <2s with a selection held in a third view | Verification |
| **Plan provenance** | unhardened (claude-solo session) | mode banner |

## Critique Pass

- *Sharpest risk:* the JS rework churns the dashboard's largest hand-written JS surface and its string-pinned tests simultaneously — a subtly wrong `busy()`/defer interaction could hold updates forever (the original bug, reborn). Mitigation: AC3's drain path is pinned three ways (selection-clear, confirm-close, chip click), and the deferred chip makes a stuck hold *visible* instead of silent.
- *SSE thread lifecycle:* `shutdown()` won't interrupt a blocked stream write; heartbeats bound the zombie window to ~15s and `daemon_threads` covers process exit. Test harness closes clients before servers.
- *htmx is 48KB inlined* — pages are served over tailnet from the mini; size is irrelevant, but the constant is read at import so a missing static file must fail LOUDLY at startup, not render a broken page (import-time assertion).
- *What this does NOT do:* no view/routing changes, no personal-estate contexts yet (this build is the platform for them), no Alpine (first local-state widget decides that), no auth changes.

## Verification

1. Suite green locally + mini (1211 + new).
2. Deploy: rsync worksweep/ (now incl. static/) to the mini, restart dashboard agent.
3. Live: open dashboard in two tabs; kick a sweep; both update in place ≤2s. Check a box in tab A, kick a sweep → A shows pending chip and holds; uncheck → A drains. ✅ an item → fragments refresh without reload. Kill/restart the dashboard agent → tabs reconnect via EventSource retry.

## Round 2 amendment (orchestrator, resolving plan-author deferrals)

- Decision 3's "four dynamic regions" is corrected to **five** (telemetry, sync, sections, branches, bar) — both enumerations already agreed; the count word was wrong.
- The five stable-id containers present in every queue state (incl. empty) are CONFIRMED as part of Decision 3 — the empty-queue OOB-target gap the plan derived (AC 13) is real and binding.
- `lastMtime` as a script-scoped variable reassigned on every /mtime response and after every swap: confirmed.
- Deploy: the existing `rsync -a --delete worksweep/` deploy already carries `worksweep/static/`; the import-time loud failure (AC 9) is the guard if it ever doesn't.
