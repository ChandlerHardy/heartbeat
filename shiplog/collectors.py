"""Collect repo activity via the GitHub CLI (`gh`).

Kept as thin shell wrappers so the pure classifier/formatter modules
can be tested without any network. Each function is a single subprocess
call with JSON output, parsed and mapped onto our model types.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import List, Sequence, Tuple

from .models import MergedPR, RepoSnapshot


def _run_gh(args: List[str], timeout: int = 30) -> str:
    """Run a gh CLI command, returning stdout as text. Raises on failure."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _parse_iso(value: str) -> datetime:
    """Parse an RFC3339/ISO 8601 timestamp from GitHub. Always returns UTC.

    Raises ValueError on malformed input. Callers must catch and skip the
    record — the previous behavior (substituting `datetime.now(timezone.utc)`)
    silently stamped broken records with the current wall time, so a stale
    release kept reappearing in every digest as "published now" and a PR
    with a corrupt mergedAt passed the window filter indefinitely.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def parse_merged_prs(raw_json: str) -> List[MergedPR]:
    """Parse `gh pr list --state merged --json ...` output.

    Returns an empty list and logs to stderr on malformed JSON. Without an
    explicit catch the bare-except in collect_snapshot swallowed every
    parse error and the report silently showed zero merged PRs.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"shiplog: parse_merged_prs JSON decode failed: {e}", file=sys.stderr)
        return []
    out: List[MergedPR] = []
    for item in data:
        merged_at = item.get("mergedAt")
        if not merged_at:
            continue
        try:
            merged_at_dt = _parse_iso(merged_at)
        except ValueError as exc:
            number = item.get("number", "?")
            print(
                f"shiplog: skipping PR #{number} with unparseable mergedAt "
                f"{merged_at!r}: {exc}",
                file=sys.stderr,
            )
            continue
        labels = tuple(
            lbl.get("name") if isinstance(lbl, dict) else str(lbl)
            for lbl in item.get("labels", [])
        )
        out.append(MergedPR(
            number=item.get("number", 0),
            title=item.get("title", ""),
            merged_at=merged_at_dt,
            author=(item.get("author") or {}).get("login", "unknown"),
            url=item.get("url", ""),
            body=item.get("body", "") or "",
            labels=labels,
        ))
    return out


def fetch_merged_prs(repo: str, since: datetime, limit: int = 100) -> List[MergedPR]:
    """Fetch merged PRs for a repo since a given UTC datetime."""
    raw = _run_gh([
        "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,mergedAt,author,url,body,labels",
    ])
    prs = parse_merged_prs(raw)
    return [pr for pr in prs if pr.merged_at >= since]


def fetch_open_pr_count(repo: str) -> int:
    raw = _run_gh([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number",
    ])
    try:
        return len(json.loads(raw))
    except json.JSONDecodeError:
        return 0


def fetch_open_issue_count(repo: str) -> int:
    raw = _run_gh([
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number",
    ])
    try:
        return len(json.loads(raw))
    except json.JSONDecodeError:
        return 0


def fetch_commit_count(repo_path: str, since_iso: str) -> int:
    """Count commits on the default branch since a timestamp using git."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "--oneline", f"--since={since_iso}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return 0
        return sum(1 for line in result.stdout.splitlines() if line.strip())
    except (subprocess.TimeoutExpired, OSError):
        return 0


def fetch_releases(repo: str, since: datetime) -> Tuple[str, ...]:
    """Return release tag names created after `since`."""
    try:
        raw = _run_gh([
            "release", "list",
            "--repo", repo,
            "--limit", "20",
            "--json", "tagName,publishedAt",
        ])
    except RuntimeError:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    out = []
    for item in data:
        published = item.get("publishedAt")
        if not published:
            continue
        try:
            published_dt = _parse_iso(published)
        except ValueError as exc:
            tag = item.get("tagName", "?")
            print(
                f"shiplog: skipping release {tag!r} with unparseable publishedAt "
                f"{published!r}: {exc}",
                file=sys.stderr,
            )
            continue
        if published_dt >= since:
            out.append(item.get("tagName", ""))
    return tuple(r for r in out if r)


def collect_snapshot(
    name: str,
    repo: str,
    repo_path: str,
    since: datetime,
) -> RepoSnapshot:
    """Gather a full RepoSnapshot. Catches errors so one bad repo doesn't kill the run.

    Each fetch failure is logged to stderr with the repo name so a silent
    "zero merged PRs" doesn't masquerade as "no activity this week".
    """
    merged: Sequence[MergedPR] = ()
    commit_count = 0
    open_prs = 0
    open_issues = 0
    releases: Tuple[str, ...] = ()

    def _warn(stage: str, exc: Exception) -> None:
        print(f"shiplog: {repo} {stage} failed: {exc}", file=sys.stderr)

    try:
        merged = tuple(fetch_merged_prs(repo, since))
    except Exception as e:  # noqa: BLE001
        _warn("merged_prs", e)
    try:
        commit_count = fetch_commit_count(repo_path, since.isoformat())
    except Exception as e:  # noqa: BLE001
        _warn("commit_count", e)
    try:
        open_prs = fetch_open_pr_count(repo)
    except Exception as e:  # noqa: BLE001
        _warn("open_prs", e)
    try:
        open_issues = fetch_open_issue_count(repo)
    except Exception as e:  # noqa: BLE001
        _warn("open_issues", e)
    try:
        releases = fetch_releases(repo, since)
    except Exception as e:  # noqa: BLE001
        _warn("releases", e)

    return RepoSnapshot(
        name=name,
        repo=repo,
        merged_prs=merged,
        commit_count=commit_count,
        open_pr_count=open_prs,
        open_issue_count=open_issues,
        releases=releases,
    )
