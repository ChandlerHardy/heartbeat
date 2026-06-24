"""Deterministic M1 rules: signals -> WorkItems. No LLM, pure + testable."""
from __future__ import annotations

import os
from glob import glob
from typing import Callable, List

from .models import Issue, MergeRequest, Todo, WorkItem

MAGI_REPORTS_BASE = os.path.expanduser("~/workspaces/pla")


def _report_glob(repo: str, iid: int) -> str:
    """Glob pattern for a repo+iid tribunal report.

    PLA work spans parallel worktree slots (pla-main, pla0..plaN) under
    MAGI_REPORTS_BASE, and a tribunal report for an MR can live under any slot,
    so the `*` matches every slot. Repo-aware (multi-repo).
    """
    return os.path.join(
        MAGI_REPORTS_BASE, "*", repo, ".magi", f"tribunal-report-mr-{iid}-*.md")


def has_magi_report(repo: str, iid: int) -> bool:
    """True if a local magi tribunal report exists for this (repo, iid) MR.

    M1 heuristic: presence of any tribunal report file for the MR iid. (SHA-
    precise dedup arrives with the M2 queue; for M1 a report's existence is a
    good-enough 'already reviewed' signal.) Injectable in tests.
    """
    return bool(glob(_report_glob(repo, iid)))


def assess_mr(mr: MergeRequest, username: str,
              has_magi: Callable[[str, int], bool]) -> List[WorkItem]:
    items: List[WorkItem] = []
    mine = mr.author == username
    if mine:
        if not has_magi(mr.repo, mr.iid):
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
        # The MR LIST endpoint omits head_pipeline, so ci_status is usually
        # "unknown" in M1 — only mention CI when we actually have a known value
        # (avoids implying we fetched pipeline data we did not).
        if mr.ci_status == "unknown":
            why = "review requested"
        else:
            ready = "CI green" if mr.ci_status == "success" else f"CI {mr.ci_status}"
            why = f"review requested ({ready})"
        items.append(WorkItem(
            schema_version=1, id=f"review:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="review_request", executor="review",
            risk="low", why=why,
            web_url=mr.web_url, sha=mr.sha))
    return items


def assess_todo(todo: Todo) -> List[WorkItem]:
    return [WorkItem(
        schema_version=1, id=f"todo:{todo.action}:{todo.web_url}", repo="", kind="todo",
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
