import json, os, subprocess, sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.collectors import (  # noqa: E402
    collect_diverged_commits_count, parse_issues, parse_mrs, parse_todos, _project,
)


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


# FIX 2 — parsers tolerate non-list JSON
def test_parse_mrs_non_list_returns_empty():
    assert parse_mrs("{}", "pb-www") == []


def test_parse_todos_non_list_returns_empty():
    assert parse_todos("{}") == []


def test_parse_issues_non_list_returns_empty():
    assert parse_issues("{}", "pb-www") == []


# FIX 2 — a single malformed row is skipped, good rows survive
def test_parse_mrs_skips_bad_row_keeps_good():
    raw = json.dumps([
        {"iid": "notanint", "title": "bad", "author": {"username": "x"},
         "web_url": "u", "description": "", "sha": "s", "draft": False,
         "reviewers": [], "updated_at": ""},
        {"iid": 7, "title": "good", "author": {"username": "y"},
         "web_url": "u2", "description": "", "sha": "s2", "draft": False,
         "reviewers": [], "updated_at": ""},
    ])
    mrs = parse_mrs(raw, "pb-www")
    assert len(mrs) == 1
    assert mrs[0].iid == 7


def test_parse_issues_skips_bad_row_keeps_good():
    raw = json.dumps([
        {"iid": "nope", "title": "bad", "web_url": "u"},
        {"iid": 5, "title": "good", "web_url": "u2"},
    ])
    issues = parse_issues(raw, "pb-www")
    assert len(issues) == 1 and issues[0].iid == 5


# FIX 7 — _project URL-encodes the project path
def test_project_encodes_slash():
    assert _project("pb-www") == "performancelivestock%2Fpb-www"


def test_project_encodes_space():
    assert _project("a b") == "performancelivestock%2Fa%20b"


# --- collect_diverged_commits_count (M4 Task H) ---------------------------

def _fake_run(stdout, rc=0):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="err\n")
    return run


def test_collect_diverged_commits_count_calls_expected_glab_api():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"diverged_commits_count": 7}', stderr="")

    with patch("subprocess.run", run):
        assert collect_diverged_commits_count("pb-www", 4020) == 7
    assert seen["cmd"][:2] == ["glab", "api"]
    assert seen["cmd"][2] == (
        "projects/performancelivestock%2Fpb-www/merge_requests/4020"
        "?include_diverged_commits_count=true")


def test_collect_diverged_commits_count_missing_field_defaults_zero():
    with patch("subprocess.run", _fake_run('{"iid": 4020}')):
        assert collect_diverged_commits_count("pb-www", 4020) == 0


def test_collect_diverged_commits_count_nonzero_exit_raises():
    with patch("subprocess.run", _fake_run("", rc=1)):
        with pytest.raises(RuntimeError, match="err"):
            collect_diverged_commits_count("pb-www", 4020)


def test_collect_diverged_commits_count_malformed_json_raises():
    with patch("subprocess.run", _fake_run("not json")):
        with pytest.raises(RuntimeError, match="decode failed"):
            collect_diverged_commits_count("pb-www", 4020)


def test_collect_diverged_commits_count_non_object_json_raises():
    with patch("subprocess.run", _fake_run("[1, 2]")):
        with pytest.raises(RuntimeError, match="not an object"):
            collect_diverged_commits_count("pb-www", 4020)


# --- the GitLab todo id, needed to mark a todo done from the dashboard -------

def test_parse_todos_captures_the_gitlab_todo_id():
    """Falsifying: drop `id=` from parse_todos and Dismiss can never clear the
    todo in GitLab -- the row goes away locally and comes back in the todo list."""
    raw = json.dumps([{
        "id": 4242, "target_type": "MergeRequest", "action_name": "assigned",
        "target_url": "https://gitlab.com/x/-/merge_requests/9",
    }])
    todos = parse_todos(raw)
    assert len(todos) == 1
    assert todos[0].id == 4242
    assert todos[0].action == "assigned"


@pytest.mark.parametrize("payload,expected", [
    ({"id": 77}, 77),
    ({"id": "77"}, 77),          # the REST payload is JSON; be liberal
    ({}, 0),                     # absent -> 0, never a KeyError
    ({"id": None}, 0),
    ({"id": 0}, 0),
])
def test_parse_todos_todo_id_coercion(payload, expected):
    row = {"target_type": "MergeRequest", "action_name": "assigned",
           "target_url": "u"}
    row.update(payload)
    assert parse_todos(json.dumps([row]))[0].id == expected


def test_parse_todos_skips_a_row_with_an_unusable_id_without_losing_the_rest():
    raw = json.dumps([
        {"id": "not-a-number", "target_type": "MergeRequest",
         "action_name": "assigned", "target_url": "u1"},
        {"id": 9, "target_type": "Issue", "action_name": "mentioned",
         "target_url": "u2"},
    ])
    todos = parse_todos(raw)
    assert [t.id for t in todos] == [9]
