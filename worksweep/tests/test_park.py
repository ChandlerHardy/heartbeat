"""The `park` executor: branch onto a free dev box, then link it from the MR.

Every edge is injected — this file must never touch ssh, http or glab.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep import park  # noqa: E402
from worksweep.devslots import DevBox  # noqa: E402
from worksweep.models import WorkItem  # noqa: E402
from worksweep.runner import RunnerError  # noqa: E402

DEV_URL = "https://dev2.chandlerhardy-dev.performancebeef.com/"


def _item(branch="chardy/1588-ranch-data", iid=4078, repo="pb-www"):
    return WorkItem(schema_version=1, id=f"hygiene-devurl:{repo}!{iid}",
                    repo=repo, kind="mr", executor="park", risk="low",
                    why="description missing dev-server link",
                    web_url=f"https://gl/x/-/merge_requests/{iid}",
                    sha="abc123", status="approved", title="t", branch=branch)


def _box(name="dev2", tier="free", url=DEV_URL, branch="other", sha="s0"):
    return DevBox(name=name, host="chandlerhardy-dev", path="/p/pb-www",
                  url=url, branch=branch, sha=sha, tier=tier)


class _Glab:
    """Records every glab invocation; serves a description on the GET."""

    def __init__(self, description="", fail_on=None):
        self.calls, self.description, self.fail_on = [], description, fail_on

    def __call__(self, args, body=None):
        self.calls.append((list(args), body))
        if self.fail_on and self.fail_on in " ".join(args):
            raise RuntimeError("glab exited 1: 403 Forbidden")
        if "-X" in args:                     # the PUT
            return "{}"
        return json.dumps({"description": self.description})

    @property
    def puts(self):
        return [c for c in self.calls if "-X" in c[0]]


def _edges(glab=None, ssh=None, http=None, synced=None):
    """Injected edges; `synced` collects sync_to_box calls."""
    synced = synced if synced is not None else []

    def fake_ssh(host, command):
        synced.append((host, command))
        return "chardy/1588-ranch-data\nnewsha123"
    return dict(run_ssh=ssh or fake_ssh,
                http_get=http or (lambda url: 200),
                run_glab=glab or _Glab())


# --- the five steps ---------------------------------------------------------

def test_park_syncs_the_branch_and_updates_the_description(monkeypatch):
    glab = _Glab(description="Some existing body.")
    calls = {}

    def fake_sync(box, branch, run_ssh, http_get, **kw):
        calls.update(box=box, branch=branch)
        return "newsha123"
    monkeypatch.setattr(park.implementer, "sync_to_box", fake_sync)

    result = park.execute(_item(), None, [_box()], **_edges(glab=glab))

    assert calls["branch"] == "chardy/1588-ranch-data"
    assert calls["box"].name == "dev2"
    assert result.box_name == "dev2"
    assert result.dev_url == DEV_URL
    assert result.result_sha == "newsha123"
    assert result.description_updated is True

    # exactly one GET then one PUT, and the PUT carries a JSON BODY
    assert len(glab.calls) == 2
    put_args, put_body = glab.puts[0]
    assert "-X" in put_args and put_args[put_args.index("-X") + 1] == "PUT"
    assert "--input" in put_args and put_args[put_args.index("--input") + 1] == "-"
    assert "Content-Type: application/json" in put_args
    assert put_body is not None
    sent = json.loads(put_body)["description"]
    assert sent.startswith(f"### Available on [{DEV_URL}]({DEV_URL})")
    assert sent.endswith("Some existing body.")


def test_park_never_sends_the_description_as_a_glab_field():
    """Falsifying: `-f description=...` is the 2026-08 array bug -- glab's own
    help says neither --field nor --raw-field parses JSON, so a description
    full of newlines and markdown arrives mangled."""
    glab = _Glab()
    park.put_description(glab, "pb-www", 4078, "line1\n\n### h [a](b)\nline3")
    args, body = glab.puts[0]
    assert "-f" not in args and "--field" not in args and "--raw-field" not in args
    assert json.loads(body)["description"] == "line1\n\n### h [a](b)\nline3"


def test_park_verifies_http_200_before_touching_the_description(monkeypatch):
    """Order matters: a failed sync must never leave the MR advertising a dev
    URL that serves an error page."""
    glab = _Glab()

    def boom_sync(box, branch, run_ssh, http_get, **kw):
        raise RunnerError("dev2 returned HTTP 502 after sync")
    monkeypatch.setattr(park.implementer, "sync_to_box", boom_sync)

    with pytest.raises(RunnerError) as e:
        park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert "502" in str(e.value)
    assert glab.calls == []                  # description untouched


def test_park_errors_with_a_clear_summary_when_no_slot_is_free():
    """Falsifying: without the guard, select_slot returning None would blow up
    somewhere less legible and the human would get no instruction."""
    with pytest.raises(RunnerError) as e:
        park.execute(_item(), None, [_box(tier="busy")], **_edges())
    msg = str(e.value)
    assert "no free dev slot" in msg
    assert "4078" in msg
    assert "re-proposes itself next sweep" in msg


def test_park_refuses_without_a_branch():
    with pytest.raises(RunnerError) as e:
        park.execute(_item(branch=""), None, [_box()], **_edges())
    assert "no source branch" in str(e.value)


def test_park_refuses_when_an_edge_is_missing():
    for missing in ("run_ssh", "http_get", "run_glab"):
        edges = _edges()
        edges[missing] = None
        with pytest.raises(RunnerError) as e:
            park.execute(_item(), None, [_box()], **edges)
        assert "wired without" in str(e.value)


# --- the skip-the-PUT rule --------------------------------------------------

def test_park_skips_the_put_when_a_dev_url_is_already_present(monkeypatch):
    """Falsifying: without the check, every re-park stacks another header on
    the description."""
    glab = _Glab(description="intro\n\nAvailable on "
                             "https://dev5.chandlerhardy-dev.performancebeef.com/\n")
    monkeypatch.setattr(park.implementer, "sync_to_box",
                        lambda *a, **k: "newsha123")
    result = park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert result.description_updated is False
    assert glab.puts == []                   # read, then left alone
    assert len(glab.calls) == 1


@pytest.mark.parametrize("description,expected_none", [
    ("", False),
    ("no link here", False),
    ("see https://dev2.chandlerhardy-dev.performancebeef.com/", True),
    ("### Available on [https://x-dev4.performancebeef.com/](x)", True),
])
def test_prepend_header_decision(description, expected_none):
    out = park.prepend_header(description, DEV_URL)
    assert (out is None) is expected_none


def test_prepend_header_shape():
    assert park.prepend_header("", DEV_URL) == \
        f"### Available on [{DEV_URL}]({DEV_URL})"
    assert park.prepend_header("body", DEV_URL) == \
        f"### Available on [{DEV_URL}]({DEV_URL})\n\nbody"
    # the header goes FIRST -- a reviewer should not have to scroll for it
    out = park.prepend_header("body", DEV_URL)
    assert out.startswith("### ")
    assert out.index("Available on") < out.index("body")


# --- failure surfaces -------------------------------------------------------

def test_a_glab_put_failure_is_an_error_not_a_silent_pass(monkeypatch):
    """Falsifying: swallowing this would report a park as done while the MR
    still has no link -- the exact silent-nag problem this executor removes."""
    monkeypatch.setattr(park.implementer, "sync_to_box",
                        lambda *a, **k: "newsha123")
    glab = _Glab(fail_on="-X")
    with pytest.raises(RuntimeError) as e:
        park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert "403" in str(e.value)


def test_a_glab_get_failure_surfaces(monkeypatch):
    monkeypatch.setattr(park.implementer, "sync_to_box",
                        lambda *a, **k: "newsha123")
    glab = _Glab(fail_on="merge_requests")
    with pytest.raises(RuntimeError):
        park.execute(_item(), None, [_box()], **_edges(glab=glab))


def test_an_unparseable_description_payload_is_a_runner_error(monkeypatch):
    monkeypatch.setattr(park.implementer, "sync_to_box",
                        lambda *a, **k: "newsha123")
    with pytest.raises(RunnerError) as e:
        park.execute(_item(), None, [_box()],
                     **_edges(glab=lambda args, body=None: "not json"))
    assert "description" in str(e.value)


# --- the done message -------------------------------------------------------

def test_done_message_shape():
    msg = park.done_message(park.ParkResult(
        iid=4078, box_name="dev2", dev_url=DEV_URL, result_sha="s",
        description_updated=True))
    assert msg.startswith("🅿️ !4078 parked on dev2 (200) · description updated")
    assert f"<{DEV_URL}>" in msg


def test_done_message_says_when_the_description_was_left_alone():
    msg = park.done_message(park.ParkResult(
        iid=4078, box_name="dev2", dev_url=DEV_URL, description_updated=False))
    assert "already had a dev link" in msg


def test_mr_path_is_url_encoded():
    assert park._mr_path("pb-www", 4078) == (
        "projects/performancelivestock%2Fpb-www/merge_requests/4078")
