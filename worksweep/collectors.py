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
