# Tribunal report — worksweep two-day arc (8450b8a..6831893)

Date: 2026-08-26 · Mode: ADVISORY retrospective (magi 0.2.5 protocol, full rebuttal round) · CR backend: Seneschal (OCI)

## Verdict

**ADVISORY — findings for the author.** outcome=NEEDS_FIXES, ship votes 1/3 (Melchior SHIP_WITH_FIXES; Balthasar, Caspar NEEDS_REWORK). Degraded: none — all three legs + 3-lens Melchior panel (design, simplifier, test-quality) reported.
CI gate: 1112/1112 tests green (local + mini, production interpreter).

## Confirmed, gating (the fix set)

| id | sev | where | finding | M/B/C | status |
|---|---|---|---|---|---|
| f-004 | Warning | `worksweep/__main__.py:135` | NaN Retry-After crashes the Discord post retry loop | con/con/con | confirmed |
| f-005 | Warning | `worksweep/__main__.py:205` | Discord post retry can duplicate a delivered digest | con/con/con | confirmed |
| f-006 | Warning | `worksweep/__main__.py:398` | Probe failure silently demotes an approved feedback row to 'proposed' | con/con/con | confirmed |
| f-007 | Warning | `worksweep/__main__.py:769` | Unlocked concurrent writers on queue.json | con/con/con | confirmed |
| f-009 | Warning | `worksweep/approvals.py:44` | 'disapprove all' matches blanket-approval regex | con/con/con | confirmed |
| f-019 | Warning | `worksweep/feedback.py:456` | The claimed effect-based feedback verification does not attribute replies or commits to this run. A claimed th | con/con/con | confirmed |
| f-020 | Warning | `worksweep/implementer.py:57` | implementer's magi advisory timeout still 1800s — impossible post-0.2.4 | con/con/con | confirmed |
| f-021 | Warning | `worksweep/implementer.py:519` | Pipeline mode can accept a stale state.md from a previous run. The reused worktree's pipeline state is neither | con/con/con | confirmed |
| f-022 | Warning | `worksweep/implementer.py:536` | Pipeline mode says it proves the claimed dev box serves HTTP 200, but a probe exception or non-200 response is | con/con/con | confirmed |
| f-024 | Warning | `worksweep/models.py:13` | Re-park to a different box leaves the MR advertising the old box | con/con/con | confirmed |
| f-026 | Warning | `worksweep/park.py:116` | park's box sync never proves the branch landed | con/ref/con | confirmed |
| f-027 | Warning | `worksweep/park.py:122` | No read-back after park's description PUT; done message hardcodes (200) | con/con/con | confirmed |
| f-028 | Warning | `worksweep/queue.py:105` | queue.json writers race across processes — lost-update window | con/con/con | confirmed |
| f-029 | Warning | `worksweep/queue.py:123` | Queue writes remain an unlocked cross-process read-modify-write. Unique temp files prevent corruption but do n | con/con/con | confirmed |

## Confirmed, watchlist (minors)

| f-001 | Minor | `worksweep/__main__.py:56` | _POST_BACKOFF_SECONDS indexed by attempt with no bounds guard | con/ref/con | confirmed |
| f-004 | Warning | `worksweep/__main__.py:135` | NaN Retry-After crashes the Discord post retry loop | con/con/con | confirmed |
| f-005 | Warning | `worksweep/__main__.py:205` | Discord post retry can duplicate a delivered digest | con/con/con | confirmed |
| f-006 | Warning | `worksweep/__main__.py:398` | Probe failure silently demotes an approved feedback row to 'proposed' | con/con/con | confirmed |
| f-007 | Warning | `worksweep/__main__.py:769` | Unlocked concurrent writers on queue.json | con/con/con | confirmed |
| f-008 | Warning | `worksweep/__main__.py:771` | New dashboard HTTP server wires privileged actions with no visible auth | con/con/ref | confirmed |
| f-009 | Warning | `worksweep/approvals.py:44` | 'disapprove all' matches blanket-approval regex | con/con/con | confirmed |
| f-013 | Warning | `worksweep/checkouts.py:118` | TOCTOU race between _is_clean check and detach on a shared worktree | con/con/con | confirmed |

## Acknowledged / deferred

- **f-008** New dashboard HTTP server wires privileged actions with no visible auth — _Same M6 Decision 5 disposition as the dashboard finding; the CSRF header check exists (dashboard.py:1495-1510) — advocate itself called this framing a partial false positive._
- **f-013** TOCTOU race between _is_clean check and detach on a shared worktree — _Cooperative-concurrency boundary documented in the round-3 fix report (Uncertain #5): a mid-window dirty worktree causes a loud RunnerError refusal, not silent damage. Accepted._
- **f-014** The dashboard approval boundary is unauthenticated. Binding to a tailnet limits reachabili — _M6 gated decision log, Decision 5: tailnet-only bind IS the trust boundary; auth layer explicitly rejected as overkill on a private tailnet. Actor field is attribution, not authorization, by design (dashboard.py:1362 com_
- **f-017** The unattended address-feedback run places untrusted GitLab thread text in the prompt whil — _Accepted residual per the round-2 fix-mode handoff ('Explicitly acceptable residuals') and feedback.py docstring: fencing + tool scoping + effect-based verification + human-audited reply quotes are the mitigations; full _
- **f-030** Post-feedback magi auto-chain has no rate limit — _Round-4 decision: watch the first live days before bounding; chained reviews are scoped to our own executor's commits._
- **f-032** No shared executor contract — four hand-copied claim handlers, drift already observable — _Accepted as the next worksweep milestone (M8 consolidation: executor result protocol + shared edge module) rather than a fix in this arc — the drift is real but every instance is individually tested and shipped._

## Refuted (false positives filtered)

- **f-002** No tests visible for _with_unaddressed / _retained_feedback probe-failure handling — test_main_v2.py has probe-exactly-once/rebind/no-dep tests + round-2 retained-row tests — 'no tests visible' is false
- **f-003** No tests visible for Discord retry/backoff logic — Discord retry tests exist (round-9 commit added them; test_main_v2 retry/post coverage) — reviewer lacked test-file access
- **f-010** No tests visible for parse_approve_all / flip / approve_all semantics — test_approvals.py holds 22 tests incl. parse_approve_all positives/negatives from M6
- **f-011** Behavior change (mr-hygiene -> park, unresolved_count -> unaddressed_count) needs updated/ — test_assessor_v2.py pins the two-arm emission matrix incl. mutual exclusivity and affordances
- **f-012** No tests visible for worktree branch-recovery logic — 8 dedicated steal/recovery tests (dirty/foreign/lookalike/no-holder/failed-detach/retry) + test_feedback _Held end-to-end

## Provenance
- Melchior: inline review + verified 3-lens panel (design, simplifier, test-quality); 4 panel advisories dropped pre-union (recorded in panel-dropped.json), incl. 2 refuted by disk evidence.
- Balthasar: Codex via bridge, 614KB code-only diff inlined; round-1 6 findings; rebuttal 34/34 votes, status ok, 0 new findings.
- Caspar: Seneschal (OCI, healthy health-check), 13 findings; advocate voted 27c/4r/3a and refuted one of its own backend's findings as misframed.
- Rebuttal ran the 0.2.4 background-dispatch protocol; Codex returned before the mechanical wait was needed.
- Notable live verifications during review: parse_approve_all('disapprove all')==True (f-009); NaN Retry-After -> sleep(nan) (f-004); park sync sha-gate skip (f-026); implementer magi timeout 1800 vs 4500 (f-020).