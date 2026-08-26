
**Plan:** `/Users/chandlerhardy/repos/heartbeat/.claude/plans/worksweep-address-feedback.md` — READ-FIRST

**Files:**
- `worksweep/models.py` (EXTEND) — `MergeRequest.unaddressed_count: int = 0` trailing default beside `unresolved_count` (models.py:38); `RUNNABLE_EXECUTORS` (models.py:83) gains `"address-feedback"`; kind/executor docstrings (models.py:91-94) refreshed; optional `ReviewThread` frozen dataclass (id, resolvable, resolved, last_author, last_note) so the probe and the executor share one thread type
- `worksweep/collectors.py` (EXTEND) — pure `unaddressed_threads(raw_json, username) -> tuple` + `parse_unaddressed_count(raw_json, username) -> int` (the `parse_*` half of the module contract, collectors.py:1-6), and the shell edge `collect_discussions(repo, iid)` using the existing `_run_glab` GET pattern (collectors.py:20-33, 133-150) against `projects/{_project(repo)}/merge_requests/{iid}/discussions?per_page=100`
- `worksweep/assessor.py` (EXTEND) — the feedback emission block (assessor.py:127-135) switches gate/executor/branch per AC #4
- `worksweep/__main__.py` (EXTEND) — opt-in `deps["discussions"]` probe threaded between `parse_graphql_sweep` (__main__.py:379-380) and the `assess_own_mr` loop (__main__.py:388-391), wired at __main__.py:699-722 beside `deps["diverged_commits"]`; `_execute_address_feedback` + `_dry_run_address_feedback` edges beside `_execute_park` (__main__.py:574-594) and registered in the `run` deps dict (__main__.py:673-692)
- `worksweep/feedback.py` (NEW) — the executor: worktree checkout, run-time thread re-fetch, one `claude -p` run, python verification, `FeedbackResult` + `done_message`. Modeled on park.py (S1) and keepcurrent.py (S3)
- `worksweep/checkouts.py` (EXTEND) — `_WORKTREE_EXECUTORS` (checkouts.py:37) gains `"address-feedback"`
- `worksweep/runner.py` (EXTEND) — `_ADDRESS_FEEDBACK` constant beside `_PARK` (runner.py:28-32), `_ALL_EXECUTORS` (runner.py:32), the `pick_claim` tuple in `_run_magi_pass` (runner.py:355) and its dispatch (runner.py:360-363), and a new `_run_address_feedback_claim` modeled on `_run_park_claim` (S2) with S4's `NeedsInputError` branch
- `worksweep/curator.py` (EXTEND) — prompt rule 2 (curator.py:109-110), `partition_counts` (curator.py:319-330, the predicate at :326), `validate` required-set (curator.py:271-273)
- `worksweep/dashboard.py` (EXTEND — Decision 8 only) — `_valid_actor` beside `_valid_number` (dashboard.py:1384-1395), `actor` read in `do_POST` after `_valid_numbers` (dashboard.py:1560-1563), threaded through `_approve` (dashboard.py:1684-1707) and `_audit` (dashboard.py:1709-1723) into `_audit_message` (dashboard.py:1353-1381) whose `suffix` literal (dashboard.py:1365) becomes actor-dependent
- `worksweep/tests/test_collectors.py` (EXTEND) — probe predicate tests, pure JSON fixtures
- `worksweep/tests/test_assessor_v2.py` (EXTEND) — emission tests incl. AC #3's falsifying test; existing `test_own_mr_feedback_and_ci_items` (:43-48) updated
- `worksweep/tests/test_handoff.py` (EXTEND) — fixtures at :128-152 gain `unaddressed_count`
- `worksweep/tests/test_feedback.py` (NEW) — executor tests, every edge injected; mirrors test_park.py's shape (test_park.py:1-60)
- `worksweep/tests/test_runner_feedback.py` (NEW) — claim-lifecycle tests; mirrors test_runner_park.py (test_runner_park.py:1-70)
- `worksweep/tests/test_curator.py` (EXTEND) — four `executor="triage", kind="feedback"` fixtures (:58, :148, :155, :203) plus AC #13's tests
- `worksweep/tests/test_apply_approvals.py` (EXTEND) — registry pin (:245-256) gains the new name
- `worksweep/tests/test_main_v2.py` (EXTEND) — sweep-seam wiring test for the opt-in probe dep
- `worksweep/tests/test_dashboard.py` (EXTEND) — `_Client.approve`/`approve_all` (:140-143, :157-162) gain an optional `actor` kwarg; AC #14's tests added beside the existing audit tests (:740-764, :1238-1261)

**AC:**  (EARS — each line falsifiable; trigger clause = precondition, SHALL clause = postcondition)
1. WHEN the probe parses an MR's discussions payload, THE worksweep unaddressed predicate SHALL count a thread if and only if the thread is `resolvable`, is not `resolved`, and the author username of its last note with `system == false` differs from `cfg.username`.
2. IF a thread's last non-system note author equals `cfg.username` THEN THE unaddressed predicate SHALL NOT count that thread, and THE sweep SHALL emit no `address-feedback` item for an authored MR whose every unresolved thread is in that state.
3. **Falsifying test** — `test_addressed_threads_emit_nothing`: `assess_own_mr` over an MR with `unresolved_count=2, unaddressed_count=0, changes_requested=False` SHALL return an item list containing no id beginning `feedback:`. Mutation: restore assessor.py:127's `mr.changes_requested or mr.unresolved_count > 0` gate → the test goes RED.
4. WHEN `mr.unaddressed_count > 0`, THE assessor SHALL emit exactly one WorkItem with id `feedback:{repo}!{iid}`, kind `feedback`, executor `address-feedback`, `branch` equal to `mr.source_branch`, and a why-string of `"{n} unaddressed thread"` (plus `"s"` when `n != 1`), prefixed with `"changes requested, "` when `mr.changes_requested` is true.
5. THE models registry SHALL list `address-feedback` in `RUNNABLE_EXECUTORS` (models.py:83) and `runner._ALL_EXECUTORS` (runner.py:32) such that `test_runnable_executors_matches_the_runner_claim_gate` stays green, and THE dashboard SHALL render an approve checkbox for a proposed `address-feedback` row with no edit to `has_checkbox` (dashboard.py:974).
6. IF `cfg.auto_approve` is left at its default THEN THE config SHALL NOT include `address-feedback` (config.py:54 stays `("keep-current",)`) and `queue.auto_approve` SHALL leave a proposed `address-feedback` row at `proposed`.
7. WHEN `deps["discussions"]` is wired and an authored MR carries `unresolved_count > 0`, THE sweep SHALL probe that MR exactly once and rebind the authored `MergeRequest` with the resulting `unaddressed_count` before `assess_own_mr` runs; IF the probe raises THEN THE sweep SHALL print the failure to stderr and continue with that MR's `unaddressed_count` at 0, never aborting the sweep (mirroring __main__.py:398-410).
8. **Falsifying test** — `test_feedback_prompt_never_resolves`: the rendered feedback prompt and `worksweep/feedback.py`'s source SHALL contain no thread-resolve instruction, no `/resolve` path, and no `"resolved": true` body, and THE verification tally SHALL count only replies and commits. Mutation: add a resolve instruction to the prompt → the test goes RED.
9. WHEN the feedback prompt is rendered, IT SHALL instruct exactly three per-thread outcomes — fixable → commit and reply `addressed in <short-sha>`; question → reply with the answer; judgment call, disagreement, or uncertainty → no reply and escalate.
10. WHEN the executor runs, IT SHALL take its checkout from `checkouts.worktree_for(cfg, repo, "address-feedback", ...)` with `"address-feedback"` present in `_WORKTREE_EXECUTORS` (checkouts.py:37), SHALL re-fetch the MR's discussions at run time rather than trusting the sweep snapshot, and SHALL verify in python that the remote branch sha advanced whenever a commit was claimed and that every thread claimed as replied has a last non-system note authored by `cfg.username`.
11. WHEN the feedback prompt is rendered, IT SHALL carry the pb-www hygiene block: recompile via `maintenance/compile-css` when the fix touched `www/home/scss/*`, bump `$script_version` in `www/home/php/templates/tab_bar_common_logic.php` on CSS or JS changes, push the branch, and sync the dev box when the MR description names one.
12. WHEN at least one thread is addressed or replied, THE runner SHALL complete the record `done` and post a tally naming the addressed, replied, and escalated counts and listing each escalated thread short-form; IF zero threads are handled and at least one is escalated THEN THE executor SHALL raise `NeedsInputError` and THE `_run_address_feedback_claim` handler SHALL catch it before `RunnerError`, flip the record to `needs-input` with the escalation summary, and post ❓ rather than ⚠️.
13. **Falsifying test** — `test_curator_requires_feedback_numbers`: `curator.validate()` SHALL return False for curated output that omits a proposed `address-feedback` record's number, while `partition_counts` counts a `feedback`/`address-feedback` row as actionable and still counts a `ci_red`/`triage` row as actionable. Mutation: drop the executor from the required-set predicate (curator.py:271-273) → the test goes RED.
14. **Falsifying test** — `test_approve_actor_attribution`: WHEN an approve POST body carries `actor: "claude"` THE audit message SHALL end `" (dashboard · claude)"`; WHEN the field is absent, is not a string, exceeds the clamp, or holds any other value, THE audit message SHALL end `" (dashboard)"` unchanged and the request SHALL still return 200. Mutation: apply the suffix unconditionally → the absent-field and non-`claude` assertions go RED.
15. THE new tests SHALL inject every edge (glab, ssh, http, subprocess, clock) and SHALL NOT touch the network, ssh, or a real subprocess, matching the discipline test_park.py:1-4 states for the sibling executor.
16. IF `mr.changes_requested` is true AND `mr.unaddressed_count == 0` THEN THE assessor SHALL emit the plain informational row — id `feedback:{repo}!{iid}`, kind `feedback`, executor `triage`, `why == "changes requested"`, no `branch` — so the MR stays visible in the digest without becoming runnable work; WHEN neither `changes_requested` nor an unaddressed thread is present, THE assessor SHALL emit no feedback row at all.
17. WHEN the executor's run-time re-fetch finds zero unaddressed threads (the reviewer replied or resolved between the sweep and the run), THE feedback executor SHALL return a normal result and THE runner SHALL complete the record `done` with the tally `"0 addressed, 0 replied, 0 escalated — threads already answered"`, and SHALL NOT raise `RunnerError`, flip the record to `error`, or post ⚠️.
18. THE feedback executor's `claude -p` run SHALL take its timeout from `cfg` with a default of 1800 seconds (`cfg.runner_timeout`, config.py:24), which sits inside the 45-minute reap window (`runner.py:20`, `runner.py:135-136`), and SHALL NOT introduce a new config key.

**TDD Mode:** `Full TDD` — new behaviour whose core is pure functions (the unaddressed predicate, the item shape, the prompt contract, the audit suffix); write each falsifying test RED first, then the production path. The source's own Field Provenance row agrees (decision-log:59).

**Owning layer:** worksweep-internal layers (PLA product Canon N/A — this is a stdlib Python CLI, not a product stack). `collectors.py:20-33,133-150` owns the GitLab I/O edge; `assessor.py:127-135` owns the emission decision; `feedback.py` (NEW) is a leaf executor owning the claude-run contract; `runner.py:355,425-455` owns claim lifecycle; `curator.py:319-330` owns digest partitioning; `dashboard.py:1353-1381` owns audit-post composition. Order of change: types → probe → emission → sweep wiring → executor → lifecycle → digest; `dashboard.py` (Decision 8) is independent of that chain.

**Downstream consumers:**
- `worksweep/queue.py:151` (`is_dismissable`) — a feedback row stops being dismissable once its executor is runnable; intended, and it is the reason the ✅ gate matters
- `worksweep/dashboard.py:974` (`has_checkbox`) — gains the approve control with no edit (source A4/AC5)
- `worksweep/approvals.py:110` (`✅ all` gate) — includes the new executor generically, no edit
- `worksweep/curator.py:326` (`partition_counts`) and `curator.py:271-273` (`validate`) — both require the rename edit (AC #13)
- `worksweep/formatter.py:118-124` (`_is_auto_merge`) — switches only on `keep-current`, so no edit
- `worksweep/tests/test_apply_approvals.py:253-256` — set-equality pin on the registry, required co-edit
- `worksweep/tests/test_dashboard.py:1245,1258` — positional `_audit_message` calls, so the new param must be keyword-with-default

**Sibling pattern:**
- `worksweep/park.py:90-132` — newest executor: edge guards, `iid_of`, result dataclass, `done_message` (quoted as S1)
- `worksweep/runner.py:425-455` — `_run_park_claim`, the claim-handler template (quoted as S2)
- `worksweep/keepcurrent.py:254-283` — `_verify_resolution`, the verify-then-restore discipline (quoted as S3)
- `worksweep/runner.py:511-519` — the only existing `NeedsInputError` routing (quoted as S4)
- `worksweep/dashboard.py:1384-1395` — `_valid_number`, the payload-field validator shape (quoted as S5)
- `worksweep/keepcurrent.py:214-232` — `_RESOLVE_PROMPT`, the inline-prompt-for-unattended-claude shape
- `worksweep/tests/test_park.py:1-60` and `worksweep/tests/test_runner_park.py:1-70` — the two test-file shapes to mirror

**Verify:**
```
python3 -m pytest worksweep/tests/ -q                        # full suite: 884 green today, expect ~+40
python3 -m pytest worksweep/tests/test_feedback.py worksweep/tests/test_runner_feedback.py -q    # targeted loop
python3 -m pytest worksweep/tests/test_dashboard.py -q -k audit_or_actor                          # Decision 8 loop
```
Production path proved: sweep GraphQL → probe → `assess_own_mr` → queue → runner claim → `feedback.execute` → Discord post. Risky seam: the unaddressed predicate (a wrong predicate either nags forever or silently drops real reviewer feedback) and the never-resolve prompt contract. Targeted mutations: restore the `unresolved_count` gate (AC #3 → RED); add a resolve instruction to the prompt (AC #8 → RED); drop the curator required-set entry (AC #13 → RED); apply the actor suffix unconditionally (AC #14 → RED). Live boundary left unproved by the suite: the real GitLab discussions REST payload shape and the real `claude -p` run — both are covered only by the source's Verification §3 live acceptance on !3997 (decision-log:70), which the orchestrator runs after merge.

**Plan provenance:** `unhardened`

### Behavioral Contract

Pre-change behaviours that must keep working:
1. An authored MR with `changes_requested` or `unresolved_count > 0` produces one `feedback:{repo}!{iid}` WorkItem (assessor.py:127-135) whose id is stable across sweeps (queue reconcile keyed on id).
2. A handed-off MR (approved + MERGEABLE + assigned to someone else) produces ONLY the `handoff:` item and no feedback/magi/hygiene rows (assessor.py:95-104), and `resolutions` closes any live `feedback:` id (assessor.py:74-76).
3. `✅ all` and the dashboard's Approve-all flip only executors in `RUNNABLE_EXECUTORS` (approvals.py:110, dashboard.py:974); non-runnable rows stay `proposed` and remain dismissable (queue.py:141-151).
4. The shared magi/keep-current/park pass claims at most ONE item per invocation, lowest number first, under one lock (runner.py:337-363).
5. Every executor failure ends in BOTH a queue status and a Discord post — silence is never an outcome (runner.py:584-593).
6. `keep-current` items collapse into one auto-merge digest line; every other executor renders generically (formatter.py:118-130).
7. The curator's LLM output is hard-rejected if it contains a URL/markdown link or an unsourced number, and every proposed/approved magi-review and issue number must appear (curator.py:230-280).
8. The dashboard approve POSTs are CSRF-guarded by the `X-Worksweep: approve` header, load the queue fresh under a write lock, save before auditing, and post exactly one `"✅ Approved: … (dashboard)"` line clamped under the Discord cap (dashboard.py:1518, 1684-1723, 1353-1381).
9. `--dry-run` never persists a claim, never posts to Discord, and never runs a real executor (__main__.py:670-692).

### What I Discovered

- **The decision log's file list is missing `checkouts.py`, and that omission is a live-branch hazard.** `worktree_for` grants a private worktree only to executors named in `_WORKTREE_EXECUTORS` (checkouts.py:37) and silently returns the SHARED magi clone otherwise (checkouts.py:49-50). A feedback executor doing `checkout -B <branch>` in the shared clone can yank the branch out from under a live 90-minute implement run — the precise incident checkouts.py:1-12 documents. One-line fix, but invisible unless you read the gate.
- **The park/keep-current claim handlers have no `NeedsInputError` branch.** `NeedsInputError` subclasses `RunnerError` (runner.py:47-55), so `_run_park_claim`'s `except RunnerError` (runner.py:443) would record Decision 6's escalation as a hard `error` with a ⚠️ instead of `needs-input` with a ❓. The only existing correct routing is in the implement pass (runner.py:511-519, quoted as S4) — copy that, not the park handler, for the escalation path.
- **`RUNNABLE_EXECUTORS` is pinned by set EQUALITY, not containment** (test_apply_approvals.py:253-256), so adding the executor without editing that test turns the whole suite red immediately. That is a feature: it is the drift guard models.py:82 advertises.
- **The rename silently changes dismissability.** `is_dismissable` is `non-terminal AND non-runnable` (queue.py:141-151), so today's `triage` feedback rows can be dismissed and tomorrow's `address-feedback` rows cannot. Since the item id is deliberately preserved (Decision 2), any feedback row already sitting in the live queue changes affordance in place at the next sweep — and any that is already `approved` becomes claimable by the runner.
- **A4 verified on disk:** `has_checkbox` (dashboard.py:974) and the `✅ all` gate (approvals.py:110) key purely off `RUNNABLE_EXECUTORS`; `formatter.py`'s only executor switch is `_is_auto_merge` on `keep-current` (formatter.py:118-124). So "dashboard/formatter need zero changes" is true for the registry path — Decision 8's dashboard edit is a separate axis.
- **`_audit_message` is called positionally in two tests** (test_dashboard.py:1245, 1258), so Decision 8's `actor` must be a keyword parameter with a default, and `suffix` must be computed before `full` so the existing clamp loop (dashboard.py:1371-1377) keeps accounting for the longer suffix.
- **The config field is `cfg.username`** (config.py:12, loaded from `gitlab.username` at config.py:109) — the value the decision log writes as `chandler.hardy`. `_gql_mr` already compares against it for review state (collectors.py:211), so the probe uses the same field, never a literal.

### Tricky Parts

- **One-way door — replies are unsendable.** The run posts replies under Chandler's GitLab identity. This is why Decision 2 keeps the executor ✅-gated per MR and Decision 3 biases uncertainty to escalate. The verification step must never "fix up" a bad reply by posting another.
- **One-way door — never resolve.** No code path, prompt line, or verification predicate may reference the resolve API. Decision 3 is absolute: resolution belongs to the thread owner. AC #8 is the machine check.
- **The emission block keeps TWO arms** (Round 3: AC #4 + AC #16) — a runnable `address-feedback` row when `unaddressed_count > 0`, and an informational `triage` row when `changes_requested` is true with zero unaddressed threads. Both share the id `feedback:{repo}!{iid}`, so an MR moves between the arms across sweeps while reconcile keeps its queue number, and `curator.partition_counts` must keep counting BOTH (AC #13).
- **`MergeRequest` is a frozen dataclass** with positional construction in `_gql_mr` (collectors.py:223-242) — add `unaddressed_count` as a TRAILING field with a default, and enrich via `dataclasses.replace`, never by mutation.
- **Probe placement is load-bearing.** The rebind must happen before the `assess_own_mr` loop (__main__.py:388-391). `bootstrap_magi_records` (__main__.py:386-387), the diverged loop (__main__.py:398-410), and `resolutions` (__main__.py:441) all read the same `authored` list afterwards, so rebinding once right after `parse_graphql_sweep` (__main__.py:379-380) keeps them consistent.
- **Probe cost gate.** Per Decision 1, probe only authored MRs with `unresolved_count > 0`. A handed-off MR that still has unresolved threads costs one wasted GET (its emission short-circuits at assessor.py:95-104); that is acceptable and must not change the emission decision.
- **Reap window.** Everything except `implement` is reaped at 45 minutes (runner.py:20, 135-136). Settled by Round 3 (AC #18): the claude run takes `cfg.runner_timeout` (1800s default, config.py:24), inside that window. A cap above 45 minutes would have healthy runs reaped mid-flight, so do not add a longer per-executor timeout.
- **The curator predicate carries two kinds in one line** (curator.py:326). Splitting it must keep `ci_red`/`triage` counting as actionable; a naive rename drops CI rows from the actionable count, which is exactly the M3.5 silent-vanish class Decision 7 cites.
- **Actor value flows into Discord.** Accept `"claude"` and nothing else; every other value (long strings, mentions, URLs, non-strings) renders the unchanged `" (dashboard)"` and still returns 200. Never reflect the submitted text into the post.
- **Claude's own approve calls need the CSRF header.** The dashboard rejects a POST without `X-Worksweep: approve` (dashboard.py:1518), so the operator-side curl carries both that header and `{"numbers": [...], "actor": "claude"}`. No browser JS change (dashboard.py:908, 921 keep sending no actor).

### Relevant Patterns

- `worksweep/park.py:90-132` — executor shape: edge guards, result dataclass, `done_message` (S1)
- `worksweep/runner.py:425-455` — claim handler to copy (S2); `worksweep/runner.py:511-519` — the `NeedsInputError` branch to add (S4)
- `worksweep/keepcurrent.py:254-283` — python verification with restore-on-failure (S3)
- `worksweep/keepcurrent.py:214-232` — `_RESOLVE_PROMPT`: the inline-prompt shape for an unattended `claude -p` run
- `worksweep/keepcurrent.py:102-211` — worktree discipline end to end: `worktree_for`, preflight clean, fetch, checkout, push, box sync
- `worksweep/collectors.py:133-150` — `collect_diverged_commits_count`: the per-MR REST probe edge to mirror exactly
- `worksweep/__main__.py:398-410` and `__main__.py:719-722` — the opt-in, degrade-not-fail probe dep pattern
- `worksweep/__main__.py:574-594` — `_execute_park` / `_dry_run_park`: the real-vs-dry executor edge pair
- `worksweep/dashboard.py:1384-1395` — payload-field validator shape (S5)
- `worksweep/tests/test_park.py:1-60`, `worksweep/tests/test_runner_park.py:1-70` — the two test shapes to mirror
- `worksweep/tests/test_dashboard.py:740-764, 1238-1261` — the audit-post tests the actor tests sit beside

