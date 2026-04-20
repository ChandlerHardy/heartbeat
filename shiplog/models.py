"""Data types for ShipLog."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class MergedPR:
    number: int
    title: str
    merged_at: datetime
    author: str
    url: str
    body: str = ""
    labels: tuple = ()  # tuple[str, ...]

    @property
    def short_title(self) -> str:
        t = self.title.strip()
        return t[:80] + ("…" if len(t) > 80 else "")


@dataclass(frozen=True)
class RepoSnapshot:
    """A single repo's activity over a window."""
    name: str
    repo: str  # "owner/name"
    merged_prs: tuple  # tuple[MergedPR, ...]
    commit_count: int = 0
    open_pr_count: int = 0
    open_issue_count: int = 0
    releases: tuple = ()  # tuple[str, ...] — release tag names

    @property
    def merged_count(self) -> int:
        return len(self.merged_prs)


@dataclass
class ShipLogReport:
    window_start: datetime
    window_end: datetime
    snapshots: List[RepoSnapshot] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def total_merged(self) -> int:
        return sum(s.merged_count for s in self.snapshots)

    @property
    def total_commits(self) -> int:
        return sum(s.commit_count for s in self.snapshots)

    @property
    def active_repos(self) -> List[RepoSnapshot]:
        return [s for s in self.snapshots if s.merged_count > 0 or s.commit_count > 0]
