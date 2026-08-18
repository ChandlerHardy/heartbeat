"""M4 Task G: `worksweep run` wiring for the implement executor.

The point of these tests is that the CLI hands the runner the edges it needs
(and only edges): no network, no ssh, no subprocess is reached here.
"""
import urllib.error
from unittest.mock import patch

import pytest

from worksweep import __main__ as m
from worksweep.config import WorksweepConfig
from worksweep.devslots import DevBox
from worksweep.models import WorkItem


def _cfg(dev_boxes=()):
    return WorksweepConfig(repos=("pb-www",), username="me",
                           discord_webhook="https://discord.com/api/webhooks/x/y",
                           checkouts_root="/co", dev_boxes=dev_boxes)


def _item():
    return WorkItem(schema_version=1, id="issue:pb-www#1775", repo="pb-www",
                    kind="issue", executor="implement", risk="low", why="w",
                    web_url="https://gl/x/-/issues/1775", sha="s",
                    status="approved", title="Add cost page inline validation")


class _Resp:
    status = 200

    def read(self, n=None):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_status_returns_code():
    with patch("urllib.request.urlopen", return_value=_Resp()):
        assert m.http_status("https://dev1.x/") == 200


def test_http_status_maps_http_error_to_its_code():
    err = urllib.error.HTTPError("https://dev1.x/", 502, "bad gateway", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        assert m.http_status("https://dev1.x/") == 502


def test_http_status_propagates_non_http_failures():
    """A DNS/connection failure is NOT a status code — it must raise so
    sync_to_box reports 'health check failed' rather than a fake 200."""
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("no route to host")):
        with pytest.raises(urllib.error.URLError):
            m.http_status("https://dev1.x/")


def test_implement_boxes_is_empty_without_dev_boxes():
    def boom(*a, **kw):
        raise AssertionError("must not probe/sweep when dev_boxes is empty")

    with patch.object(m.devslots, "probe", boom), \
         patch.object(m.collectors, "run_graphql_sweep", boom):
        assert m._implement_boxes(_cfg()) == []


def test_implement_boxes_annotates_and_excludes_claimed_boxes(tmp_path):
    from worksweep.models import QueueRecord
    import dataclasses
    import json as _json

    boxes = [DevBox(name="dev1", host="h", path="/p", url="u1", branch="master"),
             DevBox(name="dev2", host="h", path="/p", url="u2", branch="master")]
    claimed = QueueRecord(number=1, first_seen="", last_seen="",
                          item=dataclasses.replace(_item(), status="running",
                                                   dev_box="dev2"))
    qpath = tmp_path / "queue.json"
    from worksweep.queue import save_queue
    save_queue(str(qpath), [claimed])
    raw = _json.dumps({"data": {"currentUser": {
        "username": "me", "reviewRequestedMergeRequests": {"nodes": []},
        "authoredMergeRequests": {"nodes": []},
        "assignedMergeRequests": {"nodes": []}}}})

    with patch.object(m.devslots, "probe", lambda cfgs, ssh: boxes), \
         patch.object(m.collectors, "run_graphql_sweep", lambda: raw), \
         patch.object(m, "_queue_path", lambda: str(qpath)):
        out = m._implement_boxes(_cfg(dev_boxes=({"name": "dev1"},)))
    tiers = {b.name: b.tier for b in out}
    assert tiers == {"dev1": "free", "dev2": "live"}   # dev2 is claimed


def test_dry_run_implement_touches_nothing():
    box = DevBox(name="dev1", host="h", path="/p", url="https://dev1.x/",
                 branch="master", tier="free")
    result = m._dry_run_implement(_item(), _cfg(), [box])
    assert result.mr_iid == 0 and result.mr_url == ""
    assert result.branch == "feat/1775-add-cost-page-inline-validation"
    assert "dry-run" in result.magi_note


def test_run_subcommand_wires_both_executors(tmp_path):
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
    deps = seen["deps"]
    for key in ("load", "save", "post", "now", "execute", "boxes",
                "execute_implement"):
        assert key in deps, f"run deps missing {key}"


def test_run_ssh_pins_stdin_to_devnull():
    """C1's sibling: ssh must not hand the runner's stdin to the remote."""
    import subprocess
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    with patch("subprocess.run", fake_run):
        assert m.run_ssh("host", "hostname") == "ok\n"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["cmd"][:2] == ["ssh", "host"]
