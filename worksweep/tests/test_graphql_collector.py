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
    reviews, authored = parse_graphql_sweep(_raw(), _username(),
                                            ("pb-www", "pb-api", "jrg"))
    assert isinstance(reviews, list) and isinstance(authored, list)
    for mr in reviews + authored:
        assert mr.repo in ("pb-www", "pb-api", "jrg")
        assert mr.iid > 0 and mr.web_url.startswith("https://")
        assert mr.sha  # diffHeadSha present


def test_my_review_state_extracted_uppercase():
    reviews, _ = parse_graphql_sweep(_raw(), _username(),
                                     ("pb-www", "pb-api", "jrg"))
    states = {mr.my_review_state for mr in reviews}
    assert states  # every review-requested MR has a state for me
    assert all(s == s.upper() and s for s in states)


def test_repo_filter_drops_unlisted_projects():
    reviews, authored = parse_graphql_sweep(_raw(), _username(), ("pb-api",))
    assert all(mr.repo == "pb-api" for mr in reviews + authored)


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
    _, authored = parse_graphql_sweep(json.dumps(doc), "me", ("pb-www",))
    mr = authored[0]
    assert mr.changes_requested is True
    assert mr.unresolved_count == 2
    assert mr.ci_status == "failed"
    assert mr.dev_url_present is True


def test_malformed_raw_returns_empty_lists():
    assert parse_graphql_sweep("not json", "me", ("pb-www",)) == ([], [])
