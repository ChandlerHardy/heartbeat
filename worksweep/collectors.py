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
                id=int(it.get("id", 0) or 0),
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


def collect_diverged_commits_count(repo: str, iid: int) -> int:
    """M4 Task H: `divergedCommitsCount` isn't in the GraphQL MR node, so
    keep-current sensing falls back to one REST call per authored MR (that
    isn't already handed off). Raises via `_run_glab` on failure — the
    caller (run_sweep) wraps this per-MR so one bad call degrades that one
    MR's stale check to "unknown" rather than losing the whole sweep."""
    raw = _run_glab(["api",
        f"projects/{_project(repo)}/merge_requests/{iid}"
        f"?include_diverged_commits_count=true"])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"diverged-commits response for {repo}!{iid} "
                           f"decode failed: {e}")
    if not isinstance(data, dict):
        raise RuntimeError(f"diverged-commits response for {repo}!{iid} "
                           f"was not an object")
    return int(data.get("diverged_commits_count") or 0)


# --- GraphQL sweep (M3): one query mirroring the "Your work / MRs" dashboard ---

_GRAPHQL_SWEEP_QUERY = """
query {
  currentUser {
    username
    reviewRequestedMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt sourceBranch
        project { fullPath }
        author { username }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        headPipeline { status }
      }
    }
    authoredMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt description sourceBranch
        approved detailedMergeStatus
        assignees { nodes { username } }
        project { fullPath }
        author { username }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        headPipeline { status }
        resolvableDiscussionsCount resolvedDiscussionsCount
      }
    }
    assignedMergeRequests(state: opened, first: 100) {
      nodes {
        iid title draft webUrl diffHeadSha updatedAt description sourceBranch
        approved detailedMergeStatus
        assignees { nodes { username } }
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
    assignees = tuple((a or {}).get("username", "")
                      for a in ((node.get("assignees") or {}).get("nodes") or []))
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
        approved=bool(node.get("approved", False)),
        merge_status=str(node.get("detailedMergeStatus") or "").upper(),
        assignees=assignees,
        source_branch=node.get("sourceBranch") or "",
    )


def parse_graphql_sweep(raw: str, username: str, repos: tuple):
    """Pure: raw GraphQL JSON -> (review_requested, authored, assigned)
    MergeRequest lists, filtered to the configured performancelivestock
    repos. Malformed -> ([], [], [])."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"worksweep: graphql decode failed: {e}", file=sys.stderr)
        return [], [], []
    if not isinstance(data, dict):
        print(f"worksweep: graphql expected an object, got {type(data).__name__}",
              file=sys.stderr)
        return [], [], []
    data = data.get("data", data) or {}
    if not isinstance(data, dict):
        print(f"worksweep: graphql expected an object, got {type(data).__name__}",
              file=sys.stderr)
        return [], [], []
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
            _bucket("authoredMergeRequests"),
            _bucket("assignedMergeRequests"))
