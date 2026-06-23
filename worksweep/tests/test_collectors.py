import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.collectors import parse_mrs, parse_todos, parse_issues  # noqa: E402


def test_parse_mrs_empty():
    assert parse_mrs("[]", "pb-www") == []


def test_parse_mrs_basic():
    raw = json.dumps([{
        "iid": 3920, "title": "fix: x", "author": {"username": "leyang"},
        "web_url": "https://gitlab.com/x/-/merge_requests/3920",
        "description": "no dev link", "sha": "abc123", "draft": False,
        "reviewers": [{"username": "chandler.hardy"}],
        "updated_at": "2026-06-22T10:00:00Z",
        "head_pipeline": {"status": "success"},
    }])
    mrs = parse_mrs(raw, "pb-www")
    assert len(mrs) == 1
    assert mrs[0].iid == 3920
    assert mrs[0].repo == "pb-www"
    assert mrs[0].author == "leyang"
    assert mrs[0].reviewers == ("chandler.hardy",)
    assert mrs[0].ci_status == "success"
    assert mrs[0].is_draft is False


def test_parse_mrs_missing_pipeline_is_unknown():
    raw = json.dumps([{
        "iid": 1, "title": "t", "author": {"username": "me"}, "web_url": "u",
        "description": "", "sha": "s", "draft": True, "reviewers": [],
        "updated_at": "2026-06-22T10:00:00Z",
    }])
    assert parse_mrs(raw, "pb-www")[0].ci_status == "unknown"


def test_parse_mrs_malformed_json_returns_empty():
    assert parse_mrs("not json", "pb-www") == []


def test_parse_todos_basic():
    raw = json.dumps([{
        "target_type": "MergeRequest", "action_name": "review_requested",
        "target_url": "https://gitlab.com/x/-/merge_requests/9",
        "body": "Review requested",
    }])
    todos = parse_todos(raw)
    assert len(todos) == 1
    assert todos[0].action == "review_requested"


def test_parse_issues_basic():
    raw = json.dumps([{"iid": 42, "title": "bug", "web_url": "u"}])
    issues = parse_issues(raw, "pb-api")
    assert issues[0].iid == 42 and issues[0].repo == "pb-api"
