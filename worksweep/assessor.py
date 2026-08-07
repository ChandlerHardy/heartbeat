"""Deterministic M1 rules: signals -> WorkItems. No LLM, pure + testable."""
from __future__ import annotations

import os
from glob import glob
from typing import Callable, List

from .models import Issue, MergeRequest, QueueRecord, Todo, WorkItem

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


def assess_mr(mr: MergeRequest, username: str,
              has_magi: Callable[[str, int, str], bool]) -> List[WorkItem]:
    """Shim over assess_review_request/assess_own_mr for callers assessing a
    single MergeRequest without caring which bucket it's in. `has_magi` is
    the 3-arg (repo, iid, sha) shape used throughout the queue-backed M3
    assessor (see has_magi_done)."""
    if mr.author == username:
        return assess_own_mr(mr, username, has_magi)
    return assess_review_request(mr, username)


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
