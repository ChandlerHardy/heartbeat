"""Tests for ShipLog classifier."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shiplog.classifier import classify_title, group_by_category  # noqa: E402
from shiplog.models import MergedPR  # noqa: E402


def pr(title, number=1):
    return MergedPR(
        number=number,
        title=title,
        merged_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        author="chandler",
        url=f"https://github.com/x/y/pull/{number}",
    )


def test_feat_classification():
    assert classify_title("feat: add user onboarding") == "feat"
    assert classify_title("feat(api): add cursor paging") == "feat"
    assert classify_title("Add new dashboard page") == "feat"
    assert classify_title("Implement sync worker") == "feat"


def test_fix_classification():
    assert classify_title("fix: handle null user") == "fix"
    assert classify_title("fix(ui): broken button") == "fix"
    assert classify_title("hotfix: crash on startup") == "fix"


def test_refactor_classification():
    assert classify_title("refactor: extract storage layer") == "refactor"
    assert classify_title("Rework auth flow") == "refactor"
    assert classify_title("Extract helpers") == "refactor"


def test_docs_classification():
    assert classify_title("docs: update readme") == "docs"
    assert classify_title("doc: add examples") == "docs"
    assert classify_title("README updates") == "docs"


def test_chore_classification():
    assert classify_title("chore: bump deps") == "chore"
    assert classify_title("deps: update react") == "chore"
    assert classify_title("build: add Makefile target") == "chore"
    assert classify_title("Bump go from 1.22 to 1.23") == "chore"


def test_revert_classification():
    assert classify_title("revert: undo the thing") == "revert"


def test_unknown_falls_to_other():
    assert classify_title("miscellaneous changes") == "other"
    assert classify_title("some random title") == "other"


def test_label_prefix_stripped_and_reclassified():
    assert classify_title("heartbeat: Implement retry with backoff") == "feat"
    assert classify_title("heartbeat: Missing directory causes crash") == "other"
    assert classify_title("scope: Add new dashboard") == "feat"
    assert classify_title("project: fix the bug") == "fix"


def test_label_prefix_does_not_override_conventional():
    assert classify_title("feat: add something") == "feat"
    assert classify_title("fix: handle edge case") == "fix"


def test_group_by_category_order():
    prs = [
        pr("docs: readme", 1),
        pr("feat: new thing", 2),
        pr("fix: bug", 3),
        pr("feat: another", 4),
    ]
    groups = group_by_category(prs)
    keys = list(groups.keys())
    # feat should come before fix and docs per CATEGORY_ORDER
    assert keys.index("feat") < keys.index("fix")
    assert keys.index("fix") < keys.index("docs")
    assert len(groups["feat"]) == 2
    assert len(groups["fix"]) == 1


def test_group_by_category_drops_empty():
    prs = [pr("feat: x", 1)]
    groups = group_by_category(prs)
    assert list(groups.keys()) == ["feat"]
