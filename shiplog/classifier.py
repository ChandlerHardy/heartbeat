"""Classify merged PRs into conventional-commit-style categories."""

from __future__ import annotations

import re
from typing import Dict, List

from .models import MergedPR


# Category → ordered list of (regex, priority) patterns. First match wins.
CATEGORY_PATTERNS: Dict[str, List[re.Pattern]] = {
    "feat": [
        re.compile(r"^feat(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^feature(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^add\b", re.IGNORECASE),
        re.compile(r"^implement\b", re.IGNORECASE),
        re.compile(r"^introduce\b", re.IGNORECASE),
    ],
    "fix": [
        re.compile(r"^fix(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^hotfix(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^bugfix(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^patch\b", re.IGNORECASE),
        re.compile(r"^resolve\b", re.IGNORECASE),
        re.compile(r"^fix(es)?\s", re.IGNORECASE),
    ],
    "refactor": [
        re.compile(r"^refactor(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^refac(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^rework\b", re.IGNORECASE),
        re.compile(r"^restructure\b", re.IGNORECASE),
        re.compile(r"^extract\b", re.IGNORECASE),
    ],
    "docs": [
        re.compile(r"^docs?(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^doc\b", re.IGNORECASE),
        re.compile(r"^readme\b", re.IGNORECASE),
    ],
    "test": [
        re.compile(r"^test(s)?(\([^)]*\))?[!:]", re.IGNORECASE),
    ],
    "perf": [
        re.compile(r"^perf(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^optimize\b", re.IGNORECASE),
    ],
    "chore": [
        re.compile(r"^chore(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^build(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^ci(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^deps?(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^bump\b", re.IGNORECASE),
        re.compile(r"^upgrade\b", re.IGNORECASE),
    ],
    "style": [
        re.compile(r"^style(\([^)]*\))?[!:]", re.IGNORECASE),
        re.compile(r"^format\b", re.IGNORECASE),
        re.compile(r"^lint\b", re.IGNORECASE),
    ],
    "revert": [
        re.compile(r"^revert\b", re.IGNORECASE),
    ],
}


CATEGORY_ORDER = ("feat", "fix", "refactor", "perf", "docs", "test", "chore", "style", "revert", "other")

# Matches "label: " style prefixes (e.g. "heartbeat: ...") so we can strip them
# and re-classify on the real intent. Only strips if the label is not already a
# known conventional type.
_LABEL_PREFIX_RE = re.compile(r"^([A-Za-z][\w-]*)\s*:\s+")

_CONVENTIONAL_TYPES = set(CATEGORY_PATTERNS.keys())


def _match_patterns(t: str) -> str:
    for category, patterns in CATEGORY_PATTERNS.items():
        for pattern in patterns:
            if pattern.match(t):
                return category
    return "other"


def classify_title(title: str) -> str:
    """Return the category bucket name for a PR title.

    Falls back to stripping non-conventional "label: " prefixes (e.g.
    "heartbeat: Implement retry") and re-classifying on the remainder so
    that custom label schemes still get good categorization.
    """
    t = title.strip()
    category = _match_patterns(t)
    if category != "other":
        return category

    m = _LABEL_PREFIX_RE.match(t)
    if m and m.group(1).lower() not in _CONVENTIONAL_TYPES:
        stripped = t[m.end():]
        return _match_patterns(stripped)
    return "other"


def group_by_category(prs: List[MergedPR]) -> Dict[str, List[MergedPR]]:
    """Group PRs by category, preserving insertion order per category.

    Returned dict has keys in CATEGORY_ORDER (ones with content only).
    """
    buckets: Dict[str, List[MergedPR]] = {c: [] for c in CATEGORY_ORDER}
    for pr in prs:
        category = classify_title(pr.title)
        buckets.setdefault(category, []).append(pr)
    # Drop empty categories.
    return {c: prs for c, prs in buckets.items() if prs}
