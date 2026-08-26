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
from typing import Callable, List

from .models import Issue, MergeRequest, ReviewNote, ReviewThread, Todo

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


# --- discussions probe: which threads are still waiting on Chandler? --------
#
# The GraphQL sweep only carries resolvable/resolved COUNTS, so it cannot tell
# "the reviewer asked something and is waiting" from "I answered and the
# reviewer hasn't resolved it yet". Those two feel identical in a digest and
# only one is work. Decision 1 (2026-08-25) makes the second signal a targeted
# per-MR REST call, run only for authored MRs that have unresolved threads at
# all -- typically two or three across the whole queue.


# GitLab's page size for the discussions endpoint, and the number of pages
# the probe will walk before giving up. 20 x 100 = 2000 discussions, which is
# far past any real MR; the cap exists so a server answering full pages
# forever cannot spin the sweep.
_PER_PAGE = 100
_MAX_PAGES = 20


def discussions_path(repo: str, iid: int, page: int = 1) -> str:
    return (f"projects/{_project(repo)}/merge_requests/{int(iid)}"
            f"/discussions?per_page={_PER_PAGE}&page={int(page)}")


def discussions_pages(fetch: Callable[[int], str]) -> tuple:
    """Walk `fetch(page)` until a short page arrives; the raw page bodies.

    Paging is not optional here. GitLab returns every SYSTEM note ("added 1
    commit", "changed this line in version 3") as its own discussion, so a
    busy MR passes 100 discussions on housekeeping alone -- and the reviewer's
    actual question, being older, sorts onto a later page. Reading only page 1
    would fail closed AND silent: no unaddressed threads found, no error, the
    work simply never proposed.
    """
    pages = []
    for page in range(1, _MAX_PAGES + 1):
        raw = fetch(page)
        pages.append(raw)
        if len(_loads_list(raw, "discussions")) < _PER_PAGE:
            return tuple(pages)
    print(f"worksweep: discussions paging hit the {_MAX_PAGES}-page cap — "
          f"reading the first {_MAX_PAGES * _PER_PAGE} discussions only",
          file=sys.stderr)
    return tuple(pages)


def _pages(raw) -> list:
    """One page body or several -- callers hand us either."""
    return [raw] if isinstance(raw, (str, bytes)) else list(raw or [])


def parse_threads(raw_json) -> tuple:
    """Every discussion on an MR as a ReviewThread, across every page given.
    Malformed payload -> ().

    A thread's resolvable/resolved state lives on its notes, not on the
    discussion object: it is `resolvable` when ANY note is, and `resolved`
    only when EVERY resolvable note is (GitLab resolves a whole thread at
    once, so in practice they agree -- `all` is the conservative reading,
    since a half-resolved thread is not finished).
    """
    out = []
    for d in [d for page in _pages(raw_json)
              for d in _loads_list(page, "parse_threads")]:
        if not isinstance(d, dict):
            continue
        notes = [n for n in (d.get("notes") or []) if isinstance(n, dict)]
        resolvable_notes = [n for n in notes if n.get("resolvable")]
        human = [n for n in notes if not n.get("system")]
        last = human[-1] if human else None
        resolved_by = ""
        for n in notes:
            who = ((n.get("resolved_by") or {}) or {}).get("username", "")
            if who:
                resolved_by = who
        out.append(ReviewThread(
            id=str(d.get("id") or ""),
            resolvable=bool(resolvable_notes),
            resolved=bool(resolvable_notes)
                     and all(bool(n.get("resolved")) for n in resolvable_notes),
            last_author=((last or {}).get("author") or {}).get("username", ""),
            last_note=(last or {}).get("body") or "",
            resolved_by=resolved_by,
            notes=tuple(ReviewNote(
                author=((n.get("author") or {}) or {}).get("username", ""),
                system=bool(n.get("system")),
                body=n.get("body") or "",
                created_at=str(n.get("created_at") or "")) for n in notes),
        ))
    return tuple(out)


def unaddressed_threads(raw_json, username: str) -> tuple:
    """The threads on this MR that are waiting on `username`.

    Unaddressed iff resolvable, NOT resolved, and the last non-system note is
    somebody else's. The three exclusions each drop a thread that is genuinely
    not Chandler's move:

    * not resolvable -- an ordinary comment thread, nothing to answer;
    * resolved -- the owner already closed it;
    * my own reply is the last word -- the ball is in the reviewer's court,
      and this is the whole class the old `unresolved_count` signal nagged
      about forever.

    A thread with no human note at all (`last_author == ""`, e.g. nothing but
    GitLab's own "added 1 commit") is nobody's question and is excluded too.
    """
    return tuple(t for t in parse_threads(raw_json)
                 if t.resolvable and not t.resolved
                 and t.last_author and t.last_author != username)


def parse_unaddressed_count(raw_json, username: str) -> int:
    return len(unaddressed_threads(raw_json, username))


def collect_discussions(repo: str, iid: int) -> tuple:
    """Shell edge: every page of an MR's discussions, returned RAW.

    Raw rather than parsed because the two callers want different slices of
    the same payload -- the sweep wants a count, the `address-feedback`
    executor wants the threads themselves -- and both go through the pure
    parse_* functions above, which are the only things tests need to exercise.
    Raises via `_run_glab`; the sweep wraps this per-MR so one bad call
    degrades that one MR to zero rather than losing the whole sweep.
    """
    return discussions_pages(
        lambda page: _run_glab(["api", discussions_path(repo, iid, page)]))

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
