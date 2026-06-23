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


@dataclass(frozen=True)
class WorkItem:
    schema_version: int
    id: str
    repo: str
    kind: str       # "mr" | "review_request" | "todo" | "issue"
    executor: str   # "magi-review" | "mr-hygiene" | "review" | "triage"
    risk: str       # "low" | "medium" | "high"
    why: str
    web_url: str
    sha: str
    status: str = "proposed"
