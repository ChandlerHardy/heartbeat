"""Deterministic M1 rules: signals -> WorkItems. No LLM, pure + testable."""
from __future__ import annotations

import os
import re
from glob import glob
from typing import Callable, FrozenSet, List, Set, Tuple

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
        risk="low", why=why, web_url=mr.web_url, sha=mr.sha, title=mr.title)]


def resolutions(review_mrs: List[MergeRequest], username: str,
               authored: List[MergeRequest] = ()) -> dict:
    """ids the sensor says are settled: review items where my state is a
    waiting-on-author state, plus a `feedback:{repo}!{iid}` resolution for
    any authored MR that has become handed-off (see is_handed_off) -- a
    mid-flight `approved` feedback item would otherwise linger forever
    (reconcile's _RETAIN_IF_GONE keeps approved records as-is once the fresh
    sweep no longer proposes them). `authored` defaults to () for backward
    compat with callers assessing only the review-requested bucket.
    reconcile() flips matching queue records done."""
    out = {}
    for mr in review_mrs:
        if username in mr.reviewers and mr.my_review_state not in REVIEW_ACTIONABLE_STATES:
            out[f"review:{mr.repo}!{mr.iid}"] = "already-reviewed"
    for mr in authored:
        if is_handed_off(mr, username):
            out[f"feedback:{mr.repo}!{mr.iid}"] = "handed-off"
    return out


def is_handed_off(mr: MergeRequest, username: str) -> bool:
    """True when an authored MR is ready-to-merge and no longer Chandler's
    work: overall approval satisfied, GitLab says it's mergeable, and it's
    assigned to someone other than the author (the maintainer who will
    click merge). `mr.approved` alone (LGTM'd but not yet mergeable) is NOT
    a handoff -- see assess_own_mr, which still suppresses just the
    magi-review item in that narrower case."""
    return (mr.approved and mr.merge_status == "MERGEABLE"
           and any(a != username for a in mr.assignees))


def assess_own_mr(mr: MergeRequest, username: str,
                  has_magi: Callable[[str, int, str], bool]) -> List[WorkItem]:
    if mr.author != username:
        return []
    if is_handed_off(mr, username):
        # Ready to merge, handed to a maintainer -- not Chandler's work
        # anymore. No feedback/magi/hygiene noise, just one informational
        # item so the digest still shows where the MR landed.
        others = ", ".join(a for a in mr.assignees if a != username)
        return [WorkItem(
            schema_version=1, id=f"handoff:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="handoff", executor="none", risk="low",
            why=f"ready to merge → assigned to {others}",
            web_url=mr.web_url, sha=mr.sha, title=mr.title)]
    items: List[WorkItem] = []
    # `mr.approved` (LGTM'd, even if not yet mergeable) means magi review has
    # done its job -- magi is pre-review, not post-approval -- so suppress
    # just the magi item, independent of has_magi's queue-history check.
    if not mr.approved and not has_magi(mr.repo, mr.iid, mr.sha):
        items.append(WorkItem(
            schema_version=1, id=f"magi:{mr.repo}!{mr.iid}@{mr.sha}",
            repo=mr.repo, kind="mr", executor="magi-review", risk="low",
            why="no magi-review yet", web_url=mr.web_url, sha=mr.sha, title=mr.title))
    # Drafts often don't have a dev link yet (the environment isn't assigned/
    # ready until the MR leaves draft) -- exempt them from the hygiene nag.
    if not mr.is_draft and not mr.dev_url_present:
        # `park`, not the old inert `mr-hygiene`: the runner can actually fix
        # this one (put the branch on a free box and write the link back), so
        # it is real approvable work rather than a nag that sat on the
        # dashboard forever. `branch` is required by the executor and comes
        # from the MR itself, exactly as assess_stale does it.
        items.append(WorkItem(
            schema_version=1, id=f"hygiene-devurl:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="mr", executor="park", risk="low",
            why="description missing dev-server link", web_url=mr.web_url,
            sha=mr.sha, title=mr.title, branch=mr.source_branch))
    if mr.changes_requested or mr.unresolved_count > 0:
        n = mr.unresolved_count
        why = "changes requested" if mr.changes_requested else ""
        if n:
            why = (why + ", " if why else "") + f"{n} unresolved thread{'s' if n != 1 else ''}"
        items.append(WorkItem(
            schema_version=1, id=f"feedback:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="feedback", executor="triage", risk="low",
            why=why, web_url=mr.web_url, sha=mr.sha, title=mr.title))
    if mr.ci_status == "failed":
        items.append(WorkItem(
            schema_version=1, id=f"ci:{mr.repo}!{mr.iid}",
            repo=mr.repo, kind="ci_red", executor="triage", risk="low",
            why="head pipeline failed", web_url=mr.web_url, sha=mr.sha, title=mr.title))
    return items


def assess_assigned_mr(mr: MergeRequest, username: str,
                       tracked: Set[Tuple[str, int]]) -> List[WorkItem]:
    """An MR assigned to me via GitLab's `assignedMergeRequests` bucket.

    Self-assigned MRs (author == username) are already fully covered by
    assess_own_mr's magi/hygiene/feedback/ci items, and an MR already seen in
    another bucket this sweep (review-requested or authored -- passed in via
    `tracked` as (repo, iid) pairs) is already represented -- so both cases
    emit nothing here to avoid a redundant "assigned to you" line.
    """
    if mr.author == username:
        return []
    if (mr.repo, mr.iid) in tracked:
        return []
    return [WorkItem(
        schema_version=1, id=f"assigned:{mr.repo}!{mr.iid}",
        repo=mr.repo, kind="assigned_mr", executor="triage", risk="low",
        why="assigned to you", web_url=mr.web_url, sha=mr.sha, title=mr.title)]


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
        web_url=todo.web_url, sha="", todo_id=todo.id)]


# Two narrow patterns instead of a blanket "#\d+ anywhere in the title" scan
# -- a bare findall over the whole title over-suppresses (a title like
# "feat(#1701): follow-up to #796 review" would wrongly cover unrelated
# issue #796 too). Only two shapes count as a real "this MR covers that
# issue" claim:
#  1. the leading conventional-commit tag: `feat(#1701):`, `refactor(#1681):`,
#     optionally prefixed with GitLab's "Draft: " marker.
#  2. an explicit closing keyword anywhere in the title: "Closes #42",
#     "Fixes #42", "Resolves #42".
# A source-branch fallback (`/(\d{3,5})-`) is deferred -- the GraphQL sweep
# node doesn't fetch the source branch name today -- so v1 is title-only,
# per the M3.5 plan.
_ISSUE_TAG_RE = re.compile(r"^\s*(?:Draft:\s*)?[a-z]+\(#(\d+)\)")
_ISSUE_CLOSE_RE = re.compile(r"(?:[Cc]loses|[Ff]ixes|[Rr]esolves)\s+#(\d+)")


def covered_issue_iids(authored: List[MergeRequest]) -> Set[int]:
    """Issue iids already covered by an open authored MR's title, so
    assess_issue can suppress the redundant separate issue item. Only the
    leading conventional-commit tag and explicit closing keywords count --
    an incidental "#NNN" reference elsewhere in the title does not."""
    covered: Set[int] = set()
    for mr in authored:
        tag = _ISSUE_TAG_RE.match(mr.title)
        if tag:
            covered.add(int(tag.group(1)))
        covered.update(int(n) for n in _ISSUE_CLOSE_RE.findall(mr.title))
    return covered


def assess_issue(issue: Issue,
                 covered: FrozenSet[int] = frozenset()) -> List[WorkItem]:
    """An assigned issue -- suppressed when an open authored MR's title
    already references it (covered_issue_iids), since the MR item is the
    actionable one and a separate issue item would just be a duplicate."""
    if issue.iid in covered:
        return []
    return [WorkItem(
        schema_version=1, id=f"issue:{issue.repo}#{issue.iid}", repo=issue.repo,
        kind="issue", executor="implement", risk="low",
        why=f"assigned issue: {issue.title}", web_url=issue.web_url, sha="",
        title=issue.title)]


def assess_stale(mr: MergeRequest, diverged: int, threshold: int) -> List[WorkItem]:
    """An authored MR whose branch has fallen `threshold`+ commits behind
    master (REST `diverged_commits_count`, not carried on the GraphQL node)
    -- the `keep-current` executor merges master in and syncs the result to
    whichever dev box serves the branch. Handed-off MRs are the CALLER's job
    to exempt (run_sweep skips the REST call entirely for those -- the
    maintainer will merge, not Chandler), not this function's."""
    if diverged < threshold:
        return []
    return [WorkItem(
        schema_version=1, id=f"stale:{mr.repo}!{mr.iid}",
        repo=mr.repo, kind="stale", executor="keep-current", risk="low",
        why=f"{diverged} commits behind master", web_url=mr.web_url,
        sha=mr.sha, title=mr.title, branch=mr.source_branch)]


def _normalize_todo_url(url: str) -> str:
    """Strip a Discord/GitLab note-anchor fragment (#note_...) and any
    trailing slash so URL-based matching ignores anchor/trailing-slash
    noise between a todo's target_url and an item/MR's web_url."""
    return (url or "").split("#", 1)[0].rstrip("/")


def filter_todos(todos: List[Todo], items: List[WorkItem],
                 tracked_mrs: List[MergeRequest]) -> List[Todo]:
    """Hard filter: GitLab todos are noisy compared to the GraphQL sweep's
    authoritative buckets (review-requested/authored/assigned). Drop a todo
    when:
      - its action is `review_requested` or `assigned` (both buckets already
        cover these unconditionally), or
      - its normalized web_url matches a non-todo item emitted this sweep, or
        any MR in the review/authored/assigned buckets (already tracked).
    Survivors are genuine mentions/direct-addresses on things not otherwise
    tracked."""
    tracked_urls = {_normalize_todo_url(it.web_url) for it in items
                    if it.kind != "todo" and it.web_url}
    tracked_urls |= {_normalize_todo_url(mr.web_url) for mr in tracked_mrs
                     if mr.web_url}
    out = []
    for td in todos:
        if td.action in ("review_requested", "assigned"):
            continue
        if _normalize_todo_url(td.web_url) in tracked_urls:
            continue
        out.append(td)
    return out


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
