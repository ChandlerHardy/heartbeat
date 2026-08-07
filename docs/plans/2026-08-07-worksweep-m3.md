# Worksweep M3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the worksweep sensor agree with GitLab's review-state truth, move it to the always-on mini with a never-silent Discord contract, and add the first executor: approved review items run `magi-review` unattended.

**Architecture:** The GraphQL API that backs GitLab's "Your work / Merge requests" dashboard replaces the reviewer-list REST query, so per-reviewer `reviewState` decides what is actionable. The queue gains `done`/`error` lifecycle states and becomes the memory of completed magi runs (replacing the MacBook-local `.magi` file glob). A third launchd job (runner) drains `approved` items by invoking `claude -p "/magi:magi-review !<iid>"` in a mini-local checkout — advisory mode, draft-only outputs.

**Tech Stack:** Python 3 stdlib only (repo rule — no pip deps), `glab` CLI (REST + GraphQL), macOS launchd, `claude` CLI with the magi plugin, pytest for tests.

**Spec:** `docs/proposals/2026-08-07-worksweep-m3-actor-design.md` (approved 2026-08-07).

## Global Constraints

- Stdlib only; no new Python dependencies (existing repo convention).
- All parse/assess/reconcile logic is pure and unit-tested; subprocess/network lives at thin edges (`_run_glab`, `_post_discord`, runner `execute`) — mirrors the existing collectors pattern.
- Queue writes are atomic (temp file + `os.replace`) — never regress this.
- Discord webhook host allowlist + no-redirect opener are security controls — never bypass `_post_discord`.
- Executor v1 is `magi-review` only; advisory mode; no code edits, no publishing, no pushes.
- Timeouts: executor hard timeout **1800s**; stale `running` reap **45 min**; terminal-record retention **90 days**.
- Sweep message contract: a sweep that runs always posts ≥1 Discord message (digest, 🔍 heartbeat, or ⚠️ error). Intake/runner post only on events (approval confirm, completion, failure) — not heartbeats.
- Tests run from the repo root: `python3 -m pytest worksweep/tests/ -q`.
- Work happens on `main` after Task 1 merges the M2 branch. Commit messages follow the existing `feat(worksweep):`/`fix(worksweep):`/`docs(worksweep):` style. Per user's global rule: no Claude-Session trailers.

## File Structure

| File | Responsibility |
|---|---|
| `worksweep/models.py` (modify) | +4 WorkItem lifecycle fields, +3 MergeRequest review-state fields |
| `worksweep/collectors.py` (modify) | +`run_graphql_sweep` (shell edge), +`parse_graphql_sweep` (pure) |
| `worksweep/assessor.py` (modify) | Review-state bucket mapping, new item kinds, `resolutions()`, queue-backed `has_magi_done`, `bootstrap_magi_records` |
| `worksweep/queue.py` (modify) | `reconcile` v2: resolutions→done, error re-propose, terminal retention + 90-day compaction |
| `worksweep/runner.py` (create) | Claim/lock/reap/execute/report — the M3 executor |
| `worksweep/__main__.py` (modify) | GraphQL wiring, single-message contract, ⚠️ error post, `run` subcommand |
| `worksweep/tests/fixtures/graphql_sweep.json` (create) | Frozen real GraphQL response |
| `worksweep/tests/test_graphql_collector.py`, `test_assessor_v2.py`, `test_queue_lifecycle.py`, `test_runner.py` (create) | Task test files |
| `etc/mini/com.chandlerhardy.worksweep.plist`, `...worksweep-intake.plist`, `...worksweep-runner.plist` (create) | Mini launchd agents |
| `docs/worksweep-mini-cutover.md` (create) | Cutover + decommission checklist |

---

### Task 1: Merge the M2 branch

**Files:** none (git only). The branch `feat/worksweep-m2-queue-approval` (8 commits + the design docs committed on it) was already magi-reviewed (`.planning/handoffs/worksweep-magi-fixes.md`).

- [ ] **Step 1: Verify clean state and merge**

```bash
cd ~/repos/heartbeat
git status --short   # expect only untracked .codex/ .planning/ — do NOT commit those
git checkout main && git pull
git merge --no-ff feat/worksweep-m2-queue-approval -m "merge: worksweep M2 queue + Discord approval intake"
```

- [ ] **Step 2: Run the full suite on main**

Run: `python3 -m pytest worksweep/tests/ -q`
Expected: all pass (suite was green on the branch).

- [ ] **Step 3: Push**

```bash
git push origin main
```

---

### Task 2: Model fields for lifecycle + review state

**Files:**
- Modify: `worksweep/models.py`
- Test: `worksweep/tests/test_models_v2.py` (create)

**Interfaces:**
- Produces: `WorkItem` gains `claimed_at: str = ""`, `done_reason: str = ""`, `result_sha: str = ""`, `report_path: str = ""`, `error_summary: str = ""`; status domain becomes `"proposed" | "approved" | "running" | "done" | "error"`. `MergeRequest` gains `my_review_state: str = ""`, `changes_requested: bool = False`, `unresolved_count: int = 0`. All appended with defaults so every existing constructor call and `WorkItem(**d["item"])` queue-load keeps working.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_models_v2.py
"""New lifecycle/review-state fields default cleanly and round-trip the queue."""
import dataclasses

from worksweep.models import MergeRequest, QueueRecord, WorkItem
from worksweep.queue import load_queue, save_queue


def _item(**kw):
    base = dict(schema_version=1, id="review:pb-www!1", repo="pb-www",
                kind="review_request", executor="magi-review", risk="low",
                why="review requested", web_url="https://x/1", sha="abc")
    base.update(kw)
    return WorkItem(**base)


def test_workitem_new_fields_default_empty():
    it = _item()
    assert (it.claimed_at, it.done_reason, it.result_sha,
            it.report_path, it.error_summary) == ("", "", "", "", "")


def test_workitem_roundtrips_queue_with_new_fields(tmp_path):
    p = str(tmp_path / "q.json")
    it = _item(status="done", done_reason="executor-completed",
               result_sha="abc", report_path="/r.md", claimed_at="t1")
    save_queue(p, [QueueRecord(number=1, item=it, first_seen="t0", last_seen="t1")])
    loaded = load_queue(p)
    assert loaded[0].item == it


def test_old_queue_record_without_new_fields_loads(tmp_path):
    # A queue file written by M2 code lacks the new keys entirely.
    p = str(tmp_path / "q.json")
    old = _item()
    d = dataclasses.asdict(old)
    for k in ("claimed_at", "done_reason", "result_sha", "report_path", "error_summary"):
        d.pop(k)
    import json
    (tmp_path / "q.json").write_text(json.dumps(
        [{"number": 1, "first_seen": "t0", "last_seen": "t0", "item": d}]))
    assert load_queue(p)[0].item.id == "review:pb-www!1"


def test_mergerequest_review_state_fields_default():
    mr = MergeRequest(repo="pb-www", iid=1, title="t", author="a",
                      web_url="u", description="", sha="s", is_draft=False,
                      reviewers=(), ci_status="unknown", updated_at="")
    assert (mr.my_review_state, mr.changes_requested, mr.unresolved_count) == ("", False, 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_models_v2.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'claimed_at'` (and similar).

- [ ] **Step 3: Implement**

In `worksweep/models.py`, append to `WorkItem` (after `status`):

```python
    status: str = "proposed"  # "proposed" | "approved" | "running" | "done" | "error"
    claimed_at: str = ""      # ISO8601 — set when the runner claims (status=running)
    done_reason: str = ""     # "executor-completed" | "already-reviewed" | "mr-merged" | "bootstrap-glob"
    result_sha: str = ""      # head SHA the executor actually reviewed
    report_path: str = ""     # tribunal report path written by the executor
    error_summary: str = ""   # short failure text (status=error)
```

Append to `MergeRequest` (after `updated_at`):

```python
    my_review_state: str = ""       # GitLab reviewState enum for cfg.username, "" = unknown
    changes_requested: bool = False # any reviewer state REQUESTED_CHANGES on my MR
    unresolved_count: int = 0       # resolvable - resolved discussions on my MR
```

(Update the `status` comment line in place — don't duplicate the field.)

- [ ] **Step 4: Run full suite**

Run: `python3 -m pytest worksweep/tests/ -q`
Expected: all pass (defaults keep old constructors valid).

- [ ] **Step 5: Commit**

```bash
git add worksweep/models.py worksweep/tests/test_models_v2.py
git commit -m "feat(worksweep): lifecycle + review-state model fields"
```

---

### Task 3: GraphQL collector

**Files:**
- Modify: `worksweep/collectors.py`
- Create: `worksweep/tests/fixtures/graphql_sweep.json`
- Test: `worksweep/tests/test_graphql_collector.py` (create)

**Interfaces:**
- Consumes: `MergeRequest` with the Task 2 fields; existing `_run_glab(args, timeout)`.
- Produces: `run_graphql_sweep() -> str` (shell edge, raw JSON) and `parse_graphql_sweep(raw: str, username: str, repos: tuple) -> tuple[list[MergeRequest], list[MergeRequest]]` returning `(review_requests, authored)`. Both lists carry `my_review_state` (the state of `username`'s reviewer entry, uppercased) and authored MRs additionally carry `changes_requested`/`unresolved_count`/`description`.

- [ ] **Step 1: Freeze a real fixture FIRST (verifies the query against the live schema)**

```bash
cd ~/repos/heartbeat
glab api graphql -f query='
query {
  currentUser {
    username
    reviewRequestedMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt
        project { fullPath }
        author { username }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        headPipeline { status }
      }
    }
    authoredMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt description
        project { fullPath }
        author { username }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        headPipeline { status }
        resolvableDiscussionsCount resolvedDiscussionsCount
      }
    }
  }
}' > worksweep/tests/fixtures/graphql_sweep.json
python3 -m json.tool worksweep/tests/fixtures/graphql_sweep.json | head -30
```

Expected: valid JSON containing `currentUser` with both MR lists and `reviewState` values like `"REVIEWED"`/`"UNREVIEWED"`. **If the query errors, fix the field names against the live schema before writing any code** — the fixture is the contract. Manually cross-check the buckets against the dashboard screenshot state (reviewed MRs must carry a non-UNREVIEWED state). If the live response contains nothing in a needed shape (e.g. no MR currently has `headPipeline`), hand-edit a *copy* of a real node to cover it and note the edit in a `"_fixture_notes"` key at the top level (parsers must ignore unknown keys — they read only what they need).

- [ ] **Step 2: Write the failing test**

```python
# worksweep/tests/test_graphql_collector.py
"""parse_graphql_sweep maps the frozen live response onto MergeRequest."""
import json
import os

from worksweep.collectors import parse_graphql_sweep

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "graphql_sweep.json")


def _raw():
    with open(FIX) as f:
        return f.read()


def _username():
    data = json.loads(_raw())
    data = data.get("data", data)
    return data["currentUser"]["username"]


def test_parses_both_lists_without_error():
    reviews, authored = parse_graphql_sweep(_raw(), _username(),
                                            ("pb-www", "pb-api", "jrg"))
    assert isinstance(reviews, list) and isinstance(authored, list)
    for mr in reviews + authored:
        assert mr.repo in ("pb-www", "pb-api", "jrg")
        assert mr.iid > 0 and mr.web_url.startswith("https://")
        assert mr.sha  # diffHeadSha present


def test_my_review_state_extracted_uppercase():
    reviews, _ = parse_graphql_sweep(_raw(), _username(),
                                     ("pb-www", "pb-api", "jrg"))
    states = {mr.my_review_state for mr in reviews}
    assert states  # every review-requested MR has a state for me
    assert all(s == s.upper() and s for s in states)


def test_repo_filter_drops_unlisted_projects():
    reviews, authored = parse_graphql_sweep(_raw(), _username(), ("pb-api",))
    assert all(mr.repo == "pb-api" for mr in reviews + authored)


def test_synthetic_authored_fields():
    # Deterministic synthetic doc for the authored-only fields.
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "7", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/7",
            "diffHeadSha": "s7", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "Available on https://dev1.chandlerhardy-dev.performancebeef.com/x",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": [
                {"username": "r1", "mergeRequestInteraction": {"reviewState": "REQUESTED_CHANGES"}}]},
            "headPipeline": {"status": "FAILED"},
            "resolvableDiscussionsCount": 5, "resolvedDiscussionsCount": 3}]}}}}
    _, authored = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    mr = authored[0]
    assert mr.changes_requested is True
    assert mr.unresolved_count == 2
    assert mr.ci_status == "failed"
    assert mr.dev_url_present is True


def test_malformed_raw_returns_empty_lists():
    assert parse_graphql_sweep("not json", "me", ("pb-www",)) == ([], [])
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_graphql_collector.py -q`
Expected: FAIL — `ImportError: cannot import name 'parse_graphql_sweep'`.

- [ ] **Step 4: Implement in `worksweep/collectors.py`**

```python
# --- GraphQL sweep (M3): one query mirroring the "Your work / MRs" dashboard ---

_GRAPHQL_SWEEP_QUERY = """
query {
  currentUser {
    username
    reviewRequestedMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt
        project { fullPath }
        author { username }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        headPipeline { status }
      }
    }
    authoredMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt description
        project { fullPath }
        author { username }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        headPipeline { status }
        resolvableDiscussionsCount resolvedDiscussionsCount
      }
    }
  }
}
"""


def run_graphql_sweep() -> str:
    """Shell edge: run the dashboard-equivalent GraphQL query via glab."""
    return _run_glab(["api", "graphql", "-f", f"query={_GRAPHQL_SWEEP_QUERY}"])


def _gql_mr(node: dict, username: str) -> "MergeRequest":
    """Map one GraphQL MR node -> MergeRequest. Raises on missing must-haves."""
    full_path = ((node.get("project") or {}).get("fullPath") or "")
    repo = full_path.split("/", 1)[1] if "/" in full_path else full_path
    my_state = ""
    reviewers = []
    for rv in ((node.get("reviewers") or {}).get("nodes") or []):
        uname = (rv or {}).get("username", "")
        reviewers.append(uname)
        if uname == username:
            my_state = (((rv or {}).get("mergeRequestInteraction") or {})
                        .get("reviewState") or "").upper()
    changes_requested = any(
        (((rv or {}).get("mergeRequestInteraction") or {}).get("reviewState") or "")
        .upper() == "REQUESTED_CHANGES"
        for rv in ((node.get("reviewers") or {}).get("nodes") or []))
    resolvable = int(node.get("resolvableDiscussionsCount") or 0)
    resolved = int(node.get("resolvedDiscussionsCount") or 0)
    pipe = (node.get("headPipeline") or {}).get("status") or "unknown"
    return MergeRequest(
        repo=repo,
        iid=int(node.get("iid", 0)),
        title=node.get("title", ""),
        author=((node.get("author") or {}).get("username") or ""),
        web_url=node.get("webUrl", ""),
        description=node.get("description") or "",
        sha=node.get("diffHeadSha") or "",
        is_draft=bool(node.get("draft", False)),
        reviewers=tuple(reviewers),
        ci_status=str(pipe).lower(),
        updated_at=node.get("updatedAt", ""),
        my_review_state=my_state,
        changes_requested=changes_requested,
        unresolved_count=max(0, resolvable - resolved),
    )


def parse_graphql_sweep(raw: str, username: str, repos: tuple):
    """Pure: raw GraphQL JSON -> (review_requested, authored) MergeRequest lists,
    filtered to the configured performancelivestock repos. Malformed -> ([], [])."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"worksweep: graphql decode failed: {e}", file=sys.stderr)
        return [], []
    data = data.get("data", data) or {}
    cu = data.get("currentUser") or {}

    def _bucket(key: str):
        out = []
        for node in ((cu.get(key) or {}).get("nodes") or []):
            try:
                mr = _gql_mr(node or {}, username)
            except (ValueError, TypeError, AttributeError) as e:
                print(f"worksweep: graphql skipping bad node: {e}", file=sys.stderr)
                continue
            if mr.repo in repos:
                out.append(mr)
        return out

    return (_bucket("reviewRequestedMergeRequests"),
            _bucket("authoredMergeRequests"))
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest worksweep/tests/test_graphql_collector.py worksweep/tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add worksweep/collectors.py worksweep/tests/test_graphql_collector.py worksweep/tests/fixtures/graphql_sweep.json
git commit -m "feat(worksweep): GraphQL dashboard-state collector"
```

---

### Task 4: Assessor v2 — bucket mapping, new item kinds, resolutions

**Files:**
- Modify: `worksweep/assessor.py`
- Test: `worksweep/tests/test_assessor_v2.py` (create)

**Interfaces:**
- Consumes: `MergeRequest` (Task 2 fields), `WorkItem`.
- Produces:
  - `REVIEW_ACTIONABLE_STATES = ("UNREVIEWED", "REVIEW_STARTED", "UNAPPROVED", "")`
  - `assess_review_request(mr, username) -> List[WorkItem]` — emits `id=f"review:{repo}!{iid}"` only when `mr.my_review_state` is actionable; drafts included, tagged `(draft)`.
  - `assess_own_mr(mr, username, has_magi: Callable[[str, int, str], bool]) -> List[WorkItem]` — magi item (`id=f"magi:{repo}!{iid}@{sha}"`), dev-url hygiene item (unchanged id), NEW `feedback:{repo}!{iid}` (kind `feedback`, executor `triage`) when `changes_requested or unresolved_count > 0`, NEW `ci:{repo}!{iid}` (kind `ci_red`, executor `triage`) when `ci_status == "failed"`.
  - `resolutions(review_mrs, username) -> Dict[str, str]` — `{f"review:{repo}!{iid}": "already-reviewed"}` for every review-requested MR whose `my_review_state` is NOT actionable.
  - NOTE: `has_magi` signature changes from `(repo, iid)` to `(repo, iid, sha)` — Task 6 provides the queue-backed implementation; this task updates `assess_mr` callers in tests only.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_assessor_v2.py
"""Review-state buckets -> items/resolutions; new own-MR item kinds."""
from worksweep.assessor import (
    assess_own_mr, assess_review_request, resolutions)
from worksweep.models import MergeRequest


def _mr(**kw):
    base = dict(repo="pb-www", iid=9, title="t", author="other",
                web_url="https://gl/x/-/merge_requests/9", description="",
                sha="s9", is_draft=False, reviewers=("chandler.hardy",),
                ci_status="unknown", updated_at="")
    base.update(kw)
    return MergeRequest(**base)


def test_unreviewed_emits_review_item():
    items = assess_review_request(_mr(my_review_state="UNREVIEWED"), "chandler.hardy")
    assert [i.id for i in items] == ["review:pb-www!9"]
    assert items[0].executor == "magi-review"


def test_reviewed_emits_nothing():
    for state in ("REVIEWED", "REQUESTED_CHANGES", "APPROVED"):
        assert assess_review_request(_mr(my_review_state=state), "chandler.hardy") == []


def test_draft_review_request_included_and_tagged():
    items = assess_review_request(
        _mr(my_review_state="UNREVIEWED", is_draft=True), "chandler.hardy")
    assert len(items) == 1 and "(draft)" in items[0].why


def test_resolutions_for_waiting_states():
    mrs = [_mr(iid=1, my_review_state="REVIEWED"),
           _mr(iid=2, my_review_state="UNREVIEWED"),
           _mr(iid=3, my_review_state="REQUESTED_CHANGES")]
    assert resolutions(mrs, "chandler.hardy") == {
        "review:pb-www!1": "already-reviewed",
        "review:pb-www!3": "already-reviewed"}


def test_own_mr_feedback_and_ci_items():
    mr = _mr(author="chandler.hardy", changes_requested=True,
             unresolved_count=2, ci_status="failed")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)
    ids = {i.id for i in items}
    assert "feedback:pb-www!9" in ids
    assert "ci:pb-www!9" in ids
    assert "magi:pb-www!9@s9" not in ids  # has_magi True suppresses


def test_own_mr_magi_item_when_no_history():
    mr = _mr(author="chandler.hardy")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert any(i.id == "magi:pb-www!9@s9" for i in items)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_assessor_v2.py -q`
Expected: FAIL — `ImportError` on the new names.

- [ ] **Step 3: Implement in `worksweep/assessor.py`**

Replace `assess_mr` with the split pair (keep `assess_todo`, `assess_issue`, `dedupe` unchanged; delete `has_magi_report`/`_report_glob` usage from the assess path — Task 6 repurposes the glob for bootstrap only):

```python
# States in which GitLab still expects MY review. "" = state unknown (be loud,
# not silent: unknown renders as actionable rather than vanishing).
REVIEW_ACTIONABLE_STATES = ("UNREVIEWED", "REVIEW_STARTED", "UNAPPROVED", "")


def assess_review_request(mr: MergeRequest, username: str) -> List[WorkItem]:
    """A review-requested MR is actionable only while GitLab says my review
    state is outstanding. Reviewed/requested-changes/approved -> no item (the
    sensor's resolutions() closes any existing one)."""
    if username not in mr.reviewers:
        return []
    if mr.my_review_state not in REVIEW_ACTIONABLE_STATES:
        return []
    why = "review requested"
    if mr.ci_status not in ("unknown", ""):
        ready = "CI green" if mr.ci_status == "success" else f"CI {mr.ci_status}"
        why = f"review requested ({ready})"
    if mr.is_draft:
        why += " (draft)"
    return [WorkItem(
        schema_version=1, id=f"review:{mr.repo}!{mr.iid}",
        repo=mr.repo, kind="review_request", executor="magi-review",
        risk="low", why=why, web_url=mr.web_url, sha=mr.sha)]


def resolutions(review_mrs: List[MergeRequest], username: str) -> dict:
    """ids the sensor says are settled: review items where my state is a
    waiting-on-author state. reconcile() flips matching queue records done."""
    out = {}
    for mr in review_mrs:
        if username in mr.reviewers and mr.my_review_state not in REVIEW_ACTIONABLE_STATES:
            out[f"review:{mr.repo}!{mr.iid}"] = "already-reviewed"
    return out


def assess_own_mr(mr: MergeRequest, username: str,
                  has_magi: Callable[[str, int, str], bool]) -> List[WorkItem]:
    if mr.author != username:
        return []
    items: List[WorkItem] = []
    if not has_magi(mr.repo, mr.iid, mr.sha):
        items.append(WorkItem(
            schema_version=1, id=f"magi:{mr.repo}!{mr.iid}@{mr.sha}",
            repo=mr.repo, kind="mr", executor="magi-review", risk="low",
            why="no magi-review yet", web_url=mr.web_url, sha=mr.sha))
    if not mr.dev_url_present:
        items.append(WorkItem(
            schema_version=1, id=f"hygiene-devurl:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="mr", executor="mr-hygiene", risk="low",
            why="description missing dev-server link", web_url=mr.web_url, sha=mr.sha))
    if mr.changes_requested or mr.unresolved_count > 0:
        n = mr.unresolved_count
        why = "changes requested" if mr.changes_requested else ""
        if n:
            why = (why + ", " if why else "") + f"{n} unresolved thread{'s' if n != 1 else ''}"
        items.append(WorkItem(
            schema_version=1, id=f"feedback:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="feedback", executor="triage", risk="low",
            why=why, web_url=mr.web_url, sha=mr.sha))
    if mr.ci_status == "failed":
        items.append(WorkItem(
            schema_version=1, id=f"ci:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="ci_red", executor="triage", risk="low",
            why="head pipeline failed", web_url=mr.web_url, sha=mr.sha))
    return items
```

Keep a thin `assess_mr(mr, username, has_magi)` shim delegating to the pair so `test_assessor.py`'s existing cases keep passing IF they still typecheck against the new `has_magi` arity — update those existing tests' lambdas from `lambda r, i:` to `lambda r, i, s:` in the same commit.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest worksweep/tests/ -q`
Expected: all pass (including updated `test_assessor.py`).

- [ ] **Step 5: Commit**

```bash
git add worksweep/assessor.py worksweep/tests/test_assessor_v2.py worksweep/tests/test_assessor.py
git commit -m "feat(worksweep): review-state buckets, feedback/ci items, resolutions"
```

---

### Task 5: Queue lifecycle v2

**Files:**
- Modify: `worksweep/queue.py`
- Test: `worksweep/tests/test_queue_lifecycle.py` (create)

**Interfaces:**
- Consumes: `QueueRecord`, `WorkItem` (Task 2 fields), `resolutions()` dict shape from Task 4.
- Produces: `reconcile(existing, fresh, now, resolved=None)` — new keyword arg, default keeps old call sites working. New rules on top of the M2 rules:
  1. `resolved: Dict[id, reason]` — a matching record in any non-terminal status flips to `done` (`done_reason=reason`, `last_seen=now`); retained.
  2. Terminal statuses `done`/`error` are retained when gone from the sweep, until compaction.
  3. Prior `error` + id present in fresh → `proposed` (retry cycle).
  4. Prior `done` + id present in fresh → resurrect to `proposed` **only if the fresh sha differs from the prior sha**; same sha stays `done` (stops the executor-done → still-UNREVIEWED resurrect loop; the known limit "re-request at the identical sha stays done" is documented in the spec).
  5. Compaction: terminal records whose `last_seen` is older than **90 days** relative to `now` are dropped. Helper `_older_than_days(iso_a, iso_b, days) -> bool` using `datetime.datetime.fromisoformat`; unparseable timestamps → NOT older (never destroy on bad data).

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_queue_lifecycle.py
"""reconcile v2: resolutions, terminal retention, error retry, done resurrect."""
import dataclasses
import datetime

from worksweep.models import QueueRecord, WorkItem
from worksweep.queue import reconcile

NOW = "2026-08-07T12:00:00+00:00"


def _item(id="review:pb-www!1", sha="s1", status="proposed", **kw):
    return WorkItem(schema_version=1, id=id, repo="pb-www",
                    kind="review_request", executor="magi-review", risk="low",
                    why="review requested", web_url="https://x/1", sha=sha,
                    status=status, **kw)


def _rec(item, number=1, last_seen="2026-08-06T00:00:00+00:00"):
    return QueueRecord(number=number, item=item,
                       first_seen="2026-08-01T00:00:00+00:00", last_seen=last_seen)


def test_resolved_id_flips_done_and_is_retained():
    out = reconcile([_rec(_item(status="proposed"))], [], NOW,
                    resolved={"review:pb-www!1": "already-reviewed"})
    assert out[0].item.status == "done"
    assert out[0].item.done_reason == "already-reviewed"
    assert out[0].last_seen == NOW


def test_resolved_does_not_touch_terminal_records():
    done = _item(status="done", done_reason="executor-completed")
    out = reconcile([_rec(done)], [], NOW,
                    resolved={"review:pb-www!1": "already-reviewed"})
    assert out[0].item.done_reason == "executor-completed"


def test_done_retained_when_gone_from_sweep():
    out = reconcile([_rec(_item(status="done"))], [], NOW)
    assert len(out) == 1 and out[0].item.status == "done"


def test_error_reproposed_when_signal_persists():
    prior = _rec(_item(status="error", error_summary="boom"))
    out = reconcile([prior], [_item()], NOW)
    assert out[0].item.status == "proposed"
    assert out[0].number == 1  # number stable


def test_done_same_sha_stays_done():
    prior = _rec(_item(status="done", result_sha="s1"))
    out = reconcile([prior], [_item(sha="s1")], NOW)
    assert out[0].item.status == "done"


def test_done_new_sha_resurrects_proposed():
    prior = _rec(_item(status="done", result_sha="s1"))
    out = reconcile([prior], [_item(sha="s2")], NOW)
    assert out[0].item.status == "proposed" and out[0].item.sha == "s2"


def test_compaction_drops_old_terminal_records():
    old = (datetime.datetime.fromisoformat(NOW)
           - datetime.timedelta(days=91)).isoformat()
    stale_done = _rec(_item(id="review:pb-www!2", status="done"),
                      number=2, last_seen=old)
    fresh_done = _rec(_item(status="done"))
    out = reconcile([stale_done, fresh_done], [], NOW)
    assert [r.number for r in out] == [1]


def test_compaction_never_drops_unparseable_timestamps():
    weird = _rec(_item(status="done"), last_seen="not-a-date")
    assert len(reconcile([weird], [], NOW)) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_queue_lifecycle.py -q`
Expected: FAIL — `TypeError: reconcile() got an unexpected keyword argument 'resolved'`.

- [ ] **Step 3: Implement in `worksweep/queue.py`**

```python
_TERMINAL = ("done", "error")
_RETAIN_IF_GONE = ("approved", "running", "done", "error")
_COMPACT_AFTER_DAYS = 90


def _older_than_days(iso_ts: str, iso_now: str, days: int) -> bool:
    """True when iso_ts is more than `days` before iso_now. Unparseable -> False
    (never destroy a record on bad data)."""
    import datetime
    try:
        ts = datetime.datetime.fromisoformat(iso_ts)
        now = datetime.datetime.fromisoformat(iso_now)
    except (ValueError, TypeError):
        return False
    if (ts.tzinfo is None) != (now.tzinfo is None):   # naive/aware mix -> keep
        return False
    return (now - ts) > datetime.timedelta(days=days)


def reconcile(existing: List[QueueRecord], fresh: List[WorkItem],
              now: str, resolved: dict | None = None) -> List[QueueRecord]:
    """Fold a sweep into the queue. M2 rules plus the M3 lifecycle:
    resolutions -> done; error+present -> retry; done+new-sha -> resurrect;
    terminal retained until 90-day compaction."""
    resolved = resolved or {}
    by_id = {r.item.id: r for r in existing}
    fresh_ids = {it.id for it in fresh}
    next_num = max((r.number for r in existing), default=0) + 1

    out: List[QueueRecord] = []
    for it in fresh:
        prior = by_id.get(it.id)
        if prior is None:
            out.append(QueueRecord(number=next_num, item=it,
                                   first_seen=now, last_seen=now))
            next_num += 1
            continue
        ps = prior.item.status
        if ps == "error":
            merged = dataclasses.replace(it, status="proposed")
        elif ps == "done":
            if prior.item.sha == it.sha:
                out.append(QueueRecord(number=prior.number, item=prior.item,
                                       first_seen=prior.first_seen, last_seen=now))
                continue
            merged = dataclasses.replace(it, status="proposed")
        elif prior.item.sha == it.sha:
            merged = dataclasses.replace(it, status=ps,
                                         claimed_at=prior.item.claimed_at)
        else:
            merged = dataclasses.replace(it, status="proposed")
        out.append(QueueRecord(number=prior.number, item=merged,
                               first_seen=prior.first_seen, last_seen=now))

    for r in existing:
        if r.item.id in fresh_ids:
            continue
        reason = resolved.get(r.item.id)
        if reason and r.item.status not in _TERMINAL:
            out.append(QueueRecord(
                number=r.number, first_seen=r.first_seen, last_seen=now,
                item=dataclasses.replace(r.item, status="done",
                                         done_reason=reason)))
            continue
        if r.item.status in _RETAIN_IF_GONE:
            out.append(r)

    out = [r for r in out
           if not (r.item.status in _TERMINAL
                   and _older_than_days(r.last_seen, now, _COMPACT_AFTER_DAYS))]
    out.sort(key=lambda r: r.number)
    return out
```

Note: resolutions apply to records *absent from fresh* (a resolved id is by definition not emitted by the sensor). The M2 docstring rules stay true for `proposed`/`approved`/`running`.

- [ ] **Step 4: Run full suite (M2 reconcile tests must still pass)**

Run: `python3 -m pytest worksweep/tests/ -q`
Expected: all pass. If `test_queue_reconcile.py` asserted that a gone `proposed` item is dropped — that behavior is unchanged; only terminal states gained retention.

- [ ] **Step 5: Commit**

```bash
git add worksweep/queue.py worksweep/tests/test_queue_lifecycle.py
git commit -m "feat(worksweep): queue lifecycle — done/error, resolutions, 90d compaction"
```

---

### Task 6: Queue-backed magi history + glob bootstrap

**Files:**
- Modify: `worksweep/assessor.py`
- Test: `worksweep/tests/test_magi_history.py` (create)

**Interfaces:**
- Consumes: `QueueRecord` list, `has_magi_report(repo, iid)` (existing glob, becomes bootstrap-only), `MergeRequest`.
- Produces:
  - `has_magi_done(records, repo, iid, sha) -> bool` — True when any record is `executor == "magi-review"`, `status == "done"`, matches repo, its id contains `f"!{iid}@"` or equals `f"review:{repo}!{iid}"`, and (`item.sha == sha` or `item.result_sha == sha`).
  - `bootstrap_magi_records(records, authored, now, report_exists=has_magi_report) -> List[QueueRecord]` — for each authored MR with no magi-done record at its current sha where the glob finds a report, append a synthetic `done` record (`done_reason="bootstrap-glob"`, number = next). Idempotent; no-op where the glob matches nothing (the mini).

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_magi_history.py
"""Magi 'already reviewed' comes from queue history, seeded once from the glob."""
from worksweep.assessor import bootstrap_magi_records, has_magi_done
from worksweep.models import MergeRequest, QueueRecord, WorkItem

NOW = "2026-08-07T12:00:00+00:00"


def _done(id, sha="", result_sha="", repo="pb-www", number=1):
    return QueueRecord(number=number, first_seen=NOW, last_seen=NOW,
                       item=WorkItem(schema_version=1, id=id, repo=repo,
                                     kind="mr", executor="magi-review",
                                     risk="low", why="", web_url="", sha=sha,
                                     status="done", result_sha=result_sha))


def _mr(iid=9, sha="s9"):
    return MergeRequest(repo="pb-www", iid=iid, title="t",
                        author="chandler.hardy", web_url="u", description="",
                        sha=sha, is_draft=False, reviewers=(),
                        ci_status="unknown", updated_at="")


def test_done_at_current_sha_counts():
    recs = [_done("magi:pb-www!9@s9", sha="s9")]
    assert has_magi_done(recs, "pb-www", 9, "s9") is True


def test_done_at_stale_sha_does_not_count():
    recs = [_done("magi:pb-www!9@old", sha="old")]
    assert has_magi_done(recs, "pb-www", 9, "s9") is False


def test_executor_review_result_sha_counts():
    recs = [_done("review:pb-www!9", sha="s9", result_sha="s9")]
    assert has_magi_done(recs, "pb-www", 9, "s9") is True


def test_bootstrap_seeds_only_missing(tmp_path):
    recs = bootstrap_magi_records([], [_mr()], NOW,
                                  report_exists=lambda r, i: True)
    assert len(recs) == 1
    assert recs[0].item.done_reason == "bootstrap-glob"
    assert recs[0].item.id == "magi:pb-www!9@s9"
    # idempotent second pass
    again = bootstrap_magi_records(recs, [_mr()], NOW,
                                   report_exists=lambda r, i: True)
    assert len(again) == 1


def test_bootstrap_noop_without_reports():
    assert bootstrap_magi_records([], [_mr()], NOW,
                                  report_exists=lambda r, i: False) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_magi_history.py -q`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement in `worksweep/assessor.py`**

```python
def has_magi_done(records, repo: str, iid: int, sha: str) -> bool:
    """Queue-backed replacement for the .magi file glob: a magi run for this
    (repo, iid) at the CURRENT head sha is recorded as a done record."""
    for r in records:
        it = r.item
        if it.executor != "magi-review" or it.status != "done" or it.repo != repo:
            continue
        if f"!{iid}@" not in it.id and it.id != f"review:{repo}!{iid}":
            continue
        if sha and (it.sha == sha or it.result_sha == sha):
            return True
    return False


def bootstrap_magi_records(records, authored, now: str,
                           report_exists=None):
    """One-time migration: seed done records from the legacy .magi glob so the
    first queue-backed sweep doesn't re-propose already-reviewed MRs. Idempotent;
    a machine without the worktrees (the mini) is a natural no-op."""
    report_exists = report_exists or has_magi_report
    out = list(records)
    next_num = max((r.number for r in out), default=0) + 1
    for mr in authored:
        if has_magi_done(out, mr.repo, mr.iid, mr.sha):
            continue
        if not report_exists(mr.repo, mr.iid):
            continue
        out.append(QueueRecord(
            number=next_num, first_seen=now, last_seen=now,
            item=WorkItem(schema_version=1, id=f"magi:{mr.repo}!{mr.iid}@{mr.sha}",
                          repo=mr.repo, kind="mr", executor="magi-review",
                          risk="low", why="seeded from legacy .magi report",
                          web_url=mr.web_url, sha=mr.sha, status="done",
                          done_reason="bootstrap-glob", result_sha=mr.sha)))
        next_num += 1
    return out
```

(`QueueRecord` import: `from .models import Issue, MergeRequest, QueueRecord, Todo, WorkItem`. `has_magi_report`/`_report_glob` stay in the file, now bootstrap-only.)

- [ ] **Step 4: Run tests, commit**

Run: `python3 -m pytest worksweep/tests/ -q` → all pass.

```bash
git add worksweep/assessor.py worksweep/tests/test_magi_history.py
git commit -m "feat(worksweep): queue-backed magi history with one-time glob bootstrap"
```

---

### Task 7: Wire `__main__` — GraphQL path, single-message contract, ⚠️ error post

**Files:**
- Modify: `worksweep/__main__.py`
- Test: `worksweep/tests/test_main_v2.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 3–6.
- Produces: `run_sweep(cfg, deps) -> int` — extracted, dependency-injected sweep so tests never shell out. `deps` is a plain dict: `{"graphql": callable_returning_raw, "todos": ..., "issues": ..., "post": callable(webhook, content), "load": ..., "save": ..., "now": ...}`. `main()` maps CLI → deps and adds the try/except ⚠️ wrapper. Message contract: items → digest message(s) (existing `format_messages_from_records`, parts labeled when >1); zero actionable items → `🔍 Worksweep: nothing needs you (checked N MRs, M authored)`; any exception → `⚠️ Worksweep sweep failed: {type}: {e}` posted best-effort, exit 1.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_main_v2.py
"""run_sweep: graphql wiring, one-message contract, error post."""
import json

from worksweep.__main__ import run_sweep
from worksweep.config import WorksweepConfig


def _cfg():
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y")


def _gql(review_nodes=(), authored_nodes=()):
    return json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": list(review_nodes)},
        "authoredMergeRequests": {"nodes": list(authored_nodes)}}}})


def _node(iid=1, state="UNREVIEWED"):
    return {"iid": str(iid), "title": "t", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{iid}",
            "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-07T00:00:00Z",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "other"},
            "reviewers": {"nodes": [
                {"username": "me", "mergeRequestInteraction": {"reviewState": state}}]},
            "headPipeline": None}


def _deps(store, raw, queue=None):
    return {
        "graphql": lambda: raw,
        "todos": lambda: [],
        "issues": lambda repo, user: [],
        "post": lambda hook, content: store.append(content),
        "load": lambda: list(queue or []),
        "save": lambda records: store.append(("saved", records)),
        "now": lambda: "2026-08-07T12:00:00+00:00",
    }


def test_actionable_item_posts_digest():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(review_nodes=[_node()])))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) >= 1 and "review" in texts[0].lower()


def test_nothing_actionable_posts_heartbeat():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(review_nodes=[_node(state="REVIEWED")])))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1 and texts[0].startswith("🔍")


def test_collector_exception_posts_error_and_exits_1():
    posts = []
    deps = _deps(posts, "")
    deps["graphql"] = lambda: (_ for _ in ()).throw(RuntimeError("glab exploded"))
    rc = run_sweep(_cfg(), deps)
    assert rc == 1
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1 and texts[0].startswith("⚠️") and "glab exploded" in texts[0]


def test_never_zero_messages():
    for raw in (_gql(), _gql(review_nodes=[_node()])):
        posts = []
        run_sweep(_cfg(), _deps(posts, raw))
        assert any(isinstance(p, str) for p in posts)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_main_v2.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_sweep'`.

- [ ] **Step 3: Implement in `worksweep/__main__.py`**

```python
def run_sweep(cfg: WorksweepConfig, deps: Dict[str, Callable]) -> int:
    """One sweep under the message contract: digest, 🔍 heartbeat, or ⚠️ error —
    never silence. All I/O arrives via `deps` so tests stay hermetic."""
    post = deps["post"]

    def _post_all(messages) -> None:
        if cfg.discord_webhook:
            for m in messages:
                post(cfg.discord_webhook, m)
        else:
            for m in messages:
                print(m)

    try:
        raw = deps["graphql"]()
        review_mrs, authored = collectors.parse_graphql_sweep(
            raw, cfg.username, cfg.repos)

        items = []
        for mr in review_mrs:
            items += assessor.assess_review_request(mr, cfg.username)
        records0 = deps["load"]()
        records0 = assessor.bootstrap_magi_records(
            records0, authored, deps["now"]())
        for mr in authored:
            items += assessor.assess_own_mr(
                mr, cfg.username,
                has_magi=lambda r, i, s: assessor.has_magi_done(records0, r, i, s))
        try:
            for td in deps["todos"]():
                items += assessor.assess_todo(td)
        except Exception as e:
            print(f"worksweep: todos collection failed: {e}", file=sys.stderr)
        for repo in cfg.repos:
            try:
                for iss in deps["issues"](repo, cfg.username):
                    items += assessor.assess_issue(iss)
            except Exception as e:
                print(f"worksweep: issues for {repo} failed: {e}", file=sys.stderr)
        items = assessor.dedupe(items)

        resolved = assessor.resolutions(review_mrs, cfg.username)
        records = reconcile(records0, items, deps["now"](), resolved=resolved)
        try:
            deps["save"](records)
        except OSError as e:
            print(f"worksweep: could not persist queue: {e}", file=sys.stderr)

        actionable = [r for r in records if r.item.status in ("proposed", "approved", "running")]
        if actionable:
            _post_all(format_messages_from_records(records))
        else:
            _post_all([f"🔍 Worksweep: nothing needs you "
                       f"(checked {len(review_mrs)} review requests, "
                       f"{len(authored)} authored MRs)"])
        return 0
    except Exception as e:
        msg = f"⚠️ Worksweep sweep failed: {type(e).__name__}: {e}"
        try:
            if cfg.discord_webhook:
                post(cfg.discord_webhook, msg)
        except Exception as post_err:
            print(f"worksweep: error post also failed: {post_err}", file=sys.stderr)
        print(msg, file=sys.stderr)
        return 1
```

In `main()`, replace the sweep branch (the `collect_fns` block through the final `print`) with:

```python
    deps = {
        "graphql": collectors.run_graphql_sweep,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
        "post": _post_discord,
        "load": lambda: load_queue(_queue_path()),
        "save": lambda records: save_queue(_queue_path(), records),
        "now": _now,
    }
    if args.dry_run or not args.discord:
        deps["post"] = lambda hook, content: print(content)
    return run_sweep(cfg, deps)
```

Check `format_messages_from_records` renders only non-terminal records (done/error must not clutter the digest) — if it renders all, filter in `run_sweep` before formatting: `format_messages_from_records([r for r in records if r.item.status not in ("done", "error")])`, and keep the actionable check consistent with the filter. Also verify `format_messages_from_records` labels multi-part digests (`(1/2)`) — if not, add the label in `formatter.py` in this task.

- [ ] **Step 4: Run full suite**

Run: `python3 -m pytest worksweep/tests/ -q`
Expected: all pass. `test_main.py` (M2) may need its `collect_fns` fixtures swapped for the `deps` shape — update those tests in this commit rather than keeping a dead code path.

- [ ] **Step 5: Live dry-run acceptance (MacBook, real glab)**

```bash
cd ~/repos/heartbeat && python3 -m worksweep --dry-run
```

Expected against today's dashboard: the 3 "Review requested" MRs appear; the 2 "Waiting for author" MRs (incl. !4020) do NOT; your 2 draft MRs produce own-MR items as applicable; nothing already-reviewed shows. Fix mapping bugs before committing.

- [ ] **Step 6: Commit**

```bash
git add worksweep/__main__.py worksweep/formatter.py worksweep/tests/test_main_v2.py worksweep/tests/test_main.py
git commit -m "feat(worksweep): GraphQL sweep wiring + never-silent message contract"
```

---

### Task 8: Runner core — claim, lock, reap (pure logic)

**Files:**
- Create: `worksweep/runner.py`
- Test: `worksweep/tests/test_runner.py` (create)

**Interfaces:**
- Consumes: `QueueRecord`, `WorkItem` (Task 2 fields).
- Produces (all pure except the lock pair):
  - `STALE_RUNNING_MINUTES = 45`
  - `reap_stale(records, now) -> Tuple[List[QueueRecord], List[QueueRecord]]` — running items with `claimed_at` older than 45 min flip to `error` (`error_summary="stale claim reaped"`); returns `(updated, reaped)`.
  - `pick_claim(records) -> Optional[QueueRecord]` — lowest-numbered `approved` item with `executor == "magi-review"`; None otherwise.
  - `claim(records, number, now) -> List[QueueRecord]` — that record → `status="running"`, `claimed_at=now`.
  - `complete(records, number, result_sha, report_path, now) -> List[QueueRecord]` — → `done`, `done_reason="executor-completed"`.
  - `fail(records, number, error_summary, now) -> List[QueueRecord]` — → `error`, summary truncated to 500 chars.
  - `acquire_lock(path) -> bool` / `release_lock(path)` — `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` writing the pid; on `FileExistsError` read the pid, and if `os.kill(pid, 0)` raises `ProcessLookupError` (or the pid is unparseable) remove the stale lock and retry once; otherwise False.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_runner.py
"""Runner claim/reap/complete state machine + lockfile."""
import datetime
import os

from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import (
    acquire_lock, claim, complete, fail, pick_claim, reap_stale, release_lock)

NOW = "2026-08-07T12:00:00+00:00"


def _rec(number, status="approved", executor="magi-review", claimed_at=""):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"review:pb-www!{number}",
                      repo="pb-www", kind="review_request", executor=executor,
                      risk="low", why="", web_url=f"https://gl/x/-/merge_requests/{number}",
                      sha=f"s{number}", status=status, claimed_at=claimed_at))


def test_pick_lowest_approved_magi_item():
    recs = [_rec(3), _rec(1, status="proposed"), _rec(2)]
    assert pick_claim(recs).number == 2


def test_pick_ignores_other_executors():
    assert pick_claim([_rec(1, executor="triage")]) is None


def test_claim_sets_running_and_timestamp():
    out = claim([_rec(1)], 1, NOW)
    assert out[0].item.status == "running" and out[0].item.claimed_at == NOW


def test_reap_stale_running():
    old = (datetime.datetime.fromisoformat(NOW)
           - datetime.timedelta(minutes=46)).isoformat()
    fresh = (datetime.datetime.fromisoformat(NOW)
             - datetime.timedelta(minutes=10)).isoformat()
    recs = [_rec(1, status="running", claimed_at=old),
            _rec(2, status="running", claimed_at=fresh)]
    updated, reaped = reap_stale(recs, NOW)
    assert [r.number for r in reaped] == [1]
    assert updated[0].item.status == "error"
    assert updated[1].item.status == "running"


def test_complete_and_fail():
    done = complete([_rec(1, status="running")], 1, "s1", "/r.md", NOW)
    assert done[0].item.status == "done"
    assert done[0].item.report_path == "/r.md"
    err = fail([_rec(2, status="running")], 2, "x" * 600, NOW)
    assert err[0].item.status == "error" and len(err[0].item.error_summary) == 500


def test_lockfile_excludes_second_holder(tmp_path):
    p = str(tmp_path / "runner.lock")
    assert acquire_lock(p) is True
    assert acquire_lock(p) is False      # held by a live pid (ours)
    release_lock(p)
    assert not os.path.exists(p)


def test_stale_lock_from_dead_pid_is_broken(tmp_path):
    p = str(tmp_path / "runner.lock")
    with open(p, "w") as f:
        f.write("999999999")             # certainly not a live pid
    assert acquire_lock(p) is True
    release_lock(p)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: worksweep.runner`.

- [ ] **Step 3: Implement `worksweep/runner.py` (state machine + lock half)**

```python
"""M3 executor runner: drain approved magi-review items, one at a time.

Pure state-machine functions (pick/claim/complete/fail/reap) are unit-tested;
the subprocess edge (execute) and the CLI glue live in run_once/execute. The
lockfile guarantees single-flight across overlapping launchd fires."""
from __future__ import annotations

import dataclasses
import datetime
import os
from typing import List, Optional, Tuple

from .models import QueueRecord

STALE_RUNNING_MINUTES = 45
_ERROR_SUMMARY_MAX = 500


def _replace(rec: QueueRecord, now: str, **item_changes) -> QueueRecord:
    return QueueRecord(number=rec.number, first_seen=rec.first_seen,
                       last_seen=now,
                       item=dataclasses.replace(rec.item, **item_changes))


def pick_claim(records: List[QueueRecord]) -> Optional[QueueRecord]:
    candidates = [r for r in records
                  if r.item.status == "approved" and r.item.executor == "magi-review"]
    return min(candidates, key=lambda r: r.number) if candidates else None


def claim(records: List[QueueRecord], number: int, now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="running", claimed_at=now)
            if r.number == number else r for r in records]


def complete(records: List[QueueRecord], number: int, result_sha: str,
             report_path: str, now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="done", done_reason="executor-completed",
                     result_sha=result_sha, report_path=report_path)
            if r.number == number else r for r in records]


def fail(records: List[QueueRecord], number: int, error_summary: str,
         now: str) -> List[QueueRecord]:
    return [_replace(r, now, status="error",
                     error_summary=(error_summary or "")[:_ERROR_SUMMARY_MAX])
            if r.number == number else r for r in records]


def reap_stale(records: List[QueueRecord],
               now: str) -> Tuple[List[QueueRecord], List[QueueRecord]]:
    updated, reaped = [], []
    for r in records:
        if r.item.status == "running" and _stale(r.item.claimed_at, now):
            nr = _replace(r, now, status="error",
                          error_summary="stale claim reaped")
            updated.append(nr)
            reaped.append(nr)
        else:
            updated.append(r)
    return updated, reaped


def _stale(claimed_at: str, now: str) -> bool:
    try:
        t = datetime.datetime.fromisoformat(claimed_at)
        n = datetime.datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return True   # unparseable claim time -> reap (running must be provable)
    if (t.tzinfo is None) != (n.tzinfo is None):
        return True
    return (n - t) > datetime.timedelta(minutes=STALE_RUNNING_MINUTES)


def acquire_lock(path: str) -> bool:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if attempt == 2:
                return False
            try:
                with open(path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                return False           # holder alive
            except (ValueError, ProcessLookupError):
                try:
                    os.remove(path)    # stale -> break it, retry once
                except FileNotFoundError:
                    pass
            except PermissionError:
                return False           # alive under another uid
    return False


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run tests, commit**

Run: `python3 -m pytest worksweep/tests/test_runner.py -q` → PASS.

```bash
git add worksweep/runner.py worksweep/tests/test_runner.py
git commit -m "feat(worksweep): runner state machine + single-flight lock"
```

---

### Task 9: Runner execute + `run` subcommand

**Files:**
- Modify: `worksweep/runner.py`, `worksweep/__main__.py`, `worksweep/config.py`
- Test: `worksweep/tests/test_runner_execute.py` (create)

**Interfaces:**
- Consumes: Task 8 functions; `_post_discord`, `load_queue`/`save_queue`, `WorksweepConfig`.
- Produces:
  - Config: `WorksweepConfig` gains `checkouts_root: str = ""`, `claude_bin: str = "claude"`, `runner_timeout: int = 1800`, read from an optional `"runner"` block in `~/etc/heartbeat.json` (`{"runner": {"checkouts_root": "...", "claude_bin": "...", "timeout_seconds": 1800}}`).
  - `runner.execute(item, cfg, run_subprocess=subprocess.run) -> Tuple[str, str]` — returns `(result_sha, report_path)`; raises `RunnerError(str)` on any failure.
  - `runner.find_report(checkout: str, iid: int) -> Optional[str]` — newest `.magi/tribunal-report-mr-{iid}-*.md` by mtime.
  - `runner.extract_verdict(report_path) -> str` — lines from the `## Verdict` heading up to the next `## ` heading, capped at 12 lines; `""` on any error.
  - `runner.run_once(cfg, deps) -> int` — reap → pick → claim+save → execute → complete/fail+save → Discord post. `deps = {"load","save","post","now","execute"}`. CLI: `python3 -m worksweep run [--dry-run]`; `--dry-run` swaps `execute` for a stub returning `(item.sha, "(dry-run)")`.
  - `_iid_of(item) -> int` — trailing integer of `web_url` path (`.../merge_requests/4020` → 4020).

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_runner_execute.py
"""execute() subprocess contract + run_once orchestration (all edges injected)."""
import os
import subprocess

import pytest

from worksweep.config import WorksweepConfig, load_config
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import RunnerError, execute, extract_verdict, find_report, run_once

NOW = "2026-08-07T12:00:00+00:00"


def _cfg(tmp_path):
    return WorksweepConfig(
        repos=("pb-www",), username="me",
        discord_webhook="https://discord.com/api/webhooks/x/y",
        checkouts_root=str(tmp_path), claude_bin="claude", runner_timeout=1800)


def _approved(number=1):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"review:pb-www!{number}",
                      repo="pb-www", kind="review_request",
                      executor="magi-review", risk="low", why="",
                      web_url="https://gl/x/-/merge_requests/4020",
                      sha="s1", status="approved"))


def test_runner_config_block(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                 '"discord_webhook": "https://discord.com/api/webhooks/x/y",'
                 '"runner": {"checkouts_root": "/co", "timeout_seconds": 900}}')
    cfg = load_config(str(p))
    assert cfg.checkouts_root == "/co"
    assert cfg.runner_timeout == 900
    assert cfg.claude_bin == "claude"


def test_execute_invokes_fetch_then_claude(tmp_path):
    os.makedirs(tmp_path / "pb-www")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-2026-08-07.md"
     ).write_text("## Verdict\nSHIP\n")
    sha, report = execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)
    assert sha == "s1" and report.endswith("tribunal-report-mr-4020-2026-08-07.md")
    assert calls[0][0][:3] == ("git", "-C", str(tmp_path / "pb-www"))
    assert calls[1][0][0] == "claude"
    assert "/magi:magi-review !4020" in calls[1][0]
    assert calls[1][1] == str(tmp_path / "pb-www")


def test_execute_missing_checkout_raises(tmp_path):
    with pytest.raises(RunnerError, match="checkout"):
        execute(_approved().item, _cfg(tmp_path))


def test_execute_nonzero_claude_raises(tmp_path):
    os.makedirs(tmp_path / "pb-www")

    def fake_run(cmd, **kw):
        rc = 1 if cmd[0] == "claude" else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom\n")

    with pytest.raises(RunnerError, match="boom"):
        execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)


def test_find_report_picks_newest(tmp_path):
    magi = tmp_path / ".magi"
    magi.mkdir()
    a = magi / "tribunal-report-mr-7-2026-08-01.md"
    b = magi / "tribunal-report-mr-7-2026-08-07.md"
    a.write_text("old"); b.write_text("new")
    os.utime(a, (1, 1))
    assert find_report(str(tmp_path), 7) == str(b)
    assert find_report(str(tmp_path), 8) is None


def test_extract_verdict_section(tmp_path):
    r = tmp_path / "r.md"
    r.write_text("# T\n\n## Verdict\nline1\nline2\n\n## Next\nx\n")
    v = extract_verdict(str(r))
    assert "line1" in v and "## Next" not in v


def test_run_once_happy_path(tmp_path):
    posts, saves = [], []
    deps = {"load": lambda: [_approved()],
            "save": lambda recs: saves.append(recs),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW,
            "execute": lambda item, cfg: ("s1", "/r.md")}
    assert run_once(_cfg(tmp_path), deps) == 0
    final = saves[-1]
    assert final[0].item.status == "done"
    assert any("magi-review" in p for p in posts)


def test_run_once_failure_posts_warning(tmp_path):
    posts, saves = [], []

    def boom(item, cfg):
        raise RunnerError("claude timed out")

    deps = {"load": lambda: [_approved()], "save": lambda r: saves.append(r),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW, "execute": boom}
    assert run_once(_cfg(tmp_path), deps) == 1
    assert saves[-1][0].item.status == "error"
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_nothing_approved_is_quiet(tmp_path):
    posts = []
    deps = {"load": lambda: [], "save": lambda r: None,
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW, "execute": lambda i, c: ("", "")}
    assert run_once(_cfg(tmp_path), deps) == 0
    assert posts == []   # runner is event-only, no heartbeat spam every 10 min
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest worksweep/tests/test_runner_execute.py -q`
Expected: FAIL — `ImportError: cannot import name 'RunnerError'` etc.

- [ ] **Step 3: Implement**

`worksweep/config.py` — extend the dataclass and loader:

```python
    checkouts_root: str = ""   # runner: parent dir of per-repo clones on the mini
    claude_bin: str = "claude"
    runner_timeout: int = 1800  # hard cap for one magi-review run (seconds)
```

```python
    rn = data.get("runner") or {}
    return WorksweepConfig(
        ...existing fields...,
        checkouts_root=rn.get("checkouts_root", ""),
        claude_bin=rn.get("claude_bin", "claude"),
        runner_timeout=int(rn.get("timeout_seconds", 1800)),
    )
```

`worksweep/runner.py` — append:

```python
import glob as _glob
import re
import subprocess
from typing import Callable, Dict

from .models import WorkItem

_LOCK_DEFAULT = os.path.expanduser("~/.worksweep/runner.lock")


class RunnerError(RuntimeError):
    """Executor failure with a human-postable summary."""


def _iid_of(item: WorkItem) -> int:
    m = re.search(r"/merge_requests/(\d+)", item.web_url)
    if not m:
        raise RunnerError(f"cannot find MR iid in web_url: {item.web_url!r}")
    return int(m.group(1))


def find_report(checkout: str, iid: int) -> Optional[str]:
    hits = _glob.glob(os.path.join(checkout, ".magi",
                                   f"tribunal-report-mr-{iid}-*.md"))
    return max(hits, key=os.path.getmtime) if hits else None


def extract_verdict(report_path: str) -> str:
    try:
        with open(report_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    out, capturing = [], False
    for line in lines:
        if line.startswith("## "):
            if capturing:
                break
            capturing = line.lower().startswith("## verdict")
            continue
        if capturing and line.strip():
            out.append(line)
        if len(out) >= 12:
            break
    return "\n".join(out)


def execute(item: WorkItem, cfg,
            run_subprocess: Callable = subprocess.run) -> Tuple[str, str]:
    """Fetch + run `claude -p "/magi:magi-review !<iid>"` in the repo checkout.
    Returns (result_sha, report_path). Raises RunnerError on any failure."""
    checkout = os.path.join(cfg.checkouts_root, item.repo)
    if not os.path.isdir(checkout):
        raise RunnerError(f"no checkout for {item.repo} at {checkout}")
    iid = _iid_of(item)
    try:
        fetch = run_subprocess(["git", "-C", checkout, "fetch", "origin"],
                               capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RunnerError("git fetch timed out")
    if fetch.returncode != 0:
        raise RunnerError(f"git fetch failed: {(fetch.stderr or '').strip()[-300:]}")
    try:
        proc = run_subprocess(
            [cfg.claude_bin, "-p", f"/magi:magi-review !{iid}"],
            cwd=checkout, capture_output=True, text=True,
            timeout=cfg.runner_timeout)
    except subprocess.TimeoutExpired:
        raise RunnerError(f"magi-review !{iid} exceeded {cfg.runner_timeout}s")
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-15:])
        raise RunnerError(f"magi-review !{iid} exited {proc.returncode}: {tail}")
    report = find_report(checkout, iid) or ""
    return item.sha, report


def run_once(cfg, deps: Dict[str, Callable], lock_path: str = _LOCK_DEFAULT) -> int:
    """One runner pass: reap stale claims, then run at most one approved item."""
    if not acquire_lock(lock_path):
        return 0    # another runner is live — that's fine, not an error
    try:
        now = deps["now"]()
        records = deps["load"]()
        records, reaped = reap_stale(records, now)
        if reaped:
            deps["save"](records)
            for r in reaped:
                _post(deps, cfg, f"⚠️ Worksweep runner: reaped stale claim "
                                 f"#{r.number} ({r.item.repo} {r.item.id})")
        target = pick_claim(records)
        if target is None:
            return 0
        records = claim(records, target.number, now)
        deps["save"](records)
        try:
            result_sha, report_path = deps["execute"](target.item, cfg)
        except RunnerError as e:
            records = fail(records, target.number, str(e), deps["now"]())
            deps["save"](records)
            _post(deps, cfg, f"⚠️ Worksweep runner: #{target.number} "
                             f"magi-review failed — {e}")
            return 1
        records = complete(records, target.number, result_sha, report_path,
                           deps["now"]())
        deps["save"](records)
        verdict = extract_verdict(report_path) if report_path else ""
        msg = (f"🧙 magi-review done — #{target.number} {target.item.repo} "
               f"<{target.item.web_url}>\n"
               + (f"```\n{verdict}\n```\n" if verdict else "")
               + (f"report: `{report_path}`" if report_path
                  else "(no report file found)"))
        _post(deps, cfg, msg)
        return 0
    finally:
        release_lock(lock_path)


def _post(deps, cfg, content: str) -> None:
    try:
        if cfg.discord_webhook:
            deps["post"](cfg.discord_webhook, content)
        else:
            print(content)
    except Exception as e:
        print(f"worksweep runner: discord post failed: {e}", file=os.sys.stderr)
```

(`import sys` at top instead of `os.sys` — use `sys.stderr`.)

`worksweep/__main__.py` — extend the CLI:

```python
    ap.add_argument("command", nargs="?", choices=["intake", "run"], ...)
```

```python
    if args.command == "run":
        from . import runner as _runner
        deps = {
            "load": lambda: load_queue(_queue_path()),
            "save": lambda records: save_queue(_queue_path(), records),
            "post": _post_discord,
            "now": _now,
            "execute": (lambda item, c: (item.sha, "(dry-run)"))
                       if args.dry_run else _runner.execute,
        }
        return _runner.run_once(cfg, deps)
```

- [ ] **Step 4: Run full suite**

Run: `python3 -m pytest worksweep/tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add worksweep/runner.py worksweep/config.py worksweep/__main__.py worksweep/tests/test_runner_execute.py worksweep/tests/test_config.py
git commit -m "feat(worksweep): magi-review executor — run subcommand, config, Discord report"
```

---

### Task 10: Mini launchd agents + cutover checklist

**Files:**
- Create: `etc/mini/com.chandlerhardy.worksweep.plist`, `etc/mini/com.chandlerhardy.worksweep-intake.plist`, `etc/mini/com.chandlerhardy.worksweep-runner.plist`
- Create: `docs/worksweep-mini-cutover.md`

No unit tests — plists are validated by `plutil -lint` and the checklist is executed by hand on the mini.

- [ ] **Step 1: Write the three plists**

`etc/mini/com.chandlerhardy.worksweep.plist` — copy the existing MacBook plist and change ONLY: the comment header (mini, always-on — no wake race), `Hour` → `9` (daily 9:00 mini-local; confirm the mini's TZ is CT during cutover, adjust if UTC), and both log paths → `/Users/chandlerhardy/heartbeat-reports/worksweep.log|.err` (same). PATH stays `/Users/chandlerhardy/.pyenv/shims:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin` — verify pyenv exists on the mini during cutover; if the mini uses system python3, drop the shims entry.

`etc/mini/com.chandlerhardy.worksweep-intake.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Worksweep intake poller (mini): reads Discord approval replies every 5 min. -->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.chandlerhardy.worksweep-intake</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/chandlerhardy/repos/heartbeat/bin/worksweep.sh</string>
        <string>intake</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/chandlerhardy/.pyenv/shims:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/chandlerhardy</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/chandlerhardy/heartbeat-reports/worksweep-intake.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/chandlerhardy/heartbeat-reports/worksweep-intake.err</string>
</dict>
</plist>
```

`etc/mini/com.chandlerhardy.worksweep-runner.plist` — same shape with `Label` `com.chandlerhardy.worksweep-runner`, ProgramArguments third element `run`, `StartInterval` `600`, log paths `worksweep-runner.log|.err`.

- [ ] **Step 2: Lint**

Run: `plutil -lint etc/mini/*.plist`
Expected: `OK` ×3.

- [ ] **Step 3: Write `docs/worksweep-mini-cutover.md`**

```markdown
# Worksweep mini cutover checklist

Prereqs on the mini (verify each, in order):
- [ ] `git clone git@github.com:<owner>/heartbeat.git ~/repos/heartbeat` (or pull latest main)
- [ ] `glab auth status` succeeds for gitlab.com performancelivestock (mini is PLA-provided — allowed)
- [ ] `python3 --version` ≥ 3.10; `python3 -m pytest worksweep/tests/ -q` green in ~/repos/heartbeat
- [ ] `~/etc/heartbeat.json` copied from the MacBook; add the runner block:
      `"runner": {"checkouts_root": "/Users/chandlerhardy/worksweep-checkouts"}`
- [ ] Checkouts: `mkdir -p ~/worksweep-checkouts && cd ~/worksweep-checkouts && git clone <gitlab>/pb-www.git` (repeat per configured repo)
- [ ] `claude --version` works; magi plugin installed (`claude -p "/magi:magi-core" …` not needed — verify with `claude -p "say ok"` then a real dry approval)
- [ ] codex CLI authenticated (Balthasar leg): `codex --version`
- [ ] `~/.worksweep/` queue: copy `~/.worksweep/queue.json` + `intake-cursor` from the MacBook (preserves numbers + history) — do this LAST, after the MacBook agents are unloaded
- [ ] TZ check: `date` — if the mini is not CT, adjust `Hour` in the sweep plist

Cutover:
- [ ] MacBook: `launchctl unload -w ~/Library/LaunchAgents/com.chandlerhardy.worksweep.plist` (and the intake plist if loaded); delete both from ~/Library/LaunchAgents
- [ ] Copy queue/cursor to the mini (step above)
- [ ] Mini: `cp etc/mini/*.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.chandlerhardy.worksweep*.plist`
- [ ] Smoke: `launchctl start com.chandlerhardy.worksweep` → expect exactly one Discord message (digest or 🔍)
- [ ] Break test: temporarily rename `glab` → run sweep → expect ⚠️ in Discord, restore
- [ ] First real executor run: reply `✅ <n>` to a review item → within 15 min expect 🧙 completion with verdict + a pending draft review on the MR (verify drafts are PENDING, not published)
```

- [ ] **Step 4: Commit**

```bash
git add etc/mini/ docs/worksweep-mini-cutover.md
git commit -m "feat(worksweep): mini launchd agents + cutover checklist"
git push origin main
```

---

### Task 11: Live acceptance

No code. Run on the MacBook first, then cut over.

- [ ] **Step 1: Dashboard parity check (MacBook).** `python3 -m worksweep --dry-run` and compare with https://gitlab.com/dashboard/merge_requests: every "Review requested" MR with your state unreviewed appears; every "Waiting for author" MR is absent; your authored MRs show feedback/ci items matching the dashboard badges. Any mismatch is a bug in Task 3/4 — fix before proceeding.
- [ ] **Step 2: Execute `docs/worksweep-mini-cutover.md` end to end.**
- [ ] **Step 3: First unattended review.** Approve one item from the digest; verify the 🧙 report, the pending drafts on the MR, and that the NEXT sweep shows the item `done` (not re-proposed).
- [ ] **Step 4: Close out.** Update `~/.claude/dev-docs/ops-board.md` Agent-estate section: worksweep = mini, loud, M3-v1 live.

---

## Self-review notes (already applied)

- **Spec coverage:** sensor truth → Tasks 3/4; queue lifecycle → 5; magi history + bootstrap → 6; single-message contract + error post → 7; mini runtime → 10; executor → 8/9; rollout acceptance → 11; M2 merge → 1.
- **Known limit carried from spec:** re-request-at-identical-SHA stays `done` (Task 5 rule 4); intake/runner are event-only posters (no heartbeat) — the daily sweep is the liveness signal.
- **Type consistency:** `has_magi` is `(repo, iid, sha)` everywhere after Task 4; `reconcile(existing, fresh, now, resolved=None)` keyword shape is what Task 7 calls; runner deps dict keys (`load/save/post/now/execute`) match between Tasks 9's `run_once` and `__main__` wiring.
