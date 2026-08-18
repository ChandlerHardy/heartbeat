# Worksweep M3.5 — Signal Filters + Curated Digest

**Goal:** Kill digest noise deterministically, then add an LLM curation pass that turns the item list into a succinct, actionable briefing — with a validator and raw-digest fallback so the never-silent contract survives a flaky LLM.

**Context:** Live system on the mini (heartbeat main @ 70418a0, 167 tests). First real digest (2026-08-17) was 3 messages / 29 items, dominated by: dev-URL hygiene on parked drafts, assigned-issue items duplicating in-flight MRs, and raw GitLab todos. Global constraints from the M3 plan hold: stdlib only, pure functions + thin edges, atomic queue writes, posts only via `_post_discord`, never-silent, tests via `python3 -m pytest worksweep/tests/ -q`, no Claude trailers.

## Task A — Sensor signal filters (worksweep/assessor.py, collectors.py, __main__.py)

1. **Assignee bucket:** add `assignedMergeRequests(state: opened, first: 100)` to `_GRAPHQL_SWEEP_QUERY` (same node fields as authored). Parse as a third list `assigned`; MRs in `assigned` but NOT authored-by-me and NOT already in the other buckets emit `assigned:{repo}!{iid}` (kind `assigned_mr`, executor `triage`, why "assigned to you") — dedupe by id against nothing else (new id namespace). Update the fixture by re-freezing from live (Task A step 1, same discipline as M3 Task 3) — if the live account has no assigned-not-authored MR, synthetic-test the parse path only.
2. **Draft hygiene exemption:** in `assess_own_mr`, emit the `hygiene-devurl` item only when `not mr.is_draft`.
3. **Issue-covered-by-MR suppression:** new pure fn `covered_issue_iids(authored: List[MergeRequest]) -> set[int]` — for each OPEN authored MR, extract issue refs from the title via `re.findall(r"#(\d+)", title)` and from the source branch via `re.search(r"/(\d{3,5})-", ...)`  — branch not available in the GraphQL node today, so title-only is acceptable v1 (`feat(#1701):` convention). `assess_issue` gains a guard: skip when `issue.iid in covered`. Wire through `run_sweep`.
4. **Todo hard filter** (replaces the I5 URL-equality dedupe with a stronger rule, still in `run_sweep` after `dedupe`):
   - normalize each todo's `web_url` by stripping any `#note_...` / fragment suffix and trailing `/`
   - drop the todo if its normalized URL matches the normalized `web_url` of ANY non-todo item emitted this sweep OR any MR in the review/authored/assigned buckets (pass those URLs in)
   - drop todos whose `action` is `review_requested` or `assigned` unconditionally (both have authoritative buckets now)
   - keep what survives (true mentions / directly_addressed on things not otherwise tracked)
5. Tests: draft exemption; covered-issue suppression (title `feat(#1701): ...` suppresses issue 1701, unrelated issue survives); todo filter (note-anchor URL vs MR item, review_requested dropped, novel mention survives); assigned bucket parse + item emission + no-self-assign duplication.

## Task B — Age marker (worksweep/formatter.py)

- `_item_line` gains an optional age suffix: given a record whose `first_seen` is > 5 days before now, append `⏳{d}d` (integer days). Requires threading `now` into `format_messages_from_records(records, now=None)` — default None = no age markers (backward compatible); `run_sweep` passes `deps["now"]()`.
- Age computed from `first_seen` with the same tolerant parsing discipline as `_older_than_days` (unparseable → no marker).
- Tests: 6-day proposed record renders `⏳6d`; fresh record doesn't; unparseable first_seen doesn't; `now=None` renders nothing.

## Task C — Curator (new worksweep/curator.py + __main__ wiring)

**Contract:** `curate(records, now, run_llm) -> Optional[str]` — pure orchestration, LLM behind the injected `run_llm(prompt) -> str` edge.

1. Build the prompt from non-terminal records: for each, `number | kind | executor | repo | ref | why | age-days | status`. Instruct: produce a Discord-ready briefing under 1800 bytes — lead with actionable reviews (magi-review items, numbered, one line each), then feedback/ci on own MRs, then a single collapsed line for parked/hygiene/mention noise ("N low-priority items held in queue: 43, 44, …"). MUST reference items by their exact queue numbers; MUST NOT invent numbers; keep the ✅ instructions implicit (footer is appended by the formatter).
2. **Validator (deterministic, in curator.py):** reject the LLM output (return None) unless: every number matching `\b\d{1,4}\b` referenced in the output exists in the queue's non-terminal numbers; AND every `magi-review`-executor `proposed`/`approved` item's number appears somewhere in the output; AND the output is ≤ 1800 bytes UTF-8 and non-empty. On rejection, log why to stderr.
3. **Edge:** `run_llm` in production = subprocess `[claude, -p, prompt]` with 120s timeout, cwd = repo root; claude binary from a new config field `curator_bin` (default `"claude"`, reuse `claude_bin` config plumbing — add `curate: bool = True` toggle under the `runner` config block as `"curate"`; when False, skip curation entirely).
4. **Wiring in `run_sweep`:** when actionable items exist, try `curate(...)`; on a non-None result post `[header + curated + footer]` as ONE message; on None fall back to the existing `format_messages_from_records` path. Heartbeat/error paths unchanged. The digest header gains `(curated)` when the curator ran, so a fallback is visually distinct.
5. Tests: validator accepts a good briefing and rejects (a) invented number, (b) missing magi-review number, (c) oversized output; run_sweep posts curated single message when run_llm succeeds; falls back to raw multi-part when run_llm raises/times out/fails validation; `curate=False` config skips the LLM entirely.

## Task D — Deploy + live verify (mini)

1. Push heartbeat main; rsync to `mini:~/repos/heartbeat` (same exclusions as cutover).
2. Add `"curate": true` to the mini's `~/etc/heartbeat.json` runner block.
3. Run the full suite on the mini (existing venv), then `launchctl kickstart gui/$UID/com.chandlerhardy.worksweep` and verify the posted digest: single curated message, actionable items lead, parked/hygiene collapsed, valid ✅ numbers, age markers present.
4. Break check: temporarily set `curator_bin` to a bogus path, kickstart, confirm graceful fallback to raw digest (not silence), restore.

**Review discipline:** subagent implementer + task review per task (A, B, C); D is operational. Final: run the loop-closure integration test suite green before deploy.

## Task E — Titles in the digest + ready-to-merge handoff suppression (added 2026-08-17)

**Why:** first curated digest listed `pb-www !4007 -- 3 unresolved threads` — unreadable without the title, and wrong: !4007 is approved (2 LGTMs), `detailedMergeStatus: MERGEABLE`, `blockingDiscussionsResolved`, and assigned to the maintainer (leyang) to merge. Non-blocking threads on a handed-off MR are not Chandler's work.

1. **GraphQL:** add `title` (already fetched), `approved`, `detailedMergeStatus`, `assignees { nodes { username } }` to the authored + assigned MR nodes (review-requested nodes: `title` only is enough). Re-freeze the fixture from live. `MergeRequest` gains `approved: bool = False`, `merge_status: str = ""` (upper-cased enum), `assignees: tuple = ()`. Parse in `_gql_mr`.
2. **WorkItem gains `title: str = ""`** — set from `mr.title` / `issue.title` in every assess_* emitter (todo items: leave "" — their `why` already carries the target). Old queue records load with `""` (defaulted).
3. **Handoff rule (assessor):** `is_handed_off(mr, username) -> bool` = `mr.approved and mr.merge_status == "MERGEABLE" and any(a != username for a in mr.assignees)`. In `assess_own_mr`, when handed off: emit NO `feedback:` item, NO `magi:` item, NO hygiene item; instead emit ONE `handoff:{repo}!{iid}` item (kind `handoff`, executor `none`, risk `low`, why `"ready to merge → assigned to <other-assignees>"`). Also: `mr.approved` alone (LGTM'd but not yet mergeable) suppresses the `magi:` item — magi is pre-review, not post-approval.
4. **Formatter:** `_item_line` renders the title (truncated to 60 chars, single-line, `…`) after the ref link: `12. `magi-review` pb-www [#4061](url) *Add Usage column to the feed inventory…* — review requested (CI green)`. `handoff` items render with a ✅ prefix and are placed in a trailing "Handed off (no action)" group by the numbered formatter — they still get numbers (stable-numbering contract) but the curator is told they're informational.
5. **Curator prompt:** record lines gain `| title` ; instructions: write each actionable line as `<num>. <repo> !<iid> — <short title> — <why>`; put handoff items in one trailing informational line ("Handed off: !4007 → leyang"), never under "Needs your review". Validator: `handoff` items are NOT required to appear (they're informational) but their numbers stay whitelisted.
6. **Reconcile:** a `feedback:` record whose MR becomes handed-off is absent from fresh → already dropped by the M2 rule (proposed+gone). Add `handoff` ids to `resolutions()`? No — the feedback id just disappears; but add a resolution `{"feedback:{repo}!{iid}": "handed-off"}` so an APPROVED feedback item (mid-flight) also closes cleanly rather than lingering.
7. Tests: parse fields; handoff detection (approved+mergeable+other assignee = True; approved but self-only assignee = False; mergeable but not approved = False); own-MR emission under handoff (only the handoff item); approved-not-mergeable suppresses magi item only; title truncation; curator prompt carries titles; validator ignores missing handoff numbers.
