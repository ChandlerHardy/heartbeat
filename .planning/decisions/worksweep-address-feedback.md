<!-- GATED: ExitPlanMode approved -->
# Worksweep: `address-feedback` executor — unaddressed review threads become runnable work

## Context

On authored MRs, worksweep currently emits an inert `triage` item ("changes requested; N unresolved threads") that can only be dismissed. Chandler wants those actionable: ✅ (Discord or dashboard) → seneschal checks out the branch, addresses the reviewer's threads, and replies — the same executor pattern that turned the `mr-hygiene` nag into `park`. Two contract refinements from Chandler (2026-08-25): **replies yes, resolution never** ("it's their feedback and they decide when it's been resolved"), and the signal is **unaddressed** threads, not unresolved — a thread we've already fixed-and-replied-to is waiting on the reviewer and needs nothing from us. Live acceptance target: **!3997**, which has fresh threads from Le today.

## Assumption Ledger (verified this session)

- **A1** — The sweep GraphQL carries only `resolvableDiscussionsCount`/`resolvedDiscussionsCount` (collectors.py:176-178, 233-237); per-thread authorship requires a follow-up call. ✔ read this session.
- **A2** — REST `projects/:id/merge_requests/:iid/discussions` returns per-note `author.username`, `system`, `resolvable`, `resolved` — sufficient to compute "last word in the thread". ✔ used live this session posting/verifying the !4076/!4078 drafts.
- **A3** — The curated digest special-cases feedback items by executor name: curator.py:326 (`partition_counts`) and prompt rule 2 ("executor is `triage` and kind is `feedback` or `ci_red`"). Renaming the executor requires touching both. ✔ read this session.
- **A4** — Dashboard approve buttons / `✅ all` eligibility key purely off `RUNNABLE_EXECUTORS` (models.py:83); no dashboard code changes needed. ✔ M6 zombie-row fix established this.
- **A5** — The shared magi/keep-current/park runner pass (runner.py:356-362) tolerates long claims (live magi tribunals run ~22 min) and has per-executor claim handlers + `needs_input` (runner.py:106) for escalation. ✔ read this session.
- **A6** — Only a handful of authored MRs ever have unresolved threads at once, so a per-MR REST probe is cheap and does not belong in the big sweep query. ✔ current queue shows 2-3 feedback items max.

## Decision Log (decision · rationale · rejected alternative)

1. **Signal = "unaddressed", computed by a targeted per-MR REST probe.** For each authored MR with `unresolved_count > 0`, fetch discussions; a thread is **unaddressed** iff it is resolvable, not resolved, and the last non-system note's author ≠ chandler.hardy. Addressed-but-unresolved threads (our reply is the last word) are waiting-on-reviewer and emit **nothing** — they drop off digest and dashboard entirely. *Rejected:* keeping `unresolved_count` as the signal (nags forever about threads already answered); extending the sweep GraphQL with nested discussions (heavy for 100 MRs when 2-3 need it).
2. **New executor `address-feedback`, added to `RUNNABLE_EXECUTORS`, ✅-gated — never auto-approved.** The feedback item keeps its id `feedback:{repo}!{iid}` (reconcile/fresh-wins continuity), switches executor from `triage`, gains `branch=mr.source_branch`, why = "N unaddressed thread(s)" (prefixed "changes requested, " when set). Emitted only when `unaddressed_count > 0`. `changes_requested` with zero unaddressed threads stays a plain `triage` info row (nothing concrete to act on). *Rejected:* auto-approve (this executor posts replies under Chandler's name — per-MR consent required); a new item id (would renumber existing rows for no benefit).
3. **Per-thread contract in the claude run: fix+reply / reply-only / escalate — and it NEVER resolves a thread.** Fixable → commit, reply "addressed in `<short-sha>`" on the thread; question → reply with the answer; judgment call or disagreement → no reply, escalate. Uncertainty biases to escalate. Resolution belongs to the thread owner, full stop — the prompt forbids the resolve API and verification never counts resolution. *Rejected:* resolving threads it fixed (Chandler explicitly leaves resolution to whoever owns the thread).
4. **Executor module `worksweep/feedback.py` modeled on keepcurrent.py/park.py: inline prompt, python-verified edges.** Worktree checkout of the branch (keepcurrent's worktree discipline), re-fetch unaddressed threads at run time (fresh state — reviewers may have replied since the sweep), one `claude -p` run doing the work, then python verification: remote sha advanced when commits were claimed, replies present on the threads it claims to have answered, honest tally. *Rejected:* routing through `pipeline_command`/a chandler-personal skill (puts the contract outside heartbeat's test suite; keepcurrent's inline-prompt + verify pattern is the proven shape for unattended claude).
5. **pb-www hygiene rides in the prompt: SCSS predicate → `maintenance/compile-css`, cache-buster bump on CSS/JS changes, push; sync the parked dev box when the MR description names one** (keepcurrent's box-sync pattern). *Rejected:* skipping box sync (fixes invisible on the dev URL reviewers use).
6. **Outcome mapping:** ≥1 thread addressed or replied → `done` with tally ("addressed 2, replied 1, escalated 1 → needs you"); zero handled and ≥1 escalated → `needs-input` with the escalation summary; the Discord post always lists escalated threads verbatim-short so Chandler can act from his phone. *Rejected:* silent partials (violates the never-silent contract).
7. **Curator/formatter follow the rename:** curator prompt rule 2 + `partition_counts` (curator.py:326) switch to `executor address-feedback` for kind `feedback` (kind `ci_red` stays `triage`), and unaddressed-feedback numbers join the validator's required-actionable set with a "✅ to address" affordance, like magi items. Formatter needs no structural change (executor renders generically). Dashboard: **zero changes** (A4). *Rejected:* leaving curator matching on `triage` (feedback lines would silently vanish from the digest — the exact M3.5 curated-fallback failure class).

## Scope / files

`worksweep/collectors.py` (discussions probe), `worksweep/models.py` (MergeRequest.unaddressed_count + executor registry), `worksweep/assessor.py` (emission), `worksweep/feedback.py` (new executor), `worksweep/runner.py` (claim handler + dep wiring), `worksweep/curator.py` (rule 2 + partition + validator), `worksweep/__main__.py` (probe + executor wiring), tests mirroring park's suite. Single repo, single session — L2.

## Acceptance Criteria (EARS)

- AC1: WHEN an authored MR has an unresolved thread whose last non-system note is NOT by the configured username, the sweep SHALL emit an `address-feedback` item with `unaddressed_count` in its why-string; WHEN every unresolved thread's last non-system note IS by the configured username, the sweep SHALL emit no feedback item at all.
- AC2 (falsifying): `test_addressed_threads_emit_nothing` SHALL pass with the change and SHALL fail when the unaddressed filter is removed (reverting to unresolved-count emission makes the asserted empty item list non-empty).
- AC3: WHEN an `address-feedback` item is approved and claimed, the executor SHALL run the claude worktree pass and SHALL NOT invoke any thread-resolve API; verification SHALL assert the resolve endpoint is absent from the prompt contract and the tally counts only replies/commits (falsifying: `test_feedback_prompt_never_resolves` fails if a resolve instruction or call is introduced).
- AC4: WHEN the run addresses at least one thread, the record SHALL complete `done` with an honest tally naming addressed/replied/escalated counts; WHEN zero threads are handled and at least one is escalated, the record SHALL flip to `needs-input` with the escalation summary.
- AC5: WHEN `RUNNABLE_EXECUTORS` gains `address-feedback`, the dashboard SHALL render approve controls for proposed feedback items with no dashboard code change, and `✅ all` SHALL include them.
- AC6: WHEN the curated digest renders, feedback items SHALL appear under rule 2 keyed on executor `address-feedback` and their numbers SHALL be validator-required (falsifying: `test_curator_requires_feedback_numbers` fails if the validator whitelist drops them).

## Decision Coverage

| Decision | Covered by |
|---|---|
| 1. Unaddressed signal via REST probe | collectors probe + AC1/AC2 tests |
| 2. `address-feedback` executor, ✅-gated, RUNNABLE | models/assessor changes + AC5 |
| 3. fix+reply / reply-only / escalate, never resolve | feedback.py prompt contract + AC3 |
| 4. keepcurrent-shaped module, python-verified | feedback.py + runner claim handler tests |
| 5. pb-www hygiene + box sync in prompt | feedback.py prompt sections |
| 6. Outcome mapping done/needs-input | AC4 tests |
| 7. Curator/formatter rename | curator rule 2 + partition + AC6 |

## Field Provenance

| Field | Value | Source |
|---|---|---|
| **Plan** | this file (gated decision-log; `@plan-author` authors the per-issue plan from it) | plan-mode session 2026-08-25 |
| **Files** | collectors.py, models.py, assessor.py, feedback.py (new), runner.py, curator.py, __main__.py + tests | all read this session (assessor.py:100-180, collectors.py:150-250, runner.py:340-460, curator.py:95-130/300-330, models.py WorkItem) |
| **AC** | AC1–AC6 above | decisions 1–7 |
| **TDD Mode** | Full TDD (feature) | /do work-type table |
| **Owning layer** | assessor.py owns emission decisions; collectors.py owns GitLab I/O edges; feedback.py is a NEW leaf executor owning the claude-run contract; runner.py owns claim lifecycle (its `_run_park_claim`/`_run_keep_current_claim` handlers are the seam) | runner.py:356-362 read verbatim this session |
| **Downstream consumers** | curator (partition_counts + prompt rule 2, curator.py:326), formatter (generic executor rendering), dashboard (RUNNABLE_EXECUTORS-driven, zero change), intake ✅ flow (generic) | curator.py:326 read this session |
| **Sibling pattern** | park.py + `_run_park_claim` (newest executor, same shape); keepcurrent.py worktree + verify discipline; implementer.py claude-run edge | files read this session |
| **Verify** | `python3 -m pytest worksweep/tests/ -q` locally + mini; live ✅ on !3997 | Verification section |
| **Plan provenance** | unhardened (claude-solo session — plan-checkpoint hook skip-modes) | mode banner Step 0 |

## Verification

1. Full suite green locally + on the mini (currently 884 tests; expect ~+40).
2. Deploy: rsync worksweep/ to the mini, suite green there.
3. Live acceptance: next sweep should propose "address-feedback — !3997 — N unaddressed threads" (Le's fresh threads today); ✅ it from the dashboard; verify replies land on !3997's threads, **no thread gets resolved**, dev box synced if parked, Discord tally honest. Threads answered by the run must disappear from the next sweep (unaddressed → waiting-on-reviewer).

## Critique Pass

- *Sharpest risk:* the claude run misjudges a reviewer disagreement as fixable and commits an unwanted change. Mitigations: ✅ is per-MR consent; commits land on the feature branch (reviewable, revertable); the prompt's uncertainty rule biases to escalate; verification reports exactly what was committed.
- *Reply authenticity:* replies post from Chandler's account. Accepted — he approved posting replies outright, and the agent-review culture with Le is established. The "addressed in `<sha>`" format keeps replies auditable.
- *Race with reviewers:* thread state re-fetched at run time, not trusted from the sweep snapshot.
- *What this does NOT do:* never resolves threads, never touches other authors' MRs (authored-MR bucket only), never auto-approves, doesn't handle `ci_red` (stays triage).
