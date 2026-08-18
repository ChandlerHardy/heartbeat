"""parse_graphql_sweep maps the frozen live response onto MergeRequest."""
import json
import os

from worksweep.collectors import parse_graphql_sweep

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "graphql_sweep.json")


def _raw():
    with open(FIX) as f:
        return f.read()


def _username():
    data = json.loads(_raw())
    data = data.get("data", data)
    return data["currentUser"]["username"]


def test_parses_both_lists_without_error():
    reviews, authored, assigned = parse_graphql_sweep(_raw(), _username(),
                                                       ("pb-www", "pb-api", "jrg"))
    assert isinstance(reviews, list) and isinstance(authored, list)
    assert isinstance(assigned, list)
    for mr in reviews + authored + assigned:
        assert mr.repo in ("pb-www", "pb-api", "jrg")
        assert mr.iid > 0 and mr.web_url.startswith("https://")
        assert mr.sha  # diffHeadSha present


def test_my_review_state_extracted_uppercase():
    reviews, _, _ = parse_graphql_sweep(_raw(), _username(),
                                        ("pb-www", "pb-api", "jrg"))
    states = {mr.my_review_state for mr in reviews}
    assert states  # every review-requested MR has a state for me
    assert all(s == s.upper() and s for s in states)


def test_fixture_mrs_carry_source_branch():
    # M4 Task F -- the 2026-08-18 re-freeze added sourceBranch to the live
    # query; every MR the live fixture carries should have a non-empty one.
    reviews, authored, assigned = parse_graphql_sweep(_raw(), _username(),
                                                       ("pb-www", "pb-api", "jrg"))
    for mr in reviews + authored + assigned:
        assert mr.source_branch


def test_repo_filter_drops_unlisted_projects():
    reviews, authored, assigned = parse_graphql_sweep(_raw(), _username(), ("pb-api",))
    assert all(mr.repo == "pb-api" for mr in reviews + authored + assigned)


# Task A.1 — assignee bucket
def test_assigned_bucket_parses_without_error():
    _, _, assigned = parse_graphql_sweep(_raw(), _username(),
                                         ("pb-www", "pb-api", "jrg"))
    assert isinstance(assigned, list) and assigned
    for mr in assigned:
        assert mr.repo in ("pb-www", "pb-api", "jrg")
        assert mr.iid > 0 and mr.web_url.startswith("https://")


def test_synthetic_assigned_not_authored_by_me():
    # The live account's assignedMergeRequests bucket happened to be entirely
    # self-authored MRs at freeze time (2026-08-17 re-freeze), so the "MR
    # assigned to me but authored by someone else" path is covered
    # synthetically here per the plan's fallback.
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": []},
        "assignedMergeRequests": {"nodes": [{
            "iid": "55", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/55",
            "diffHeadSha": "s55", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "someone-else"},
            "reviewers": {"nodes": []},
            "headPipeline": {"status": "SUCCESS"},
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}]}}}}
    _, _, assigned = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    assert len(assigned) == 1
    assert assigned[0].iid == 55
    assert assigned[0].author == "someone-else"


def test_missing_assigned_key_defaults_to_empty_list():
    # Backward-compat: a raw payload frozen before this field existed (or any
    # tolerant partial response) must not raise -- just yield no assigned MRs.
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": []}}}}
    _, _, assigned = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    assert assigned == []


def test_synthetic_authored_fields():
    # Deterministic synthetic doc for the authored-only fields.
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "7", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/7",
            "diffHeadSha": "s7", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "Available on https://dev1.chandlerhardy-dev.performancebeef.com/x",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": [
                {"username": "r1", "mergeRequestInteraction": {"reviewState": "REQUESTED_CHANGES"}}]},
            "headPipeline": {"status": "FAILED"},
            "resolvableDiscussionsCount": 5, "resolvedDiscussionsCount": 3}]}}}}
    _, authored, _ = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    mr = authored[0]
    assert mr.changes_requested is True
    assert mr.unresolved_count == 2
    assert mr.ci_status == "failed"
    assert mr.dev_url_present is True


# M4 Task F -- sourceBranch feeds devslots.classify.
def test_source_branch_extracted_from_authored_node():
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "7", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/7",
            "diffHeadSha": "s7", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "", "sourceBranch": "feat/1775-thing",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": []},
            "headPipeline": {"status": "SUCCESS"},
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}]}}}}
    _, authored, _ = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    assert authored[0].source_branch == "feat/1775-thing"


def test_missing_source_branch_key_defaults_empty():
    # Backward-compat: a raw payload frozen before this field existed (or a
    # tolerant partial response) must not raise -- just yield "".
    doc = {"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "7", "title": "t", "draft": False,
            "webUrl": "https://gitlab.com/performancelivestock/pb-www/-/merge_requests/7",
            "diffHeadSha": "s7", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": []},
            "headPipeline": {"status": "SUCCESS"},
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}]}}}}
    _, authored, _ = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    assert authored[0].source_branch == ""


def test_malformed_raw_returns_empty_lists():
    assert parse_graphql_sweep("not json", "me", ("pb-www",)) == ([], [], [])


def test_non_dict_json_returns_empty_lists():
    # Valid JSON that isn't an object must not raise — same contract as a
    # decode failure: ([], [], []).
    for raw in ("null", "[]", "123", '"str"'):
        assert parse_graphql_sweep(raw, "me", ("pb-www",)) == ([], [], [])
