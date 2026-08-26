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
    """Records every glab invocation; serves the MR on the GET.

    STATEFUL: a PUT changes what the next GET returns, because park now reads
    the description back to prove the write landed (f-027). A stub that served
    a frozen description would make that read-back untestable -- and stubs
    that hid an omission are exactly what let f-026 survive thirteen tests.
    """

    def __init__(self, description="", fail_on=None, sha="mrhead1"):
        self.calls, self.description, self.fail_on = [], description, fail_on
        self.sha = sha

    def __call__(self, args, body=None):
        self.calls.append((list(args), body))
        if self.fail_on and self.fail_on in " ".join(args):
            raise RuntimeError("glab exited 1: 403 Forbidden")
        if "-X" in args:                     # the PUT
            self.description = json.loads(body)["description"]
            return "{}"
        return json.dumps({"description": self.description, "sha": self.sha})

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

    # GET (description + head sha), PUT, then the read-back that proves the
    # PUT landed (f-027). The PUT carries a JSON BODY.
    assert len(glab.calls) == 3
    assert len(glab.puts) == 1
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
    # park READS the MR first now (it needs the head sha to hold the sync to),
    # but it must still never WRITE: no PUT, description untouched.
    assert glab.puts == []


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
    # the link must name THIS box: a link to a DIFFERENT box is now retargeted
    # rather than skipped (f-024), which has its own tests below.
    glab = _Glab(description=f"intro\n\nAvailable on {DEV_URL}\n")
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
        description_updated=True, http_status=200))
    assert msg.startswith("🅿️ !4078 parked on dev2 (200) · description updated")
    assert f"<{DEV_URL}>" in msg


def test_done_message_says_when_the_description_was_left_alone():
    msg = park.done_message(park.ParkResult(
        iid=4078, box_name="dev2", dev_url=DEV_URL, description_updated=False,
        http_status=200))
    assert "already had this dev link" in msg


def test_mr_path_is_url_encoded():
    assert park._mr_path("pb-www", 4078) == (
        "projects/performancelivestock%2Fpb-www/merge_requests/4078")


# --- f-026 / f-027 / f-024: park must prove what it reports ---------------
#
# The tribunal found park claiming three things it never checked: that the
# branch landed (sync args omitted, so the sha gate inside sync_to_box silently
# skipped), that the description was updated (PUT-didn't-raise), and that the
# box serves 200 (a format literal). The old stubs swallowed **kw, which is
# exactly why the omission survived thirteen tests.


class _StrictSync:
    """A sync_to_box stand-in that accepts ONLY the real signature.

    No **kw: a caller that omits expected_sha/claim_branch/claim_sha fails
    here loudly instead of silently skipping the verification it claims to do.
    """

    def __init__(self, landed="newsha123"):
        self.landed, self.calls = landed, []

    def __call__(self, box, branch, run_ssh, http_get, expected_sha,
                 claim_branch, claim_sha):
        self.calls.append(dict(box=box, branch=branch,
                               expected_sha=expected_sha,
                               claim_branch=claim_branch, claim_sha=claim_sha))
        return self.landed


def _mr_json(description="", sha="mrhead1"):
    return json.dumps({"description": description, "sha": sha})


class _Mr:
    """glab stub serving a real MR payload, recording PUTs and re-reads."""

    def __init__(self, description="", sha="mrhead1", put_lands=True):
        self.description, self.sha, self.put_lands = description, sha, put_lands
        self.calls = []

    def __call__(self, args, body=None):
        self.calls.append((list(args), body))
        if "-X" in args:
            if self.put_lands:
                self.description = json.loads(body)["description"]
            return "{}"
        return _mr_json(self.description, self.sha)

    @property
    def puts(self):
        return [c for c in self.calls if "-X" in c[0]]

    @property
    def gets(self):
        return [c for c in self.calls if "-X" not in c[0]]


def test_park_proves_the_branch_actually_landed(monkeypatch):
    """f-026. The adjacent comment claimed "returns only on a branch that
    actually landed" -- but with expected_sha omitted, sync_to_box's sha gate
    is skipped entirely. Every other caller passes it."""
    sync = _StrictSync()
    monkeypatch.setattr(park.implementer, "sync_to_box", sync)
    glab = _Mr(sha="mrhead1")
    park.execute(_item(), None, [_box(branch="other", sha="boxsha0")],
                 **_edges(glab=glab))
    call = sync.calls[0]
    assert call["expected_sha"] == "mrhead1"      # the MR's own head
    assert call["claim_branch"] == "other"        # what the probe just saw
    assert call["claim_sha"] == "boxsha0"


def test_park_reads_the_description_back_after_writing_it(monkeypatch):
    """f-027. description_updated was True purely because the PUT did not
    raise -- the same shape-not-effect asymmetry this arc fixed for feedback."""
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    glab = _Mr(description="intro")
    result = park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert result.description_updated is True
    assert len(glab.gets) == 2                   # read, PUT, read back
    assert DEV_URL in glab.description


def test_a_put_that_does_not_stick_is_an_error(monkeypatch):
    """GitLab accepting the PUT is not the same as the description changing."""
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    glab = _Mr(description="intro", put_lands=False)
    with pytest.raises(RunnerError) as e:
        park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert "did not stick" in str(e.value) or "read back" in str(e.value)


def test_the_done_message_reports_the_status_it_measured(monkeypatch):
    """f-027. `(200)` was a format literal, and http_get was dead in all 13
    park tests -- nothing proved park even wired the edge."""
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    probes = []
    result = park.execute(_item(), None, [_box()],
                          **_edges(glab=_Mr(),
                                   http=lambda url: (probes.append(url), 200)[1]))
    assert probes == [DEV_URL]
    assert result.http_status == 200
    assert "(200)" in park.done_message(result)


def test_a_box_that_stops_serving_between_sync_and_report_is_an_error(
        monkeypatch):
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    with pytest.raises(RunnerError) as e:
        park.execute(_item(), None, [_box()],
                     **_edges(glab=_Mr(), http=lambda url: 502))
    assert "502" in str(e.value)


# --- f-024: re-parking onto a different box -------------------------------

OTHER_URL = "https://dev5.chandlerhardy-dev.performancebeef.com/"


def test_a_link_to_a_different_box_is_retargeted(monkeypatch):
    """f-024. has_dev_url matches ANY dev host, so parking on dev2 while the
    description named dev5 skipped the PUT and reported dev2 in Discord --
    leaving the MR advertising a box that no longer serves the branch."""
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    glab = _Mr(description=f"### Available on [{OTHER_URL}]({OTHER_URL})\n\nbody")
    result = park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert result.description_updated is True
    assert OTHER_URL not in glab.description
    assert DEV_URL in glab.description
    assert "body" in glab.description            # the rest is preserved
    msg = park.done_message(result)
    assert "dev5" in msg or "moved" in msg or "retargeted" in msg


def test_a_link_to_the_same_box_is_still_left_alone(monkeypatch):
    """The re-park no-op stays a no-op -- otherwise every sweep rewrites the
    description for nothing."""
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    glab = _Mr(description=f"### Available on [{DEV_URL}]({DEV_URL})\n\nbody")
    result = park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert result.description_updated is False
    assert glab.puts == []


def test_a_trailing_slash_is_not_a_different_box(monkeypatch):
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    bare = DEV_URL.rstrip("/")
    glab = _Mr(description=f"Available on {bare}")
    result = park.execute(_item(), None, [_box()], **_edges(glab=glab))
    assert result.description_updated is False


def test_retarget_header_is_pure_and_decides_all_three_cases():
    same = f"### Available on [{DEV_URL}]({DEV_URL})"
    assert park.retarget_header(same, DEV_URL) is None
    moved = park.retarget_header(f"Available on {OTHER_URL}\n\nbody", DEV_URL)
    assert moved is not None and DEV_URL in moved and OTHER_URL not in moved
    assert "body" in moved
    fresh = park.retarget_header("body", DEV_URL)
    assert fresh is not None and fresh.startswith("### Available on")


def test_the_done_message_renders_the_field_not_a_literal():
    """f-027. In production park raises before building a result with any
    other status, so the literal and the field agree on every real run -- the
    complaint was that nothing PROVED the number was measured. Pinned here
    where the two can be told apart."""
    msg = park.done_message(park.ParkResult(
        iid=4078, box_name="dev2", dev_url=DEV_URL, description_updated=True,
        http_status=418))
    assert "(418)" in msg
    assert "(200)" not in msg


def test_park_actually_calls_the_http_edge(monkeypatch):
    """The edge was dead in all thirteen original park tests, which is how
    "(200)" survived as a claim nothing checked."""
    monkeypatch.setattr(park.implementer, "sync_to_box", _StrictSync())
    probes = []
    park.execute(_item(), None, [_box()],
                 **_edges(glab=_Mr(), http=lambda u: (probes.append(u), 200)[1]))
    assert probes == [DEV_URL]
