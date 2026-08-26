import json, os, subprocess, sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.collectors import (  # noqa: E402
    collect_discussions, collect_diverged_commits_count, parse_issues,
    parse_mrs, parse_threads, parse_todos, parse_unaddressed_count,
    unaddressed_threads, _project,
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


# --- the unaddressed-threads probe (address-feedback, 2026-08-25) -----------
#
# Decision 1: a thread is UNADDRESSED iff it is resolvable, is not resolved,
# and the author of its last non-system note is not the configured user.
# Everything else is waiting on the reviewer (or on nobody) and must emit
# nothing -- the whole point of the rename is that worksweep stops nagging
# about threads Chandler already answered.

def _note(author, body="b", system=False, resolvable=True, resolved=False):
    return {"body": body, "system": system, "resolvable": resolvable,
            "resolved": resolved, "author": {"username": author}}


def _discussions_payload():
    """Every state a real MR's discussions payload can be in, at once."""
    return json.dumps([
        # 1. reviewer had the last word -> UNADDRESSED
        {"id": "t-reviewer-last",
         "notes": [_note("leyang", "this query is N+1")]},
        # 2. Chandler replied last -> waiting on the reviewer, NOT unaddressed
        {"id": "t-mine-last",
         "notes": [_note("leyang", "this query is N+1"),
                   _note("chandler.hardy", "addressed in abc1234")]},
        # 3. already resolved, even though the reviewer spoke last
        {"id": "t-resolved",
         "notes": [_note("leyang", "nit", resolved=True)]},
        # 4. an ordinary (non-resolvable) comment thread
        {"id": "t-unresolvable",
         "notes": [_note("leyang", "nice", resolvable=False)]},
        # 5. a system note trails the reviewer's -> still UNADDRESSED
        {"id": "t-system-tail",
         "notes": [_note("leyang", "wrong table"),
                   _note("gitlab-bot", "changed this line in version 3",
                         system=True)]},
        # 6. a system note trails MY reply -> still not unaddressed
        {"id": "t-mine-then-system",
         "notes": [_note("leyang", "wrong table"),
                   _note("chandler.hardy", "fixed"),
                   _note("gitlab-bot", "changed this line", system=True)]},
        # 7. nothing but system notes -> nobody is waiting on Chandler
        {"id": "t-only-system",
         "notes": [_note("gitlab-bot", "added 1 commit", system=True)]},
    ])


def test_unaddressed_predicate_counts_exactly_the_reviewer_last_threads():
    threads = unaddressed_threads(_discussions_payload(), "chandler.hardy")
    assert tuple(t.id for t in threads) == ("t-reviewer-last", "t-system-tail")
    assert parse_unaddressed_count(_discussions_payload(),
                                   "chandler.hardy") == 2


def test_unaddressed_thread_carries_the_reviewer_and_their_last_word():
    threads = unaddressed_threads(_discussions_payload(), "chandler.hardy")
    first = threads[0]
    assert first.id == "t-reviewer-last"
    assert first.last_author == "leyang"
    assert first.last_note == "this query is N+1"
    assert first.resolvable is True
    assert first.resolved is False


def test_a_thread_whose_last_word_is_mine_is_never_unaddressed():
    """AC #2: the addressed-but-unresolved case -- the exact class the old
    `unresolved_count` signal nagged about forever."""
    raw = json.dumps([{"id": "t", "notes": [
        _note("leyang", "q"), _note("chandler.hardy", "answered")]}])
    assert unaddressed_threads(raw, "chandler.hardy") == ()
    assert parse_unaddressed_count(raw, "chandler.hardy") == 0


def test_the_username_is_read_from_the_caller_not_hardcoded():
    """The same payload flips entirely when the configured user changes."""
    raw = json.dumps([{"id": "t", "notes": [
        _note("leyang", "q"), _note("chandler.hardy", "answered")]}])
    assert tuple(t.id for t in unaddressed_threads(raw, "leyang")) == ("t",)


def test_parse_threads_returns_every_thread_with_its_last_non_system_author():
    """The executor's verification needs ALL threads, not just the unaddressed
    ones: it proves a thread it claims to have replied to now has Chandler as
    its last non-system author."""
    threads = parse_threads(_discussions_payload())
    assert {t.id: t.last_author for t in threads} == {
        "t-reviewer-last": "leyang",
        "t-mine-last": "chandler.hardy",
        "t-resolved": "leyang",
        "t-unresolvable": "leyang",
        "t-system-tail": "leyang",
        "t-mine-then-system": "chandler.hardy",
        "t-only-system": "",
    }


def test_malformed_discussions_payload_degrades_to_zero():
    assert parse_unaddressed_count("not json", "chandler.hardy") == 0
    assert parse_unaddressed_count('{"error": "404"}', "chandler.hardy") == 0
    assert unaddressed_threads("[]", "chandler.hardy") == ()


def test_collect_discussions_gets_the_right_rest_path():
    """The shell edge mirrors collect_diverged_commits_count: one GET, and
    the project path is URL-encoded exactly as _project builds it."""
    with patch("worksweep.collectors._run_glab", return_value="[]") as g:
        assert collect_discussions("pb-www", 3997) == "[]"
    args = g.call_args[0][0]
    assert args[0] == "api"
    assert args[1] == (f"projects/{_project('pb-www')}/merge_requests/3997"
                       f"/discussions?per_page=100")
