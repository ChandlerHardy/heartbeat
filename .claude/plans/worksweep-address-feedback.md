## Issue: (none — freeform personal-repo work) — worksweep `address-feedback` executor
## Branch: feat/worksweep-address-feedback
## Repo: heartbeat (/Users/chandlerhardy/repos/heartbeat)
## Status: READY
## Source: decision-log

> **Resolution recorded.** The one source-internal contradiction this plan halted on was
> resolved by the orchestrator + Chandler in the gated log's `## Round 3 resolution`
> (decision-log:85-91, commit e70233e): Decision 2 wins, AC1 clause 2 is clarified to the
> thread axis, and both previously-deferred gaps are adopted as recommended. Those three
> settlements are folded into AC #16, #17, and #18 and into `## Resolved Ambiguity (Round 3)`
> below. No other plan content changed. Status is `READY`.

## Decision Log (gated)

Source: `/Users/chandlerhardy/repos/heartbeat/.planning/decisions/worksweep-address-feedback.md`
(gate attestation `<!-- GATED: ExitPlanMode approved -->` present at line 1; commits f2804a8 +
427502a). Quoted bit-exact below by byte-range splice, verified by full-range diff (Step 3).

### Assumption Ledger + Decision Log + Scope + Acceptance Criteria (decision-log:8-38)

```decision
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
```

### Round 2 amendment — Decision 8 + AC7 (decision-log:79-83)

```decision
## Round 2 amendment (Chandler, mid-ceremony 2026-08-25)

8. **Actor-attributed approvals.** The dashboard approve POSTs (`/approve` selected and approve-all) accept an optional `actor` string field. When present and equal to `"claude"`, the Discord audit line renders "(dashboard · claude)"; absent/other → "(dashboard)" exactly as today. Rationale: Chandler will sometimes tell Claude (on his laptop, tailnet) to accept items on his behalf — "✅ all, then move on" without context-switching to the dashboard; the ✅ gate stays a human-consent gate, so the audit trail must stay legible about which hand pressed the button. Claude-side policy (not enforced in code): agents only approve on Chandler's explicit instruction, never on their own initiative. *Rejected:* separate authenticated agent endpoint (overkill on a tailnet-only, CSRF-guarded writer); no attribution (audit ambiguity between human and agent approvals).
   - AC7: WHEN an approve POST carries `actor: "claude"`, the Discord audit post SHALL contain "(dashboard · claude)"; WHEN the field is absent, the post SHALL contain "(dashboard)" unchanged (falsifying: `test_approve_actor_attribution` fails if the actor suffix is dropped or applied unconditionally).
   - Scope note: validate/clamp the actor string (short whitelist or length-cap + sanitize) — it flows into a Discord post.
```

### Round 3 resolution — HALT settlement + adopted deferrals (decision-log:85-91)

```decision
## Round 3 resolution (orchestrator, 2026-08-25 — resolves the plan-author's HALT_SPEC_AMBIGUITY)

**AC1 clause 2 is clarified to the thread axis, Decision 2 wins.** For an authored MR with `changes_requested == True` and zero unaddressed threads, the sweep SHALL emit the plain `triage` info row exactly as Decision 2 states ("changes requested" with nothing concrete to act on is information, not runnable work). AC1 clause 2 ("no feedback item at all") constrains only the thread-derived emission: no `address-feedback` item when every unresolved thread has our reply as its last non-system note. `test_addressed_threads_emit_nothing`'s fixture uses `changes_requested=False` so the asserted list is empty.

**Deferred decisions adopted as recommended:**
- Run-time re-fetch finding zero unaddressed threads (reviewer replied/resolved between sweep and run) → complete `done` with an honest "0 addressed, 0 replied, 0 escalated — threads already answered" tally. Never an error.
- The feedback executor's `claude -p` timeout comes from cfg (default 1800s), inside the 45-min reap window.
```

## Sibling Patterns

Located by `rg`/`grep -n` and Read-verified this session; quoted bit-exact by byte-range splice.
These are the concrete templates the implementer models on — not name-drops.

### S1 — `park.execute` + `ParkResult` reporting (worksweep/park.py:90-132)

The newest executor's whole shape: guard the injected edges, derive the iid, refuse without a
branch, do the work, return a frozen result dataclass, render a Discord `done_message`.

```sibling
def execute(item: WorkItem, cfg, boxes: Sequence,
            run_ssh: Callable[[str, str], str] = None,
            http_get: Callable[[str], int] = None,
            run_glab: Callable = None) -> ParkResult:
    """Park one MR's branch on a free dev box and link it from the description.

    Order matters: the description is only touched AFTER the box is proven to
    serve HTTP 200, so a failed sync can never leave the MR advertising a dev
    URL that shows an error page.
    """
    if run_ssh is None or http_get is None or run_glab is None:
        raise RunnerError("park executor is wired without an ssh/http/glab edge")
    iid = iid_of(item)
    branch = item.branch
    if not branch:
        raise RunnerError(f"no source branch recorded for !{iid} "
                          f"(WorkItem.branch was not set by the assessor)")

    slot = implementer.select_slot(boxes)
    if slot is None:
        raise RunnerError(
            f"no free dev slot to park !{iid} on — free one or reclaim a box, "
            f"then re-approve (this item re-proposes itself next sweep)")

    # Syncs, checks for drift, and health-checks the box: returns only on a
    # branch that actually landed AND a 200.
    result_sha = implementer.sync_to_box(slot, branch, run_ssh, http_get)

    updated = False
    description = fetch_description(run_glab, item.repo, iid)
    new_description = prepend_header(description, slot.url)
    if new_description is not None:
        put_description(run_glab, item.repo, iid, new_description)
        updated = True
    return ParkResult(iid=iid, box_name=slot.name, dev_url=slot.url,
                      result_sha=result_sha, description_updated=updated)


def done_message(result: ParkResult) -> str:
    tail = ("description updated" if result.description_updated
            else "description already had a dev link")
    return (f"🅿️ !{result.iid} parked on {result.box_name} (200) · {tail}\n"
            f"<{result.dev_url}>")
```

### S2 — `runner._run_park_claim` (worksweep/runner.py:425-455)

The claim-handler template `_run_address_feedback_claim` copies. Note what it does NOT do:
it has no `NeedsInputError` branch, so a `NeedsInputError` would be swallowed by its
`except RunnerError` and recorded as `error`. AC #12 requires the new handler to add that
branch (see S4).

```sibling
def _run_park_claim(cfg, deps: Dict[str, Callable],
                    target: QueueRecord) -> int:
    """The park half of the shared magi/keep-current/park pass.

    Called with the claim already saved as `running`. Like keep-current, every
    exit ends in BOTH a queue status and a Discord post: this executor takes
    over a dev box and rewrites an MR description, so a silent failure would
    leave a box occupied and nobody told.
    """
    number = target.number
    if "execute_park" not in deps:
        _fail_and_post(deps, cfg, number,
                       "park executor is not wired into this runner "
                       "(no execute_park dep)", _PARK)
        return 1
    try:
        result = deps["execute_park"](target.item, cfg)
    except RunnerError as e:
        _fail_and_post(deps, cfg, number, str(e), _PARK)
        return 1
    except Exception as e:
        _fail_and_post(deps, cfg, number, f"{type(e).__name__}: {e}", _PARK)
        return 1
    updated = _apply_to_fresh(
        deps, cfg, number,
        lambda fresh: complete(fresh, number, result.result_sha, "",
                               deps["now"]()))
    if updated is not None:
        from . import park as _park       # local: park imports implementer
        _post(deps, cfg, _park.done_message(result))
    return 0
```

### S3 — `keepcurrent._verify_resolution` (worksweep/keepcurrent.py:254-283)

The "trust nothing the claude run did until python proves it, restore on any failure" discipline
that Decision 4's `python-verified edges` names. The feedback executor's verification mirrors this
shape with different predicates (remote sha advanced / reply present) — and, per Decision 3,
**never** a resolution predicate.

```sibling
def _verify_resolution(run_subprocess: Callable, checkout: str,
                       pre: str) -> None:
    """Trust nothing the resolver did until proven: no unresolved files, the
    merge actually committed with origin/master as second parent, and a
    clean tree. Any failure restores `pre` -- nothing half-resolved may
    reach the push."""
    unresolved = _git(run_subprocess, checkout,
                      ["diff", "--name-only", "--diff-filter=U"],
                      allow_fail=True).strip()
    if unresolved:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError(f"resolver left unresolved conflicts: "
                          f"{', '.join(unresolved.split())}")
    merge_head = _run(["git", "-C", checkout, "rev-parse", "-q", "--verify",
                       "MERGE_HEAD"], run_subprocess, timeout=_GIT_TIMEOUT)
    if merge_head.returncode == 0:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError("resolver did not commit the merge")
    parent2 = _git(run_subprocess, checkout, ["rev-parse", "HEAD^2"],
                   allow_fail=True).strip()
    master = _git(run_subprocess, checkout, ["rev-parse", "origin/master"],
                  allow_fail=True).strip()
    if not parent2 or not master or parent2 != master:
        _restore(run_subprocess, checkout, pre)
        raise RunnerError("resolver's HEAD is not a merge of origin/master")
    status = _git(run_subprocess, checkout, ["status", "--porcelain"],
                  allow_fail=True)
    if status.strip():
        _restore(run_subprocess, checkout, pre)
        raise RunnerError("resolver left a dirty tree")
```

### S4 — the only existing `NeedsInputError` routing (worksweep/runner.py:511-519)

`_run_implement_pass` is the sole precedent for Decision 6's `needs-input` outcome: catch
`NeedsInputError` BEFORE `RunnerError`, call `needs_input(...)` (runner.py:106-112), post ❓,
and return 0 — a question is a handled outcome, not a failure.

```sibling
        try:
            result = deps["execute_implement"](target.item, cfg, [slot])
        except NeedsInputError as e:
            if _apply_to_fresh(
                    deps, cfg, number,
                    lambda fresh: needs_input(fresh, number, str(e),
                                              deps["now"]())) is not None:
                _post(deps, cfg, f"❓ #{iid} needs your input: {e}")
            return 0        # a question is a handled outcome, not a failure
```

### S5 — payload-field validation (worksweep/dashboard.py:1384-1395)

`_valid_number`'s shape is the template for Decision 8's `actor` validation: a small pure
function beside the other validators, returning the clean value or the safe default, with the
trap documented in the docstring.

```sibling
def _valid_number(payload) -> Optional[int]:
    """Validate a `{"number": N}` envelope. None when malformed (-> 400).

    Bools are excluded explicitly: `isinstance(True, int)` is True in Python,
    so `{"number": true}` would otherwise dismiss record 1.
    """
    if not isinstance(payload, dict):
        return None
    number = payload.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return number
```

## Resolved Ambiguity (Round 3)

The one source-internal contradiction is settled in the gated source itself (decision-log:85-91,
quoted verbatim above). Recorded here so a reviewer can see what was decided and why, without
re-litigating it.

| Conflict (now settled) | Source A | Source B | Adopted resolution |
|---|---|---|---|
| An authored MR with `changes_requested == True` and **zero** unaddressed threads (every unresolved thread already answered by Chandler, or no unresolved threads at all): does the sweep emit a feedback row? | **AC1 clause 2** (decision-log:33): "WHEN every unresolved thread's last non-system note IS by the configured username, the sweep SHALL emit **no feedback item at all**." | **Decision 2** (decision-log:20): "`changes_requested` with zero unaddressed threads **stays a plain `triage` info row** (nothing concrete to act on)." | **Decision 2 wins** (decision-log:87). AC1 clause 2 constrains only the thread-derived emission — no `address-feedback` item when every unresolved thread has our reply as its last non-system note. The plain `triage` info row survives for the `changes_requested`-only case, and `test_addressed_threads_emit_nothing`'s fixture uses `changes_requested=False`. Implemented by **AC #16**; the falsifying test is **AC #3**. |

**Blast radius, now bounded:** the emission branch in `assessor.assess_own_mr` (assessor.py:127-135)
keeps two arms rather than one — the runnable `address-feedback` arm (AC #4) and the informational
`triage` arm (AC #16). `curator.partition_counts` therefore keeps its `triage`/`feedback` predicate
alongside the new `address-feedback` one (AC #13), and the digest's actionable count is unchanged
for a REQUESTED_CHANGES MR whose threads are all answered.

## Field Provenance

| Plan field | Derived from (quoted source reference) | Notes |
|---|---|---|
| **Plan** `.claude/plans/worksweep-address-feedback.md` | orchestrator prompt (`output_dir`) | Non-source field (sanctioned) |
| **Files** entry `worksweep/collectors.py` | D-log row 1 ("targeted per-MR REST probe"); §Scope/files (decision-log:29) | Probe edge + pure predicate; collectors.py:1-6 docstring mandates the `collect_*` shell / `parse_*` pure split |
| **Files** entry `worksweep/models.py` | D-log row 1 + row 2; §Scope/files (decision-log:29) | `MergeRequest.unaddressed_count` beside `unresolved_count` (models.py:38); `RUNNABLE_EXECUTORS` (models.py:83) |
| **Files** entry `worksweep/assessor.py` | D-log row 2 ("switches executor from `triage`, gains `branch=mr.source_branch`") | Emission block is assessor.py:127-135 |
| **Files** entry `worksweep/feedback.py` (NEW) | D-log row 3 + row 4 + row 5 | New leaf executor module named verbatim in row 4 |
| **Files** entry `worksweep/checkouts.py` | D-log row 4 ("Worktree checkout of the branch (keepcurrent's worktree discipline)") | **Scope delta vs §Scope/files** — see `## Discovered Scope Deltas`; `_WORKTREE_EXECUTORS` (checkouts.py:37) gates who gets a worktree, and `worktree_for` silently returns the SHARED clone (checkouts.py:49-50) for any executor not listed |
| **Files** entry `worksweep/runner.py` | D-log row 4 + row 6; A5 (decision-log:14) | `_ALL_EXECUTORS` (runner.py:32), pick_claim gate (runner.py:355), new claim handler beside `_run_park_claim` (runner.py:425-455) |
| **Files** entry `worksweep/curator.py` | D-log row 7 ("prompt rule 2 + `partition_counts` (curator.py:326)") | Rule 2 at curator.py:109-110, `partition_counts` at curator.py:319-330, `validate` required-set at curator.py:271-273 |
| **Files** entry `worksweep/__main__.py` | D-log row 1 (probe) + §Scope/files "(probe + executor wiring)" | Sweep seam __main__.py:379-391; opt-in dep pattern __main__.py:398-410 + 719-722; executor edge beside `_execute_park` (__main__.py:574-594) |
| **Files** entry `worksweep/dashboard.py` | D-log row 8 (decision-log:81) "The dashboard approve POSTs (`/approve` selected and approve-all) accept an optional `actor` string field" | Round-2 amendment. Spans: `_audit_message` (dashboard.py:1353-1381, suffix literal at :1365), POST routing (dashboard.py:1560-1566), `_approve` (dashboard.py:1684-1707), `_audit` (dashboard.py:1709-1723), validator siblings (dashboard.py:1332-1350, 1384-1395) |
| **AC #1, #2** unaddressed predicate | D-log row 1: "a thread is **unaddressed** iff it is resolvable, not resolved, and the last non-system note's author ≠ chandler.hardy" + A2 (decision-log:11) | Three-clause predicate quoted verbatim; `chandler.hardy` is `cfg.username` (config.py:12, populated at config.py:109 from `gitlab.username`) |
| **AC #3** falsifying emission test | source AC2 (decision-log:34) verbatim test name | Mutation named by the source itself |
| **AC #4** item shape | D-log row 2: id kept, "switches executor from `triage`, gains `branch=mr.source_branch`, why = \"N unaddressed thread(s)\" (prefixed \"changes requested, \" when set). Emitted only when `unaddressed_count > 0`" | Plural rule copied from the existing why-string builder (assessor.py:131) |
| **AC #5** registry + dashboard | D-log row 2 + source AC5 (decision-log:37) + A4 (decision-log:13) | `has_checkbox` keys purely off `RUNNABLE_EXECUTORS` (dashboard.py:974) — Read-verified, so AC5's "no dashboard code change" holds for the registry path |
| **AC #6** never auto-approved | D-log row 2: "✅-gated — never auto-approved"; *Rejected:* auto-approve | `cfg.auto_approve` default `("keep-current",)` (config.py:54) must not gain the new name |
| **AC #7** sweep probe seam | D-log row 1 + A6 (decision-log:15) "a per-MR REST probe is cheap and does not belong in the big sweep query" | Degrade-not-fail behaviour mirrors the sibling opt-in probe (__main__.py:398-410) |
| **AC #8** never-resolve | D-log row 3: "the prompt forbids the resolve API and verification never counts resolution" + source AC3 (decision-log:35) | Test name `test_feedback_prompt_never_resolves` is the source's |
| **AC #9** three-way classification | D-log row 3 verbatim: "Fixable → commit, reply \"addressed in `<short-sha>`\" on the thread; question → reply with the answer; judgment call or disagreement → no reply, escalate. Uncertainty biases to escalate." | Reply format is quoted, not invented |
| **AC #10** worktree + re-fetch + verify | D-log row 4 verbatim: "Worktree checkout of the branch (keepcurrent's worktree discipline), re-fetch unaddressed threads at run time … then python verification: remote sha advanced when commits were claimed, replies present on the threads it claims to have answered, honest tally" | Verification predicates enumerated by the source |
| **AC #11** pb-www hygiene block | D-log row 5 verbatim: "SCSS predicate → `maintenance/compile-css`, cache-buster bump on CSS/JS changes, push; sync the parked dev box when the MR description names one" | Concrete paths from the sibling: SCSS pathspec `www/home/scss/*` (keepcurrent.py:64), cache-buster file (keepcurrent.py:67) |
| **AC #12** outcome mapping | D-log row 6 verbatim (decision-log:24) + source AC4 (decision-log:36) | `needs-input` machinery is runner.py:106-112; routing precedent runner.py:511-519 (S4) |
| **AC #13** curator rename | D-log row 7 verbatim + source AC6 (decision-log:38) | `ci_red` stays `triage` is explicit in row 7 |
| **AC #14** actor attribution | D-log row 8 + AC7 (decision-log:82) verbatim: "WHEN an approve POST carries `actor: \"claude\"`, the Discord audit post SHALL contain \"(dashboard · claude)\"; WHEN the field is absent, the post SHALL contain \"(dashboard)\" unchanged" | Whitelist-of-one is **derived from AC7's own postcondition set**, not chosen: AC7 admits exactly two rendered outcomes, so no non-`claude` actor value can ever reach Discord. That satisfies the scope note's "short whitelist" option (decision-log:83) without a free judgment call |
| **AC #15** hermetic tests | testing-philosophy §Mock Boundary Rules + D-log row 4 "python-verified edges"; sibling discipline stated at test_park.py:1-4 ("this file must never touch ssh, http or glab") | Non-source-derivable portion is the stack convention |
| **TDD Mode** `Full TDD` | testing-philosophy §Phase 0 + source Field Provenance row "TDD Mode: Full TDD (feature)" (decision-log:59) | New behaviour with a pure-function core; sanctioned non-source field, and the source agrees |
| **Owning layer** (worksweep-internal) | D-log source Owning-layer row (decision-log:60) verbatim mapping | See the diagnostic field for the file:line map |
| **Downstream consumers** | D-log source row (decision-log:61) + `rg RUNNABLE_EXECUTORS` sweep | Enumerated with file:line in the diagnostic field |
| **Sibling pattern** | D-log row 4 ("modeled on keepcurrent.py/park.py") + source Sibling row (decision-log:62) | `rg`-located, Read-verified, quoted into `## Sibling Patterns` S1-S5 per Step 4c |
| **Verify** | detected stack convention + source Verification §1 (decision-log:68) | `python3 -m pytest worksweep/tests/ -q`; non-source field is the command shape |
| **Plan provenance** `unhardened` | orchestrator prompt; source Field Provenance row (decision-log:64) | No cross-model harden checkpoint ran |
| **AC #16** `changes_requested`-only informational row | Round 3 resolution (decision-log:87) verbatim: "the sweep SHALL emit the plain `triage` info row exactly as Decision 2 states"; D-log row 2 (decision-log:20) | Settles the halted branch; `why="changes requested"` is the existing string at assessor.py:129 |
| **AC #17** zero-unaddressed run-time outcome | Round 3 adopted deferral (decision-log:90) verbatim: "complete `done` with an honest \"0 addressed, 0 replied, 0 escalated — threads already answered\" tally. Never an error." | Fills D-log row 6's missing third branch (decision-log:24) |
| **AC #18** claude-run timeout | Round 3 adopted deferral (decision-log:91) verbatim: "comes from cfg (default 1800s), inside the 45-min reap window" | `cfg.runner_timeout` already defaults to 1800 (config.py:24), so no config.py edit |
| **decision-verify** Round 3 resolution (decision-log:85-91, 7 lines) | diff against source-range: clean (0 bytes) | Same byte-range splice discipline |
| **decision-verify** Decision Log (decision-log:8-38, 31 lines) | diff against source-range: clean (0 bytes) | Full-range diff verified — quoted by byte-range splice, so no transliteration of `≠ → ✅ ✔ —` is possible |
| **decision-verify** Round 2 amendment (decision-log:79-83, 5 lines) | diff against source-range: clean (0 bytes) | Same splice discipline |
| **diff-against-current** `MergeRequest` (models.py:23-46 vs D-log row 1) | 1 delta: `unaddressed_count: int = 0` is absent today (`unresolved_count` at models.py:38 is the only thread field) → AC #4/#7. No existing field changes type or default | Trailing-default placement required: the dataclass is frozen with positional construction in `_gql_mr` (collectors.py:223-242) |
| **diff-against-current** `RUNNABLE_EXECUTORS` (models.py:83 vs D-log row 2) | 1 delta: 4-tuple gains `"address-feedback"` → AC #5. Pinned by an equality test (test_apply_approvals.py:253-256), so the literal set in that test is a required co-edit | A set-equality pin, not a subset check — it fails closed |
| **diff-against-current** feedback emission (assessor.py:127-135 vs D-log row 2) | 3 deltas: gate `changes_requested or unresolved_count > 0` → `unaddressed_count > 0` (AC #4); `executor="triage"` → `"address-feedback"` (AC #4); `branch=` absent → `mr.source_branch` (AC #4). 4th delta: the `changes_requested`-only arm is RETAINED as an informational `triage` row per the Round 3 resolution (AC #16) | `id`, `kind`, `risk`, `web_url`, `sha`, `title` unchanged — the id is deliberately preserved (row 2: reconcile/fresh-wins continuity) |
| **diff-against-current** `_ALL_EXECUTORS` / pick_claim (runner.py:32, 355 vs D-log row 4 + A5) | 2 deltas: `_ALL_EXECUTORS` gains the name; `pick_claim(records, (_MAGI, _KEEP_CURRENT, _PARK))` at runner.py:355 gains it → AC #5, #12. `_SINGLE_FLIGHT` (runner.py:40) unchanged — the shared pass runs one claim per invocation regardless (runner.py:337-342) | |
| **diff-against-current** `partition_counts` (curator.py:319-330 vs D-log row 7) | 1 delta: line 326's `r.item.executor == "triage" and r.item.kind in ("feedback", "ci_red")` must split so `feedback` keys on `address-feedback` while `ci_red` keys on `triage` → AC #13 | The one-line predicate carries both kinds today; splitting it is the whole edit |
| **diff-against-current** `validate` required-set (curator.py:271-273 vs D-log row 7) | 1 delta: predicate `executor == "magi-review" or kind == "issue"` gains proposed/approved `address-feedback` → AC #13 | `_MAGI_LEAD_STATUSES` (curator.py:55) already includes `needs-input`, which is correct for this executor |
| **diff-against-current** `_audit_message` (dashboard.py:1353-1381 vs D-log row 8) | 2 deltas: signature gains a keyword-defaulted `actor` param (positional call sites at test_dashboard.py:1245, 1258 must keep working); the `suffix` literal at dashboard.py:1365 becomes actor-dependent → AC #14. The clamp loop below it needs no change because it already closes over `suffix` | |
| **diff-against-current** approve POST path (dashboard.py:1560-1566, 1684-1707, 1709-1723 vs D-log row 8) | 3 deltas: `do_POST` reads+validates `actor` from the already-parsed payload after `_valid_numbers` (dashboard.py:1560-1563); `_approve(path, numbers)` → `_approve(path, numbers, actor)`; `_audit(numbers, updated)` → `_audit(numbers, updated, actor)` → AC #14. `/dismiss`'s own audit (dashboard.py:1671-1673) is OUT of scope — row 8 names only the approve POSTs | The browser JS (dashboard.py:908, 921) deliberately keeps sending `{numbers:n}` with no actor → renders "(dashboard)" exactly as today |
| **impact-trace** `RUNNABLE_EXECUTORS` consumers | exhaustive `rg -n 'RUNNABLE_EXECUTORS' worksweep/`, every hit Read-verified: `queue.py:22,151` (is_dismissable — **behaviour change, in scope, AC #5 note**), `dashboard.py:51,974` (has_checkbox — no change, A4 confirmed), `approvals.py:23,110` (`✅ all` gate — no change, source AC5 satisfied generically), `models.py:83` (definition), `tests/test_apply_approvals.py:249-256` (**required co-edit**) | No call site is out of scope; the only code edit is the tuple itself plus the test pin |
| **impact-trace** `kind == "feedback"` consumers | exhaustive `rg -n '"feedback"' worksweep/` (non-test), each Read-verified: `assessor.py:134` (emission — AC #4), `models.py:91` (kind docstring — comment refresh), `curator.py:326` (partition — AC #13). `formatter.py` has **no** feedback/triage branch — its only executor switch is `_is_auto_merge` on `keep-current` (formatter.py:118-124), so row 7's "Formatter needs no structural change" is confirmed on disk | `dashboard.py:298` also switches only on `keep-current` — an address-feedback row lands in `_NEEDS_YOU` via `is_actionable`, which is the intended placement |
| **impact-trace** `_audit_message` callers | exhaustive `rg -n '_audit_message' worksweep/`: `dashboard.py:1353` (def), `dashboard.py:1714` (`_audit`), `tests/test_dashboard.py:1245, 1258` (positional 2-arg calls) → AC #14 requires the new param to be keyword-with-default so both test call sites stay green untouched | |
| **test-surface** feedback emission + curator | exhaustive `grep -n 'feedback\|unresolved'` across `worksweep/tests/`, each Read-verified: `test_assessor_v2.py:43-48` (own-MR feedback+ci ids), `test_handoff.py:128-152` (handoff suppression; :147-152 asserts the feedback id at `unresolved_count=1`), `test_curator.py:58, 148-157, 203` (four fixtures constructing `kind="feedback", executor="triage"`), `test_apply_approvals.py:245-256` (registry pin) | All five files are required co-edits — enumerated as Phase 8 work, mapped in `## Decision Coverage` |
| **test-surface** dashboard audit | exhaustive `grep -n 'audit\|(dashboard)'` in `test_dashboard.py`, Read-verified: `:740-753`, `:756-764` (served approve/approve-all confirmations), `:1238-1253` (clamp), `:1256-1261` (exact string) — all four assert the bare `"(dashboard)"` and MUST stay green with the actor field absent; `_Client.approve`/`approve_all` (test_dashboard.py:140-143, 157-162) hardcode the body and need an optional `actor` kwarg → AC #14 | The clamp test (:1238-1253) is the regression risk if `suffix` is computed after `full` |

## Decision Coverage

| Gated decision | Implementing task (AC #) | Verification |
|---|---|---|
| **Row 1** — signal = unaddressed, targeted per-MR REST probe (decision-log:19) | AC #1, #2, #7 (Phase 1 + Phase 3) | `test_unaddressed_predicate` — a fixture discussions payload with 4 threads (unresolvable / resolved / last-note-by-reviewer / last-note-by-me) asserts the count is exactly 1 and names which thread survived; pure, no network |
| **Row 1** — addressed-but-unresolved emits nothing (decision-log:19) | AC #3 (falsifying) | `test_addressed_threads_emit_nothing` — `assess_own_mr` returns no `feedback:` id; mutation: restore the `unresolved_count > 0` gate at assessor.py:127 → RED |
| **Row 2** — `address-feedback` item shape, id preserved, branch added (decision-log:20) | AC #4 | `test_address_feedback_item_shape` — asserts id `feedback:pb-www!3997`, executor `address-feedback`, `branch` equal to the MR's `source_branch`, and the exact why-string for n=1 and n=3 |
| **Row 2** — joins `RUNNABLE_EXECUTORS`, ✅-gated (decision-log:20) + source AC5 | AC #5, #6 | `test_runnable_executors_matches_the_runner_claim_gate` (updated, test_apply_approvals.py:245-256) stays green; `test_address_feedback_is_not_auto_approved` asserts the name is absent from the `cfg.auto_approve` default and that `queue.auto_approve` leaves a proposed row `proposed`; `test_dashboard_renders_checkbox_for_address_feedback` asserts `has_checkbox` is True with **zero** dashboard edits |
| **Row 3** — fix+reply / reply-only / escalate, NEVER resolve (decision-log:21) | AC #8 (falsifying), AC #9 | `test_feedback_prompt_never_resolves` — asserts the rendered prompt and the module source contain no `/resolve`, no `"resolved": true`, and no `resolve` instruction verb; mutation: add a resolve instruction to the prompt → RED. `test_feedback_prompt_states_the_three_classes` asserts each class and the `addressed in <short-sha>` reply format appear |
| **Row 4** — keepcurrent-shaped module, worktree, run-time re-fetch, python verification (decision-log:22) | AC #10, AC #12 | `test_feedback_uses_its_own_worktree` asserts `worktree_for(cfg, repo, "address-feedback")` returns the `.worktrees/<repo>-address-feedback` path (not the shared clone); `test_feedback_refetches_threads_at_run_time` asserts the executor's GET fires during `execute` and the sweep-time count is not trusted; `test_feedback_verification_rejects_an_unpushed_commit_claim` asserts a claimed commit with an unchanged remote sha raises `RunnerError` |
| **Row 5** — pb-www hygiene + box sync ride in the prompt (decision-log:23) | AC #11 | `test_feedback_prompt_carries_pbwww_hygiene` asserts the prompt names `maintenance/compile-css`, the `www/home/scss/*` predicate, the `$script_version` cache-buster path, `push`, and the conditional dev-box sync |
| **Row 6** — outcome mapping done-with-tally vs needs-input (decision-log:24) | AC #12 | `test_feedback_done_posts_an_honest_tally` asserts the record completes `done` and the post names addressed/replied/escalated counts plus each escalated thread; `test_feedback_zero_handled_escalation_is_needs_input` asserts a `NeedsInputError` flips the record to `needs-input` (not `error`) and posts ❓ — mirrors test_runner_park.py's deps-injection shape |
| **Row 7** — curator rule 2 + partition + validator (decision-log:25) | AC #13 (falsifying) | `test_partition_counts_keys_feedback_on_address_feedback` asserts a `feedback`/`address-feedback` row counts actionable AND a `ci_red`/`triage` row still does; `test_curator_requires_feedback_numbers` asserts `validate()` returns False when a proposed address-feedback number is missing from the output; mutation: drop the executor from the required-set predicate (curator.py:271-273) → RED |
| **Row 7** — formatter/dashboard need no structural change (decision-log:25) | AC #5 (no-change assertion) | Confirmed on disk (formatter.py:118-124, dashboard.py:974) and pinned by the dashboard checkbox test above — no formatter edit is planned or needed |
| **Row 8** — actor-attributed approvals (decision-log:81) + AC7 | AC #14 (falsifying) | `test_approve_actor_attribution` — pure: `_audit_message([1], recs, actor="claude")` ends `" (dashboard · claude)"`, `_audit_message([1], recs)` ends `" (dashboard)"`, and `actor="mallory"` also ends `" (dashboard)"`; served: a POST body `{"numbers":[1],"actor":"claude"}` produces one Discord post containing `(dashboard · claude)`. Mutation: apply the suffix unconditionally → the absent-field and non-`claude` assertions go RED |
| **Row 8 scope note** — clamp/sanitize the actor value (decision-log:83) | AC #14 | `test_approve_actor_rejects_hostile_values` — a 5000-char actor, an actor containing a Discord mention/URL, and a non-string actor each produce the unchanged `" (dashboard)"` suffix and a 200 (never a 500, never reflected text) |
| **Round 3** — AC1 clause 2 clarified to the thread axis, Decision 2 wins (decision-log:87) | AC #16 (Phase 2) | `test_changes_requested_without_unaddressed_threads` — asserts exactly one `feedback:` item with executor `triage`, `why == "changes requested"`, and an empty `branch`; and `test_no_signal_emits_no_feedback_row` asserts the empty list when neither signal is present |
| **Round 3** — run-time zero-unaddressed → `done`, never an error (decision-log:90) | AC #17 (Phase 4 + 5) | `test_feedback_zero_unaddressed_at_run_time_is_done` — asserts the record completes `done`, the post carries the `0 addressed, 0 replied, 0 escalated — threads already answered` tally, and no ⚠️ is posted; mutation: raise `RunnerError` on an empty thread set → the test goes RED |
| **Round 3** — claude-run timeout from cfg, default 1800s (decision-log:91) | AC #18 (Phase 4) | `test_feedback_run_uses_the_cfg_timeout` — asserts the injected subprocess receives `timeout=cfg.runner_timeout` and that the value is below the 45-minute reap limit |

## Discovered Scope Deltas

Two files that the source's §Scope/files line (decision-log:29) does not name, both derived from
quoted decisions rather than invented:

1. **`worksweep/checkouts.py`** — Decision 4 mandates "keepcurrent's worktree discipline", and
   `worktree_for` grants a private worktree only to executors listed in `_WORKTREE_EXECUTORS`
   (checkouts.py:37); anything else silently receives the SHARED magi clone (checkouts.py:49-50).
   Without this one-line addition the feedback executor's `checkout -B <branch>` can yank the
   branch out from under a live 90-minute implement run — the exact failure the module docstring
   (checkouts.py:1-12) says the worktree split exists to prevent. In scope, AC #10.
2. **`worksweep/dashboard.py`** — added by the Round 2 amendment (Decision 8). Note this does not
   contradict source AC5's "no dashboard code change": AC5 constrains the **executor-registry**
   path (approve controls come free from `RUNNABLE_EXECUTORS`, dashboard.py:974, still true), while
   Decision 8 is an orthogonal change to the approve **POST/audit** path. Both hold at once.

## Critique Pass   (Step 7b — one inline self-critique pass)

| Dimension | Result |
|---|---|
| Completeness | weakness: first draft had no coverage row for the source's "clamp/sanitize the actor string" scope note (decision-log:83), which is a real obligation with a hostile-input failure mode → fix applied: added the Row-8-scope-note coverage row and folded the hostile-value assertions into AC #14 |
| DAG order | clean — Phases 1→8 are strictly orderable (types before probe before emission before wiring before executor before lifecycle before digest); Phase 7 (dashboard actor) is independent of Phases 1-6 and may land in either order, stated explicitly in the phase table |
| Pre/postconditions (EARS) | weakness: draft AC #12 bundled the done and needs-input outcomes in one loose sentence with no trigger clause → fix applied: split into an explicit `WHEN … SHALL` (done-with-tally) and `IF … THEN … SHALL` (zero-handled escalation), each independently falsifiable |
| Failure path | clean — unwanted-behaviour ACs exist for the probe failing (AC #7 degrade-not-fail), the resolve prohibition (AC #8), auto-approval (AC #6), zero-handled escalation (AC #12), and hostile actor values (AC #14) |
| Concrete layer-map | weakness: the source's owning-layer row names modules but no line numbers → fix applied: the Owning layer field now cites the exact seam in each module (assessor.py:127-135, collectors.py:20-33, runner.py:355 + 425-455, curator.py:319-330, dashboard.py:1353-1381) |
| Halt settlement (Round 3) | weakness: the plan shipped with one unadjudicated source conflict and two unspecified branches → fix applied: all three settled in the gated source (decision-log:85-91) and folded into AC #16, #17, #18 with coverage rows and named tests; `## Resolved Ambiguity (Round 3)` records the decision so it is not re-litigated |
| Reversibility | clean, with two one-way doors flagged in Tricky Parts: (a) the executor pushes commits and posts replies under Chandler's GitLab identity — a reply cannot be unsent, which is why Decision 2 keeps it ✅-gated per MR; (b) flipping a row's executor from `triage` to `address-feedback` makes it non-dismissable (queue.py:141-151) while keeping its id, so any already-`approved` feedback row in the live queue becomes claimable on the next runner pass — call this out at deploy time |

<!-- spawn-contract Diagnostic Fields -->

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

<!-- Sprint-specific sections -->

## Layout (UI tasks only)

N/A — no UI surface. Decision 8 changes an audit string, not a rendered page.

## Component Spec (UI tasks only)

N/A.

## Phase Breakdown

| Phase | Files | Gate |
|---|---|---|
| 1 — Sense | models.py, collectors.py, tests/test_collectors.py | AC #1, #2 green on pure fixtures |
| 2 — Emit | assessor.py, models.py (registry), tests/test_assessor_v2.py, tests/test_handoff.py, tests/test_apply_approvals.py | AC #3 (falsifying), #4, #5, #6, #16 |
| 3 — Sweep wiring | __main__.py, tests/test_main_v2.py | AC #7 |
| 4 — Executor | feedback.py (NEW), checkouts.py, tests/test_feedback.py | AC #8 (falsifying), #9, #10, #11, #17, #18 |
| 5 — Lifecycle | runner.py, __main__.py (edges), tests/test_runner_feedback.py | AC #12, #17 |
| 6 — Digest | curator.py, tests/test_curator.py | AC #13 (falsifying) |
| 7 — Actor attribution (independent) | dashboard.py, tests/test_dashboard.py | AC #14 (falsifying) |
| 8 — Suite | all of the above | Full suite green; AC #15 |

## Acceptance Criteria

The source's own EARS criteria (decision-log:33-38, 82), cross-referenced to this plan's AC numbers:

- [ ] Source AC1 (as clarified by Round 3, decision-log:87) — unaddressed emission / no-emission on the thread axis → plan AC #1, #2, #4; the `changes_requested`-only informational row → plan AC #16
- [ ] Source AC2 — `test_addressed_threads_emit_nothing` falsifying → plan AC #3
- [ ] Source AC3 — claude worktree pass, never resolves → plan AC #8, #9, #10
- [ ] Source AC4 — done-with-tally vs needs-input → plan AC #12
- [ ] Source AC5 — `RUNNABLE_EXECUTORS` + dashboard controls, no dashboard code change → plan AC #5, #6
- [ ] Source AC6 — curator rule 2 + validator-required numbers → plan AC #13
- [ ] Source AC7 — actor-attributed approve audit → plan AC #14
- [ ] Round 3 adopted deferrals (decision-log:90-91) — zero-unaddressed run-time outcome → plan AC #17; cfg-sourced claude timeout → plan AC #18

## Decisions adopted by the orchestrator (Round 3, decision-log:85-91)

Nothing remains deferred. All three items this plan raised are settled in the gated source and
folded into the ACs above.

1. **HALT — `changes_requested` with zero unaddressed threads.** Adopted candidate (a): Decision 2
   wins; AC1 clause 2 constrains only the thread-derived emission. → `## Resolved Ambiguity
   (Round 3)`, AC #16, and the `test_addressed_threads_emit_nothing` fixture
   (`changes_requested=False`).
2. **Run-time re-fetch finds zero unaddressed threads.** Adopted as recommended: complete `done`
   with the `"0 addressed, 0 replied, 0 escalated — threads already answered"` tally, never an
   error. → AC #17.
3. **The `claude -p` timeout for the feedback run.** Adopted as recommended: from `cfg`, default
   1800s (`cfg.runner_timeout`, config.py:24), inside the 45-minute reap window; no new config key.
   → AC #18.
