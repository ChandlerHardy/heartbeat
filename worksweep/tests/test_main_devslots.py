"""M4 Task F: run_sweep wiring for dev-slot sensing.

Dev-slot sensing is entirely opt-in: `cfg.dev_boxes` empty (the default)
means run_sweep never touches `deps["ssh"]` at all, so every pre-M4 test
(none of which provide an "ssh" dep) keeps passing unmodified.
"""
import json

from worksweep.__main__ import run_sweep
from worksweep.config import WorksweepConfig
from worksweep.models import QueueRecord, WorkItem


def _cfg(dev_boxes=()):
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y",
                           dev_boxes=dev_boxes)


def _gql(review_nodes=(), authored_nodes=(), assigned_nodes=()):
    return json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": list(review_nodes)},
        "authoredMergeRequests": {"nodes": list(authored_nodes)},
        "assignedMergeRequests": {"nodes": list(assigned_nodes)}}}})


def _node(iid=1, state="UNREVIEWED", source_branch=""):
    return {"iid": str(iid), "title": "t", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{iid}",
            "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-07T00:00:00Z",
            "sourceBranch": source_branch,
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "other"},
            "reviewers": {"nodes": [
                {"username": "me", "mergeRequestInteraction": {"reviewState": state}}]},
            "headPipeline": None}


def _deps(store, raw, queue=None, ssh=None):
    d = {
        "graphql": lambda: raw,
        "todos": lambda: [],
        "issues": lambda repo, user: [],
        "post": lambda hook, content: store.append(content),
        "load": lambda: list(queue or []),
        "save": lambda records: store.append(("saved", records)),
        "now": lambda: "2026-08-07T12:00:00+00:00",
    }
    if ssh is not None:
        d["ssh"] = ssh
    return d


_BOXES = ({"name": "dev1", "host": "h1", "path": "/p1", "url": "u1"},)


def test_no_dev_boxes_never_calls_ssh():
    posts = []

    def boom(host, cmd):
        raise AssertionError("ssh should not be called when dev_boxes is empty")

    rc = run_sweep(_cfg(dev_boxes=()), _deps(posts, _gql(review_nodes=[_node()]), ssh=boom))
    assert rc == 0


def test_no_ssh_dep_degrades_gracefully_when_dev_boxes_configured(capsys):
    posts = []
    rc = run_sweep(_cfg(dev_boxes=_BOXES), _deps(posts, _gql(review_nodes=[_node()])))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) >= 1
    assert "Dev slots:" not in texts[0]


def test_slot_line_appears_in_raw_digest():
    posts = []
    rc = run_sweep(_cfg(dev_boxes=_BOXES),
                   _deps(posts, _gql(review_nodes=[_node()]),
                        ssh=lambda h, c: "feat/999-orphan\nsha1\n"))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    joined = "\n".join(texts)
    assert "Dev slots: dev1 free" in joined


def test_slot_line_reflects_handed_off_branch():
    posts = []
    handed = _node(iid=4007, state="APPROVED", source_branch="feat/1701-thing")
    # `authored` bucket (not review) drives is_handed_off (needs approved +
    # MERGEABLE + a non-author assignee) -- build one directly via the raw
    # GraphQL doc so `approved`/`detailedMergeStatus`/`assignees` are set.
    raw = json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": [{
            "iid": "4007", "title": "t", "draft": False,
            "webUrl": "https://gl/x/-/merge_requests/4007",
            "diffHeadSha": "s4007", "updatedAt": "2026-08-07T00:00:00Z",
            "description": "", "sourceBranch": "feat/1701-thing",
            "approved": True, "detailedMergeStatus": "MERGEABLE",
            "assignees": {"nodes": [{"username": "maintainer"}]},
            "project": {"fullPath": "performancelivestock/pb-www"},
            "author": {"username": "me"},
            "reviewers": {"nodes": []}, "headPipeline": {"status": "success"},
            "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0,
        }]},
        "assignedMergeRequests": {"nodes": []}}}})
    rc = run_sweep(_cfg(dev_boxes=_BOXES),
                   _deps(posts, raw, ssh=lambda h, c: "feat/1701-thing\nsha1\n"))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    joined = "\n".join(texts)
    assert "dev1 reclaimable" in joined


def test_claimed_box_excluded_even_if_branch_would_be_free():
    posts = []
    running_item = WorkItem(schema_version=1, id="issue:pb-www#1", repo="pb-www",
                            kind="issue", executor="implement", risk="low",
                            why="assigned issue: x", web_url="https://gl/x/-/issues/1",
                            sha="", status="running", dev_box="dev1")
    queue = [QueueRecord(number=1, item=running_item,
                         first_seen="t0", last_seen="t0")]
    rc = run_sweep(_cfg(dev_boxes=_BOXES),
                   _deps(posts, _gql(review_nodes=[_node(iid=2)]), queue=queue,
                        ssh=lambda h, c: "feat/999-orphan\nsha1\n"))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    joined = "\n".join(texts)
    assert "dev1 live" in joined
    assert "dev1 free" not in joined


def test_ssh_probe_exception_degrades_gracefully_no_crash(capsys):
    posts = []

    def boom(h, c):
        raise RuntimeError("ssh h1 timed out after 20s")

    rc = run_sweep(_cfg(dev_boxes=_BOXES),
                   _deps(posts, _gql(review_nodes=[_node()]), ssh=boom))
    assert rc == 0
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) >= 1
    # unreachable box -> unknown branch -> live tier, never crashes the sweep
    joined = "\n".join(texts)
    assert "Dev slots: dev1 live" in joined


def test_curated_message_keeps_slot_line_and_footer_under_cap(monkeypatch):
    """A near-cap curated body + a long 6-box slot line must never truncate the
    header, slot line, or the ✅ footer — only the LLM body may be trimmed."""
    from worksweep import __main__ as m
    from worksweep.config import WorksweepConfig
    from worksweep.formatter import DISCORD_MAX_CHARS, _FOOTER
    import json

    cfg = WorksweepConfig(
        repos=("pb-www",), username="me",
        discord_webhook="https://discord.com/api/webhooks/x/y",
        curate=True,
        dev_boxes=tuple({"name": f"dev{i}", "host": "h", "path": f"/p{i}",
                         "url": f"https://dev{i}.example/"} for i in range(6)))
    review_node = {"iid": "1", "title": "t", "draft": False,
                   "webUrl": "https://gl/x/-/merge_requests/1",
                   "diffHeadSha": "s1", "updatedAt": "2026-08-17T00:00:00Z",
                   "sourceBranch": "feat/x",
                   "project": {"fullPath": "performancelivestock/pb-www"},
                   "author": {"username": "other"},
                   "reviewers": {"nodes": [{"username": "me",
                       "mergeRequestInteraction": {"reviewState": "UNREVIEWED"}}]},
                   "headPipeline": None}
    raw = json.dumps({"data": {"currentUser": {"username": "me",
        "reviewRequestedMergeRequests": {"nodes": [review_node]},
        "authoredMergeRequests": {"nodes": []},
        "assignedMergeRequests": {"nodes": []}}}})
    posts = []
    # every box "free": ssh returns master + a sha -> long slot line
    deps = {"graphql": lambda: raw, "todos": lambda: [],
            "issues": lambda repo, u: [], "post": lambda h, c: posts.append(c),
            "load": lambda: [], "save": lambda r: None,
            "now": lambda: "2026-08-17T12:00:00+00:00",
            "ssh": lambda host, cmd: "master\nabc123\n",
            # LLM returns a valid, near-cap body that cites the required number
            "llm": lambda prompt: ("Needs your review:\n1. pb-www !1 -- t -- review requested\n"
                                   + ("x" * 1600))}
    assert m.run_sweep(cfg, deps) == 0
    msg = [p for p in posts if isinstance(p, str)][0]
    assert len(msg.encode("utf-8")) <= DISCORD_MAX_CHARS
    assert msg.startswith("### 🔭")
    assert "Dev slots:" in msg
    assert msg.rstrip().endswith(_FOOTER.rstrip())
