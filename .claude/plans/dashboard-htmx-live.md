## Issue: (none — freeform personal-repo work) — Worksweep dashboard: htmx live updates (fragment swaps + SSE)
## Branch: feat/dashboard-htmx-live
## Repo: heartbeat (`/Users/chandlerhardy/repos/heartbeat`)
## Status: READY
## Source: decision-log

## Decision Log (gated)

### Gated in plan mode on 2026-08-27 (user-approved)

Attestation verified: `<!-- GATED: ExitPlanMode approved -->` at `.planning/decisions/dashboard-htmx-live.md:1`.
Quoted bit-exact below; `decision-verify` rows in `## Field Provenance` record the full-range diffs.

#### Verified constraints (`.planning/decisions/dashboard-htmx-live.md:10-16`)

```decision
- `render_page` (dashboard.py:1221) is the only HTML entry, but the per-section renderers it calls are already pure functions: `_sections_html`:1091, `_branches_html`:1124, `_telemetry_html`:1150, `_sync_html`:1185, `_bar_html`:1198 — fragment endpoints are a composition change, not a rewrite.
- `test_page_is_self_contained` (test_dashboard.py:866-878) forbids `<script src=` and any external href/src → htmx must be **inlined**, not CDN'd and not even same-origin `src`'d (unless the test is relaxed — we won't).
- Handler default is HTTP/1.0 (`Connection: close`) → SSE needs `protocol_version = "HTTP/1.1"` on the handler class; `_send` (body-first, fixed Content-Length) cannot serve SSE — the route needs its own write loop.
- Direct listeners (`#approve-selected`:904, `#approve-all`:910, `#sync`:933) and the load-time `baseMtime` capture (:753) break under innerHTML swaps; the delegated document-level listeners (:885-903) survive swaps fine.
- Consent scoping is DOM state: per-row `input[value=N]` + `data-blanket` ARE the numbers POSTed; every record renders twice (sections + branches) with checkbox twin-mirroring; filter state is inline `style.display` re-derived by `applyFilter()`.
- `_WRITE_LOCK` must never be held while streaming; tests drive real HTTP on port 0 via the `_Server` harness (test_dashboard.py:119-175, 5s client timeout — SSE tests need their own read handling).
- Dead constants `_REFRESH_SECONDS`:63, `_MTIME_POLL_SECONDS`:84 — delete in passing.
```

#### Decision Log rows 1-8 (`.planning/decisions/dashboard-htmx-live.md:20-27`)

```decision
1. **htmx core only, vendored as a repo file, inlined at import.** `worksweep/static/htmx.min.js` (pinned release, version + sha256 recorded in a sibling `htmx.version` file), read ONCE at module import into a constant, emitted inline in the page `<head>`. Keeps `test_page_is_self_contained` intact verbatim, keeps dashboard.py readable, keeps the repo's no-CDN/no-npm discipline (a vendored asset is not a dependency). *Rejected:* CDN (external host, violates self-containment); `<script src="/static/...">` (needs the test relaxed for zero benefit); pasting 48KB into dashboard.py (unreadable, breaks the AST-based tests' file hygiene).
2. **Native `EventSource` + ~15 lines of glue, no htmx SSE extension.** The client opens `/events`; on a `queue` event it triggers the fragment refresh. EventSource auto-reconnects natively. *Rejected:* the sse extension (second vendored file to solve a problem 15 lines solve).
3. **One fragment endpoint, out-of-band swaps: `GET /fragments`** (CSRF-free read, like `/mtime`) returns one response containing the four dynamic regions — telemetry, sync button, `.sections` content, `.branches` content, bar — each tagged `hx-swap-oob`, so one round trip updates the whole page atomically. New pure composer `render_fragments(records, now, queue_mtime) -> str` reusing the existing per-section renderers verbatim; `render_page` calls the same composer so full page and fragments cannot drift. *Rejected:* per-section endpoints (N round trips, tearing between sections); replacing `<body>` (nukes `<html data-layout>`, filter state, scroll).
4. **Refresh triggers:** (a) SSE `queue` event; (b) immediately after any 200 from `/approve`, `/approve-all`, `/dismiss` (replacing today's `location.reload()` in `send()` — actions feel instant); (c) a degraded fallback: if `EventSource` errors terminally, fall back to the existing 10s mtime-poll loop BUT reloading fragments, not the page. The 5-min `tick()` full reload dies. *Rejected:* keeping the poll as primary (SSE is the point); no fallback (a proxy or browser quirk would silently freeze the page).
5. **Selection-aware deferred swap replaces the silent `busy()` hold.** On a refresh trigger with `selected().length > 0 || confirming || inflight`: do NOT swap; set a visible "queue changed — update pending" chip on the header; apply the deferred refresh the moment the selection clears (checkbox change → count 0), the confirm dialog closes, or the user clicks the chip. After EVERY swap: re-run `applyFilter(); refresh(); marks()` and re-apply checkbox state for values captured before the swap (checked numbers whose inputs still exist; vanished rows drop naturally — the server re-validates numbers on POST anyway, so consent enforcement stays server-side). *Rejected:* swap-under-selection with state re-application only (the row list visibly shifting under a half-built selection is the UX the busy guard rightly exists to prevent); today's silent stale hold (the bug that prompted this build).
6. **All direct listeners become delegated** (approve-selected, approve-all, sync join the document-level click handler; `syncDone` re-queries `#sync` instead of closing over it; `baseMtime` is deleted with the poll). Pure pre-htmx refactor, shipped first, zero behavior change. *Rejected:* htmx `hx-preserve` on the buttons (preserves DOM nodes but not the bound-once-listener architecture problem).
7. **SSE endpoint `GET /events`:** handler sets `protocol_version = "HTTP/1.1"` (class-wide — safe: `_send` always sets Content-Length); the route writes `text/event-stream` headers then loops: stat queue mtime every 1s (own stat, no shared broadcaster, no locks — thread-per-connection makes per-client polling correct and simple), on change emit `event: queue\ndata: {token}\n\n`, plus a `: heartbeat` comment every 15s so dead clients surface as write errors and the thread exits. Never touches `_WRITE_LOCK`. Server gains no new state. *Rejected:* a broadcaster registry with condition variables (shared mutable state + lock discipline for zero user-visible gain at ≤3 concurrent viewers); inotify/kqueue (non-portable, stdlib-awkward).
8. **CSRF/actions unchanged.** POSTs keep the `X-Worksweep` header via the existing `send()`; `/fragments` and `/events` are CSRF-free reads like `/mtime`; approve/dismiss/sweep handlers untouched.
```

#### Scope / files (`.planning/decisions/dashboard-htmx-live.md:31`)

```decision
`worksweep/dashboard.py` (fragment composer, /fragments + /events routes, JS rework, listener delegation, dead-constant removal), `worksweep/static/htmx.min.js` + `worksweep/static/htmx.version` (new, vendored), `worksweep/tests/test_dashboard.py` (JS-string tests reworked for the new blocks; new fragment/SSE endpoint tests via the `_Server` harness with raw-read SSE handling), `etc/mini/` unchanged (same agent). Single repo, L2, ships via the personal-repo green pipeline.
```

#### Acceptance Criteria AC1-AC8 (`.planning/decisions/dashboard-htmx-live.md:35-42`)

```decision
- AC1: WHEN `queue.json`'s mtime changes while a client holds `/events` open, the server SHALL emit a `queue` SSE event within 2s, and the page SHALL apply updated fragments without a page reload (falsifying: `test_events_emits_on_mtime_change` fails if the stat loop or event framing is removed).
- AC2: WHEN `GET /fragments` is requested, the response SHALL contain the telemetry, sync, sections, branches, and bar regions each marked for out-of-band swap, byte-identical in content to the same regions of `render_page` for the same records (falsifying: `test_fragments_match_page_regions` fails if the composers drift).
- AC3: WHEN a refresh trigger fires while boxes are checked (or a confirm dialog is open), the page SHALL NOT swap, SHALL show the update-pending chip, and SHALL apply the deferred refresh when the selection count returns to zero (JS-source assertions pin the guard, chip, and drain path).
- AC4: WHEN any of `/approve`, `/approve-all`, `/dismiss` returns 200, the client SHALL request `/fragments` instead of calling `location.reload()` (falsifying: JS-source test fails if `location.reload` reappears in `send()`).
- AC5: The rendered page SHALL remain self-contained — `test_page_is_self_contained` passes UNCHANGED (no `<script src=`, no external refs) with htmx inlined from the vendored file, and the vendored file's sha256 SHALL match `htmx.version` (falsifying: `test_vendored_htmx_integrity`).
- AC6: WHEN the SSE connection errors terminally, the client SHALL fall back to a 10s fragment-refresh poll (never a page reload), and the 5-minute full-reload `tick()` SHALL be gone (falsifying: JS-source test fails if `tick()`/full-reload fallback survives).
- AC7: WHILE an SSE stream is open, the handler SHALL hold no queue lock and other routes SHALL remain responsive (test: concurrent `/approve` during an open stream completes within the harness timeout).
- AC8: WHEN fragments swap, checkbox selections for still-present rows SHALL survive, `applyFilter`/`refresh`/`marks` SHALL re-run, and the approve bar's disabled/hidden/count state SHALL be recomputed (JS-source assertions).
```

## Sibling Patterns

### `/mtime` — the CSRF-free read route `/fragments` and `/events` are modelled on (`worksweep/dashboard.py:1527-1532`)

```sibling
        if path == "/mtime":
            # Read-only and side-effect free, so no CSRF guard: it leaks only
            # "when did the queue last change", which the page already shows.
            # The Sync flow polls this to know when a kicked sweep has landed.
            self._text(200, mtime_token(_queue_mtime(self.server.queue_path)))
            return
```

Both new GET routes sit in this same `do_GET` chain, above the `if path != "/"` 404 guard, and carry the identical no-CSRF rationale (read-only, leaks only "when did the queue last change"). `/fragments` uses `_send`; `/events` does NOT (see Tricky Parts).

### `_Server.request` — the harness the SSE helper must NOT reuse (`worksweep/tests/test_dashboard.py:133-140`)

```sibling
    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()
```

`conn.getresponse()` + `r.read()` block until the response body is complete. An SSE stream never completes, so this helper deadlocks until the fixed 5s timeout and then raises. The new `_Server.stream()` helper (Phase 4) is the raw-socket counterpart; its shape is specified in `### Tricky Parts`.

### Script-block scoping already used once in the suite (`worksweep/tests/test_dashboard.py:2074-2078`)

```sibling
    # satisfy this on their own.
    body = _page([_rec(1)])
    script = body[body.rindex("<script>"):]
    assert re.search(r"^  setTimeout\(poll,POLL_MS\);$", script, re.M)
    assert re.search(r"^  setTimeout\(tick,FALLBACK_MS\);$", script, re.M)
```

`body[body.rindex("<script>"):]` is the in-repo precedent for scoping a JS assertion to the last script block. Phase 2 generalises exactly this line into a `_script(page)` helper and re-points every whole-page JS assertion at it. This is the single most load-bearing test-infrastructure change in the plan (see `### What I Discovered`).

## Source Notes — verification of the gated map, and one enumeration discrepancy

Every line-precise claim in the gated log's `## Verified constraints` was re-verified on disk this session. All of them hold:

| Gated claim | Re-verified | Result |
|---|---|---|
| `render_page`:1221, `_sections_html`:1091, `_branches_html`:1124, `_telemetry_html`:1150, `_sync_html`:1185, `_bar_html`:1198 | `grep -n '^def ' worksweep/dashboard.py` | exact, all six |
| `test_page_is_self_contained` at test:866-878 forbids `<script src=` and external refs | read 866-878 | exact; five assertion families (see AC-P2.1) |
| Handler default is HTTP/1.0; no `protocol_version` set | `grep -n protocol_version worksweep/dashboard.py` → 0 hits | confirmed |
| `_send` is body-first with fixed Content-Length | read dashboard.py:1466-1480 | confirmed — cannot serve SSE |
| Direct listeners `#approve-selected`:904, `#approve-all`:910, `#sync`:933; `baseMtime`:753; delegated listeners :885-903 | `grep -n addEventListener worksweep/dashboard.py` | all five line numbers exact |
| Dead constants `_REFRESH_SECONDS`:63, `_MTIME_POLL_SECONDS`:84 | repo-wide `grep -rn --include=*.py` | one hit each (the definition itself) — genuinely dead |
| `_Server` harness at test:119-175 with a 5s client timeout | read 119-175 | exact |
| htmx 2.0.7 vendored, sha256 in `htmx.version` | `shasum -a 256` vs `htmx.version` | matches `60231ae6ba9db3825eb15a261122d5f55921c4d53b66bf637dc18b4ee27c79f9`; 51076 bytes; commit `4d4ca2a` |
| Baseline suite green | `python3 -m pytest worksweep/tests/ -q` | `1211 passed in 3.73s` |

**Enumeration discrepancy (resolved, not a HALT).** Decision 3 opens "returns one response containing the **four** dynamic regions" and then enumerates **five**: "telemetry, sync button, `.sections` content, `.branches` content, bar". AC2 independently enumerates the same five: "the telemetry, sync, sections, branches, and bar regions". Two enumerations agree; only the count word disagrees, and no reading of "four" selects which region to drop. The plan implements **five** regions. This is not a source-internal contradiction under Step 6 Case A (no branch point is left undecided), so the plan ships `READY`; the log's count word is listed under `Decisions deferred to orchestrator` as a one-word amendment.

## Field Provenance

| Plan field | Derived from (gated decision row) | Notes |
|---|---|---|
| **Plan** `/Users/chandlerhardy/repos/heartbeat/.claude/plans/dashboard-htmx-live.md` | orchestrator prompt `output_dir` | not source-derived (orchestrator input) |
| **Files** entry 1 `worksweep/dashboard.py` | Scope/files (`:31`) "fragment composer, /fragments + /events routes, JS rework, listener delegation, dead-constant removal" | direct cite |
| **Files** entry 2 `worksweep/static/htmx.min.js` + `htmx.version` | D-log row 1 "vendored as a repo file, inlined at import" | already on disk at commit `4d4ca2a`; plan CONSUMES, does not fetch |
| **Files** entry 3 `worksweep/tests/test_dashboard.py` | Scope/files (`:31`) "JS-string tests reworked for the new blocks; new fragment/SSE endpoint tests via the `_Server` harness with raw-read SSE handling" | direct cite |
| **AC #P1.1-P1.4** listener delegation, dead constants | D-log row 6 (all direct listeners delegated; `syncDone` re-queries) + Verified constraints `:16` (dead constants) | row 6's "Pure pre-htmx refactor, shipped first" fixes the phase order |
| **AC #P2.1-P2.2** self-containment + sha256 integrity | AC5 (`:39`) + D-log row 1 | AC5 names `test_vendored_htmx_integrity` explicitly |
| **AC #P2.3** import-time loud failure | Critique Pass (`:76`) "a missing static file must fail LOUDLY at startup, not render a broken page (import-time assertion)" | the gated log's own critique is binding source |
| **AC #P2.4-P2.5** `_script()` scoping helper + head ordering | AC5 (`:39`) "with htmx inlined" ∧ Verified constraints `:11` (page-string tests) — see `### What I Discovered` for the forcing collision | derived consequence, not invention: inlining htmx makes 4 existing whole-page assertions false-positive |
| **AC #P3.1** `/fragments` five oob regions, byte-identical content | D-log row 3 + AC2 (`:36`) | five regions per the two agreeing enumerations |
| **AC #P3.2** stable-id containers in every queue state | D-log row 3 ("out-of-band swaps") ∧ `render_page`:1258-1268 empty-queue branch | derived: an oob swap needs a target that exists in both the empty and non-empty page |
| **AC #P3.3** `/fragments` replaces `location.reload()` in `send()` | D-log row 4(b) + AC4 (`:38`) | direct cite |
| **AC #P3.4** deferred swap + pending chip + three drain paths | D-log row 5 + AC3 (`:37`) | drain paths enumerated verbatim from row 5 |
| **AC #P3.5** post-swap state re-application | D-log row 5 ("re-run `applyFilter(); refresh(); marks()`") + AC8 (`:42`) | direct cite |
| **AC #P3.6** `tick()` and `location.reload()` gone; poll refreshes fragments | D-log row 4(c) ("The 5-min `tick()` full reload dies") + AC6 (`:40`) | direct cite |
| **AC #P3.7** `/fragments` render failure is a 500, never a crash | `render_page`'s existing contract at dashboard.py:1431-1436 ("Nothing in here may raise: the launchd agent is KeepAlive") + do_GET:1541-1544 | pre-existing invariant the new route inherits |
| **AC #P4.1-P4.2** SSE event framing + heartbeat | D-log row 7 (`event: queue\ndata: {token}\n\n`; `: heartbeat` every 15s) + AC1 (`:35`) | framing quoted verbatim from row 7 |
| **AC #P4.3** no lock held while streaming | D-log row 7 ("Never touches `_WRITE_LOCK`") + Verified constraints `:15` + AC7 (`:41`) | direct cite |
| **AC #P4.4** `protocol_version = "HTTP/1.1"` | D-log row 7 ("class-wide — safe: `_send` always sets Content-Length") | direct cite; `_send`:1472 verified to always set it |
| **AC #P4.5** terminal-error fallback to fragment poll | D-log row 4(c) + AC6 (`:40`) | direct cite |
| **AC #P4.6** dead client surfaces as a write error, thread exits | D-log row 7 ("so dead clients surface as write errors and the thread exits") | direct cite |
| **TDD Mode** `Full TDD` (Phase 1 = `Test-After`) | gated log Field Provenance (`:63`) "Full TDD (feature), phase 1 = Test-After refactor" | source-stated; matches testing-philosophy work-type split |
| **Owning layer** `worksweep/dashboard.py` (render + serve) | gated log Field Provenance (`:64`) "dashboard.py owns all rendering + serving; the per-section renderers (:1091,:1124,:1150,:1185,:1198) are the reused seam; no other module changes" | verified: no other module imports the renderers |
| **Downstream consumers** launchd agent; rsync deploy; JS-string tests test:1469-2212 | gated log Field Provenance (`:65`) | enumerated per-phase in `**Downstream consumers:**` below |
| **Sibling pattern** `/mtime`:1527, `_Server`:119-175, `_HEAD_SCRIPT`/`_BODY_SCRIPT` | gated log Field Provenance (`:66`) | all three quoted into `## Sibling Patterns` |
| **Verify** `python3 -m pytest worksweep/tests/ -q` | detected stack convention (stdlib-only Python 3, pytest) + gated log Verification (`:80`) | not source-derived (stack convention) |
| **Plan provenance** `unhardened` | gated log Field Provenance (`:69`) "unhardened (claude-solo session)" | no plan-checkpoint hook run in this repo |
| **decision-verify** Verified constraints (`:10-16`, 7 lines) | `diff` against source-range: clean (0 bytes) | full-range diff verified — gated decisions quoted bit-exact |
| **decision-verify** Decision Log rows 1-8 (`:20-27`, 8 rows) | `diff` against source-range: clean (0 bytes) | full-range diff verified |
| **decision-verify** Scope / files (`:31`, 1 line) | `diff` against source-range: clean (0 bytes) | full-range diff verified |
| **decision-verify** Acceptance Criteria (`:35-42`, 8 rows) | `diff` against source-range: clean (0 bytes) | full-range diff verified |
| **diff-against-current** `_BODY_SCRIPT` (dashboard.py:747-953 vs D-log rows 4/5/6) | 6 deltas: (1) direct listeners :904,:910,:933 → delegated (AC-P1.1); (2) `syncDone` closure → re-query (AC-P1.2); (3) `baseMtime`:753 load-time capture → script-scoped `lastMtime` (AC-P3.6); (4) `poll()`:863-873 `location.reload()` → fragment refresh (AC-P3.6); (5) `tick()`:879-882 deleted (AC-P3.6); (6) `send()`:855 `location.reload()` → fragment refresh (AC-P3.3) | all six mapped to an AC; none dropped |
| **diff-against-current** `render_page` (dashboard.py:1221-1281 vs D-log row 3) | 3 deltas: (1) htmx `<script>` added to `<head>` after `_HEAD_SCRIPT` (AC-P2.5); (2) five regions wrapped in stable-id containers (AC-P3.2); (3) region bodies moved into the shared `render_fragments` composer (AC-P3.1) | all three mapped to an AC |
| **diff-against-current** `DashboardHandler` (dashboard.py:1431-1545 vs D-log row 7) | 2 deltas: (1) `protocol_version = "HTTP/1.1"` class attribute added (AC-P4.4); (2) two new GET routes above the `path != "/"` guard (AC-P3.1, AC-P4.1) | both mapped |
| **impact-trace** `_BODY_SCRIPT` string assertions | 8 call sites in test_dashboard.py, each Read-verified: :1390-1399 (unaffected — `refresh()` already re-queries by id), :1469-1478 (Phase 3 — `tick`/`location.reload` assertions die), :1762-1773 (**Phase 1 — line 1767 only**), :2043-2056 (unaffected if new click branches are appended), :2058-2062 (unaffected), :2066-2081 (Phase 3 — `FALLBACK_MS`/`tick` assertions die; the `rindex("<script>")` idiom is promoted to `_script()`), :2082-2087 (Phase 3 — the `baseMtime` assertion dies), :2190-2200 (unaffected — `refresh()` internals are stable; `marks();applyFilter();refresh();` string is preserved by AC-P3.5) | exhaustive `grep -n` over `getElementById\|addEventListener\|syncDone\|location.reload\|tick\|baseMtime\|POLL_MS\|FALLBACK_MS` in the test file, each hit Read |
| **impact-trace** whole-page string assertions vs inlined htmx | 4 collisions, each confirmed by `grep -cF` against `worksweep/static/htmx.min.js`: `pushState` (1 hit), `location.search` (3), `location.href` (1), `location.reload` (2). All four are asserted `not in page` at test:915-918 or are needed by AC4's falsifying assertion. Fix = AC-P2.4 `_script()` scoping. Clean (0 hits): `<script src=`, `<link`, `src="`, `@import`, `http-equiv`, `localStorage`, `</script` — so **AC5's "passes UNCHANGED" is achievable** | exhaustive collision sweep, Read-verified in context |
| **test-surface** new tests required | Phase 1: `test_all_click_handlers_are_delegated`, `test_sync_done_requeries_the_button`, `test_dead_refresh_constants_are_gone`. Phase 2: `test_vendored_htmx_integrity`, `test_htmx_is_inlined_not_referenced`, `test_missing_htmx_asset_fails_at_import`, `test_js_assertions_are_scoped_below_htmx`. Phase 3: `test_fragments_match_page_regions`, `test_fragment_targets_exist_in_every_state`, `test_fragments_needs_no_csrf_header`, `test_fragments_is_get_only`, `test_fragment_render_failure_is_a_500`, `test_actions_refresh_fragments_not_the_page`, `test_deferred_swap_holds_and_drains`, `test_post_swap_reapplies_selection_and_filters`. Phase 4: `test_events_emits_on_mtime_change`, `test_events_sends_heartbeat_comments`, `test_events_holds_no_write_lock`, `test_approve_during_open_stream_completes`, `test_http11_responses_carry_content_length`, `test_sse_error_falls_back_to_fragment_poll` | 22 new tests; every AC below names its own falsifier |

## Decision Coverage

| Gated decision | Implementing task (AC #) | Verification |
|---|---|---|
| Row 1: htmx core only, vendored, read once at import, inlined in `<head>` | Phase 2 — AC-P2.1, AC-P2.2, AC-P2.3, AC-P2.5 | `test_page_is_self_contained` passes unchanged; `test_vendored_htmx_integrity` recomputes sha256 and compares to `htmx.version`; `test_missing_htmx_asset_fails_at_import` reimports with the asset renamed and asserts the raise |
| Row 2: native `EventSource` + ~15 lines of glue, no htmx SSE extension | Phase 4 — AC-P4.1, AC-P4.5 | JS-source assertion `new EventSource('/events')` present in `_script(page)` and no `htmx.min.js` sibling extension file exists in `worksweep/static/` |
| Row 3: one `GET /fragments` endpoint, five oob regions, shared composer | Phase 3 — AC-P3.1, AC-P3.2 | `test_fragments_match_page_regions` byte-compares each of the five regions between `render_fragments` and `render_page` for the same `(records, now, mtime)`; fails if either composer drifts |
| Row 4(a): SSE `queue` event triggers refresh | Phase 4 — AC-P4.1 | `test_events_emits_on_mtime_change` (raw-socket read, ≤2s) |
| Row 4(b): refresh immediately after any 200 from `/approve`, `/approve-all`, `/dismiss` | Phase 3 — AC-P3.3 | `test_actions_refresh_fragments_not_the_page`: `"location.reload" not in _script(page)` AND the 200 branch calls the refresh |
| Row 4(c): degraded fallback = 10s poll reloading fragments, not the page; `tick()` dies | Phase 3 (poll→fragments, `tick()` deleted) + Phase 4 (demoted to error-only) — AC-P3.6, AC-P4.5 | `test_sse_error_falls_back_to_fragment_poll` pins the `onerror` handler arming the poll; `assert "tick" not in _script(page)` |
| Row 5: selection-aware deferred swap + visible pending chip + three drain paths + post-swap state re-application | Phase 3 — AC-P3.4, AC-P3.5 | `test_deferred_swap_holds_and_drains` asserts the guard, the chip toggle, and all three drain paths independently; `test_post_swap_reapplies_selection_and_filters` pins the `htmx:afterSwap` body |
| Row 6: all direct listeners delegated; `syncDone` re-queries `#sync`; `baseMtime` deleted with the poll | Phase 1 (delegation, shipped first) + Phase 3 (`baseMtime`) — AC-P1.1, AC-P1.2, AC-P1.3, AC-P3.6 | `test_all_click_handlers_are_delegated` asserts `_script(page).count("addEventListener(") == 2`; `test_sync_done_requeries_the_button` |
| Row 7: `GET /events`, `protocol_version = "HTTP/1.1"`, own write loop, 1s stat, 15s heartbeat, no locks, no server state | Phase 4 — AC-P4.1, AC-P4.2, AC-P4.3, AC-P4.4, AC-P4.6 | `test_events_holds_no_write_lock` + `test_approve_during_open_stream_completes` + `test_http11_responses_carry_content_length` |
| Row 8: CSRF/actions unchanged | Phases 3-4 — AC-P3.1, AC-P4.1 (guard placement) | `test_fragments_needs_no_csrf_header` mirrors `test_mtime_needs_no_csrf_header`:1716; the existing CSRF suite (`test_post_sweep_without_the_custom_header_is_403`:1591 and siblings) runs unmodified |
| Verified constraints `:16` — dead constants `_REFRESH_SECONDS`, `_MTIME_POLL_SECONDS` | Phase 1 — AC-P1.4 | `test_dead_refresh_constants_are_gone`: `assert not hasattr(dashboard, "_REFRESH_SECONDS")` |
| Critique Pass `:76` — missing static file must fail LOUDLY at import | Phase 2 — AC-P2.3 | `test_missing_htmx_asset_fails_at_import` |
| Verification `:80-82` — live two-browser check | Post-merge manual gate (see `**Verify:**`) | not a unit test; recorded as the deploy-time acceptance step |

## Critique Pass

| Dimension | Result |
|---|---|
| Completeness | weakness: Decision 3's shared-composer requirement gave no answer for the empty-queue page, where `render_page`:1258-1268 emits a `.clear` div instead of `.sections`/`.branches` and `_bar_html` is replaced by `""` — an oob swap would have had no target on the non-empty→empty transition → fix applied: AC-P3.2 mandates five stable-id containers present in every queue state, with the empty-state content rendered *inside* `#sections`; the derivation is cited in `## Field Provenance` and the transition is flagged in `### Tricky Parts` |
| DAG order | clean — Phase 1 (pure refactor) → Phase 2 (inline asset + test scoping) → Phase 3 (fragments) → Phase 4 (SSE). Phase 2 must land before Phase 3's `location.reload` falsifier, because that assertion is only sound once `_script()` scoping exists (htmx itself contains `location.reload` twice). No AC depends on a later phase |
| Pre/postconditions (EARS) | clean — 22 ACs, each a single trigger-clause + `SHALL` response with a named falsifier |
| Failure path | clean — six unwanted-behavior rows: AC-P1.3 (missing `#sync`), AC-P2.2 (sha256 mismatch), AC-P2.3 (missing asset at import), AC-P3.7 (`/fragments` render failure), AC-P4.5 (terminal SSE error), AC-P4.6 (client disconnect mid-stream) |
| Concrete layer-map | clean — one owning layer, `worksweep/dashboard.py`, with the reused seam pinned at `:1091`, `:1124`, `:1150`, `:1185`, `:1198`; no other module changes |
| Reversibility | weakness: `protocol_version = "HTTP/1.1"` is class-wide and changes connection semantics for every route at once, and the 51KB inline asset changes every page byte — neither is a one-way door but both are wide blast radii → fix applied: AC-P4.4 adds a Content-Length regression test across four route shapes, and Phase 2 ships the inline asset alone so a bisect isolates it. Genuinely one-way doors: none (no schema, no migration, no message routing) |

<!-- spawn-contract Diagnostic Fields -->

**Plan:** `/Users/chandlerhardy/repos/heartbeat/.claude/plans/dashboard-htmx-live.md` — READ-FIRST

**Files:**
- `worksweep/dashboard.py` (EXTEND) — dead-constant removal + listener delegation (P1); htmx inline constant read at import (P2); `render_fragments` composer, stable-id containers, `GET /fragments`, JS swap glue (P3); `GET /events`, `protocol_version = "HTTP/1.1"`, `EventSource` glue (P4)
- `worksweep/tests/test_dashboard.py` (EXTEND) — 22 new tests; 6 existing assertions edited across P1/P3; `_script(page)` helper added in P2 and every JS-source assertion re-pointed at it
- `worksweep/static/htmx.min.js` (REFERENCE) — 51076 bytes, htmx 2.0.7, already vendored at commit `4d4ca2a`; read once at import, never fetched
- `worksweep/static/htmx.version` (REFERENCE) — pin record; `sha256 60231ae6ba9db3825eb15a261122d5f55921c4d53b66bf637dc18b4ee27c79f9` is the value `test_vendored_htmx_integrity` compares against

**AC:**  (EARS — each line falsifiable; precondition = trigger clause, postcondition = SHALL response. Phases are strictly ordered; the suite must be green at every phase boundary.)

*Phase 1 — listener delegation + dead constants (pure refactor, zero behavior change, no htmx)*

1. THE `_BODY_SCRIPT` block SHALL bind exactly two listeners, both on `document` (one `click`, one `change`), and SHALL contain no `getElementById(...).addEventListener` binding. **Falsifying test:** `test_all_click_handlers_are_delegated` asserts `js.count("addEventListener(") == 2`; re-introducing any direct binding makes it 3 and fails.
2. WHEN a click reaches the delegated `document` handler THE dashboard client SHALL dispatch approve-selected, approve-all and Sync through `e.target.closest('#approve-selected')`, `closest('#approve-all')` and `closest('#sync')` branches appended below the existing `[data-set-layout]`, `[data-filter]` and `[data-dismiss]` branches. **Falsifying test:** the three new `closest` strings are asserted present AND `test_filter_toggle_logic_is_emitted`:2043 and `test_dismiss_button_is_wired_in_the_page`:2058 still pass unmodified, proving the existing branches were appended to and not restructured.
3. WHEN `syncDone(label)` runs THE dashboard client SHALL resolve the button by a fresh `document.getElementById('sync')` inside `syncDone`, not from a variable captured at load time. **Falsifying test:** `test_sync_done_requeries_the_button` asserts the re-query is inside the `syncDone` body.
4. IF `#sync` is absent from the DOM when a Sync click is delegated THEN THE dashboard client SHALL return without throwing and SHALL NOT issue a `POST /sweep`. **Falsifying test:** the guard string is asserted present; deleting it fails the test.
5. THE `worksweep.dashboard` module SHALL define neither `_REFRESH_SECONDS` nor `_MTIME_POLL_SECONDS`. **Falsifying test:** `test_dead_refresh_constants_are_gone` asserts `not hasattr(dashboard, "_REFRESH_SECONDS")` and the same for `_MTIME_POLL_SECONDS`.
6. THE Phase 1 commit SHALL leave the suite green having edited exactly one pre-existing assertion — `worksweep/tests/test_dashboard.py:1767` (`"sync.disabled=true;sync.textContent='syncing…';"`), which becomes the re-queried-local form. **Falsifying test:** `git diff --stat` on the test file plus a full-suite run; any second pre-existing assertion touched in Phase 1 means the refactor was not behavior-neutral.

*Phase 2 — vendored htmx inlined + JS-assertion scoping*

7. THE rendered page SHALL pass `test_page_is_self_contained` (test:866-878) **unchanged** with htmx inlined — no `<script src=`, no `<link`, no `src="http`, no `@import`, and the only http-scheme `href` values remaining are the queue's own `<a>` links. **Falsifying test:** the existing test, run byte-identical; a collision sweep already confirms `htmx.min.js` contains zero hits for all five assertion families and zero `</script` sequences.
8. IF the sha256 of `worksweep/static/htmx.min.js` does not equal the `sha256` field of `worksweep/static/htmx.version` THEN `test_vendored_htmx_integrity` SHALL fail. Expected value: `60231ae6ba9db3825eb15a261122d5f55921c4d53b66bf637dc18b4ee27c79f9`. **Falsifying test:** the test itself — mutating one byte of either file fails it.
9. IF `worksweep/static/htmx.min.js` is missing or empty when `worksweep.dashboard` is imported THEN THE import SHALL raise, rather than yielding a module that renders a page without htmx. **Falsifying test:** `test_missing_htmx_asset_fails_at_import` reimports the module with the asset path monkeypatched to a nonexistent file and asserts the raise.
10. THE htmx `<script>` element SHALL be emitted in `<head>` **after** `_HEAD_SCRIPT`, so the 400-character lookback in `test_layout_is_restored_from_localstorage_before_the_first_section`:906 stays inside `_HEAD_SCRIPT`. **Falsifying test:** that existing test (minus its `setTimeout(tick,` line, which Phase 3 removes) plus an explicit ordering assertion `page.index("localStorage.getItem") < page.index("htmx")`.
11. THE test module SHALL expose `_script(page)` returning only the final `<script>` block, and every JS-source assertion SHALL run against it rather than against the whole page string. **Falsifying test:** `test_js_assertions_are_scoped_below_htmx` asserts `"pushState" in page` (proving htmx really is inlined) AND `"pushState" not in _script(page)` (proving the scoping works). It fails in both directions — if htmx is not inlined, and if the scoping is dropped. This is the test that protects `test_layout_state_never_rides_in_the_url`:915-918, whose `pushState` / `location.search` / `location.href` assertions would otherwise all break on inlined htmx.

*Phase 3 — fragment composer, `/fragments`, swap glue, deferred swap*

12. WHEN `GET /fragments` is requested THE dashboard server SHALL return `200 text/html; charset=utf-8` containing exactly five regions — telemetry, sync, sections, branches, bar — each carrying an out-of-band swap marker, and each region's inner content SHALL be byte-identical to the same region of `render_page` for the same `(records, now, queue_mtime)`. **Falsifying test:** `test_fragments_match_page_regions` extracts all five regions from both outputs and compares byte-for-byte; it fails the moment the two composers drift.
13. THE page SHALL wrap each of the five dynamic regions in a stable-id container that is present in **every** queue state, including the empty queue whose `render_page` branch (dashboard.py:1258-1268) emits a `.clear` div and an empty bar. **Falsifying test:** `test_fragment_targets_exist_in_every_state` renders both an empty and a non-empty queue and asserts all five container ids are present in both; without this, the non-empty→empty transition has no oob target and the page silently keeps showing stale rows.
14. WHEN `/approve`, `/approve-all` or `/dismiss` returns 200 THE dashboard client SHALL request `/fragments` and SHALL NOT call `location.reload()`. **Falsifying test:** `test_actions_refresh_fragments_not_the_page` asserts `"location.reload" not in _script(page)` and that the `r.status===200` branch of `send()` calls the fragment refresh. (The `not in` half is only sound because of AC 11 — htmx's own source contains `location.reload` twice.)
15. WHEN a refresh trigger fires WHILE `selected().length > 0 || confirming || inflight` THE dashboard client SHALL NOT swap, and SHALL make the update-pending chip visible in the header. **Falsifying test:** `test_deferred_swap_holds_and_drains` pins the guard and the chip-show call; removing either fails.
16. WHEN a deferred refresh is pending THE dashboard client SHALL apply it on **any** of three drain paths: the selection count returning to zero on a checkbox `change`, the confirm dialog closing, or a click on the chip. **Falsifying test:** three independent assertions in `test_deferred_swap_holds_and_drains`; deleting any one drain path fails exactly one assertion, which is what stops the original silent-stale bug from being reborn as a permanent hold.
17. WHEN fragments swap THE dashboard client SHALL, in the after-swap hook, re-apply the checkbox values captured immediately before the swap for inputs that still exist, then run `applyFilter(); refresh(); marks();`, so the approve bar's disabled, hidden and count state is recomputed. **Falsifying test:** `test_post_swap_reapplies_selection_and_filters` pins the capture, the re-apply loop and the three calls; `test_bar_visibility_is_recomputed_on_every_filter_and_view_change`:2190 continues to pass unmodified.
18. THE `_BODY_SCRIPT` SHALL contain no `tick` function and no `location.reload`, and the 10s mtime poll SHALL call the fragment refresh instead of reloading. THE load-time `baseMtime` capture SHALL be replaced by a script-scoped `lastMtime` seeded at load and reassigned on every `/mtime` response and after every swap. **Falsifying test:** `assert "tick" not in _script(page)`; `test_live_poll_reloads_only_when_the_mtime_changed_and_nothing_is_busy`:2082 is rewritten to pin the fragment-refresh form of the guard.
19. IF `render_fragments` raises while serving `GET /fragments` THEN THE server SHALL return 500 with a plain-text body and SHALL NOT let the exception escape the handler, preserving the KeepAlive invariant stated at dashboard.py:1431-1436. **Falsifying test:** `test_fragment_render_failure_is_a_500` monkeypatches the composer to raise and asserts a 500 plus a still-serving `/mtime` on the same server.
20. WHEN `GET /fragments` is requested without the `X-Worksweep` header THE server SHALL still return 200, and WHEN `/fragments` is requested with `POST` THE server SHALL return 404 — mirroring `/mtime` exactly. **Falsifying test:** `test_fragments_needs_no_csrf_header` and `test_fragments_is_get_only`, modelled on test:1716 and test:1723.

*Phase 4 — SSE `/events`, `EventSource`, fallback demotion*

21. WHEN `queue.json`'s mtime changes while a client holds `GET /events` open THE server SHALL emit `event: queue` followed by a `data:` line carrying the mtime token, terminated by a blank line, within 2 seconds. **Falsifying test:** `test_events_emits_on_mtime_change` opens the raw-socket stream helper, rewrites the queue, and reads one framed event under a 2s deadline; it fails if the stat loop is removed or the framing is malformed.
22. WHILE an `/events` stream is open and the queue is unchanged THE server SHALL emit a `: heartbeat` comment line at least once per heartbeat interval, so a dead client surfaces as a write error and the streaming thread exits. **Falsifying test:** `test_events_sends_heartbeat_comments` monkeypatches the module-level heartbeat interval to a small value and reads two comment lines.
23. WHILE an `/events` stream is open THE `/events` route SHALL acquire neither `_WRITE_LOCK` nor the queue file lock, and a concurrent `POST /approve` SHALL complete within the harness's 5s client timeout. **Falsifying test:** `test_approve_during_open_stream_completes` holds a stream open and approves on a second connection; `test_events_holds_no_write_lock` asserts by source inspection that the route body references neither lock.
24. THE `DashboardHandler` SHALL set `protocol_version = "HTTP/1.1"`, and every response produced by `_send` SHALL still carry a `Content-Length` header. **Falsifying test:** `test_http11_responses_carry_content_length` asserts `HTTP/1.1` in the status line and a present `Content-Length` for `GET /`, `GET /mtime`, `GET /fragments` and a 404 — the four shapes that would otherwise hang a keep-alive client.
25. IF the `EventSource` connection errors terminally THEN THE dashboard client SHALL arm the 10s fragment-refresh poll and SHALL NOT reload the page. **Falsifying test:** `test_sse_error_falls_back_to_fragment_poll` pins the `onerror` handler arming the poll and re-asserts `"location.reload" not in _script(page)`.
26. IF the client disconnects mid-stream THEN THE `/events` loop SHALL exit on its next write rather than looping indefinitely, and `_Server.close()` SHALL return without blocking on the streaming thread. **Falsifying test:** `test_events_thread_exits_on_client_disconnect` opens a stream, closes the client socket, and asserts `_Server.close()` completes inside a 5s join — verified safe because `_DashboardServer.daemon_threads = True` (dashboard.py:1769) makes `socketserver._Threads.append` skip the request thread, so `server_close()` never joins it.

**TDD Mode:** `Full TDD` for Phases 2-4 (new endpoints and new client behavior — write the falsifier first, watch it fail, then implement) — `Test-After` for Phase 1 only, which is a behavior-preserving refactor whose contract is the existing 1211-test suite. Rationale is source-stated in the gated log's Field Provenance (`:63`).

**Owning layer:** `worksweep/dashboard.py` — the render-and-serve layer. It owns every HTML entry point and every route; the per-section renderers at `:1091`, `:1124`, `:1150`, `:1185`, `:1198` are the pure seam both `render_page` and the new `render_fragments` compose from. No other module in `worksweep/` changes.

**Downstream consumers:**
- `worksweep/tests/test_dashboard.py:1767` (Phase 1) — the single pre-existing assertion Phase 1 edits
- `worksweep/tests/test_dashboard.py:915-918` (Phase 2) — `pushState` / `location.search` / `location.href` assertions, re-pointed at `_script(page)`
- `worksweep/tests/test_dashboard.py:904` (Phase 3) — the `setTimeout(tick,` assertion inside `test_layout_is_restored_from_localstorage_before_the_first_section`, removed with `tick()`
- `worksweep/tests/test_dashboard.py:1476-1477` (Phase 3) — the `setTimeout(tick,FALLBACK_MS)` and `location.reload();` assertions in `test_timed_reload_skips_while_a_selection_or_post_is_live`
- `worksweep/tests/test_dashboard.py:2072-2079` (Phase 3) — `FALLBACK_MS=300000` and the `setTimeout(tick,FALLBACK_MS)` regex in `test_live_poll_is_always_armed_not_gated_on_a_sync_tap`
- `worksweep/tests/test_dashboard.py:2083` (Phase 3) — the `baseMtime` guard assertion in `test_live_poll_reloads_only_when_the_mtime_changed_and_nothing_is_busy`
- launchd dashboard agent — invocation unchanged; requires a restart after deploy
- rsync deploy to the mini — must now include `worksweep/static/`, or the module raises at import per AC 9

**Sibling pattern:**
- `worksweep/dashboard.py:1527-1532` — the `/mtime` route; the CSRF-free-read shape both `/fragments` and `/events` follow (quoted in `## Sibling Patterns`)
- `worksweep/tests/test_dashboard.py:133-140` — `_Server.request`; the harness the SSE tests must NOT reuse, and the counterpart the new `stream()` helper is written against (quoted in `## Sibling Patterns`)
- `worksweep/tests/test_dashboard.py:2074-2078` — the `body[body.rindex("<script>"):]` idiom Phase 2 promotes into `_script(page)` (quoted in `## Sibling Patterns`)
- `worksweep/dashboard.py:740-746` and `:747-953` — `_HEAD_SCRIPT` / `_BODY_SCRIPT`; the `"""..."""  % {...}` constant shape every new JS block follows
- `worksweep/tests/test_dashboard.py:1716` and `:1723` — `test_mtime_needs_no_csrf_header` / `test_mtime_is_get_only`; the exact pair AC 20 mirrors for `/fragments`

**Verify:**
```
python3 -m pytest worksweep/tests/ -q
```
Baseline before any change: `1211 passed in 3.73s`. Every phase boundary must be green. After Phase 4 the count is 1211 + 22 new, minus zero deletions. Deploy gate (from the gated log's `## Verification`, `:80-82`): rsync `worksweep/` including `static/` to the mini, restart the dashboard agent, then open two browser tabs on the tailnet — kick a sweep and confirm both update in place within 2s; check a box in tab A, kick a sweep, confirm A shows the pending chip and holds; uncheck and confirm A drains; approve an item and confirm fragments refresh with no page reload; restart the dashboard agent and confirm both tabs reconnect via `EventSource` retry.

**Plan provenance:** `unhardened` (claude-solo session; no plan-checkpoint hook configured in this repo)

### Behavioral Contract

Pre-change behaviors that must keep working, verified present in the 1211-test baseline:

1. The page is fully self-contained — zero external assets of any kind (test:866-878).
2. All three layouts (`checklist`, `panels`, `branches`) render into the DOM at once and switch via `data-layout` with no round trip; the choice persists in `localStorage` and never rides in the URL (test:882-919).
3. The layout-restore script runs before the first section paints, so no layout flash occurs (test:891).
4. Status pills filter rows by inline `style.display`; exactly one filter is active at a time; the filter is never persisted (test:2020, :2043).
5. A row hidden by a filter is not selectable, not submitted, and does not keep the approve bar up (test:2202).
6. The same record's checkbox appears in both the sections and branches views and the twins stay mirrored (dashboard.py:894-903).
7. The approve bar hides entirely when nothing visible is approvable, and its state is recomputed on every filter and layout change (test:2142, :2190).
8. CSRF: every POST requires the `X-Worksweep` header; `Origin`, when present, must match `Host`; `/mtime` is exempt as a read (dashboard.py:1495-1510, test:1591, :1605, :1716).
9. `POST /sweep` kicks the out-of-process agent exactly once, is throttled to one per 60s with a 429, and never runs in-process (test:1582, :1622, :1665).
10. `/dismiss` refuses runnable and already-terminal rows, marks the GitLab todo done once, and survives a `glab` failure locally (test:1858, :1872, :1917, :1925).
11. Approve and dismiss actions are actor-attributed in the Discord audit, with hostile actor values rejected (test:2221, :2280).
12. Nothing in the handler may raise — the launchd agent is KeepAlive, so an escaping exception is a restart loop (dashboard.py:1431-1436).
13. `_WRITE_LOCK` and the queue file lock are held for the shortest possible window and never across a subprocess call (dashboard.py:1725-1733, test:1987).

### What I Discovered

- **The 51KB inline asset collides with four existing whole-page string assertions, and this is the plan's critical path.** A `grep -cF` sweep of `worksweep/static/htmx.min.js` finds `pushState` (1 hit), `location.search` (3), `location.href` (1) and `location.reload` (2). `test_layout_state_never_rides_in_the_url` (test:915-918) asserts the first three are `not in page`, so **inlining htmx breaks it immediately**; and AC4's natural falsifier (`"location.reload" not in page`) would become a permanent false positive. The fix is AC 11's `_script(page)` helper, and it must land in Phase 2 — before any Phase 3 assertion depends on it. The good news from the same sweep: `htmx.min.js` has **zero** hits for `<script src=`, `<link`, `src="`, `@import`, `http-equiv` and `</script`, so AC5's demand that `test_page_is_self_contained` pass **unchanged** is achievable exactly as the gated log assumes.
- **Phase 1's churn is one line, not a file.** An exhaustive grep of the test file for `getElementById`, `addEventListener`, `syncDone`, `sync.disabled` and `sync.textContent` finds the direct-listener architecture is pinned by exactly one pre-existing assertion — `test_dashboard.py:1767`. `refresh()` (dashboard.py:788-801) already re-queries every element by id on each call, which is why `test_refresh_re_enables_both_buttons`:1390 is swap-safe as written. That makes Decision 6 genuinely shippable as a green, near-zero-diff first commit.
- **Decision 3's shared composer has an unstated empty-queue branch.** `render_page`:1258-1268 emits a `.clear` div *instead of* `.sections` and `.branches` when the queue is empty, and `_bar_html` is replaced by `""`. Class-targeted out-of-band swaps therefore have no target on the non-empty→empty transition — the page would silently keep showing stale rows after the last item is approved, which is a sharper version of the original bug. Resolved by AC 13 (stable-id containers in every state); called out again in Tricky Parts.
- **SSE teardown is provably safe, and I verified it rather than assuming.** `_DashboardServer.daemon_threads = True` (dashboard.py:1769) causes `socketserver._Threads.append` to skip the request thread entirely, so `server_close()`'s `self._threads.join()` has nothing to join and returns immediately even with a stream still open. `shutdown()` only stops the accept loop. The gated log's Critique Pass worry ("`shutdown()` won't interrupt a blocked stream write") is real for the *thread*, but it cannot hang the *test suite*.
- **`_Server.request` cannot be adapted for SSE** — `conn.getresponse()` then `r.read()` reads to completion, which for a stream means blocking to the 5s timeout and raising. The new helper must be raw-socket. Its shape is in Tricky Parts.
- `_style(page)` (test:36-39) scopes to the `<style>` block by regex, so every CSS test — including the palette and breakpoint tests — is immune to the inlined JS. Only the JS-source tests need re-scoping.

### Tricky Parts

- **Ordering inside `<head>` is load-bearing.** `test_layout_is_restored_from_localstorage_before_the_first_section`:906 slices `page[restore - 400 : page.index("</script>", restore)]` and requires all three layout names in that window. Emitting htmx *before* `_HEAD_SCRIPT` pushes htmx's tail into the 400-character lookback. Emit htmx **after** `_HEAD_SCRIPT` (AC 10); the layout-restore script still runs before first paint either way, so nothing is lost.
- **The empty-queue swap transition.** After the last item is approved, `/fragments` must return content that empties `#sections` and `#branches` and hides `#bar`. Verify this specific transition by hand on the mini, not only in a unit test — it is the state most likely to look correct in isolation and wrong in sequence.
- **`lastMtime` must not be a load-time DOM read.** `baseMtime`:753 is captured once from `#sync[data-mtime]` at load. After the sync region is swapped, that captured value is stale forever and the poll either fires continuously or never. Replace it with a script-scoped `lastMtime`, seeded at load, reassigned on every `/mtime` response **and** after every swap from the freshly-rendered sync button's `data-mtime`.
- **The deferred-swap hold is the original bug's rebirth risk.** A wrong guard interaction holds updates forever. Three independent drain paths (AC 16) and a *visible* chip are the mitigation — a stuck hold must be visible, never silent. Test each drain path separately so a single broken path fails exactly one assertion.
- **The SSE test helper shape.** `_Server` gains a raw-socket streaming helper, roughly: a `contextlib.contextmanager` that opens `socket.create_connection((self.host, self.port), timeout=...)`, sends a literal `GET /events HTTP/1.1\r\nHost: <host>:<port>\r\n\r\n`, yields a small reader object that accumulates `recv()` into a buffer and returns one blank-line-terminated SSE frame at a time under a wall-clock deadline, and closes the socket in `finally`. The test file must add `socket` and `contextlib` to its imports (test:1 currently has neither). Always close stream clients before `_Server.close()`.
- **The stat and heartbeat intervals must be module-level constants**, or AC 22 needs a 15-second test. Monkeypatch them down in the SSE tests.
- **Do not add `do_OPTIONS` and do not emit any `Access-Control-*` header** — the comment at dashboard.py:57-61 is explicit that the CSRF defense evaporates if a preflight is ever answered. Neither new route needs one.
- **`protocol_version = "HTTP/1.1"` is class-wide**, so every existing route switches to keep-alive at the same moment. It is safe only because `_send`:1472 always sets `Content-Length` — AC 24 makes that a regression test rather than a standing assumption.

### Relevant Patterns

- `worksweep/dashboard.py:1527-1532` — `/mtime`: CSRF-free read route, placed above the `path != "/"` 404 guard
- `worksweep/dashboard.py:1466-1480` — `_send`: the body-first, fixed-Content-Length writer `/fragments` uses and `/events` must bypass
- `worksweep/dashboard.py:1084-1088` — `_safe(render, record, *args)`: the per-row degradation wrapper the fragment composer inherits by reusing the section renderers
- `worksweep/dashboard.py:788-801` — `refresh()`: already re-queries every element by id, the reason it survives swaps untouched
- `worksweep/dashboard.py:885-903` — the delegated `click` and `change` listeners; Phase 1 appends three branches to the first of these
- `worksweep/tests/test_dashboard.py:119-175` — the `_Server` harness and the `serve_queue` fixture that closes servers on teardown
- `worksweep/tests/test_dashboard.py:1716`, `:1723` — the CSRF-exempt and GET-only test pair AC 20 mirrors
- `worksweep/tests/test_dashboard.py:36-39` — `_style(page)`: the precedent for scoping an assertion to one block of the page, which `_script(page)` follows

<!-- Sprint-specific sections -->

## Layout (UI tasks only)

No layout change. All three existing layouts (`checklist`, `panels`, `branches`) keep their current markup; the only structural addition is five stable-id containers wrapping regions that already exist, plus one update-pending chip in the header next to the Sync button.

## Component Spec (UI tasks only)

**Update-pending chip** (new, Phase 3). Lives in `<header class="head">` adjacent to `#sync`. Hidden by default. Shown when a refresh trigger fires while the page is busy. Text: `queue changed — update pending`. Clickable: a click is one of the three drain paths and applies the deferred swap immediately. It is rendered server-side inside the header so it participates in the sync-region out-of-band swap, and it must be hidden again by the after-swap hook.

## Acceptance Criteria

Cross-referenced with the gated log's AC1-AC8 (`.planning/decisions/dashboard-htmx-live.md:35-42`):

- [ ] AC1 (SSE emits within 2s, page applies fragments without reload) → plan AC 21, AC 12
- [ ] AC2 (`/fragments` five oob regions, byte-identical to `render_page`) → plan AC 12, AC 13
- [ ] AC3 (deferred swap under selection, pending chip, drains on clear) → plan AC 15, AC 16
- [ ] AC4 (200 from an action requests `/fragments`, never `location.reload`) → plan AC 14
- [ ] AC5 (`test_page_is_self_contained` passes unchanged; sha256 matches) → plan AC 7, AC 8, AC 9
- [ ] AC6 (terminal SSE error falls back to a fragment poll; `tick()` gone) → plan AC 18, AC 25
- [ ] AC7 (no lock held while streaming; other routes responsive) → plan AC 23, AC 24
- [ ] AC8 (selections survive swaps; `applyFilter`/`refresh`/`marks` re-run; bar recomputed) → plan AC 17
