import json, os, subprocess, sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.collectors import (  # noqa: E402
    collect_discussions, collect_diverged_commits_count, discussions_pages,
    discussions_path, parse_issues, parse_mrs, parse_threads, parse_todos,
    is_closed_state, mr_reviewers, mr_state, parse_mr_state,
    parse_unaddressed_count, unaddressed_threads, _project,
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
    """The shell edge mirrors collect_diverged_commits_count, and the project
    path is URL-encoded exactly as _project builds it."""
    with patch("worksweep.collectors._run_glab", return_value="[]") as g:
        assert collect_discussions("pb-www", 3997) == ("[]",)
    args = g.call_args[0][0]
    assert args[0] == "api"
    assert args[1] == (f"projects/{_project('pb-www')}/merge_requests/3997"
                       f"/discussions?per_page=100&page=1")


# --- pagination (fix-mode round 2, blocker 6) ------------------------------
#
# GitLab returns each SYSTEM note as its own discussion, so a busy MR blows
# past per_page=100 on housekeeping alone. Unpaginated, the probe silently
# reads page 1 and the reviewer's actual question -- older, so later in the
# list -- never arrives. Fails closed AND silent, the worst combination.

def _page(n_threads, first=0, author="leyang"):
    return json.dumps([{"id": f"t{first + i}", "notes": [_note(author)]}
                       for i in range(n_threads)])


def test_parse_threads_accepts_a_sequence_of_pages():
    pages = (_page(2, first=0), _page(2, first=2))
    assert [t.id for t in parse_threads(pages)] == ["t0", "t1", "t2", "t3"]


def test_parse_threads_still_accepts_a_single_page_string():
    assert [t.id for t in parse_threads(_page(2))] == ["t0", "t1"]


def test_unaddressed_count_spans_every_page():
    pages = (_page(100, first=0), _page(3, first=100))
    assert parse_unaddressed_count(pages, "chandler.hardy") == 103


def test_the_probe_keeps_paging_until_a_short_page_arrives():
    seen = []

    def fetch(page):
        seen.append(page)
        return _page(100 if page < 3 else 7, first=(page - 1) * 100)

    pages = discussions_pages(fetch)
    assert seen == [1, 2, 3]
    assert len(parse_threads(pages)) == 207


def test_a_single_short_page_costs_exactly_one_call():
    seen = []
    pages = discussions_pages(lambda p: (seen.append(p), _page(4))[1])
    assert seen == [1]
    assert len(parse_threads(pages)) == 4


def test_an_empty_first_page_costs_exactly_one_call():
    seen = []
    pages = discussions_pages(lambda p: (seen.append(p), "[]")[1])
    assert seen == [1]
    assert parse_threads(pages) == ()


def test_paging_stops_at_the_cap_rather_than_looping_forever():
    """A server that always answers a full page must not spin the sweep."""
    seen = []

    def fetch(page):
        seen.append(page)
        return _page(100, first=(page - 1) * 100)

    discussions_pages(fetch)
    assert seen == list(range(1, 21))


def test_discussions_path_carries_the_page_and_per_page():
    assert discussions_path("pb-www", 3997, 1) == (
        f"projects/{_project('pb-www')}/merge_requests/3997"
        f"/discussions?per_page=100&page=1")
    assert discussions_path("pb-www", 3997, 4).endswith("&page=4")


def test_collect_discussions_pages_through_glab():
    calls = []

    def fake(args, timeout=30):
        calls.append(args[1])
        return _page(100) if len(calls) == 1 else _page(2)

    with patch("worksweep.collectors._run_glab", side_effect=fake):
        pages = collect_discussions("pb-www", 3997)
    assert [c.rsplit("&page=", 1)[1] for c in calls] == ["1", "2"]
    assert len(parse_threads(pages)) == 102


# --- access-token bots are not reviewers (live finding, !4082) -------------
#
# Chandler ran an `@coderabbitai` command on a thread; CodeRabbit auto-replied
# as `group2846274botbb6bad6ee97bbb14c73a6e3e39ff610d`. That reply became the
# thread's last non-system note, so the thread read as "waiting on Chandler",
# the run correctly refused to answer a bot, and the row parked needs-input
# over pure noise.

BOT = "group2846274botbb6bad6ee97bbb14c73a6e3e39ff610d"


def _thread(tid, *authors):
    """A resolvable, unresolved thread whose notes are `authors` in order."""
    return {"id": tid, "notes": [_note(a, f"{a} said something")
                                 for a in authors]}


def test_a_bot_reply_after_my_own_does_not_make_a_thread_unaddressed():
    """The live case, exactly: reviewer asks, Chandler answers and summons
    CodeRabbit, CodeRabbit replies. Nobody is waiting on him."""
    raw = json.dumps([_thread("t1", "leyang", "chandler.hardy", BOT)])
    assert unaddressed_threads(raw, "chandler.hardy") == ()


def test_a_bot_reply_after_a_reviewers_still_leaves_it_unaddressed():
    """Walking back past the bot finds the reviewer, who IS still waiting."""
    raw = json.dumps([_thread("t1", "chandler.hardy", "leyang", BOT)])
    assert tuple(t.id for t in unaddressed_threads(raw, "chandler.hardy")) \
        == ("t1",)


def test_a_thread_of_nothing_but_bot_notes_is_nobodys_question():
    raw = json.dumps([_thread("t1", BOT, BOT)])
    assert unaddressed_threads(raw, "chandler.hardy") == ()


def test_the_last_word_skips_the_bot_so_escalations_quote_a_human():
    """`last_author`/`last_note` feed the prompt and the Discord escalation
    line. Quoting a bot's auto-reply back at Chandler is noise."""
    raw = json.dumps([_thread("t1", "chandler.hardy", "leyang", BOT)])
    t = parse_threads(raw)[0]
    assert t.last_author == "leyang"
    assert t.last_note == "leyang said something"


def test_the_bot_notes_are_still_carried_for_verification():
    """Only the LAST-WORD calculation ignores them -- the note list itself is
    what proves which replies this run posted, and must stay complete."""
    raw = json.dumps([_thread("t1", "leyang", BOT)])
    t = parse_threads(raw)[0]
    assert [n.author for n in t.notes] == ["leyang", BOT]


def test_every_access_token_bot_shape_is_recognised():
    from worksweep.collectors import _is_bot
    for name in (BOT, "group2846274bot", "group_284_bot", "project123bot",
                 "project_9_bot_deadbeef", "GROUP12BOTX"):
        assert _is_bot(name) is True, name


def test_a_mere_bot_substring_is_never_filtered():
    """Too broad a filter silently drops real review feedback, which is far
    worse than the noise it removes. Only GitLab's access-token shape counts."""
    from worksweep.collectors import _is_bot
    for name in ("leyang_bot", "robot", "botond", "dependabot", "bot",
                 "chandler.hardy", "abbot", "group_bot", "botgroup284"):
        assert _is_bot(name) is False, name


def test_a_human_named_like_a_bot_still_gets_answered():
    raw = json.dumps([_thread("t1", "chandler.hardy", "dependabot")])
    assert tuple(t.id for t in unaddressed_threads(raw, "chandler.hardy")) \
        == ("t1",)


def test_a_system_note_after_a_bot_note_changes_nothing():
    raw = json.dumps([{"id": "t1", "notes": [
        _note("leyang"), _note("chandler.hardy"), _note(BOT),
        _note("gitlab", system=True)]}])
    assert unaddressed_threads(raw, "chandler.hardy") == ()


# --- MR state probe (live finding: !3997 merged, 2026-08-27) --------------
#
# The sweep only queries OPEN merge requests, so a merged MR's rows stop being
# emitted and any error/needs-input/approved row for it is retained forever.
# Answering "is this MR still open?" needs one targeted GET.

def test_parse_mr_state_reads_the_state_field():
    assert parse_mr_state(json.dumps({"state": "merged"})) == "merged"
    assert parse_mr_state(json.dumps({"state": "opened"})) == "opened"
    assert parse_mr_state(json.dumps({"state": "closed"})) == "closed"
    assert parse_mr_state(json.dumps({"state": "MERGED"})) == "merged"


def test_parse_mr_state_on_junk_is_empty_not_a_guess():
    """"" means "we do not know", which every caller treats as "leave it
    alone" -- closing a row on a failed probe would be worse than retaining."""
    for junk in ("", "not json", "[]", json.dumps({}), json.dumps(None),
                 json.dumps({"state": None}), json.dumps({"state": 7})):
        assert parse_mr_state(junk) == "", junk


def test_is_closed_covers_merged_and_closed_only():
    assert is_closed_state("merged") is True
    assert is_closed_state("closed") is True
    assert is_closed_state("opened") is False
    assert is_closed_state("locked") is False
    assert is_closed_state("") is False


def test_mr_state_asks_for_the_right_mr():
    calls = []
    state = mr_state(lambda args, body=None: (calls.append(args),
                                              json.dumps({"state": "merged"}))[1],
                     "pb-www", 3997)
    assert state == "merged"
    assert calls[0][0] == "api"
    assert calls[0][1] == (f"projects/{_project('pb-www')}"
                           f"/merge_requests/3997")


def test_mr_state_never_raises_through_to_the_caller():
    """A probe is a nicety, not the work. Every caller falls back to "leave it
    alone", so a failure must read as "unknown" rather than take a run down."""
    def boom(args, body=None):
        raise RuntimeError("glab api timed out after 30s")
    assert mr_state(boom, "pb-www", 3997) == ""


# --- plain reviewer notes (live blind spot: !4084, 2026-08-28) ------------
#
# dasilvaja posted "Two things before this is merge-ready: ..." as a plain MR
# note -- resolvable: false, no diff anchor -- and left his reviewer state
# `unreviewed`. The predicate required a resolvable thread and changes_requested
# was false, so NEITHER feedback arm fired and the ask was invisible.

REVIEWERS = ("dasilvaja", "leyang")


def _plain(tid, *authors, individual=True):
    """A standalone MR note: a discussion with no resolvable notes."""
    return {"id": tid, "individual_note": individual,
            "notes": [_note(a, f"{a} said something", resolvable=False)
                      for a in authors]}


def test_a_reviewers_plain_note_is_unaddressed():
    """FALSIFYING: this is !4084. A listed reviewer's note is review feedback
    whether or not GitLab gave it a resolve button."""
    raw = json.dumps([_plain("t1", "dasilvaja")])
    got = unaddressed_threads(raw, "chandler.hardy", REVIEWERS)
    assert tuple(t.id for t in got) == ("t1",)


def test_our_reply_under_a_plain_note_settles_it():
    raw = json.dumps([_plain("t1", "dasilvaja", "chandler.hardy")])
    assert unaddressed_threads(raw, "chandler.hardy", REVIEWERS) == ()


def test_a_non_reviewers_plain_note_is_ignored():
    """Plain notes carry chatter from anyone -- a passer-by's comment is not
    an ask, and treating it as one turns the dashboard into the MR feed."""
    raw = json.dumps([_plain("t1", "some-observer")])
    assert unaddressed_threads(raw, "chandler.hardy", REVIEWERS) == ()


def test_a_bots_plain_note_is_ignored_even_from_a_reviewer_slot():
    raw = json.dumps([_plain("t1", BOT)])
    assert unaddressed_threads(raw, "chandler.hardy", REVIEWERS + (BOT,)) == ()


def test_a_reviewer_speaking_after_a_bot_under_a_plain_note_counts():
    raw = json.dumps([_plain("t1", "chandler.hardy", "dasilvaja", BOT)])
    assert tuple(t.id for t in unaddressed_threads(
        raw, "chandler.hardy", REVIEWERS)) == ("t1",)


def test_with_no_reviewers_listed_no_plain_note_can_qualify():
    raw = json.dumps([_plain("t1", "dasilvaja")])
    assert unaddressed_threads(raw, "chandler.hardy", ()) == ()


def test_resolvable_threads_keep_the_old_rule_exactly():
    """Unchanged: on a resolvable thread ANY non-us human counts, reviewer or
    not. The reviewer restriction buys signal on plain notes only."""
    raw = json.dumps([_thread("t1", "some-observer")])
    assert tuple(t.id for t in unaddressed_threads(
        raw, "chandler.hardy", ())) == ("t1",)


def test_the_reviewers_argument_is_optional():
    """Every existing caller passes two arguments; they must keep working and
    keep getting exactly the old resolvable-only answer."""
    raw = json.dumps([_thread("t1", "leyang"), _plain("t2", "dasilvaja")])
    assert tuple(t.id for t in unaddressed_threads(
        raw, "chandler.hardy")) == ("t1",)


def test_the_count_helper_takes_reviewers_too():
    raw = json.dumps([_thread("t1", "leyang"), _plain("t2", "dasilvaja")])
    assert parse_unaddressed_count(raw, "chandler.hardy", REVIEWERS) == 2
    assert parse_unaddressed_count(raw, "chandler.hardy") == 1


def test_an_individual_note_keeps_a_usable_discussion_id():
    """Replying converts a standalone note into a discussion, and the id in
    this payload is already the discussion id the reply endpoint wants."""
    raw = json.dumps([_plain("8f3c1a2b4d5e6f70", "dasilvaja")])
    t = parse_threads(raw)[0]
    assert t.id == "8f3c1a2b4d5e6f70"
    assert t.resolvable is False


def test_mr_reviewers_reads_the_listed_usernames():
    calls = []
    got = mr_reviewers(
        lambda args, body=None: (calls.append(args), json.dumps(
            {"reviewers": [{"username": "dasilvaja"}, {"username": "leyang"}]}))[1],
        "pb-www", 4084)
    assert got == ("dasilvaja", "leyang")
    assert calls[0][1].endswith("/merge_requests/4084")


def test_mr_reviewers_on_junk_is_empty_not_a_guess():
    for junk in ("", "not json", "[]", json.dumps({}),
                 json.dumps({"reviewers": None}),
                 json.dumps({"reviewers": [{"name": "no username"}]})):
        assert mr_reviewers(lambda a, body=None: junk, "pb-www", 1) == (), junk


def test_mr_reviewers_never_raises():
    def boom(args, body=None):
        raise RuntimeError("glab down")
    assert mr_reviewers(boom, "pb-www", 1) == ()


# --- note identity for durable dismissal (live: three LGTM rows) ---------
#
# The plain-note sensor's first sweep produced three permanent proposed rows
# whose entire content is "LGTM" (cmnoble on !4084, adamsoper on !3981/!3982).
# (Bare acks are now suppressed upstream by is_pure_ack; dismissal remains the
# tool for every reviewer note that carries words but no ask.)
# Dismissing has to mean "I have seen THIS note", never "mute this thread" --
# so the evidence is the (discussion, last note) pair, not the thread id.

def _idnote(author, note_id, body="b", resolvable=True):
    n = _note(author, body, resolvable=resolvable)
    n["id"] = note_id
    return n


def test_a_thread_carries_the_id_of_its_last_human_note():
    raw = json.dumps([{"id": "d1", "notes": [
        _idnote("leyang", 101), _idnote("chandler.hardy", 102),
        _idnote("leyang", 103)]}])
    t = parse_threads(raw)[0]
    assert t.id == "d1"
    assert t.last_note_id == "103"
    assert [n.id for n in t.notes] == ["101", "102", "103"]


def test_the_last_note_id_skips_system_and_bot_notes():
    """It has to name the note the LAST WORD calculation used, or the seen-set
    key would not match what the human actually dismissed."""
    raw = json.dumps([{"id": "d1", "notes": [
        _idnote("leyang", 101),
        dict(_idnote(BOT, 102)),
        dict(_note("gitlab", system=True), id="103")]}])
    t = parse_threads(raw)[0]
    assert t.last_author == "leyang"
    assert t.last_note_id == "101"


def test_a_missing_note_id_is_empty_not_a_guess():
    raw = json.dumps([{"id": "d1", "notes": [_note("leyang")]}])
    assert parse_threads(raw)[0].last_note_id == ""


def test_a_seen_note_is_not_unaddressed():
    """FALSIFYING. This is the whole dismissal: the row goes away because the
    human said they read that note."""
    raw = json.dumps([{"id": "d1", "notes": [_idnote("leyang", 101)]}])
    assert unaddressed_threads(raw, "chandler.hardy") != ()
    assert unaddressed_threads(raw, "chandler.hardy",
                               seen={("d1", "101")}) == ()


def test_a_new_note_on_a_dismissed_thread_comes_back():
    """FALSIFYING the other way. Dismiss means "seen THIS note" -- a reviewer
    following up must reach Chandler, or dismissal becomes a mute button."""
    raw = json.dumps([{"id": "d1", "notes": [
        _idnote("leyang", 101), _idnote("leyang", 104)]}])
    assert tuple(t.id for t in unaddressed_threads(
        raw, "chandler.hardy", seen={("d1", "101")})) == ("d1",)


def test_the_seen_key_is_the_pair_not_either_half():
    """The same note id under a different discussion, and the same discussion
    with a different note, must both still fire."""
    raw = json.dumps([{"id": "d1", "notes": [_idnote("leyang", 101)]}])
    assert unaddressed_threads(raw, "chandler.hardy",
                               seen={("d2", "101")}) != ()
    assert unaddressed_threads(raw, "chandler.hardy",
                               seen={("d1", "999")}) != ()


def test_the_seen_set_applies_to_plain_notes_too():
    """The nag rows that motivated this were bare-LGTM plain notes; those are
    suppressed upstream by is_pure_ack now, so the fixture carries a real nit
    to stay on the dismissal path."""
    raw = json.dumps([{"id": "d1", "individual_note": True,
                       "notes": [_idnote("cmnoble", 55, "one nit: rename it",
                                         resolvable=False)]}])
    assert unaddressed_threads(raw, "chandler.hardy", ("cmnoble",)) != ()
    assert unaddressed_threads(raw, "chandler.hardy", ("cmnoble",),
                               seen={("d1", "55")}) == ()


def test_the_seen_set_is_optional():
    raw = json.dumps([{"id": "d1", "notes": [_idnote("leyang", 101)]}])
    assert unaddressed_threads(raw, "chandler.hardy") != ()
    assert parse_unaddressed_count(raw, "chandler.hardy") == 1


# --- pure-ack suppression (live friction: cmnoble's "LGTM" on !4084) -------
#
# A plain note whose ENTIRE body is an approval token ("LGTM", "Looks good
# to me", a thumbs-up) is a reviewer closing the loop, not an ask -- parking
# an address-feedback row on it makes Chandler dismiss praise by hand. The
# match is exact-on-the-whole-body by design: "LGTM, but rename X" carries an
# ask and must keep counting.

def _plain_with_body(tid, author, body):
    return {"id": tid, "individual_note": True,
            "notes": [_note(author, body, resolvable=False)]}


def test_a_reviewers_bare_lgtm_is_not_unaddressed():
    for body in ("LGTM", "lgtm!", "  Looks good to me  ", "Looks good",
                 "Approved", "ship it", "+1", "\U0001F44D",
                 "LGTM \U0001F680", "Nice work!"):
        raw = json.dumps([_plain_with_body("t1", "dasilvaja", body)])
        assert unaddressed_threads(raw, "chandler.hardy", REVIEWERS) == (), body


def test_an_lgtm_with_an_ask_still_counts():
    for body in ("LGTM, but rename the flag first",
                 "Looks good to me.\nOne thing though: the cron window",
                 "LGTM once CI is green and the secret is deployed",
                 "Not LGTM"):
        raw = json.dumps([_plain_with_body("t1", "dasilvaja", body)])
        got = unaddressed_threads(raw, "chandler.hardy", REVIEWERS)
        assert tuple(t.id for t in got) == ("t1",), body


def test_ack_suppression_only_speaks_for_listed_reviewers():
    # A passer-by's LGTM was already ignored; a reviewer's real note that
    # FOLLOWS someone else's LGTM keeps counting (the ack is not the last
    # word).
    raw = json.dumps([{"id": "t1", "individual_note": True,
                       "notes": [_note("leyang", "LGTM", resolvable=False),
                                 _note("dasilvaja", "please add a test",
                                       resolvable=False)]}])
    got = unaddressed_threads(raw, "chandler.hardy", REVIEWERS)
    assert tuple(t.id for t in got) == ("t1",)


def test_an_unresolved_diff_thread_ending_in_a_pure_ack_is_settled():
    # The reviewer wrote "LGTM" under a diff thread and never clicked
    # resolve. The executor is forbidden to resolve threads, so without
    # suppression this row nags forever with nothing to do.
    raw = json.dumps([{"id": "t1", "individual_note": False,
                       "notes": [_note("dasilvaja", "is this right?",
                                       resolvable=True),
                                 _note("chandler.hardy", "yes, because ...",
                                       resolvable=False),
                                 _note("dasilvaja", "LGTM",
                                       resolvable=False)]}])
    assert unaddressed_threads(raw, "chandler.hardy", REVIEWERS) == ()


def test_an_unresolved_diff_thread_ending_in_an_ack_from_a_nonreviewer_counts():
    # Only a LISTED reviewer's ack settles: same evidence rule as the
    # plain-note arm.
    raw = json.dumps([{"id": "t1", "individual_note": False,
                       "notes": [_note("dasilvaja", "is this right?",
                                       resolvable=True),
                                 _note("some-observer", "LGTM",
                                       resolvable=False)]}])
    got = unaddressed_threads(raw, "chandler.hardy", REVIEWERS)
    assert tuple(t.id for t in got) == ("t1",)
