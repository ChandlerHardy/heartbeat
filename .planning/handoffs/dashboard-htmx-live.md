
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

