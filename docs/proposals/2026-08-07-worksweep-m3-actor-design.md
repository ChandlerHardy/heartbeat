# Worksweep M3 — Accurate Sensor, Mini Runtime, Magi Executor

**Date:** 2026-08-07 · **Status:** approved design, pre-implementation
**Direction:** Approach A ("close the loop on the mini") — chosen over a cloud-routine rebuild (loses the tribunal's codex/seneschal legs) and seneschal-as-actor on OCI (puts PLA source on a non-PLA machine, contra the risk register).

## Problem

Worksweep today is a read-only GitLab digest (M1) with a persistent WorkItem queue and Discord approval intake (M2, branch `feat/worksweep-m2-queue-approval`, unmerged). It is not used because:

1. **It lies.** "Review requested" items persist after Chandler has reviewed — the collector queries `reviewer_username` on open MRs, and GitLab keeps reviewers listed post-review. Nothing checks review state, so items only clear on merge.
2. **It misses.** Own-MR signals are limited to "no magi report" and "missing dev URL". No reviewer-feedback item, no CI-red item (the list endpoint omits `head_pipeline`), and "already magi-reviewed" is a glob over MacBook-local `.magi` files (iid-only, machine-bound, suppresses re-review forever).
3. **It fails silently.** MacBook launchd fires on wake before the network is up; the Discord post dies on an SSL handshake timeout and everything lands in a stderr file nobody reads.
4. **Approvals go nowhere.** M2 flips items to `approved`; no executor exists (M3 was never built).

## Goal

The Jarvis loop, first slice: the sweep tells the truth about what needs Chandler's attention; approving a review item in Discord causes a magi tribunal review to run unattended on the mini, with the verdict back in Discord and Warnings staged as pending draft comments. Draft-only blast radius throughout.

## Design

### 1. Sensor truth (GraphQL review states)

GitLab's "Your work / Merge requests" dashboard buckets (Returned to you / Review requested / Waiting for author) are driven by per-reviewer **review state** (`unreviewed`, `reviewed`, `requested_changes`, `approved`). The sensor adopts the same source of truth.

- **One GraphQL query** (via `glab api graphql`, replacing the per-repo REST list calls) fetches, for the configured username:
  - MRs where review is requested of me, with **my** `reviewState`, draft flag, head SHA, CI status, `updated_at`.
  - My authored open MRs with reviewer states, head pipeline status, and unresolved-discussion count.
- **Bucket → item mapping:**
  | GitLab truth | Queue behavior |
  |---|---|
  | Reviewer of MR, my state `unreviewed` | actionable `review-needed` item (kind `review_request`, executor `magi-review`) |
  | Reviewer, my state `reviewed`/`requested_changes`/`approved` | no item; an existing queue item for that MR auto-transitions `done` (reason `already-reviewed`) |
  | Review re-requested after new commits (GitLab resets my state to `unreviewed`) | item re-proposed automatically — no local SHA heuristics needed |
  | My MR: any reviewer `requested_changes` OR unresolved discussions > 0 | `address-feedback` item (executor `triage` for now) |
  | My MR: head pipeline failed | `ci-red` item (executor `triage` for now) |
  | My MR: no magi run recorded in queue history | `magi-review` item (as today, minus the file glob) |
- **Kill the `.magi` glob.** "Has magi run" becomes queue-recorded state: when the executor completes (or when a `done` record exists for that repo+iid at the current head SHA), suppress. One-time bootstrap: seed from the existing glob on first run on the MacBook, then never read the filesystem again. This removes the last MacBook dependency.
- Draft MRs: review-requested items on drafts are collected but tagged `(draft)` in the digest; they remain approvable (the !4020 session proved draft advisory reviews are useful).
- Parsing stays in pure `parse_*` functions with frozen GraphQL response fixtures, per the existing collector/test pattern.

### 2. Queue lifecycle additions

Statuses today: `proposed` → `approved` → (`running` reserved). Add:

- `done` — terminal. Set by: executor completion (with `result_sha`, `report_path`), or sensor reconciliation (`already-reviewed`, `mr-merged`, `mr-closed`).
- `error` — terminal-ish; re-proposed on next sweep if the underlying signal persists. Carries `error_summary`.
- Reconcile keeps its SHA rule (new commits reset `approved` → `proposed`). `done` records are **retained** (not dropped when gone from sweep) for N=90 days — they are the "already reviewed" memory. Compaction drops older terminal records.

### 3. Mini runtime

- Sweep (`--discord`) daily 09:00 CT + intake poller every 5 min, via launchd on the mini (always-on; wake race gone). MacBook plists (`com.chandlerhardy.worksweep*`) unloaded at cutover.
- **Message contract (scout-routine discipline): every sweep posts exactly one Discord message** — 📋 digest, 🔍 nothing-new heartbeat (with counts), or ⚠️ error carrying the exception text. A quiet channel is now always distinguishable from a broken job. stderr feeds the ⚠️ path instead of a dead `.err` file.
- Mini prerequisites (verified during rollout): `glab` auth (mini is PLA-provided — allowed), fresh clones of the PLA repos it reviews (pulled from origin, independent of laptop worktrees), `claude` CLI + magi plugin, codex CLI for Balthasar. Seneschal/Caspar: MR-mode reviews use CodeRabbit's posted review when present; degraded-run rules already handle its absence (drafts are skipped by CR — proven in the !4020 run).
- Config stays `~/etc/heartbeat.json`; webhook host allowlist and no-redirect opener carry over unchanged.

### 4. M3 executor v1 — `magi-review` only

- A third launchd job (`worksweep-runner`, every 10 min) polls the queue for `approved` items with `executor: magi-review`.
- **Claim:** atomically flip to `running` (same temp-file+replace write; single-flight lockfile so at most one review runs). `running` older than 45 min → flip to `error` (stale claim), ⚠️ to Discord.
- **Execute:** in the mini's clone of the item's repo: fetch origin, then `claude -p "/magi:magi-review !<iid>"` with a 30-minute hard timeout (below the 45-minute stale-claim threshold, so a timed-out run is reaped by its own runner, not a later one). Advisory mode is implied by non-ownership (the skill auto-selects it) — the run produces a tribunal report, a Discord-postable verdict, and pending draft comments only. No implementer dispatch, no publishing, no pushes.
- **Report back:** one Discord message per completed item — verdict block, Warning/Minor counts, report path, MR link — threaded/correlated to the digest item number. Queue record → `done` with `result_sha` (reviewed head) and `report_path`.
- **Failure:** non-zero exit or timeout → `error` + ⚠️ with the last ~15 lines of stderr.
- Other executors (`address-feedback` reply drafts, fix executor) are explicitly out of scope for v1; items carrying those kinds render in the digest but are not approvable-to-execution yet (intake replies for them get a "no executor yet" reaction).

### 5. Testing

- Pure functions (`parse_*`, bucket mapping, reconcile transitions incl. `done`/`error`) unit-tested against frozen GraphQL fixtures — extends the existing suite.
- Executor: `--dry-run` mode fabricates a tribunal result end-to-end (claim → report → done) without invoking `claude`.
- Rollout is the integration test: after phase 1 lands, one sweep on the current MacBook setup must reproduce Chandler's live dashboard (screenshot 2026-08-07: 3 review-requested, 2 returned-to-you, 2 waiting-for-author suppressed) before the mini cutover.

## Rollout order (each independently shippable)

1. Merge `feat/worksweep-m2-queue-approval` to main (8 commits, already magi-reviewed per `.planning/handoffs/worksweep-magi-fixes.md`).
2. Sensor truth + queue lifecycle (verify against the live dashboard same-day).
3. Mini cutover with the one-message contract; decommission MacBook plists.
4. Runner + magi executor; first unattended review of a real approved item.

## Out of scope (deliberate)

- Personal GitHub estate sensing (roadmap ②) — after the loop closes on PLA.
- Fix/reply executors — after trust is earned with the draft-only executor.
- Seneschal convening (M3-full "steward") — architecture intent stands; this slice keeps the actor a dumb runner invoking the magi skill.
