"""Data types for Worksweep (the GitLab sensor slice)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# A dev-server link in an MR description, per Chandler's MR convention
# ("Available on" / a *-dev*.performancebeef.com URL).
_DEV_URL_RE = re.compile(
    r"https?://[^\s)]*[-.]dev\d*[^\s)]*\.performancebeef\.com", re.I)


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
    my_review_state: str = ""       # GitLab reviewState enum for cfg.username, "" = unknown
    changes_requested: bool = False # any reviewer state REQUESTED_CHANGES on my MR
    unresolved_count: int = 0       # resolvable - resolved discussions on my MR
    approved: bool = False          # GraphQL `approved` -- overall approval satisfied
    merge_status: str = ""          # upper-cased detailedMergeStatus, e.g. "MERGEABLE"
    assignees: tuple = ()           # tuple[str, ...] usernames
    source_branch: str = ""         # GraphQL `sourceBranch` -- feeds devslots.classify

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


# The executors the runner will actually claim (runner.pick_claim is gated to
# exactly these: runner.py:353 `(_MAGI, _KEEP_CURRENT)` and runner.py:441
# `(_IMPLEMENT,)`). `triage`, `mr-hygiene` and `none` items are FYI rows a human
# acts on by hand -- nothing in worksweep ever executes them.
#
# This matters because there is no un-approve path: flipping a non-runnable item
# to `approved` strands it forever (reconcile preserves `approved`, no runner
# claims it, and only a hand-edit of queue.json gets it back). So the BLANKET
# approval paths -- Discord `✅ all` and the dashboard's "Approve all" -- gate on
# this set. A numbered `✅ N` deliberately does not: naming an item is an
# explicit human choice.
#
# Lives here because models.py is the one module with no worksweep imports, so
# approvals.py and dashboard.py can both reach it without a cycle.
# test_runnable_executors_matches_the_runner_claim_gate pins it to the runner.
RUNNABLE_EXECUTORS = ("magi-review", "keep-current", "implement")


@dataclass(frozen=True)
class WorkItem:
    schema_version: int
    id: str
    repo: str
    kind: str       # "mr" | "review_request" | "feedback" | "ci_red" | "todo" | "issue"
    executor: str   # "magi-review" | "mr-hygiene" | "triage" | "implement"
    risk: str       # "low" | "medium" | "high"
    why: str
    web_url: str
    sha: str
    # "proposed" | "approved" | "running" | "done" | "error" | "needs-input".
    # `needs-input` (M4 Task G) = the implementer halted with a question for
    # the human: terminal-ish (never auto-retried) until a fresh Discord ✅
    # flips it back to `approved` (see approvals.apply_approvals).
    status: str = "proposed"
    claimed_at: str = ""      # ISO8601 — set when the runner claims (status=running)
    done_reason: str = ""     # "executor-completed" | "already-reviewed" | "mr-merged" | "bootstrap-glob"
    result_sha: str = ""      # head SHA the executor actually reviewed
    report_path: str = ""     # tribunal report path written by the executor
    error_summary: str = ""   # short failure text (status=error)
    title: str = ""           # mr.title / issue.title -- "" for todo items
    dev_box: str = ""         # name of the dev box claimed by an `implement` executor
    mr_iid: int = 0           # Draft MR iid opened by the `implement` executor
    branch: str = ""          # M4 Task H: mr.source_branch, set by assess_stale --
                              # the `keep-current` executor's checkout target


@dataclass(frozen=True)
class QueueRecord:
    """A WorkItem with its stable digest number + sweep-tracking timestamps.

    `number` is the approval handle the formatter renders and the user replies
    to (`✅ 3`). It is assigned once at first sight and preserved across sweeps
    by the queue (see queue.reconcile) so the contract holds.
    """
    number: int
    item: WorkItem
    first_seen: str  # ISO8601 — when this id first entered the queue
    last_seen: str   # ISO8601 — last sweep that still saw this id


@dataclass(frozen=True)
class DiscordMessage:
    id: str          # snowflake (used as the `after` cursor)
    author_id: str
    content: str
    timestamp: str   # ISO8601
