"""M4 Task H: run_sweep wiring for the keep-current stale sensor, and `run`
subcommand wiring for the keep-current executor.
"""
import json
from unittest.mock import patch

from worksweep.__main__ import run_sweep
from worksweep.config import WorksweepConfig
from worksweep.keepcurrent import KeepCurrentResult


def _cfg(**kw):
    base = dict(repos=("pb-www",), username="me",
               discord_webhook="https://discord.com/api/webhooks/x/y",
               stale_threshold=5)
    base.update(kw)
    return WorksweepConfig(**base)


def _authored_node(iid=4020, source_branch="feat/1701-thing", approved=False,
                   merge_status="", assignees=()):
    return {"iid": str(iid), "title": "Feed schedule tweak", "draft": False,
           "webUrl": f"https://gl/x/-/merge_requests/{iid}",
           "diffHeadSha": f"s{iid}", "updatedAt": "2026-08-18T00:00:00Z",
           "description": "", "sourceBranch": source_branch,
           "approved": approved, "detailedMergeStatus": merge_status,
           "assignees": {"nodes": [{"username": a} for a in assignees]},
           "project": {"fullPath": "performancelivestock/pb-www"},
           "author": {"username": "me"},
           "reviewers": {"nodes": []}, "headPipeline": {"status": "success"},
           "resolvableDiscussionsCount": 0, "resolvedDiscussionsCount": 0}


def _gql(authored_nodes=()):
    return json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": list(authored_nodes)},
        "assignedMergeRequests": {"nodes": []}}}})


def _deps(store, raw, diverged_commits=None, queue=None):
    d = {
        "graphql": lambda: raw,
        "todos": lambda: [],
        "issues": lambda repo, user: [],
        "post": lambda hook, content: store.append(content),
        "load": lambda: list(queue or []),
        "save": lambda records: store.append(("saved", records)),
        "now": lambda: "2026-08-18T12:00:00+00:00",
    }
    if diverged_commits is not None:
        d["diverged_commits"] = diverged_commits
    return d


def _texts(posts):
    return [p for p in posts if isinstance(p, str)]


def test_no_diverged_commits_dep_never_touches_it():
    """Same opt-in pattern as ssh (Task F): a caller that provides no
    diverged_commits dep must not crash, and simply gets no stale items."""
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=[_authored_node()])))
    assert rc == 0


def test_stale_mr_over_threshold_produces_a_stale_item():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=[_authored_node()]),
                                 diverged_commits=lambda repo, iid: 7))
    assert rc == 0
    joined = "\n".join(_texts(posts))
    assert "commits behind master" in joined
    assert "4020" in joined


def test_stale_mr_under_threshold_produces_nothing():
    posts = []
    rc = run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=[_authored_node()]),
                                 diverged_commits=lambda repo, iid: 2))
    assert rc == 0
    joined = "\n".join(_texts(posts))
    assert "commits behind master" not in joined


def test_handed_off_mr_never_gets_a_diverged_commits_call():
    """Task H's contract: an authored MR that's already handed off (approved
    + MERGEABLE + assigned to someone else) is exempt -- the maintainer will
    merge it, so it must not even trigger the REST call, let alone produce a
    stale item, no matter how far behind master it is."""
    calls = []

    def diverged_commits(repo, iid):
        calls.append((repo, iid))
        return 999          # would obviously be stale if it were checked

    posts = []
    handed_off_node = _authored_node(
        iid=4021, approved=True, merge_status="MERGEABLE",
        assignees=("maintainer",))
    rc = run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=[handed_off_node]),
                                 diverged_commits=diverged_commits))
    assert rc == 0
    assert calls == []
    joined = "\n".join(_texts(posts))
    assert "commits behind master" not in joined


def test_diverged_commits_failure_for_one_mr_does_not_kill_the_sweep():
    posts = []

    def flaky(repo, iid):
        if iid == 4020:
            raise RuntimeError("glab api timed out after 30s")
        return 8

    nodes = [_authored_node(iid=4020), _authored_node(iid=4021)]
    rc = run_sweep(_cfg(), _deps(posts, _gql(authored_nodes=nodes),
                                 diverged_commits=flaky))
    assert rc == 0
    joined = "\n".join(_texts(posts))
    assert "4021" in joined
    assert "commits behind master" in joined


# --------------------------------------------------------------------------
# `run` subcommand wiring
# --------------------------------------------------------------------------

def test_run_subcommand_wires_keep_current_executor(tmp_path):
    from worksweep import __main__ as m
    seen = {}

    def fake_run_once(cfg, deps, *a, **kw):
        seen["deps"] = deps
        return 0

    cfgfile = tmp_path / "hb.json"
    cfgfile.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                       '"runner": {"checkouts_root": "/co"}}')
    real_load = m.load_config
    with patch.object(m, "load_config", lambda: real_load(str(cfgfile))), \
        patch("worksweep.runner.run_once", fake_run_once):
        assert m.main(["run"]) == 0
    assert "execute_keep_current" in seen["deps"]


def test_dry_run_keep_current_touches_nothing():
    from worksweep import __main__ as m
    from worksweep.models import WorkItem
    item = WorkItem(schema_version=1, id="stale:pb-www!4020", repo="pb-www",
                    kind="stale", executor="keep-current", risk="low", why="w",
                    web_url="https://gl/x/-/merge_requests/4020", sha="s4020",
                    status="approved", branch="feat/1701-thing")
    result = m._dry_run_keep_current(item, _cfg())
    assert isinstance(result, KeepCurrentResult)
    assert result.iid == 4020
    assert result.box_name == ""
    assert result.scss_recompiled is False
    assert result.result_sha == "s4020"
