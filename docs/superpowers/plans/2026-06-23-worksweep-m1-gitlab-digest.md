# Worksweep M1 — GitLab Sweep + Discord Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only GitLab "worksweep" to heartbeat that scans Chandler's PLA merge requests, review requests, GitLab to-dos, and assigned issues, applies deterministic rules to surface what needs attention, and posts a numbered Discord digest.

**Architecture:** A new Python package `worksweep/` in the heartbeat repo, mirroring the existing `shiplog/` package shape (frozen-dataclass models, thin CLI collectors with pure parse functions, pure assessor + formatter, a `__main__` entry, a `bin/worksweep.sh` wrapper). This is **M1 of the seneschal-steward design** (`~/repos/seneschal/docs/superpowers/specs/2026-06-23-seneschal-steward-design.md`): the Sensor's read-only slice. No work queue, no executors, no LLM, no writes — just sweep → assess (rule-based) → digest. M2+ (queue, approval intake, executors) are separate plans.

**Tech Stack:** Python 3.13 (stdlib only — `subprocess`, `json`, `urllib`, `dataclasses`, `re`), `glab` CLI for GitLab access, pytest. No new dependencies.

## Global Constraints

- **Read-only.** M1 issues ZERO writes to GitLab. Collectors only run `glab api` GET requests. No MR edits, no notes, no executor calls.
- **Metadata only.** Never fetch repo source or diffs. Only MR/issue/todo metadata (title, state, SHAs, CI/approval status, web_url, description text).
- **Stdlib only.** No new pip dependencies. Match `shiplog/`'s import style.
- **Pure-function testability.** Each collector is split into a thin `collect_*()` (subprocess, untested) and a pure `parse_*(raw_json: str)` (unit-tested with fixture strings) — exactly like `shiplog/collectors.py`.
- **Frozen dataclasses** for all model types (`@dataclass(frozen=True)`), matching `shiplog/models.py`.
- **Discord cap:** the formatter must cap output at `DISCORD_MAX_CHARS = 1900` to match `shiplog/formatter.py` and `bin/heartbeat-lib.sh:send_discord` (byte-level truncation contract).
- **Config:** read from `~/etc/heartbeat.json` (existing file), new `gitlab` block; reuse the existing top-level `discord_webhook`.
- **Repo:** `~/repos/heartbeat` (personal — **no `Co-Authored-By` trailers** in commits per `~/.claude/CLAUDE.md`).
- **GitLab identity:** username `chandler.hardy`; PLA project paths are `performancelivestock/<repo>` (e.g. `performancelivestock/pb-www`).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `worksweep/__init__.py` | Package marker (empty). |
| `worksweep/models.py` | Frozen dataclasses: `MergeRequest`, `Todo`, `Issue` (raw signals) + `WorkItem` (assessed proposal, the seam schema). |
| `worksweep/collectors.py` | `_run_glab()` + pure `parse_mrs/parse_todos/parse_issues(raw_json)` + thin `collect_*()` wrappers. |
| `worksweep/assessor.py` | Pure rule functions (`assess_*`) → `WorkItem`s, plus `dedupe()`. The deterministic M1 brain. |
| `worksweep/formatter.py` | `format_digest(items) -> str` — numbered Discord message, capped at 1900 bytes. |
| `worksweep/config.py` | `load_config()` — read `~/etc/heartbeat.json`, return the `gitlab` block + webhook. |
| `worksweep/__main__.py` | CLI: collect → assess → dedupe → format → (`--dry-run` stdout \| `--discord` post). |
| `worksweep/tests/test_*.py` | pytest for collectors/assessor/formatter/config. |
| `bin/worksweep.sh` | Wrapper mirroring `bin/shiplog.sh` (`--dry-run`/`--discord`). |
| `etc/heartbeat.json.example` | Modify: add the `gitlab` block. |

---

### Task 1: Models

**Files:**
- Create: `worksweep/__init__.py` (empty)
- Create: `worksweep/models.py`
- Test: `worksweep/tests/__init__.py` (empty), `worksweep/tests/test_models.py`

**Interfaces:**
- Produces:
  - `MergeRequest(repo:str, iid:int, title:str, author:str, web_url:str, description:str, sha:str, is_draft:bool, reviewers:tuple, ci_status:str, updated_at:str)` with property `dev_url_present:bool`.
  - `Todo(target:str, action:str, web_url:str)`; `Issue(repo:str, iid:int, title:str, web_url:str)`.
  - `WorkItem(schema_version:int, id:str, repo:str, kind:str, executor:str, risk:str, why:str, web_url:str, sha:str, status:str="proposed")`.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_models.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, WorkItem  # noqa: E402


def test_merge_request_dev_url_present_true():
    mr = MergeRequest(
        repo="pb-www", iid=3920, title="t", author="leyang",
        web_url="https://gitlab.com/x/-/merge_requests/3920",
        description="## Dev link\n**https://leyang-dev4.performancebeef.com/x** ready",
        sha="abc", is_draft=False, reviewers=("chandler.hardy",),
        ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )
    assert mr.dev_url_present is True


def test_merge_request_dev_url_present_false():
    mr = MergeRequest(
        repo="pb-www", iid=1, title="t", author="me", web_url="u",
        description="no link here", sha="abc", is_draft=False,
        reviewers=(), ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )
    assert mr.dev_url_present is False


def test_workitem_defaults_status_proposed():
    wi = WorkItem(schema_version=1, id="magi:pb-www!1@abc", repo="pb-www",
                  kind="mr", executor="magi-review", risk="low",
                  why="no magi review", web_url="u", sha="abc")
    assert wi.status == "proposed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worksweep'`.

- [ ] **Step 3: Write minimal implementation**

```python
# worksweep/__init__.py
```
(empty file)

```python
# worksweep/models.py
"""Data types for Worksweep (the GitLab sensor slice)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# A dev-server link in an MR description, per Chandler's MR convention
# ("Available on" / a *-dev*.performancebeef.com URL).
_DEV_URL_RE = re.compile(r"https?://[^\s)]*dev\d*[^\s)]*\.performancebeef\.com", re.I)


@dataclass(frozen=True)
class MergeRequest:
    repo: str
    iid: int
    title: str
    author: str
    web_url: str
    description: str
    sha: str
    is_draft: bool
    reviewers: tuple  # tuple[str, ...]
    ci_status: str     # "success" | "failed" | "running" | "unknown"
    updated_at: str    # ISO8601

    @property
    def dev_url_present(self) -> bool:
        return bool(_DEV_URL_RE.search(self.description or ""))


@dataclass(frozen=True)
class Todo:
    target: str
    action: str
    web_url: str


@dataclass(frozen=True)
class Issue:
    repo: str
    iid: int
    title: str
    web_url: str


@dataclass(frozen=True)
class WorkItem:
    schema_version: int
    id: str
    repo: str
    kind: str       # "mr" | "review_request" | "todo" | "issue"
    executor: str   # "magi-review" | "mr-hygiene" | "review" | "triage"
    risk: str       # "low" | "medium" | "high"
    why: str
    web_url: str
    sha: str
    status: str = "proposed"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_models.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/heartbeat
git add worksweep/__init__.py worksweep/models.py worksweep/tests/__init__.py worksweep/tests/test_models.py
git commit -m "feat(worksweep): model types for GitLab work items"
```

---

### Task 2: Collectors (pure parsers + thin glab wrappers)

**Files:**
- Create: `worksweep/collectors.py`
- Test: `worksweep/tests/test_collectors.py`

**Interfaces:**
- Consumes: `worksweep.models.MergeRequest`, `Todo`, `Issue`.
- Produces:
  - `parse_mrs(raw_json:str, repo:str) -> list[MergeRequest]`
  - `parse_todos(raw_json:str) -> list[Todo]`
  - `parse_issues(raw_json:str, repo:str) -> list[Issue]`
  - `_run_glab(args:list[str], timeout:int=30) -> str`
  - `collect_my_mrs(repo) / collect_review_requests(repo) / collect_todos() / collect_issues(repo)` — thin, untested.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_collectors.py
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.collectors import parse_mrs, parse_todos, parse_issues  # noqa: E402


def test_parse_mrs_empty():
    assert parse_mrs("[]", "pb-www") == []


def test_parse_mrs_basic():
    raw = json.dumps([{
        "iid": 3920, "title": "fix: x", "author": {"username": "leyang"},
        "web_url": "https://gitlab.com/x/-/merge_requests/3920",
        "description": "no dev link", "sha": "abc123", "draft": False,
        "reviewers": [{"username": "chandler.hardy"}],
        "updated_at": "2026-06-22T10:00:00Z",
        "head_pipeline": {"status": "success"},
    }])
    mrs = parse_mrs(raw, "pb-www")
    assert len(mrs) == 1
    assert mrs[0].iid == 3920
    assert mrs[0].repo == "pb-www"
    assert mrs[0].author == "leyang"
    assert mrs[0].reviewers == ("chandler.hardy",)
    assert mrs[0].ci_status == "success"
    assert mrs[0].is_draft is False


def test_parse_mrs_missing_pipeline_is_unknown():
    raw = json.dumps([{
        "iid": 1, "title": "t", "author": {"username": "me"}, "web_url": "u",
        "description": "", "sha": "s", "draft": True, "reviewers": [],
        "updated_at": "2026-06-22T10:00:00Z",
    }])
    assert parse_mrs(raw, "pb-www")[0].ci_status == "unknown"


def test_parse_mrs_malformed_json_returns_empty():
    assert parse_mrs("not json", "pb-www") == []


def test_parse_todos_basic():
    raw = json.dumps([{
        "target_type": "MergeRequest", "action_name": "review_requested",
        "target_url": "https://gitlab.com/x/-/merge_requests/9",
        "body": "Review requested",
    }])
    todos = parse_todos(raw)
    assert len(todos) == 1
    assert todos[0].action == "review_requested"


def test_parse_issues_basic():
    raw = json.dumps([{"iid": 42, "title": "bug", "web_url": "u"}])
    issues = parse_issues(raw, "pb-api")
    assert issues[0].iid == 42 and issues[0].repo == "pb-api"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_collectors.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_mrs'`.

- [ ] **Step 3: Write minimal implementation**

```python
# worksweep/collectors.py
"""Collect GitLab work signals via `glab api` (read-only GET).

Thin shell wrappers (collect_*) call glab; pure parse_* functions map raw
JSON onto model types and are unit-tested without any network. Mirrors
shiplog/collectors.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import List

from .models import Issue, MergeRequest, Todo

PROJECT_PREFIX = "performancelivestock"


def _run_glab(args: List[str], timeout: int = 30) -> str:
    """Run a glab command, returning stdout. Raises on failure."""
    result = subprocess.run(
        ["glab", *args], capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"glab {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _ci_status(item: dict) -> str:
    pipe = item.get("head_pipeline") or {}
    return pipe.get("status") or "unknown"


def parse_mrs(raw_json: str, repo: str) -> List[MergeRequest]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"worksweep: parse_mrs decode failed: {e}", file=sys.stderr)
        return []
    out: List[MergeRequest] = []
    for it in data:
        out.append(MergeRequest(
            repo=repo,
            iid=int(it.get("iid", 0)),
            title=it.get("title", ""),
            author=(it.get("author") or {}).get("username", ""),
            web_url=it.get("web_url", ""),
            description=it.get("description") or "",
            sha=it.get("sha") or "",
            is_draft=bool(it.get("draft", False)),
            reviewers=tuple((r or {}).get("username", "") for r in (it.get("reviewers") or [])),
            ci_status=_ci_status(it),
            updated_at=it.get("updated_at", ""),
        ))
    return out


def parse_todos(raw_json: str) -> List[Todo]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"worksweep: parse_todos decode failed: {e}", file=sys.stderr)
        return []
    return [Todo(
        target=it.get("target_type", ""),
        action=it.get("action_name", ""),
        web_url=it.get("target_url", ""),
    ) for it in data]


def parse_issues(raw_json: str, repo: str) -> List[Issue]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"worksweep: parse_issues decode failed: {e}", file=sys.stderr)
        return []
    return [Issue(repo=repo, iid=int(it.get("iid", 0)),
                  title=it.get("title", ""), web_url=it.get("web_url", "")) for it in data]


def _project(repo: str) -> str:
    # URL-encode the project path for glab api: performancelivestock%2Fpb-www
    return f"{PROJECT_PREFIX}%2F{repo}"


def collect_my_mrs(repo: str, username: str) -> List[MergeRequest]:
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/merge_requests?state=opened&author_username={username}&with_merge_status_recheck=true"])
    return parse_mrs(raw, repo)


def collect_review_requests(repo: str, username: str) -> List[MergeRequest]:
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/merge_requests?state=opened&reviewer_username={username}"])
    return parse_mrs(raw, repo)


def collect_todos() -> List[Todo]:
    return parse_todos(_run_glab(["api", "todos?state=pending&per_page=100"]))


def collect_issues(repo: str, username: str) -> List[Issue]:
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/issues?state=opened&assignee_username={username}"])
    return parse_issues(raw, repo)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_collectors.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/heartbeat
git add worksweep/collectors.py worksweep/tests/test_collectors.py
git commit -m "feat(worksweep): glab collectors with pure JSON parsers"
```

---

### Task 3: Assessor (deterministic rules → WorkItems)

**Files:**
- Create: `worksweep/assessor.py`
- Test: `worksweep/tests/test_assessor.py`

**Interfaces:**
- Consumes: `MergeRequest`, `Todo`, `Issue`, `WorkItem`.
- Produces:
  - `assess_mr(mr:MergeRequest, username:str, has_magi:Callable[[str,str],bool]) -> list[WorkItem]`
  - `assess_todo(todo:Todo) -> list[WorkItem]`
  - `assess_issue(issue:Issue) -> list[WorkItem]`
  - `dedupe(items:list[WorkItem]) -> list[WorkItem]`
  - `has_magi_report(repo:str, sha:str) -> bool` (default `has_magi`; checks local `.magi/` reports — injectable so tests stay pure).

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_assessor.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, Todo, Issue  # noqa: E402
from worksweep.assessor import assess_mr, assess_todo, dedupe  # noqa: E402


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="me", web_url="u",
                description="", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


def test_mine_without_magi_proposes_magi_review():
    items = assess_mr(_mr(author="chandler.hardy"), "chandler.hardy",
                      has_magi=lambda r, s: False)
    assert any(i.executor == "magi-review" for i in items)


def test_mine_with_magi_does_not_propose_magi_review():
    items = assess_mr(_mr(author="chandler.hardy"), "chandler.hardy",
                      has_magi=lambda r, s: True)
    assert not any(i.executor == "magi-review" for i in items)


def test_missing_dev_url_proposes_hygiene():
    items = assess_mr(_mr(author="chandler.hardy", description="no link"),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert any(i.executor == "mr-hygiene" for i in items)


def test_present_dev_url_no_hygiene():
    desc = "see https://x-dev4.performancebeef.com/y"
    items = assess_mr(_mr(author="chandler.hardy", description=desc),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert not any(i.executor == "mr-hygiene" for i in items)


def test_review_request_when_im_reviewer_not_author():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",)),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert any(i.executor == "review" for i in items)


def test_draft_review_request_is_skipped():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",), is_draft=True),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert not any(i.executor == "review" for i in items)


def test_dedupe_by_id():
    a = assess_todo(Todo(target="MergeRequest", action="review_requested",
                         web_url="https://gitlab.com/x/-/merge_requests/9"))
    again = assess_todo(Todo(target="MergeRequest", action="review_requested",
                             web_url="https://gitlab.com/x/-/merge_requests/9"))
    assert len(dedupe(a + again)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_assessor.py -v`
Expected: FAIL — `ImportError: cannot import name 'assess_mr'`.

- [ ] **Step 3: Write minimal implementation**

```python
# worksweep/assessor.py
"""Deterministic M1 rules: signals -> WorkItems. No LLM, pure + testable."""
from __future__ import annotations

import os
from glob import glob
from typing import Callable, List

from .models import Issue, MergeRequest, Todo, WorkItem

MAGI_REPORTS_GLOB = os.path.expanduser(
    "~/workspaces/pla/pla0/pb-www/.magi/tribunal-report-mr-{iid}-*.md"
)


def has_magi_report(repo: str, sha: str, iid: int = 0) -> bool:
    """True if a local magi tribunal report exists for this MR.

    M1 heuristic: presence of any tribunal report file for the MR iid. (SHA-
    precise dedup arrives with the M2 queue; for M1 a report's existence is a
    good-enough 'already reviewed' signal.) Injectable in tests.
    """
    return bool(glob(MAGI_REPORTS_GLOB.format(iid=iid)))


def assess_mr(mr: MergeRequest, username: str,
              has_magi: Callable[[str, str], bool]) -> List[WorkItem]:
    items: List[WorkItem] = []
    mine = mr.author == username
    if mine:
        if not has_magi(mr.repo, mr.sha):
            items.append(WorkItem(
                schema_version=1, id=f"magi:{mr.repo}!{mr.iid}@{mr.sha}",
                repo=mr.repo, kind="mr", executor="magi-review", risk="low",
                why="no magi-review yet", web_url=mr.web_url, sha=mr.sha))
        if not mr.dev_url_present:
            items.append(WorkItem(
                schema_version=1, id=f"hygiene-devurl:{mr.repo}!{mr.iid}",
                repo=mr.repo, kind="mr", executor="mr-hygiene", risk="low",
                why="description missing dev-server link", web_url=mr.web_url, sha=mr.sha))
    elif username in mr.reviewers and not mr.is_draft:
        ready = "CI green" if mr.ci_status == "success" else f"CI {mr.ci_status}"
        items.append(WorkItem(
            schema_version=1, id=f"review:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="review_request", executor="review",
            risk="low", why=f"review requested ({ready})",
            web_url=mr.web_url, sha=mr.sha))
    return items


def assess_todo(todo: Todo) -> List[WorkItem]:
    return [WorkItem(
        schema_version=1, id=f"todo:{todo.web_url}", repo="", kind="todo",
        executor="triage", risk="low", why=f"{todo.action} on {todo.target}",
        web_url=todo.web_url, sha="")]


def assess_issue(issue: Issue) -> List[WorkItem]:
    return [WorkItem(
        schema_version=1, id=f"issue:{issue.repo}#{issue.iid}", repo=issue.repo,
        kind="issue", executor="triage", risk="low",
        why=f"assigned issue: {issue.title}", web_url=issue.web_url, sha="")]


def dedupe(items: List[WorkItem]) -> List[WorkItem]:
    seen, out = set(), []
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        out.append(it)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_assessor.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/heartbeat
git add worksweep/assessor.py worksweep/tests/test_assessor.py
git commit -m "feat(worksweep): deterministic rule-based assessor"
```

---

### Task 4: Formatter (Discord digest)

**Files:**
- Create: `worksweep/formatter.py`
- Test: `worksweep/tests/test_formatter.py`

**Interfaces:**
- Consumes: `WorkItem`.
- Produces: `format_digest(items:list[WorkItem]) -> str`; constant `DISCORD_MAX_CHARS = 1900`.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_formatter.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem  # noqa: E402
from worksweep.formatter import format_digest, DISCORD_MAX_CHARS  # noqa: E402


def _wi(i, executor="magi-review", why="why"):
    return WorkItem(schema_version=1, id=f"x{i}", repo="pb-www", kind="mr",
                    executor=executor, risk="low", why=why,
                    web_url=f"https://gitlab.com/x/-/merge_requests/{i}", sha="abc")


def test_empty_digest_says_all_clear():
    out = format_digest([])
    assert "nothing needs you" in out.lower()


def test_digest_numbers_items():
    out = format_digest([_wi(1), _wi(2)])
    assert "1." in out and "2." in out


def test_digest_includes_executor_and_why():
    out = format_digest([_wi(1, executor="mr-hygiene", why="missing dev link")])
    assert "mr-hygiene" in out and "missing dev link" in out


def test_digest_capped_to_byte_limit():
    out = format_digest([_wi(i) for i in range(200)])
    assert len(out.encode("utf-8")) <= DISCORD_MAX_CHARS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_formatter.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_digest'`.

- [ ] **Step 3: Write minimal implementation**

```python
# worksweep/formatter.py
"""Render WorkItems into a numbered Discord digest. Matches the
shiplog/heartbeat-lib 1900-byte Discord cap."""
from __future__ import annotations

from typing import List

from .models import WorkItem

DISCORD_MAX_CHARS = 1900


def _truncate_bytes(s: str, max_bytes: int = DISCORD_MAX_CHARS) -> str:
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    cut = enc[:max_bytes - 3]
    while cut and (cut[-1] & 0xC0) == 0x80:  # rewind partial multibyte tail
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore") + "..."


def format_digest(items: List[WorkItem]) -> str:
    if not items:
        return "✅ Worksweep: nothing needs you right now."
    lines = [f"🔭 **Worksweep** — {len(items)} item(s) need you:"]
    for n, it in enumerate(items, 1):
        lines.append(f"{n}. `{it.executor}` {it.repo} — {it.why}\n   {it.web_url}")
    lines.append("\nReply e.g. `✅ 1,3` to approve (executors land in M2+).")
    return _truncate_bytes("\n".join(lines))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_formatter.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/heartbeat
git add worksweep/formatter.py worksweep/tests/test_formatter.py
git commit -m "feat(worksweep): Discord digest formatter"
```

---

### Task 5: Config loader

**Files:**
- Create: `worksweep/config.py`
- Modify: `etc/heartbeat.json.example` (add `gitlab` block)
- Test: `worksweep/tests/test_config.py`

**Interfaces:**
- Produces: `load_config(path:str|None=None) -> WorksweepConfig` where
  `WorksweepConfig(repos:tuple[str,...], username:str, discord_webhook:str)`.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_config.py
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.config import load_config  # noqa: E402


def _write(tmp, obj):
    p = os.path.join(tmp, "heartbeat.json")
    with open(p, "w") as f:
        json.dump(obj, f)
    return p


def test_load_config_reads_gitlab_block():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "discord_webhook": "https://discord/hook",
            "gitlab": {"username": "chandler.hardy", "repos": ["pb-www", "pb-api"]},
        })
        cfg = load_config(p)
        assert cfg.username == "chandler.hardy"
        assert cfg.repos == ("pb-www", "pb-api")
        assert cfg.discord_webhook == "https://discord/hook"


def test_load_config_missing_gitlab_block_yields_empty_repos():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"discord_webhook": "x"})
        cfg = load_config(p)
        assert cfg.repos == () and cfg.username == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worksweep.config'`.

- [ ] **Step 3: Write minimal implementation**

```python
# worksweep/config.py
"""Load the worksweep slice of ~/etc/heartbeat.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorksweepConfig:
    repos: tuple
    username: str
    discord_webhook: str


def load_config(path: str | None = None) -> WorksweepConfig:
    path = path or os.path.expanduser("~/etc/heartbeat.json")
    with open(path) as f:
        data = json.load(f)
    gl = data.get("gitlab") or {}
    return WorksweepConfig(
        repos=tuple(gl.get("repos") or []),
        username=gl.get("username", ""),
        discord_webhook=data.get("discord_webhook", ""),
    )
```

Then modify `etc/heartbeat.json.example` — add the `gitlab` block after `discord_webhook`:

```json
{
  "projects": [
    {
      "name": "my-project",
      "path": "/mnt/block_volume/repos/my-project",
      "stale_days": 14
    }
  ],
  "max_quick_wins_per_project": 2,
  "discord_webhook": "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL",
  "gitlab": {
    "username": "chandler.hardy",
    "repos": ["pb-www", "pb-api", "pb-ios"]
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd ~/repos/heartbeat
git add worksweep/config.py worksweep/tests/test_config.py etc/heartbeat.json.example
git commit -m "feat(worksweep): config loader for gitlab block"
```

---

### Task 6: Entry point + shell wrapper

**Files:**
- Create: `worksweep/__main__.py`
- Create: `bin/worksweep.sh`
- Test: `worksweep/tests/test_main.py`

**Interfaces:**
- Consumes: all prior modules.
- Produces: `build_digest(collect_fns, cfg, has_magi) -> str` (pure orchestration, injectable collectors for testing); `main(argv) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# worksweep/tests/test_main.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, Todo, Issue  # noqa: E402
from worksweep.config import WorksweepConfig  # noqa: E402
from worksweep.__main__ import build_digest  # noqa: E402


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="chandler.hardy", web_url="u",
                description="no link", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


def test_build_digest_end_to_end_with_injected_collectors():
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="x")
    collect_fns = {
        "my_mrs": lambda repo, user: [_mr()],
        "review_requests": lambda repo, user: [],
        "todos": lambda: [],
        "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, s: True)
    # mine + missing dev link -> exactly one hygiene item
    assert "mr-hygiene" in out
    assert "1." in out


def test_build_digest_empty_is_all_clear():
    cfg = WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="x")
    collect_fns = {
        "my_mrs": lambda repo, user: [], "review_requests": lambda repo, user: [],
        "todos": lambda: [], "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, s: True)
    assert "nothing needs you" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/heartbeat && python3 -m pytest worksweep/tests/test_main.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_digest'`.

- [ ] **Step 3: Write minimal implementation**

```python
# worksweep/__main__.py
"""Worksweep entry point: collect -> assess -> dedupe -> format -> output.

Read-only M1. `--dry-run` prints to stdout; `--discord` posts the digest.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Callable, Dict

from . import assessor, collectors
from .config import WorksweepConfig, load_config
from .formatter import format_digest


def build_digest(collect_fns: Dict[str, Callable], cfg: WorksweepConfig,
                 has_magi: Callable[[str, str], bool]) -> str:
    items = []
    for repo in cfg.repos:
        for mr in collect_fns["my_mrs"](repo, cfg.username):
            items += assessor.assess_mr(mr, cfg.username, has_magi)
        for mr in collect_fns["review_requests"](repo, cfg.username):
            items += assessor.assess_mr(mr, cfg.username, has_magi)
        for iss in collect_fns["issues"](repo, cfg.username):
            items += assessor.assess_issue(iss)
    for td in collect_fns["todos"]():
        items += assessor.assess_todo(td)
    return format_digest(assessor.dedupe(items))


def _post_discord(webhook: str, content: str) -> None:
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "WorksweepBot/1.0"})
    urllib.request.urlopen(req, timeout=15)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, no Discord")
    ap.add_argument("--discord", action="store_true", help="post digest to Discord")
    args = ap.parse_args(argv)

    cfg = load_config()
    collect_fns = {
        "my_mrs": collectors.collect_my_mrs,
        "review_requests": collectors.collect_review_requests,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
    }
    digest = build_digest(
        collect_fns, cfg,
        has_magi=lambda r, s: assessor.has_magi_report(r, s))

    if args.discord and not args.dry_run:
        if not cfg.discord_webhook:
            print("worksweep: no discord_webhook configured", file=sys.stderr)
            return 1
        _post_discord(cfg.discord_webhook, digest)
    else:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

```bash
# bin/worksweep.sh
#!/bin/bash
# Worksweep — read-only GitLab digest of MRs/reviews/todos/issues.
#   ./worksweep.sh --dry-run    # stdout only (default if no --discord)
#   ./worksweep.sh --discord    # post the digest to Discord
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
exec python3 -m worksweep "$@"
```

- [ ] **Step 4: Run the tests + make the wrapper executable**

Run:
```bash
cd ~/repos/heartbeat
chmod +x bin/worksweep.sh
python3 -m pytest worksweep/tests/ -v
```
Expected: PASS (all worksweep tests green — 24 total across tasks 1-6).

- [ ] **Step 5: Manual smoke test (real glab, read-only)**

Run: `cd ~/repos/heartbeat && ./bin/worksweep.sh --dry-run`
Expected: a printed digest listing real open MRs/reviews/todos for `chandler.hardy` (or "nothing needs you"). Verify it makes only GET calls (no MR changes appear on GitLab).

- [ ] **Step 6: Commit**

```bash
cd ~/repos/heartbeat
git add worksweep/__main__.py bin/worksweep.sh worksweep/tests/test_main.py
git commit -m "feat(worksweep): entry point + shell wrapper (read-only digest)"
```

---

## Self-Review

**Spec coverage (against the M1 milestone of the seneschal-steward spec §9):**
- "Heartbeat gains the glab sweep" → Task 2 (collectors). ✓
- "+ assess" → Task 3 (deterministic rules; LLM-assess explicitly deferred per §10). ✓
- "+ Discord digest of WorkItems" → Task 4 + Task 6. ✓
- "No executors yet" → respected; executors are M3-M5, out of this plan. ✓
- WorkItem schema (spec §4.2) → Task 1 `WorkItem` dataclass (`schema_version`, `id`, `repo`, `kind`, `executor`, `risk`, `why`, `web_url`, `sha`, `status`). ✓ (M1 omits `signal`/`result` blobs — not needed until the M2 queue persists items; noted.)
- Security boundary (spec §8): collectors are GET-only `glab api`; Global Constraints forbid writes. ✓
- Backend pluggability (spec §6): not exercised in M1 (no LLM); arrives with the LLM-assessor / executors in later plans. ✓ (intentional)

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test shows real assertions; commands have expected output. ✓

**Type consistency:** `MergeRequest`/`Todo`/`Issue`/`WorkItem` field names are identical across Tasks 1-6; `assess_mr(mr, username, has_magi)` signature matches its call in `build_digest` (Task 6); `format_digest`/`DISCORD_MAX_CHARS` names consistent (Task 4 ↔ used nowhere conflicting); `WorksweepConfig(repos, username, discord_webhook)` consistent (Task 5 ↔ Task 6). ✓

**Known M1 simplification (intentional, documented):** `has_magi_report` keys on MR iid + a hardcoded pb-www `.magi/` glob, not a SHA-precise queue. SHA-precise dedup lands with the M2 work-queue. Flagged in the assessor docstring and spec §10.
