# worksweep/tests/test_main_v2.py
"""run_sweep: graphql wiring, one-message contract, error post."""
import json

from worksweep.__main__ import run_sweep
from worksweep.config import WorksweepConfig


def _cfg():
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y")


def _gql(review_nodes=(), authored_nodes=(), assigned_nodes=()):
    return json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": list(review_nodes)},
        "authoredMergeRequests": {"nodes": list(authored_nodes)},
        "assignedMergeRequests": {"nodes": list(assigned_nodes)}}}})


def _node(iid=1, state="UNREVIEWED"):
    return {"iid": str(iid), "title": "t", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{iid}",
            "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-07T00:00:00Z",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "other"},
            "reviewers": {"nodes": [
                {"username": "me", "mergeRequestInteraction": {"reviewState": state}}]},
            "headPipeline": None}


def _deps(store, raw, queue=None):
    return {
        "graphql": lambda: raw,
        "todos": lambda: [],
        "issues": lambda repo, user: [],
        "post": lambda hook, content: store.append(content),
        "load": lambda: list(queue or []),
        "save": lambda records: store.append(("saved", records)),
        "now": lambda: "2026-08-07T12:00:00+00:00",
    }


def test_actionable_item_posts_digest():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(review_nodes=[_node()])))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) >= 1 and "review" in texts[0].lower()


def test_nothing_actionable_posts_heartbeat():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(review_nodes=[_node(state="REVIEWED")])))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1 and texts[0].startswith("🔍")


def test_collector_exception_posts_error_and_exits_1():
    posts = []
    deps = _deps(posts, "")
    deps["graphql"] = lambda: (_ for _ in ()).throw(RuntimeError("glab exploded"))
    rc = run_sweep(_cfg(), deps)
    assert rc == 1
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1 and texts[0].startswith("⚠️") and "glab exploded" in texts[0]


def test_todos_deduped_against_graphql_mr_items():
    """I5: a REST todo pointing at the same MR the GraphQL sweep already
    surfaced as a review item is redundant noise — only the review item
    should reach the digest. Matched on web_url, string-equal after a
    trailing-slash strip."""
    from worksweep.models import Todo

    posts = []
    mr_url = "https://gl/x/-/merge_requests/1"
    deps = _deps(posts, _gql(review_nodes=[_node(iid=1)]))
    deps["todos"] = lambda: [Todo(target="MergeRequest", action="review_requested",
                                  web_url=mr_url + "/")]  # trailing slash variant
    rc = run_sweep(_cfg(), deps)
    assert rc == 0
    saved = [args for args in posts if isinstance(args, tuple) and args[0] == "saved"]
    records = saved[-1][1]
    kinds = [r.item.kind for r in records if r.item.web_url.rstrip("/") == mr_url]
    assert kinds == ["review_request"]   # the todo for the same MR was dropped


def test_never_zero_messages():
    for raw in (_gql(), _gql(review_nodes=[_node()])):
        posts = []
        run_sweep(_cfg(), _deps(posts, raw))
        assert any(isinstance(p, str) for p in posts)


def _assigned_node(iid=30, author="other"):
    return {"iid": str(iid), "title": "t", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{iid}",
            "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "",
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": author},
            "reviewers": {"nodes": []},
            "headPipeline": None,
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}


# Task A.1 wiring — an MR assigned to me (not authored, not review-requested)
# surfaces as a triage item in the posted digest.
def test_assigned_mr_appears_in_digest():
    posts = []
    raw = _gql(assigned_nodes=[_assigned_node(iid=30, author="other")])
    rc = run_sweep(_cfg(), _deps(posts, raw))
    assert rc == 0
    saved = [args for args in posts if isinstance(args, tuple) and args[0] == "saved"]
    records = saved[-1][1]
    assigned_items = [r.item for r in records if r.item.kind == "assigned_mr"]
    assert len(assigned_items) == 1
    assert assigned_items[0].id == "assigned:pb-www!30"


# Task A.1 wiring — a self-assigned MR (assignee == author, GitLab's common
# default) must not duplicate as a separate "assigned to you" item; it's
# already fully covered by the authored-MR items.
def test_self_assigned_mr_no_duplicate_item():
    posts = []
    raw = _gql(authored_nodes=[_assigned_node(iid=31, author="me")],
              assigned_nodes=[_assigned_node(iid=31, author="me")])
    rc = run_sweep(_cfg(), _deps(posts, raw))
    assert rc == 0
    saved = [args for args in posts if isinstance(args, tuple) and args[0] == "saved"]
    records = saved[-1][1]
    assert not any(r.item.kind == "assigned_mr" for r in records)


# Task A.3 wiring — an assigned issue already covered by an open authored
# MR's title (`feat(#42): ...`) is suppressed from the digest.
def test_covered_issue_suppressed_from_digest():
    from worksweep.models import Issue

    posts = []
    authored = [{"iid": "40", "title": "feat(#42): thing", "draft": False,
                "webUrl": "https://gl/x/-/merge_requests/40",
                "diffHeadSha": "s40", "updatedAt": "2026-08-07T00:00:00Z",
                "description": "Available on https://dev1.chandlerhardy-dev.performancebeef.com/",
                "project": {"fullPath": "performancelivestock/pb-www"},
                "author": {"username": "me"},
                "reviewers": {"nodes": []}, "headPipeline": {"status": "success"},
                "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}]
    raw = _gql(authored_nodes=authored)
    deps = _deps(posts, raw)
    deps["issues"] = lambda repo, user: [
        Issue(repo="pb-www", iid=42, title="thing", web_url="https://gl/x/-/issues/42")]
    rc = run_sweep(_cfg(), deps)
    assert rc == 0
    saved = [args for args in posts if isinstance(args, tuple) and args[0] == "saved"]
    records = saved[-1][1]
    assert not any(r.item.kind == "issue" for r in records)


def test_uncovered_issue_still_appears_in_digest():
    from worksweep.models import Issue

    posts = []
    raw = _gql()
    deps = _deps(posts, raw)
    deps["issues"] = lambda repo, user: [
        Issue(repo="pb-www", iid=99, title="unrelated", web_url="https://gl/x/-/issues/99")]
    rc = run_sweep(_cfg(), deps)
    assert rc == 0
    saved = [args for args in posts if isinstance(args, tuple) and args[0] == "saved"]
    records = saved[-1][1]
    assert any(r.item.kind == "issue" for r in records)
