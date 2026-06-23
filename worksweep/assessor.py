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
