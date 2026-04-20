"""Tests for ShipLog formatter."""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shiplog.formatter import format_discord, format_markdown  # noqa: E402
from shiplog.models import MergedPR, RepoSnapshot, ShipLogReport  # noqa: E402


def pr(title, number=1):
    return MergedPR(
        number=number,
        title=title,
        merged_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        author="chandler",
        url=f"https://github.com/x/y/pull/{number}",
    )


def make_report():
    return ShipLogReport(
        window_start=datetime(2026, 4, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 12, tzinfo=timezone.utc),
        snapshots=[
            RepoSnapshot(
                name="crooked-finger",
                repo="ChandlerHardy/crooked-finger",
                merged_prs=(pr("feat: add analytics", 1), pr("fix: chart crash", 2)),
                commit_count=15,
                open_pr_count=1,
                open_issue_count=3,
            ),
            RepoSnapshot(
                name="gnomestead-web",
                repo="ChandlerHardy/gnomestead-web",
                merged_prs=(pr("docs: update readme", 5),),
                commit_count=4,
                open_pr_count=0,
                open_issue_count=2,
            ),
        ],
    )


def test_markdown_contains_header_and_totals():
    report = make_report()
    md = format_markdown(report, ascii_only=True)
    assert "ShipLog" in md
    assert "2026-04-05" in md
    assert "2026-04-12" in md
    assert "3 merged" in md  # 2 + 1
    assert "19 commits" in md  # 15 + 4


def test_markdown_sections_per_active_repo():
    report = make_report()
    md = format_markdown(report, ascii_only=True)
    assert "crooked-finger" in md
    assert "gnomestead-web" in md
    assert "Features" in md
    assert "Fixes" in md
    assert "Docs" in md


def test_markdown_empty_report():
    report = ShipLogReport(
        window_start=datetime(2026, 4, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 12, tzinfo=timezone.utc),
    )
    md = format_markdown(report)
    assert "No activity" in md


def test_markdown_includes_errors():
    report = make_report()
    report.errors.append("crooked-finger: gh rate-limited")
    md = format_markdown(report)
    assert "Collection errors" in md
    assert "rate-limited" in md


def test_discord_message_compact():
    report = make_report()
    out = format_discord(report)
    assert "ShipLog" in out
    assert "crooked-finger" in out
    assert len(out) <= 1900


def test_discord_truncates_long_output():
    snapshot = RepoSnapshot(
        name="big",
        repo="owner/big",
        merged_prs=tuple(pr(f"feat: thing {i}", i) for i in range(50)),
    )
    report = ShipLogReport(
        window_start=datetime(2026, 4, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 12, tzinfo=timezone.utc),
        snapshots=[snapshot],
    )
    out = format_discord(report, max_chars=500)
    assert len(out) <= 500


def test_discord_shows_top_3_prs_with_overflow_count():
    snapshot = RepoSnapshot(
        name="x",
        repo="owner/x",
        merged_prs=tuple(pr(f"feat: thing {i}", i) for i in range(6)),
    )
    report = ShipLogReport(
        window_start=datetime(2026, 4, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 12, tzinfo=timezone.utc),
        snapshots=[snapshot],
    )
    out = format_discord(report)
    assert "+3 more" in out
