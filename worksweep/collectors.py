"""Collect GitLab work signals via `glab api` (read-only GET).

Thin shell wrappers (collect_*) call glab; pure parse_* functions map raw
JSON onto model types and are unit-tested without any network. Mirrors
shiplog/collectors.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from typing import List

from .models import Issue, MergeRequest, Todo

PROJECT_PREFIX = "performancelivestock"


def _run_glab(args: List[str], timeout: int = 30) -> str:
    """Run a glab command (read-only GET), returning stdout. Raises a clean
    RuntimeError on timeout, a missing glab binary, or a non-zero exit."""
    try:
        result = subprocess.run(
            ["glab", *args], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"glab {' '.join(args)} timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("glab not found on PATH — install GitLab CLI (`glab`)")
    if result.returncode != 0:
        raise RuntimeError(f"glab {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _ci_status(item: dict) -> str:
    pipe = item.get("head_pipeline") or {}
    return pipe.get("status") or "unknown"


def _loads_list(raw_json: str, where: str) -> list:
    """json.loads + guard: a decode error or a non-list payload yields []."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"worksweep: {where} decode failed: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"worksweep: {where} expected a list, got {type(data).__name__}",
              file=sys.stderr)
        return []
    return data


def parse_mrs(raw_json: str, repo: str) -> List[MergeRequest]:
    out: List[MergeRequest] = []
    for it in _loads_list(raw_json, "parse_mrs"):
        try:
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
        except (ValueError, TypeError, AttributeError) as e:
            print(f"worksweep: parse_mrs skipping bad row: {e}", file=sys.stderr)
    return out


def parse_todos(raw_json: str) -> List[Todo]:
    out: List[Todo] = []
    for it in _loads_list(raw_json, "parse_todos"):
        try:
            out.append(Todo(
                target=it.get("target_type", ""),
                action=it.get("action_name", ""),
                web_url=it.get("target_url", ""),
            ))
        except (ValueError, TypeError, AttributeError) as e:
            print(f"worksweep: parse_todos skipping bad row: {e}", file=sys.stderr)
    return out


def parse_issues(raw_json: str, repo: str) -> List[Issue]:
    out: List[Issue] = []
    for it in _loads_list(raw_json, "parse_issues"):
        try:
            out.append(Issue(repo=repo, iid=int(it.get("iid", 0)),
                             title=it.get("title", ""), web_url=it.get("web_url", "")))
        except (ValueError, TypeError, AttributeError) as e:
            print(f"worksweep: parse_issues skipping bad row: {e}", file=sys.stderr)
    return out


def _project(repo: str) -> str:
    # URL-encode the project path for glab api: performancelivestock%2Fpb-www
    return urllib.parse.quote(f"{PROJECT_PREFIX}/{repo}", safe="")


def collect_my_mrs(repo: str, username: str) -> List[MergeRequest]:
    user = urllib.parse.quote(username, safe="")
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/merge_requests?state=opened&author_username={user}&with_merge_status_recheck=true&per_page=100"])
    return parse_mrs(raw, repo)


def collect_review_requests(repo: str, username: str) -> List[MergeRequest]:
    user = urllib.parse.quote(username, safe="")
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/merge_requests?state=opened&reviewer_username={user}&per_page=100"])
    return parse_mrs(raw, repo)


def collect_todos() -> List[Todo]:
    return parse_todos(_run_glab(["api", "todos?state=pending&per_page=100"]))


def collect_issues(repo: str, username: str) -> List[Issue]:
    user = urllib.parse.quote(username, safe="")
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/issues?state=opened&assignee_username={user}&per_page=100"])
    return parse_issues(raw, repo)
