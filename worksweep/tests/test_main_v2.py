# worksweep/tests/test_main_v2.py
"""run_sweep: graphql wiring, one-message contract, error post."""
import json
import re

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


# --- a ✅ revoked by a fresh push must be explained, not silent -------------

def _saved(store):
    """The records from the last `save` call the sweep made."""
    return [p[1] for p in store if isinstance(p, tuple) and p[0] == "saved"][-1]


def _approve_all_with_a_new_sha(records):
    """Model the live case: Chandler ✅'d, then the author pushed."""
    import dataclasses
    return [dataclasses.replace(
        r, item=dataclasses.replace(r.item, status="approved", sha="OLD-SHA"))
        for r in records]


def test_a_reset_approval_is_explained_in_discord():
    """Falsifying: without the notice the item silently reappears as proposed,
    which is exactly what confused Chandler live (2026-08-25)."""
    posts = []
    raw = _gql(review_nodes=[_node(iid=4078), _node(iid=4076)])
    run_sweep(_cfg(), _deps(posts, raw))                  # first sweep numbers them
    approved = _approve_all_with_a_new_sha(_saved(posts))

    posts2 = []
    rc = run_sweep(_cfg(), _deps(posts2, raw, queue=approved))
    assert rc == 0
    texts = [p for p in posts2 if isinstance(p, str)]
    notice = [t for t in texts if t.startswith("↩️ re-proposed")]
    assert len(notice) == 1, texts

    # exact numbers, bolded, with masked refs -- and it lands AFTER the digest
    nums = sorted(int(n) for n in re.findall(r"\*\*(\d+)\*\*", notice[0]))
    assert nums == sorted(r.number for r in approved)
    assert "[#4078](https://gl/x/-/merge_requests/4078)" in notice[0]
    assert texts.index(notice[0]) == len(texts) - 1

    # and the records really were reset
    assert {r.item.status for r in _saved(posts2)} == {"proposed"}


def test_no_notice_when_nothing_was_reset():
    """A sweep that revokes no ✅ must stay quiet -- one digest, no footnote."""
    import dataclasses
    posts = []
    raw = _gql(review_nodes=[_node(iid=4078)])
    run_sweep(_cfg(), _deps(posts, raw))
    # approved, and the sha did NOT move
    approved = [dataclasses.replace(
        r, item=dataclasses.replace(r.item, status="approved"))
        for r in _saved(posts)]

    posts2 = []
    assert run_sweep(_cfg(), _deps(posts2, raw, queue=approved)) == 0
    texts = [p for p in posts2 if isinstance(p, str)]
    assert not [t for t in texts if t.startswith("↩️ re-proposed")]
    assert {r.item.status for r in _saved(posts2)} == {"approved"}


def test_a_failing_notice_does_not_fail_the_sweep():
    """Never-silent runs both ways: the digest is the contract, so a broken
    footnote must not turn a good sweep into a ⚠️ error post."""
    import worksweep.__main__ as wsmain
    posts = []
    raw = _gql(review_nodes=[_node(iid=4078)])
    run_sweep(_cfg(), _deps(posts, raw))
    approved = _approve_all_with_a_new_sha(_saved(posts))

    posts2 = []
    original = wsmain.format_reproposed
    wsmain.format_reproposed = lambda numbered: (_ for _ in ()).throw(
        RuntimeError("formatter blew up"))
    try:
        rc = run_sweep(_cfg(), _deps(posts2, raw, queue=approved))
    finally:
        wsmain.format_reproposed = original
    assert rc == 0
    texts = [p for p in posts2 if isinstance(p, str)]
    assert not [t for t in texts if t.startswith("⚠️")]
    assert len(texts) >= 1                      # the digest still went out


# --- the discussions probe seam (address-feedback, 2026-08-25) -------------
#
# Opt-in exactly like deps["diverged_commits"]: absent -> the sweep never
# probes and simply proposes no address-feedback work; present -> the authored
# MergeRequest is rebound with its unaddressed_count BEFORE assess_own_mr sees
# it. Every other consumer of `authored` (bootstrap_magi_records, the stale
# loop, resolutions) reads the rebound list, so the two can never disagree.

def _authored(iid=3997, unresolved=2, changes_requested=False):
    reviewers = ([{"username": "leyang", "mergeRequestInteraction":
                   {"reviewState": "REQUESTED_CHANGES"}}]
                 if changes_requested else [])
    return {"iid": str(iid), "title": "Ranch data tab", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{iid}",
            "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-25T00:00:00Z",
            "description": "Available on "
                           "https://dev2.chandlerhardy-dev.performancebeef.com/",
            "sourceBranch": "chardy/1588-ranch-data",
            "approved": False, "detailedMergeStatus": "",
            "assignees": {"nodes": []},
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": reviewers},
            "headPipeline": {"status": "success"},
            "resolvableDiscussionsCount": unresolved,
            "resolvedDiscussionsCount": 0}


def _threads(*authors):
    return json.dumps([
        {"id": f"t{i}", "notes": [{"body": "look at this", "system": False,
                                   "resolvable": True, "resolved": False,
                                   "author": {"username": a}}]}
        for i, a in enumerate(authors)])


def _probe_deps(store, raw, discussions=None, queue=None):
    d = _deps(store, raw, queue=queue)
    if discussions is not None:
        d["discussions"] = discussions
    return d


def _saved_items(store):
    saved = [p for p in store if isinstance(p, tuple) and p[0] == "saved"]
    return {r.item.id: r.item for r in saved[-1][1]} if saved else {}


def test_no_discussions_dep_never_probes_and_proposes_no_feedback_work():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=[_authored()])))
    assert rc == 0
    assert "feedback:pb-www!3997" not in _saved_items(posts)


def test_the_probe_rebinds_the_mr_before_the_assessor_sees_it():
    posts = []
    rc = run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("leyang", "me")))
    assert rc == 0
    item = _saved_items(posts)["feedback:pb-www!3997"]
    assert item.executor == "address-feedback"
    assert item.why == "1 unaddressed thread"      # "me" thread doesn't count
    assert item.branch == "chardy/1588-ranch-data"


def test_each_authored_mr_is_probed_exactly_once():
    calls = []
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored(iid=3997), _authored(iid=4001)]),
        discussions=lambda repo, iid: (calls.append((repo, iid)),
                                       _threads("leyang"))[1]))
    assert calls == [("pb-www", 3997), ("pb-www", 4001)]


def test_an_mr_with_no_unresolved_threads_is_never_probed():
    """Decision 1's cost gate: only authored MRs that have unresolved threads
    at all are worth a REST call."""
    calls = []
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored(iid=3997, unresolved=0)]),
        discussions=lambda repo, iid: (calls.append(iid), "[]")[1]))
    assert calls == []


def test_a_failing_probe_degrades_that_one_mr_and_never_aborts_the_sweep():
    """AC #7: one bad glab call must not cost the whole digest. The MR falls
    back to unaddressed_count 0 -- so a changes-requested MR still shows up,
    as the informational row."""
    posts = []

    def flaky(repo, iid):
        if iid == 3997:
            raise RuntimeError("glab api timed out after 30s")
        return _threads("leyang")

    rc = run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored(iid=3997, changes_requested=True),
                                    _authored(iid=4001)]),
        discussions=flaky))
    assert rc == 0
    items = _saved_items(posts)
    assert items["feedback:pb-www!3997"].executor == "triage"
    assert items["feedback:pb-www!3997"].why == "changes requested"
    assert items["feedback:pb-www!4001"].executor == "address-feedback"


def test_a_handed_off_mr_is_never_probed():
    calls = []
    posts = []
    node = _authored(iid=3997)
    node["approved"] = True
    node["detailedMergeStatus"] = "MERGEABLE"
    node["assignees"] = {"nodes": [{"username": "maintainer"}]}
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[node]),
        discussions=lambda repo, iid: (calls.append(iid), _threads("leyang"))[1]))
    assert calls == []
    assert "feedback:pb-www!3997" not in _saved_items(posts)


def test_run_sweep_wires_the_real_discussions_edge():
    from unittest.mock import patch
    from worksweep import __main__ as m
    from worksweep import collectors
    seen = {}
    with patch.object(m, "load_config", _cfg), \
            patch.object(m, "run_sweep",
                         lambda cfg, deps: (seen.update(deps=deps), 0)[1]):
        assert m.main([]) == 0
    assert seen["deps"]["discussions"] is collectors.collect_discussions


# --- probe failure and cleared signals (fix-mode round 2, 11 + 12) ---------

def _queue_rec(number, executor="address-feedback", status="proposed",
               why="2 unaddressed threads", iid=3997):
    from worksweep.models import QueueRecord, WorkItem
    return QueueRecord(
        number=number, first_seen="2026-08-25T00:00:00+00:00",
        last_seen="2026-08-25T00:00:00+00:00",
        item=WorkItem(schema_version=1, id=f"feedback:pb-www!{iid}",
                      repo="pb-www", kind="feedback", executor=executor,
                      risk="low", why=why,
                      web_url=f"https://gl/x/-/merge_requests/{iid}",
                      sha=f"s{iid}", status=status,
                      branch="chardy/1588-ranch-data"))


def _saved(store):
    saved = [p for p in store if isinstance(p, tuple) and p[0] == "saved"]
    return saved[-1][1] if saved else []


def test_a_failed_probe_keeps_the_row_it_cannot_re_derive():
    """A dropped row frees its number for reuse, and the highest number is
    reused first -- so a stale `✅ 12` on Chandler's phone would approve a
    completely different item."""
    def boom(repo, iid):
        raise RuntimeError("glab api timed out after 30s")

    posts = []
    prior = [_queue_rec(12)]
    rc = run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                       discussions=boom, queue=prior))
    assert rc == 0
    rows = {r.item.id: r for r in _saved(posts)}
    row = rows["feedback:pb-www!3997"]
    assert row.number == 12
    assert row.item.executor == "address-feedback"
    assert "probe failed" in row.item.why


def test_a_failed_probe_does_not_invent_a_row_that_never_existed():
    posts = []
    def boom(repo, iid):
        raise RuntimeError("nope")
    run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                  discussions=boom))
    assert "feedback:pb-www!3997" not in {r.item.id for r in _saved(posts)}


def test_a_failed_probe_never_overrides_a_row_the_assessor_still_emits():
    """changes_requested is known without the probe, so the informational arm
    is still derivable -- the retained row must not shadow it."""
    posts = []
    prior = [_queue_rec(12)]
    def boom(repo, iid):
        raise RuntimeError("nope")
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored(changes_requested=True)]),
        discussions=boom, queue=prior))
    rows = {r.item.id: r for r in _saved(posts)}
    assert rows["feedback:pb-www!3997"].item.executor == "triage"
    assert rows["feedback:pb-www!3997"].item.why == "changes requested"


def test_a_cleared_signal_closes_a_stranded_error_row():
    """W12: the run errored, then the reviewer resolved everything. Without
    this the ⚠️ row sits on the dashboard forever."""
    posts = []
    prior = [_queue_rec(12, status="error")]
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("me"), queue=prior))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "done"
    assert row.item.done_reason == "signal-cleared"


def test_a_failed_probe_leaves_an_errored_row_visible_not_closed():
    """We do not know what the threads say, so we may not call them settled.
    The row comes back as ordinary retryable work, flagged, rather than being
    closed as `signal-cleared` on a guess."""
    posts = []
    prior = [_queue_rec(12, status="error")]
    def boom(repo, iid):
        raise RuntimeError("nope")
    run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                  discussions=boom, queue=prior))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "proposed"
    assert row.item.done_reason != "signal-cleared"
    assert "probe failed" in row.item.why


def test_an_unwired_probe_never_reports_a_cleared_signal():
    """FALSIFYING. Without the probe dep every authored MR reads
    unaddressed_count 0 -- which is ignorance, not a cleared signal. Declaring
    it cleared would close error rows across the whole queue on no evidence."""
    posts = []
    prior = [_queue_rec(12, status="error")]
    run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=[_authored()]),
                            queue=prior))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "error"
    assert row.item.done_reason != "signal-cleared"


def test_changes_requested_is_never_a_cleared_signal():
    posts = []
    prior = [_queue_rec(12, status="error")]
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored(changes_requested=True)]),
        discussions=lambda repo, iid: _threads("me"), queue=prior))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status != "done"


# --- the chained re-review vs the next sweep (2026-08-26) ------------------
#
# The runner appends `magi:{repo}!{iid}@{sha}` as `approved`. The very next
# sweep assesses the same MR at the same head, and assess_own_mr proposes a
# magi item for exactly that sha -- so the two have to recognise each other.

AUTO_WHY = "post-feedback re-review (auto)"


def _chained_magi(number=13, iid=3997, sha=None, status="approved"):
    from worksweep.models import QueueRecord, WorkItem
    sha = sha or f"s{iid}"
    return QueueRecord(
        number=number, first_seen="2026-08-25T00:00:00+00:00",
        last_seen="2026-08-25T00:00:00+00:00",
        item=WorkItem(schema_version=1, id=f"magi:pb-www!{iid}@{sha}",
                      repo="pb-www", kind="mr", executor="magi-review",
                      risk="low", why=AUTO_WHY,
                      web_url=f"https://gl/x/-/merge_requests/{iid}", sha=sha,
                      status=status, title="Ranch data tab"))


def _magi_rows(store):
    return [r for r in _saved(store) if r.item.executor == "magi-review"]


def test_the_next_sweep_does_not_queue_a_second_review_of_the_same_head():
    """FALSIFYING. Without this the sweep proposes its own magi row for the
    same sha every single pass."""
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("me"),
        queue=[_chained_magi()]))
    rows = _magi_rows(posts)
    assert len(rows) == 1
    assert rows[0].item.id == "magi:pb-www!3997@s3997"


def test_the_auto_marker_survives_the_next_sweep():
    """The row must still say how it got approved -- a sweep that rewrites
    the why to "no magi-review yet" erases the only provenance there is."""
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("me"),
        queue=[_chained_magi()]))
    row = _magi_rows(posts)[0]
    assert row.item.why == AUTO_WHY
    assert row.item.status == "approved"       # and it is still claimable
    assert row.number == 13                    # keeping its handle


def test_a_running_review_also_suppresses_a_duplicate_proposal():
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("me"),
        queue=[_chained_magi(status="running")]))
    assert len(_magi_rows(posts)) == 1


def test_a_new_head_still_earns_a_fresh_proposal():
    """The guard is per-sha. A push after the review was queued is genuinely
    unreviewed code and must propose its own row."""
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("me"),
        queue=[_chained_magi(sha="olderhead")]))
    ids = {r.item.id for r in _magi_rows(posts)}
    assert "magi:pb-www!3997@s3997" in ids


def test_a_proposed_magi_row_is_still_re_proposed_every_sweep():
    """Only rows the queue RETAINS when they drop out (approved/running) may
    suppress their own proposal. A `proposed` one would be dropped outright
    and its number recycled."""
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("me"),
        queue=[_chained_magi(status="proposed")]))
    rows = _magi_rows(posts)
    assert len(rows) == 1
    assert rows[0].item.status == "proposed"
    assert rows[0].number == 13


# --- a parked question un-strands end to end (live finding, !4082) ---------

def _parked_feedback(number=12, status="needs-input", iid=3997):
    from worksweep.models import QueueRecord, WorkItem
    return QueueRecord(
        number=number, first_seen="2026-08-25T00:00:00+00:00",
        last_seen="2026-08-25T00:00:00+00:00",
        item=WorkItem(
            schema_version=1, id=f"feedback:pb-www!{iid}", repo="pb-www",
            kind="feedback", executor="address-feedback", risk="low",
            why="1 unaddressed thread",
            web_url=f"https://gl/x/-/merge_requests/{iid}", sha=f"s{iid}",
            status=status, error_summary="1 thread needs your call",
            branch="chardy/1588-ranch-data"))


def _bot_answered(tid="t1"):
    """Reviewer asks, Chandler answers, an access-token bot replies last."""
    bot = "group2846274botbb6bad6ee97bbb14c73a6e3e39ff610d"
    return json.dumps([{"id": tid, "notes": [
        {"body": "q", "system": False, "resolvable": True, "resolved": False,
         "author": {"username": "leyang"}},
        {"body": "answered", "system": False, "resolvable": True,
         "resolved": False, "author": {"username": "me"}},
        {"body": "analysis", "system": False, "resolvable": True,
         "resolved": False, "author": {"username": bot}}]}])


def test_the_bot_fix_un_strands_the_row_it_parked():
    """The two halves meeting: the bot filter clears the signal, and the row
    that was parked on that signal closes instead of asking forever."""
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _bot_answered(),
        queue=[_parked_feedback()]))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "done"
    assert row.item.done_reason == "signal-cleared"
    assert row.number == 12


def test_a_parked_row_whose_threads_still_wait_keeps_asking():
    """FALSIFYING the other direction: the reviewer's question is still the
    last word, so the row must stay parked and keep its summary."""
    posts = []
    run_sweep(_cfg(), _probe_deps(
        posts, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("leyang"),
        queue=[_parked_feedback()]))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "needs-input"
    assert row.item.error_summary == "1 thread needs your call"


def test_a_parked_row_is_not_closed_when_the_probe_could_not_look():
    posts = []
    def boom(repo, iid):
        raise RuntimeError("glab timed out")
    run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                  discussions=boom,
                                  queue=[_parked_feedback()]))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.done_reason != "signal-cleared"


# --- f-006: a network blip must not spend a ✅ (tribunal, 2026-08-26) ------
#
# `_retained_feedback` hard-set status="proposed" on the carried-forward row.
# So a transient probe failure silently discarded a human approval -- and the
# only signal was the row quietly reappearing as unapproved.

def test_a_probe_blip_does_not_discard_an_approval():
    """FALSIFYING. The ✅ is the whole consent mechanism; losing one to a
    network error means work the human sanctioned silently stops happening."""
    posts = []
    def boom(repo, iid):
        raise RuntimeError("glab api timed out after 30s")
    run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                  discussions=boom,
                                  queue=[_queue_rec(12, status="approved")]))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "approved"
    assert "probe failed" in row.item.why
    assert row.number == 12


def test_a_probe_blip_does_not_unpark_a_question():
    posts = []
    def boom(repo, iid):
        raise RuntimeError("nope")
    run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                  discussions=boom,
                                  queue=[_queue_rec(12, status="needs-input")]))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "needs-input"


def test_a_probe_blip_leaves_a_proposed_row_proposed():
    posts = []
    def boom(repo, iid):
        raise RuntimeError("nope")
    run_sweep(_cfg(), _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                                  discussions=boom,
                                  queue=[_queue_rec(12, status="proposed")]))
    row = {r.item.id: r for r in _saved(posts)}["feedback:pb-www!3997"]
    assert row.item.status == "proposed"


def test_the_probe_failed_marker_does_not_expire_the_approval():
    """The consent rule compares why-strings for address-feedback rows, so the
    marker itself would otherwise read as "the ask changed" and reset the ✅
    on the very next sweep -- turning a one-sweep blip into a lost approval
    anyway, one step later."""
    posts = []
    def boom(repo, iid):
        raise RuntimeError("nope")
    # the ask itself is unchanged across both sweeps (one thread, still
    # waiting) -- the ONLY difference is worksweep's own "(probe failed)" note
    deps = _probe_deps(posts, _gql(authored_nodes=[_authored()]),
                       discussions=boom,
                       queue=[_queue_rec(12, status="approved",
                                         why="1 unaddressed thread")])
    run_sweep(_cfg(), deps)
    blipped = _saved(posts)

    # next sweep: the probe recovers and re-derives the same ask
    posts2 = []
    run_sweep(_cfg(), _probe_deps(
        posts2, _gql(authored_nodes=[_authored()]),
        discussions=lambda repo, iid: _threads("leyang"), queue=blipped))
    row = {r.item.id: r for r in _saved(posts2)}["feedback:pb-www!3997"]
    assert row.item.status == "approved"
    assert "probe failed" not in row.item.why


def test_the_retained_row_does_not_assert_a_status_of_its_own():
    """Belt to reconcile's braces. Reconcile takes the PRIOR record's status on
    the consent path, so this function's status is normally overridden -- but
    it must not claim a demotion it does not own, or the next person to change
    reconcile's precedence re-introduces f-006 without touching this file."""
    from worksweep.__main__ import _retained_feedback
    for status in ("approved", "needs-input", "proposed", "running"):
        prior = _queue_rec(12, status=status,
                           why="1 unaddressed thread").item
        assert _retained_feedback(prior).status == status


def test_the_probe_failed_marker_is_added_once_not_stacked():
    """Two blips in a row must not read "(probe failed) (probe failed)"."""
    from worksweep.__main__ import _retained_feedback
    prior = _queue_rec(12, status="approved", why="1 unaddressed thread").item
    once = _retained_feedback(prior)
    twice = _retained_feedback(once)
    assert once.why == "1 unaddressed thread (probe failed)"
    assert twice.why == once.why
