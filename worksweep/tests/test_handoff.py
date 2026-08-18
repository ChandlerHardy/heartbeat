"""M3.5 Task E: titles in the digest + ready-to-merge handoff suppression.

!4007 (pb-www) is the live anchor: approved=True, merge_status="MERGEABLE",
assignees containing "leyang" and "chandler.hardy" -- a handed-off MR that
must yield exactly one handoff item from assess_own_mr, never a feedback/
magi/hygiene item, for username chandler.hardy.
"""
import json
import os

from worksweep.assessor import assess_own_mr, is_handed_off, resolutions
from worksweep.collectors import parse_graphql_sweep
from worksweep.curator import _record_line, build_prompt, validate
from worksweep.formatter import _item_line, _truncate_title, format_digest_from_records
from worksweep.models import MergeRequest, QueueRecord, WorkItem

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "graphql_sweep.json")


def _raw():
    with open(FIX) as f:
        return f.read()


def _mr(**kw):
    base = dict(repo="pb-www", iid=4007, title="feat(#1701): Add Usage column",
               author="chandler.hardy",
               web_url="https://gl/x/-/merge_requests/4007", description="",
               sha="s4007", is_draft=False, reviewers=("leyang", "alliecather"),
               ci_status="success", updated_at="",
               approved=False, merge_status="", assignees=())
    base.update(kw)
    return MergeRequest(**base)


# --- models: new field defaults --------------------------------------------

def test_mergerequest_handoff_fields_default():
    mr = MergeRequest(repo="pb-www", iid=1, title="t", author="a", web_url="u",
                      description="", sha="s", is_draft=False, reviewers=(),
                      ci_status="unknown", updated_at="")
    assert mr.approved is False
    assert mr.merge_status == ""
    assert mr.assignees == ()


def test_workitem_title_defaults_empty():
    wi = WorkItem(schema_version=1, id="x", repo="pb-www", kind="mr",
                  executor="triage", risk="low", why="w", web_url="u", sha="s")
    assert wi.title == ""


# --- GraphQL parse -----------------------------------------------------

def test_gql_mr_parses_approved_merge_status_assignees():
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "9", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/9",
            "diffHeadSha": "s9", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": []},
            "headPipeline": {"status": "SUCCESS"},
            "approved": True, "detailedMergeStatus": "mergeable",
            "assignees": {"nodes": [{"username": "leyang"}, {"username": "me"}]},
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}]},
        "assignedMergeRequests": {"nodes": []}}}}
    _, authored, _ = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    mr = authored[0]
    assert mr.approved is True
    assert mr.merge_status == "MERGEABLE"  # upper-cased
    assert mr.assignees == ("leyang", "me")


def test_gql_mr_missing_handoff_fields_default_safely():
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "9", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/9",
            "diffHeadSha": "s9", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": []},
            "headPipeline": {"status": "SUCCESS"},
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}]},
        "assignedMergeRequests": {"nodes": []}}}}
    _, authored, _ = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    mr = authored[0]
    assert mr.approved is False
    assert mr.merge_status == ""
    assert mr.assignees == ()


# --- is_handed_off -------------------------------------------------------

def test_is_handed_off_true_when_approved_mergeable_other_assignee():
    mr = _mr(approved=True, merge_status="MERGEABLE",
             assignees=("leyang", "chandler.hardy"))
    assert is_handed_off(mr, "chandler.hardy") is True


def test_is_handed_off_false_when_approved_but_self_only_assignee():
    mr = _mr(approved=True, merge_status="MERGEABLE",
             assignees=("chandler.hardy",))
    assert is_handed_off(mr, "chandler.hardy") is False


def test_is_handed_off_false_when_mergeable_but_not_approved():
    mr = _mr(approved=False, merge_status="MERGEABLE",
             assignees=("leyang", "chandler.hardy"))
    assert is_handed_off(mr, "chandler.hardy") is False


def test_is_handed_off_false_when_approved_not_mergeable():
    mr = _mr(approved=True, merge_status="DRAFT_STATUS",
             assignees=("leyang", "chandler.hardy"))
    assert is_handed_off(mr, "chandler.hardy") is False


# --- assess_own_mr under handoff -----------------------------------------

def test_own_mr_handed_off_emits_only_handoff_item():
    # Would otherwise ALSO trigger hygiene (no dev url) + feedback (unresolved
    # threads) + ci (failed) -- handoff must suppress all of them.
    mr = _mr(approved=True, merge_status="MERGEABLE",
             assignees=("leyang", "chandler.hardy"),
             description="no dev link here", unresolved_count=3,
             ci_status="failed")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert len(items) == 1
    it = items[0]
    assert it.id == "handoff:pb-www!4007"
    assert it.kind == "handoff"
    assert it.executor == "none"
    assert it.risk == "low"
    assert "leyang" in it.why
    assert it.title == mr.title


def test_own_mr_approved_not_mergeable_suppresses_only_magi_item():
    mr = _mr(approved=True, merge_status="DRAFT_STATUS", assignees=("chandler.hardy",),
             description="no dev link here", unresolved_count=1)
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    ids = {i.id for i in items}
    assert not any(i.executor == "magi-review" for i in items)
    assert "hygiene-devurl:pb-www!4007" in ids
    assert "feedback:pb-www!4007" in ids


def test_own_mr_not_approved_still_proposes_magi():
    mr = _mr(approved=False)
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert any(i.executor == "magi-review" for i in items)


def test_own_mr_items_carry_title():
    mr = _mr(approved=False, description="no dev link here")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert items and all(i.title == mr.title for i in items)


# --- resolutions() handoff resolution -------------------------------------

def test_resolutions_includes_handed_off_feedback_id():
    mr = _mr(approved=True, merge_status="MERGEABLE",
             assignees=("leyang", "chandler.hardy"))
    out = resolutions([], "chandler.hardy", authored=[mr])
    assert out["feedback:pb-www!4007"] == "handed-off"


def test_resolutions_authored_defaults_to_empty_backward_compat():
    assert resolutions([], "chandler.hardy") == {}


def test_resolutions_not_handed_off_mr_no_resolution():
    mr = _mr(approved=False)
    out = resolutions([], "chandler.hardy", authored=[mr])
    assert "feedback:pb-www!4007" not in out


# --- formatter: title rendering / truncation ------------------------------

def test_truncate_title_under_limit_unchanged():
    assert _truncate_title("short title") == "short title"


def test_truncate_title_over_60_chars_truncated_with_ellipsis():
    long_title = "Add Usage column to the feed inventory emailed report for everyone"
    out = _truncate_title(long_title)
    assert len(out) <= 61  # 60 chars + ellipsis char
    assert out.endswith("…")
    assert long_title.startswith(out[:-1])


def test_truncate_title_collapses_newlines_single_line():
    assert "\n" not in _truncate_title("line one\nline two")


def test_truncate_title_empty_is_empty():
    assert _truncate_title("") == ""


def test_item_line_renders_title():
    it = WorkItem(schema_version=1, id="x", repo="pb-www", kind="mr",
                 executor="magi-review", risk="low", why="review requested",
                 web_url="https://gl/x/-/merge_requests/4061", sha="s",
                 title="Add Usage column to the feed inventory emailed report")
    line = _item_line(12, it)
    assert "12." in line
    assert "*Add Usage column" in line
    assert "— review requested" in line


def test_item_line_no_title_segment_when_title_empty():
    it = WorkItem(schema_version=1, id="x", repo="pb-www", kind="mr",
                 executor="magi-review", risk="low", why="review requested",
                 web_url="https://gl/x/-/merge_requests/4061", sha="s")
    line = _item_line(12, it)
    assert "*" not in line


def test_item_line_handoff_gets_checkmark_prefix():
    it = WorkItem(schema_version=1, id="handoff:pb-www!4007", repo="pb-www",
                 kind="handoff", executor="none", risk="low",
                 why="ready to merge → assigned to leyang",
                 web_url="https://gl/x/-/merge_requests/4007", sha="s",
                 title="Add Usage column")
    line = _item_line(12, it)
    assert line.startswith("✅ 12.") or line.startswith("✅12.") or "✅" in line.split(".")[0]


def test_format_digest_places_handoff_in_trailing_group():
    normal = WorkItem(schema_version=1, id="a", repo="pb-www", kind="mr",
                      executor="magi-review", risk="low", why="review requested",
                      web_url="https://gl/x/-/merge_requests/1", sha="s")
    handoff = WorkItem(schema_version=1, id="handoff:pb-www!4007", repo="pb-www",
                       kind="handoff", executor="none", risk="low",
                       why="ready to merge → assigned to leyang",
                       web_url="https://gl/x/-/merge_requests/4007", sha="s")
    recs = [QueueRecord(number=2, item=handoff, first_seen="t", last_seen="t"),
           QueueRecord(number=1, item=normal, first_seen="t", last_seen="t")]
    out = format_digest_from_records(recs)
    assert "Handed off" in out
    assert out.index("1.") < out.index("Handed off")
    assert out.index("Handed off") < out.index("2.")


# --- curator: title in record line + handoff not required -----------------

def test_record_line_includes_title():
    wi = WorkItem(schema_version=1, id="x4061", repo="pb-www", kind="review_request",
                 executor="magi-review", risk="low", why="review requested",
                 web_url="https://gl/x/-/merge_requests/4061", sha="s",
                 title="Add Usage column")
    rec = QueueRecord(number=1, item=wi, first_seen="2026-08-17T12:00:00+00:00",
                      last_seen="2026-08-17T12:00:00+00:00")
    line = _record_line(rec, "2026-08-17T12:00:00+00:00")
    assert "Add Usage column" in line


def test_build_prompt_carries_titles():
    wi = WorkItem(schema_version=1, id="x4061", repo="pb-www", kind="review_request",
                 executor="magi-review", risk="low", why="review requested",
                 web_url="https://gl/x/-/merge_requests/4061", sha="s",
                 title="Add Usage column")
    rec = QueueRecord(number=1, item=wi, first_seen="2026-08-17T12:00:00+00:00",
                      last_seen="2026-08-17T12:00:00+00:00")
    prompt = build_prompt([rec], "2026-08-17T12:00:00+00:00")
    assert "Add Usage column" in prompt


def test_validator_ignores_missing_handoff_number():
    # A handoff record's number need not appear anywhere in the output --
    # it's informational, not actionable -- but its number is still allowed
    # (whitelisted) if the LLM does choose to mention it.
    magi_wi = WorkItem(schema_version=1, id="x1", repo="pb-www", kind="review_request",
                       executor="magi-review", risk="low", why="review requested",
                       web_url="https://gl/x/-/merge_requests/1", sha="s",
                       status="proposed")
    handoff_wi = WorkItem(schema_version=1, id="handoff:pb-www!4007", repo="pb-www",
                          kind="handoff", executor="none", risk="low",
                          why="ready to merge → assigned to leyang",
                          web_url="https://gl/x/-/merge_requests/4007", sha="s")
    recs = [QueueRecord(number=1, item=magi_wi, first_seen="t", last_seen="t"),
           QueueRecord(number=7, item=handoff_wi, first_seen="t", last_seen="t")]
    out = "1. pb-www !1 — review requested"
    assert validate(out, recs) is True  # #7 never mentioned, still passes


def test_validator_still_allows_handoff_number_when_mentioned():
    handoff_wi = WorkItem(schema_version=1, id="handoff:pb-www!4007", repo="pb-www",
                          kind="handoff", executor="none", risk="low",
                          why="ready to merge → assigned to leyang",
                          web_url="https://gl/x/-/merge_requests/4007", sha="s")
    recs = [QueueRecord(number=7, item=handoff_wi, first_seen="t", last_seen="t")]
    out = "Handed off: 7 -- pb-www !4007 -> leyang"
    assert validate(out, recs) is True


# --- Live sanity anchor: MR !4007 -----------------------------------------

def test_live_fixture_4007_parses_as_handed_off():
    data = json.loads(_raw())
    username = data["data"]["currentUser"]["username"]
    _, authored, _ = parse_graphql_sweep(_raw(), username, ("pb-www", "pb-api", "jrg"))
    mr4007 = next((m for m in authored if m.iid == 4007), None)
    assert mr4007 is not None, "!4007 must be present in the re-frozen fixture"
    assert mr4007.approved is True
    assert mr4007.merge_status == "MERGEABLE"
    assert "leyang" in mr4007.assignees
    assert "chandler.hardy" in mr4007.assignees


def test_live_fixture_4007_yields_exactly_one_handoff_item():
    data = json.loads(_raw())
    username = data["data"]["currentUser"]["username"]
    _, authored, _ = parse_graphql_sweep(_raw(), username, ("pb-www", "pb-api", "jrg"))
    mr4007 = next(m for m in authored if m.iid == 4007)
    items = assess_own_mr(mr4007, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert len(items) == 1
    assert items[0].id == "handoff:pb-www!4007"
