"""Tests for ShipLog collectors — focus on pure parsing (no gh calls)."""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shiplog.collectors import _parse_iso, parse_merged_prs  # noqa: E402


def test_parse_iso_z_suffix():
    dt = _parse_iso("2026-04-12T10:30:00Z")
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.day == 12
    assert dt.tzinfo is not None


def test_parse_iso_with_offset():
    dt = _parse_iso("2026-04-12T10:30:00+00:00")
    assert dt.tzinfo is not None


def test_parse_merged_prs_empty():
    assert parse_merged_prs("[]") == []


def test_parse_merged_prs_basic():
    raw = json.dumps([
        {
            "number": 42,
            "title": "feat: add thing",
            "mergedAt": "2026-04-10T12:00:00Z",
            "author": {"login": "chandler"},
            "url": "https://github.com/x/y/pull/42",
            "body": "does things",
            "labels": [{"name": "heartbeat"}, {"name": "feature"}],
        }
    ])
    prs = parse_merged_prs(raw)
    assert len(prs) == 1
    pr = prs[0]
    assert pr.number == 42
    assert pr.title == "feat: add thing"
    assert pr.author == "chandler"
    assert pr.merged_at == datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    assert "heartbeat" in pr.labels
    assert "feature" in pr.labels


def test_parse_merged_prs_missing_merged_at_is_skipped():
    raw = json.dumps([
        {"number": 1, "title": "pending", "mergedAt": None, "author": {"login": "x"}, "url": ""}
    ])
    assert parse_merged_prs(raw) == []


def test_parse_merged_prs_missing_author_defaults_unknown():
    raw = json.dumps([
        {
            "number": 1,
            "title": "t",
            "mergedAt": "2026-04-10T12:00:00Z",
            "author": None,
            "url": "",
        }
    ])
    prs = parse_merged_prs(raw)
    assert prs[0].author == "unknown"


def test_short_title_truncates_long():
    from shiplog.models import MergedPR
    long_title = "feat: " + "x" * 200
    pr = MergedPR(
        number=1, title=long_title, merged_at=datetime.now(timezone.utc),
        author="x", url="",
    )
    short = pr.short_title
    assert len(short) <= 81
    assert short.endswith("…")
