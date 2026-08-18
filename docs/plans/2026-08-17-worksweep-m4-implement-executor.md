# Worksweep M4 — Implement Executor + Dev-Slot Sensing + Keep-Current

**Goal:** Seneschal offers to implement assigned issues, parks each on an available dev site, drives the full Ferdinand workflow (`/do` → handoff → TDD implementer → review panel) followed by a magi tribunal on the resulting Draft MR, and keeps every open branch current with master — all on explicit human ✅, one implementation in flight at a time.

**Decisions locked with Chandler 2026-08-17:**
- Draft MRs only (v1 ceiling). Never merges, never force-pushes, never edits master.
- Quality bar = full Ferdinand ceremony (`/rubric:do` on PLA repos — rubric is the installed plugin; `/do` semantics) + magi-review of the Draft MR. Rubric-for-non-PLA is future.
- Dev-slot tiers: **free** (box's branch has no open MR, or its MR is closed/merged) → **handed-off** (box's MR is approved + MERGEABLE + assigned to a maintainer, e.g. dev4/dev5 today) → **never** (box's MR is under live review). Reclaiming a handed-off box posts a Discord note ("dev4 reassigned from !4006 → #1775 — !4006 is approved/awaiting merge").
- First real ✅ target: #1775 (bounded). Perf trio #1703–#1705 after one round-trip is observed.
- Constraints inherited from M3/M3.5: stdlib only; pure fns + thin subprocess edges; atomic queue writes; posts only via `_post_discord`; never-silent; tests `python3 -m pytest worksweep/tests/ -q`; no Claude trailers.

## Task F — Dev-slot sensing (worksweep/devslots.py NEW, config, assessor, formatter, curator)

1. Config: `runner.dev_boxes` list `[{"name":"dev1","host":"chandlerhardy-dev","path":"/home/chandlerhardy/dev1.chandlerhardy-dev/pb-www","url":"https://dev1.chandlerhardy-dev.performancebeef.com/"}, …]` (mini's heartbeat.json; MacBook copy for tests). Empty list = feature off.
2. `devslots.probe(boxes, run_ssh) -> List[DevBox]` (edge injected): one ssh per box → `git branch --show-current` + `git rev-parse HEAD`. Returns `DevBox(name, host, path, url, branch, sha)`; unreachable box → `branch=""` (unknown, treated as never-reclaim).
3. `devslots.classify(boxes, authored, assigned, review_mrs, username) -> Dict[name, tier]` (pure): map box.branch → MR (by `source_branch` — ADD `sourceBranch` to the GraphQL MR nodes + `MergeRequest.source_branch`); no MR / MR state merged|closed → `free`; MR handed-off per Task E's `is_handed_off` → `handed_off`; else `live`. Unknown branch → `live` (fail-safe).
4. `devslots.pick(tiers) -> Optional[str]`: first `free`, else first `handed_off`, else None. Deterministic order = config order.
5. Queue: `WorkItem.dev_box: str = ""` — the executor stamps the claimed box on claim; classification treats a box claimed by a `running`/`approved` implement item as `live` (no double-booking) — pass the queue's claimed boxes into `classify`.
6. Sensor: assigned issues (post covered-issue suppression) become kind `issue`, executor **`implement`** (was `triage`), why `"assigned issue: <title>"`; the formatter/curator render the slot availability once at the top of the issues group: `Dev slots: dev1 free · dev4/dev5 reclaimable (approved, awaiting merge) · others live` — computed from `classify`, not per item.
7. Tests: classify tiers (free/handed_off/live/unknown), pick order, claimed-box exclusion, probe parse of ssh output + unreachable → unknown.

## Task G — Implement executor (worksweep/runner.py + worksweep/implementer.py NEW)

1. `runner.pick_claim` gains executor `implement` (still lowest-number-first across BOTH executors, but **single-flight per executor kind**: at most one `running` implement item at a time; magi-review may run concurrently under its own lock file `~/.worksweep/runner-implement.lock`).
2. `implementer.execute(item, cfg, boxes, run_subprocess, run_ssh) -> ImplementResult(mr_iid, mr_url, dev_url, branch, report_path, verdict)`:
   a. `slot = devslots.pick(classify(...))`; None → `RunnerError("no dev slot available — free one or reclaim")` (item → error, ⚠️ posted, re-proposed next sweep).
   b. Stamp `dev_box` on the record + save (claim the box before any long work).
   c. Checkout: `git -C <checkouts_root>/<repo> fetch origin && git checkout -B feat/<iid>-<slug> origin/master` (slug = kebab of first 5 title words; if the branch already exists on origin → reuse it, don't reset).
   d. `claude -p "/rubric:do #<iid>"` in that checkout, timeout `runner.implement_timeout` (default 5400s = 90 min); non-zero → RunnerError with stderr tail. If the transcript/last stdout contains a HALT marker (`HALT_INSUFFICIENT_CONTEXT`, `HALT_SPEC_AMBIGUITY`, or the /do plan-mode question shape) → item → `needs-input` (NEW status, terminal-ish like error; re-proposed only on next human ✅), post the question text to Discord.
   e. Verify work exists: `git log origin/master..HEAD` non-empty AND `git status --porcelain` clean; else RunnerError("implementer produced no commits").
   f. Push branch; sync onto the slot box: `git-push-sync`-equivalent over ssh (fetch + `checkout -B <branch> origin/<branch>` + verify sha match + curl 200 on box url — reuse the exact recipe hardened 2026-08-17 in ~/bin/git-push-sync; port it into `implementer.sync_to_box(box, branch, run_ssh, http_get)`).
   g. Open Draft MR via `glab mr create --draft --source-branch <branch> --target-branch master --title "Draft: <type>(#<iid>): <title>" --description-file <tmp>` — description generated by `claude -p` using the honest-pr-authoring shape (or the /do output's MR body if it already produced one) and MUST include `Available on <dev_url>` (MR convention). Parse iid from output.
   h. Run the tribunal: `claude -p "/magi:magi-review !<iid>"` (advisory auto-mode since Chandler owns it — hmm: Chandler IS the author here → magi-review runs the FULL fix loop by default. v1: pass `--advisory` explicitly so the executor never auto-fixes; findings are staged as pending drafts for Chandler). Extract verdict block.
   i. Return the result; run_once posts: `🛠️ implemented #<iid> → Draft !<mr> (<dev_url>) · magi: <verdict line> · branch <name>`; record → `done` with `result_sha`, `report_path`, `mr_iid`.
3. Reap: implement `running` older than `implement_timeout + 15min` → error (separate constant from the 45-min magi reap).
4. Config: `runner.implement_timeout` (5400), `runner.dev_boxes` (Task F). Both under the existing runner block.
5. Tests (all subprocess/ssh injected): slot-none → error; halt marker → needs-input + posted question; no-commits → error; happy path → Draft MR created + synced + magi run + done with mr_iid; concurrency: implement + magi-review claims coexist, second implement waits; needs-input not re-picked without new ✅ (intake: `✅ n` on a needs-input item flips it back to approved).

## Task H — Keep-current executor (worksweep/assessor.py, runner.py)

1. GraphQL: `divergedCommitsCount` isn't in the MR node; use REST `merge_requests/<iid>?include_diverged_commits_count=true` for authored open MRs (one call each, ≤10 MRs → acceptable; batch through `_run_glab`). Sensor emits `stale:{repo}!{iid}` (kind `stale`, executor `keep-current`, why `"<n> commits behind master"`) when `diverged_commits_count >= runner.stale_threshold` (default 5). Handed-off MRs (Task E) are EXEMPT — the maintainer will merge them.
2. Executor `keep-current`: in the checkout, `git checkout <source_branch> && git merge origin/master --no-edit`; on conflict → `git merge --abort` + RunnerError("conflicts in <files>") (never auto-resolve source conflicts — mirrors merge-master's Non-Negotiable 2; the compiled-CSS/`$script_version` auto-classes are out of scope for v1 → treat all conflicts as stop). SCSS predicate (`git diff --name-only <pre>..HEAD -- '*.scss'` non-empty) → run `maintenance/compile-css` + commit; push; sync to the box currently serving that branch (via devslots) with verify; done. Post: `🔄 !<iid> merged master (+<n>), dev<k> verified 200`.
3. Batch approval: intake already supports `✅ 1,3-9`; document "✅ all stale" as a future nicety, not built.
4. Tests: threshold gating, handed-off exemption, conflict → abort+error, scss predicate → compile step invoked, sync verify failure → error not done.

## Task I — Deploy + first real round-trip

1. Push; rsync to mini; add `dev_boxes` (6 boxes) + `implement_timeout` + `stale_threshold` to mini config; verify `glab mr create` works non-interactively on the mini (`glab auth` is https/api — should); install the rubric plugin on the mini (mirror `~/repos/rubric` like magi) so `/rubric:do` resolves; confirm `claude -p "/rubric:do --help"`-style probe returns.
2. Kickstart sweep → digest shows the 4 issues with `implement` executor + dev-slot line.
3. Chandler ✅ #1775 → observe: claim dev1 → branch → /do → Draft MR with dev URL → magi advisory verdict → 🛠️ post. Review the MR by hand. Only then consider #1703–#1705.
4. Update ops-board + memory.

**Review discipline:** subagent implementer + task review + fix loop per task (F, G, H); final whole-branch review before Task I. G is the highest-risk task (subprocess-heavy, writes to GitLab) — reviewer must trace every failure path to a Discord post + queue status.
