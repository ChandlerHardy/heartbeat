"""M4 Task G: the implement executor's subprocess/ssh/http contract.

Every edge is injected — this file must never touch the network, a real
checkout, ssh, glab, or claude. Each test asserts BOTH the command shape and
the failure path's classification (RunnerError vs NeedsInputError), because
the runner maps those onto queue status + Discord post.
"""
import os
import subprocess

import pytest

from worksweep import implementer
from worksweep.config import WorksweepConfig
from worksweep.devslots import DevBox
from worksweep.implementer import (
    ImplementResult, branch_name, build_description, detect_halt, execute,
    open_draft_mr, select_slot, slug_of, sync_to_box,
)
from worksweep.models import WorkItem
from worksweep.runner import NeedsInputError, RunnerError

NOW = "2026-08-17T12:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="me",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, implement_timeout=5400)
    base.update(kw)
    return WorksweepConfig(**base)


def _item(iid=1775, title="Add cost page inline validation for entry"):
    return WorkItem(schema_version=1, id=f"issue:pb-www#{iid}", repo="pb-www",
                    kind="issue", executor="implement", risk="low",
                    why=f"assigned issue: {title}",
                    web_url=f"https://gl/x/-/issues/{iid}", sha="",
                    status="approved", title=title)


def _box(name="dev1", tier="free", mr_iid=0):
    return DevBox(name=name, host="chandlerhardy-dev", path="/p/pb-www",
                  url=f"https://{name}.chandlerhardy-dev.performancebeef.com/",
                  branch="master", sha="deadbeef", tier=tier, mr_iid=mr_iid)


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def test_slug_is_kebab_of_first_five_title_words():
    assert slug_of("Add cost page inline validation for entry") == \
        "add-cost-page-inline-validation"


def test_slug_strips_punctuation_and_survives_empty_title():
    assert slug_of("Fix: the *thing* (again)!") == "fix-the-thing-again"
    assert slug_of("") == "issue"
    assert slug_of("!!! ???") == "issue"


def test_branch_name_shape():
    assert branch_name(1775, "Add cost page inline validation for entry") == \
        "feat/1775-add-cost-page-inline-validation"


def test_detect_halt_markers_and_question_line():
    assert detect_halt("all good") is None
    assert "HALT_INSUFFICIENT_CONTEXT" in detect_halt(
        "blah\nHALT_INSUFFICIENT_CONTEXT: no spec for the totals column\nmore")
    assert "HALT_SPEC_AMBIGUITY" in detect_halt("x\nHALT_SPEC_AMBIGUITY here\n")
    assert detect_halt("noise\nQUESTION: which table owns the total?\n") \
        .startswith("QUESTION: which table owns the total?")


def test_detect_halt_excerpt_is_bounded():
    text = "HALT_SPEC_AMBIGUITY\n" + "\n".join(f"line{i}" for i in range(200))
    assert len(detect_halt(text)) <= implementer.HALT_EXCERPT_MAX


def test_select_slot_prefers_free_then_handed_off_then_none():
    free, handed = _box("dev1", "free"), _box("dev4", "handed_off", mr_iid=4006)
    live = _box("dev2", "live")
    assert select_slot([live, handed, free]).name == "dev1"
    assert select_slot([live, handed]).name == "dev4"
    assert select_slot([live]) is None
    assert select_slot([]) is None


# --------------------------------------------------------------------------
# sync_to_box (ssh + http edges injected)
# --------------------------------------------------------------------------

def _ssh_seq(*responses):
    """Fake ssh returning the scripted responses in order, recording commands."""
    calls = []

    def run(host, command):
        calls.append(command)
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    run.calls = calls
    return run


def test_sync_to_box_runs_hardened_recipe_and_verifies():
    seen = {}
    calls = []

    def fake_ssh(host, cmd):
        calls.append(cmd)
        seen["host"], seen["cmd"] = host, cmd
        if len(calls) == 1:
            return "master\ndeadbeef\n"      # drift re-probe
        return "remote: feat/1775-x @ abc123\nabc123\n"

    codes = []
    got = sync_to_box(_box(), "feat/1775-x", fake_ssh,
                      lambda url: codes.append(url) or 200,
                      expected_sha="abc123")
    assert got == "abc123"
    assert seen["host"] == "chandlerhardy-dev"
    assert "git fetch origin 'feat/1775-x'" in seen["cmd"]
    assert "git checkout -q -B 'feat/1775-x' 'origin/feat/1775-x'" in seen["cmd"]
    assert codes == ["https://dev1.chandlerhardy-dev.performancebeef.com/"]


def _ok_probe(*sync_responses):
    """ssh fake: the drift re-probe answers with the box's claim-time state,
    then the sync half answers with `sync_responses`."""
    return _ssh_seq("master\ndeadbeef\n", *sync_responses)


def test_sync_to_box_sha_mismatch_raises():
    with pytest.raises(RunnerError, match="did NOT land"):
        sync_to_box(_box(), "b", _ok_probe("zzz\n"), lambda url: 200,
                    expected_sha="abc123")


def test_sync_to_box_non_200_raises():
    with pytest.raises(RunnerError, match="HTTP 502"):
        sync_to_box(_box(), "b", _ok_probe("abc\n"), lambda url: 502,
                    expected_sha="abc")


def test_sync_to_box_ssh_failure_raises_runner_error():
    def boom(host, cmd):
        raise RuntimeError("ssh chandlerhardy-dev timed out after 300s")

    with pytest.raises(RunnerError, match="timed out"):
        sync_to_box(_box(), "b", boom, lambda url: 200)


def test_sync_to_box_http_failure_raises_runner_error():
    def boom(url):
        raise OSError("connection reset")

    with pytest.raises(RunnerError, match="connection reset"):
        sync_to_box(_box(), "b", _ok_probe("abc\n"), boom, expected_sha="abc")


# --------------------------------------------------------------------------
# description generation
# --------------------------------------------------------------------------

def _desc_run(claude_stdout="", claude_rc=0, raise_timeout=False):
    def run(cmd, **kw):
        if cmd[0] == "claude":
            if raise_timeout:
                raise subprocess.TimeoutExpired(cmd, 60)
            return subprocess.CompletedProcess(cmd, claude_rc,
                                               stdout=claude_stdout, stderr="e")
        return subprocess.CompletedProcess(cmd, 0, stdout="stat\n", stderr="")
    return run


def test_build_description_uses_llm_and_keeps_dev_url(tmp_path):
    body = "Adds inline validation.\n\nAvailable on https://dev1.x/\n"
    out = build_description("/co", _cfg(tmp_path), 1775, "T",
                            "https://dev1.x/", "feat/1775-t", "log\n",
                            _desc_run(claude_stdout=body))
    assert "Adds inline validation." in out
    assert out.count("Available on https://dev1.x/") == 1


def test_build_description_appends_missing_dev_url_line(tmp_path):
    out = build_description("/co", _cfg(tmp_path), 1775, "T",
                            "https://dev1.x/", "feat/1775-t", "log\n",
                            _desc_run(claude_stdout="A body with no dev link\n"))
    assert "Available on https://dev1.x/" in out


def test_build_description_falls_back_when_llm_fails(tmp_path):
    for run in (_desc_run(claude_rc=1), _desc_run(raise_timeout=True),
                _desc_run(claude_stdout="   ")):
        out = build_description("/co", _cfg(tmp_path), 1775, "Ttl",
                                "https://dev1.x/", "feat/1775-t", "c1 msg\n", run)
        assert "Available on https://dev1.x/" in out
        assert "#1775" in out


# --------------------------------------------------------------------------
# open_draft_mr
# --------------------------------------------------------------------------

def _glab_run(create_out="!42 https://gl/x/-/merge_requests/42\n", create_rc=0,
              view_json='{"iid": 42, "draft": true, "title": "Draft: x"}',
              view_rc=0, update_rc=0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[:3] == ["glab", "mr", "create"]:
            return subprocess.CompletedProcess(cmd, create_rc, stdout=create_out,
                                               stderr="glab err\n")
        if cmd[:3] == ["glab", "mr", "view"]:
            return subprocess.CompletedProcess(cmd, view_rc, stdout=view_json,
                                               stderr="")
        if cmd[:3] == ["glab", "mr", "update"]:
            return subprocess.CompletedProcess(cmd, update_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return run


def test_open_draft_mr_parses_iid_and_url():
    calls = []
    mr_iid, url = open_draft_mr("/co", 1775, "Ttl", "body", "feat/1775-t",
                                _glab_run(calls=calls))
    assert mr_iid == 42
    assert url == "https://gl/x/-/merge_requests/42"
    create = calls[0]
    assert "--draft" in create and "--yes" in create
    assert create[create.index("--source-branch") + 1] == "feat/1775-t"
    assert create[create.index("--target-branch") + 1] == "master"
    assert create[create.index("--title") + 1] == "feat(#1775): Ttl"
    assert create[create.index("--description") + 1] == "body"


def test_open_draft_mr_nonzero_raises():
    with pytest.raises(RunnerError, match="glab err"):
        open_draft_mr("/co", 1, "T", "b", "br", _glab_run(create_rc=1))


def test_open_draft_mr_unparseable_output_raises():
    with pytest.raises(RunnerError, match="could not parse"):
        open_draft_mr("/co", 1, "T", "b", "br", _glab_run(create_out="ok done\n"))


def test_open_draft_mr_marks_draft_when_glab_did_not():
    """Read-back says non-draft -> force it -> read back AGAIN and accept."""
    calls, views = [], {"n": 0}

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:3] == ["glab", "mr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="!42 https://gl/x/-/merge_requests/42\n", stderr="")
        if cmd[:3] == ["glab", "mr", "view"]:
            views["n"] += 1
            return subprocess.CompletedProcess(
                cmd, 0, stdout=('{"draft": false}' if views["n"] == 1
                                else '{"draft": true}'), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    open_draft_mr("/co", 1, "T", "b", "br", run)
    assert ["glab", "mr", "update", "42", "--draft", "--yes"] in calls
    assert views["n"] == 2      # the forced update is verified, not trusted


def test_open_draft_mr_raises_when_it_cannot_be_made_draft():
    """A live (non-draft) MR the executor opened is a real hazard — never
    silent: raise so the runner posts ⚠️ with the MR link."""
    with pytest.raises(RunnerError, match="not a draft"):
        open_draft_mr("/co", 1, "T", "b", "br",
                      _glab_run(view_json='{"iid": 42, "draft": false}',
                                update_rc=1))


def test_open_draft_mr_already_draft_skips_update():
    calls = []
    open_draft_mr("/co", 1, "T", "b", "br", _glab_run(calls=calls))
    assert not any(c[:3] == ["glab", "mr", "update"] for c in calls)


# --------------------------------------------------------------------------
# execute — full path with every edge injected
# --------------------------------------------------------------------------

class _Edges:
    """Scriptable subprocess/ssh/http triple for execute()."""

    def __init__(self, **kw):
        self.calls = []
        self.ssh_calls = []
        self.http_calls = []
        self.do_rc = kw.get("do_rc", 0)
        self.do_out = kw.get("do_out", "implemented and committed\n")
        self.log_out = kw.get("log_out", "abc123 feat: thing\n")
        self.status_out = kw.get("status_out", "")
        self.ls_remote_out = kw.get("ls_remote_out", "")
        self.head_sha = kw.get("head_sha", "abc123")
        self.push_rc = kw.get("push_rc", 0)
        self.magi_rc = kw.get("magi_rc", 0)
        self.report_body = kw.get("report_body", "## Verdict\nSHIP with nits\n")
        self.checkout = kw.get("checkout", "")
        # what the box reports back from the sync half (default: it landed)
        self.box_sha = kw.get("box_sha", None)
        # what the drift re-probe sees (default: exactly what was claimed)
        self.probe_out = kw.get("probe_out", "master\ndeadbeef\n")

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        c = list(cmd)
        if c[0] == "git":
            sub = c[3] if len(c) > 3 else ""
            if sub == "ls-remote":
                return subprocess.CompletedProcess(c, 0, stdout=self.ls_remote_out,
                                                   stderr="")
            if sub == "log":
                return subprocess.CompletedProcess(c, 0, stdout=self.log_out,
                                                   stderr="")
            if sub == "status":
                return subprocess.CompletedProcess(c, 0, stdout=self.status_out,
                                                   stderr="")
            if sub == "rev-parse":
                return subprocess.CompletedProcess(c, 0, stdout=self.head_sha + "\n",
                                                   stderr="")
            if sub == "push":
                return subprocess.CompletedProcess(c, self.push_rc, stdout="",
                                                   stderr="rejected: non-fast-forward\n")
            if sub == "diff":
                return subprocess.CompletedProcess(c, 0, stdout=" 2 files\n", stderr="")
            return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
        if c[0] == "claude":
            prompt = c[2]
            if prompt.startswith("/rubric:do"):
                return subprocess.CompletedProcess(c, self.do_rc, stdout=self.do_out,
                                                   stderr="do stderr\n")
            if prompt.startswith("/magi:magi-review"):
                if self.report_body and self.checkout:
                    d = os.path.join(self.checkout, ".magi")
                    os.makedirs(d, exist_ok=True)
                    with open(os.path.join(
                            d, "tribunal-report-mr-42-2026-08-17.md"), "w") as f:
                        f.write(self.report_body)
                return subprocess.CompletedProcess(c, self.magi_rc, stdout="",
                                                   stderr="magi stderr\n")
            return subprocess.CompletedProcess(c, 0, stdout="body text\n", stderr="")
        if c[0] == "glab":
            return _glab_run()(c, **kw)
        return subprocess.CompletedProcess(c, 0, stdout="", stderr="")

    def ssh(self, host, command):
        self.ssh_calls.append((host, command))
        if len(self.ssh_calls) == 1:
            return self.probe_out
        landed = self.head_sha if self.box_sha is None else self.box_sha
        return f"remote\n{landed}\n"

    def http(self, url):
        self.http_calls.append(url)
        return 200


def _worktree(tmp_path):
    """review fix C1: implement runs in its own worktree
    (<checkouts_root>/.worktrees/<repo>-implement), not the shared clone."""
    return tmp_path / ".worktrees" / "pb-www-implement"


def _run_execute(tmp_path, edges=None, boxes=None, cfg=None, item=None):
    root = tmp_path / "pb-www"
    root.mkdir(exist_ok=True)
    edges = edges or _Edges()
    edges.checkout = str(_worktree(tmp_path))
    return execute(item or _item(), cfg or _cfg(tmp_path),
                   boxes if boxes is not None else [_box()],
                   run_subprocess=edges.run, run_ssh=edges.ssh,
                   http_get=edges.http), edges


def test_execute_happy_path(tmp_path):
    result, edges = _run_execute(tmp_path)
    assert isinstance(result, ImplementResult)
    assert result.mr_iid == 42
    assert result.branch == "feat/1775-add-cost-page-inline-validation"
    assert result.dev_box == "dev1"
    assert result.dev_url.endswith("performancebeef.com/")
    assert result.result_sha == "abc123"
    assert "SHIP with nits" in result.verdict
    assert result.report_path.endswith("tribunal-report-mr-42-2026-08-17.md")
    assert result.reassigned_from == ""

    flat = [" ".join(c) for c, _ in edges.calls]
    assert any(f.endswith("fetch origin") for f in flat)
    assert any("/rubric:do #1775" in f for f in flat)
    assert any("glab mr create" in f for f in flat)
    assert any("/magi:magi-review !42 --advisory" in f for f in flat)
    # the /do run gets the implement timeout, not the magi one
    do_call = next(kw for c, kw in edges.calls
                   if c[0] == "claude" and c[2].startswith("/rubric:do"))
    assert do_call["timeout"] == 5400
    assert do_call["cwd"] == str(_worktree(tmp_path))
    # push happened before the box sync, and the box sync verified the sha
    assert edges.ssh_calls and edges.http_calls == [result.dev_url]


def test_execute_no_slot_raises(tmp_path):
    with pytest.raises(RunnerError, match="no dev slot available"):
        _run_execute(tmp_path, boxes=[_box(tier="live")])


def test_execute_missing_checkout_raises(tmp_path):
    with pytest.raises(RunnerError, match="no checkout"):
        execute(_item(), _cfg(tmp_path), [_box()],
                run_subprocess=_Edges().run, run_ssh=lambda h, c: "",
                http_get=lambda u: 200)


def test_execute_halt_marker_raises_needs_input(tmp_path):
    edges = _Edges(do_out="working...\nHALT_SPEC_AMBIGUITY: two totals columns\n")
    with pytest.raises(NeedsInputError) as ei:
        _run_execute(tmp_path, edges=edges)
    assert "HALT_SPEC_AMBIGUITY" in str(ei.value)
    # halted -> no MR, no push, no sync
    flat = [" ".join(c) for c, _ in edges.calls]
    assert not any("glab mr create" in f for f in flat)
    assert not any(" push " in f for f in flat)
    assert edges.ssh_calls == []


def test_execute_halt_detected_even_when_do_exits_nonzero(tmp_path):
    edges = _Edges(do_rc=2, do_out="QUESTION: which feedyard owns this?\n")
    with pytest.raises(NeedsInputError, match="QUESTION"):
        _run_execute(tmp_path, edges=edges)


def test_execute_do_nonzero_without_halt_raises_runner_error(tmp_path):
    with pytest.raises(RunnerError, match="do stderr"):
        _run_execute(tmp_path, edges=_Edges(do_rc=1, do_out="crashed\n"))


def test_execute_do_timeout_raises_runner_error(tmp_path):
    class T(_Edges):
        def run(self, cmd, **kw):
            if cmd[0] == "claude" and cmd[2].startswith("/rubric:do"):
                raise subprocess.TimeoutExpired(cmd, 5400)
            return super().run(cmd, **kw)

    with pytest.raises(RunnerError, match="5400"):
        _run_execute(tmp_path, edges=T())


def test_execute_no_commits_raises(tmp_path):
    with pytest.raises(RunnerError, match="no commits"):
        _run_execute(tmp_path, edges=_Edges(log_out="\n"))


def test_execute_dirty_tree_raises(tmp_path):
    with pytest.raises(RunnerError, match="uncommitted"):
        _run_execute(tmp_path, edges=_Edges(status_out=" M a.php\n"))


def test_execute_push_failure_raises(tmp_path):
    with pytest.raises(RunnerError, match="non-fast-forward"):
        _run_execute(tmp_path, edges=_Edges(push_rc=1))


def test_execute_reuses_existing_remote_branch(tmp_path):
    edges = _Edges(ls_remote_out="abc123\trefs/heads/feat/1775-x\n")
    _run_execute(tmp_path, edges=edges)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any(f.endswith("checkout feat/1775-add-cost-page-inline-validation")
               for f in flat)
    assert any("pull --ff-only" in f for f in flat)
    assert not any("checkout -B" in f for f in flat)


def test_execute_creates_branch_from_master_when_absent(tmp_path):
    _, edges = _run_execute(tmp_path, edges=_Edges(ls_remote_out=""))
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("checkout -B feat/1775-add-cost-page-inline-validation origin/master"
               in f for f in flat)


def test_execute_missing_magi_report_is_not_fatal(tmp_path):
    result, _ = _run_execute(tmp_path, edges=_Edges(report_body=""))
    assert result.mr_iid == 42
    assert result.report_path == ""
    assert result.verdict == ""


def test_execute_magi_nonzero_is_not_fatal(tmp_path):
    result, _ = _run_execute(tmp_path, edges=_Edges(magi_rc=1, report_body=""))
    assert result.mr_iid == 42
    assert "magi" in result.magi_note.lower()


def test_execute_handed_off_box_records_reassignment(tmp_path):
    result, _ = _run_execute(tmp_path,
                             boxes=[_box("dev4", "handed_off", mr_iid=4006)])
    assert result.dev_box == "dev4"
    assert result.reassigned_from == "4006"


def test_execute_sync_failure_raises_after_mr_is_not_created(tmp_path):
    """Sync happens before the MR is opened, so a dead box never leaves a
    dangling Draft MR behind."""
    edges = _Edges(box_sha="DIFFERENT")
    with pytest.raises(RunnerError, match="did NOT land"):
        _run_execute(tmp_path, edges=edges)
    assert not any("glab" in c[0] for c, _ in edges.calls)


def test_execute_bad_item_id_raises(tmp_path):
    import dataclasses
    bad = dataclasses.replace(_item(), id="issue:pb-www", web_url="https://gl/x")
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    with pytest.raises(RunnerError, match="issue iid"):
        execute(bad, _cfg(tmp_path), [_box()], run_subprocess=_Edges().run,
                run_ssh=lambda h, c: "", http_get=lambda u: 200)


def test_annotate_boxes_attaches_tier_and_mr_iid():
    from worksweep.models import MergeRequest
    mr = MergeRequest(repo="pb-www", iid=4006, title="t", author="me",
                      web_url="u", description="", sha="s", is_draft=False,
                      reviewers=(), ci_status="success", updated_at="",
                      approved=True, merge_status="MERGEABLE",
                      assignees=("maintainer",), source_branch="feat/old")
    boxes = [DevBox(name="dev4", host="h", path="/p", url="u",
                    branch="feat/old", sha="s")]
    out = implementer.annotate_boxes(boxes, [mr], "me", frozenset())
    assert out[0].tier == "handed_off"
    assert out[0].mr_iid == 4006


# --------------------------------------------------------------------------
# end-to-end: run_once driving the REAL execute() over faked edges
# --------------------------------------------------------------------------

def _e2e(tmp_path, edges):
    from worksweep.models import QueueRecord
    from worksweep.runner import run_once
    root = tmp_path / "pb-www"
    root.mkdir(exist_ok=True)
    edges.checkout = str(_worktree(tmp_path))
    rec = QueueRecord(number=7, first_seen=NOW, last_seen=NOW, item=_item())
    state = {"records": [rec]}
    posts = []
    deps = {"load": lambda: list(state["records"]),
            "save": lambda recs: state.__setitem__("records", list(recs)),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW,
            "execute": lambda i, c: ("", ""),
            "boxes": lambda: [_box()],
            "execute_implement": lambda item, c, bx: execute(
                item, c, bx, run_subprocess=edges.run, run_ssh=edges.ssh,
                http_get=edges.http)}
    rc = run_once(_cfg(tmp_path), deps,
                  lock_path=str(tmp_path / "runner.lock"),
                  implement_lock_path=str(tmp_path / "runner-implement.lock"))
    return rc, state["records"][0], posts


def test_end_to_end_implement_records_done_and_posts(tmp_path):
    rc, rec, posts = _e2e(tmp_path, _Edges())
    assert rc == 0
    assert rec.item.status == "done"
    assert rec.item.mr_iid == 42 and rec.item.dev_box == "dev1"
    assert rec.item.result_sha == "abc123"
    assert any("🛠️ implementing #1775 on dev1" in p for p in posts)
    assert any("🛠️ implemented #1775 → Draft !42" in p for p in posts)


def test_end_to_end_halt_records_needs_input_and_posts_question(tmp_path):
    rc, rec, posts = _e2e(tmp_path, _Edges(
        do_out="HALT_INSUFFICIENT_CONTEXT: no spec for the totals column\n"))
    assert rc == 0
    assert rec.item.status == "needs-input"
    assert "HALT_INSUFFICIENT_CONTEXT" in rec.item.error_summary
    assert any(p.startswith("❓ #1775 needs your input:") for p in posts)
    assert not any(p.startswith("⚠️") for p in posts)


def test_end_to_end_sync_failure_records_error_and_posts_warning(tmp_path):
    rc, rec, posts = _e2e(tmp_path, _Edges(box_sha="NOPE"))
    assert rc == 1
    assert rec.item.status == "error"
    assert "did NOT land" in rec.item.error_summary
    assert any(p.startswith("⚠️") and "did NOT land" in p for p in posts)


# ==========================================================================
# Task G review fixes (2026-08-18)
# ==========================================================================

# --- C1: every subprocess gets stdin=DEVNULL -------------------------------

def test_every_subprocess_gets_devnull_stdin(tmp_path):
    """c0e7791 all over again: under launchd there is no TTY, `claude -p`
    exits 1 in ~3s on an inherited stdin and `glab` blocks on a prompt for
    the full timeout. EVERY spawn this module makes must pin stdin."""
    _, edges = _run_execute(tmp_path)
    spawned = [(c, kw) for c, kw in edges.calls]
    assert spawned, "execute spawned nothing"
    for cmd, kw in spawned:
        assert kw.get("stdin") is subprocess.DEVNULL, \
            f"{cmd[0]} {cmd[1:3]} was spawned without stdin=DEVNULL"
    kinds = {c[0] for c, _ in spawned}
    assert {"git", "claude", "glab"} <= kinds


def test_devnull_stdin_on_the_description_and_magi_passes(tmp_path):
    _, edges = _run_execute(tmp_path)
    claude_calls = [(c, kw) for c, kw in edges.calls if c[0] == "claude"]
    prompts = [c[2][:20] for c, _ in claude_calls]
    assert any(p.startswith("/rubric:do") for p in prompts)
    assert any(p.startswith("/magi:magi-review") for p in prompts)
    assert any(p.startswith("Write the merge") for p in prompts)
    for _, kw in claude_calls:
        assert kw["stdin"] is subprocess.DEVNULL


# --- I3: sync_to_box safety ------------------------------------------------

def test_sync_to_box_only_drops_a_stash_it_created():
    """A reviewer's pre-existing stash@{0} must survive: the drop is guarded
    by a before/after stash-count comparison, never unconditional."""
    ssh = _ssh_seq("master\ndeadbeef\n", "abc123\n")
    sync_to_box(_box(), "feat/1775-x", ssh, lambda u: 200, expected_sha="abc123")
    script = ssh.calls[-1]
    assert "git stash list" in script
    assert "git stash drop" in script
    drop_line = next(ln for ln in script.splitlines() if "git stash drop" in ln)
    # the drop must be inside a guard, not a bare statement
    assert drop_line.strip().startswith("if ") or "then" in drop_line


def test_sync_to_box_aborts_when_the_box_moved_since_it_was_claimed():
    ssh = _ssh_seq("someone-elses-branch\nffff\n", "abc123\n")
    with pytest.raises(RunnerError, match="moved"):
        sync_to_box(_box(), "feat/1775-x", ssh, lambda u: 200,
                    expected_sha="abc123")
    assert len(ssh.calls) == 1          # never ran the write half


def test_sync_to_box_aborts_when_the_box_sha_moved():
    ssh = _ssh_seq("master\nSOMETHING-ELSE\n", "abc123\n")
    with pytest.raises(RunnerError, match="moved"):
        sync_to_box(_box(), "feat/1775-x", ssh, lambda u: 200,
                    expected_sha="abc123")


def test_sync_to_box_accepts_explicit_claim_time_state():
    ssh = _ssh_seq("other\nzzz\n", "abc123\n")
    got = sync_to_box(_box(), "feat/1775-x", ssh, lambda u: 200,
                      expected_sha="abc123", claim_branch="other",
                      claim_sha="zzz")
    assert got == "abc123"


def test_sync_to_box_skips_the_drift_check_when_claim_state_is_unknown():
    import dataclasses
    ssh = _ssh_seq("abc123\n")
    box = dataclasses.replace(_box(), branch="", sha="")
    assert sync_to_box(box, "b", ssh, lambda u: 200, expected_sha="abc123") \
        == "abc123"


def test_sync_to_box_probe_failure_raises():
    def boom(host, cmd):
        raise RuntimeError("ssh timed out")

    with pytest.raises(RunnerError, match="ssh timed out"):
        sync_to_box(_box(), "b", boom, lambda u: 200)


# --- I4: draft is proved, not hoped for ------------------------------------

def test_open_draft_mr_raises_when_the_forced_update_did_not_take():
    """update exits 0 but the read-back still says non-draft -> raise, and
    name the orphan MR's URL so the ⚠️ is actionable."""
    run = _glab_run(
        create_out="!42 https://gl/x/-/merge_requests/42\n",
        view_json='{"iid": 42, "draft": false}', update_rc=0)
    with pytest.raises(RunnerError) as ei:
        open_draft_mr("/co", 1, "T", "b", "br", run)
    assert "not a draft" in str(ei.value)
    assert "https://gl/x/-/merge_requests/42" in str(ei.value)


def test_open_draft_mr_accepts_a_draft_confirmed_by_the_read_back():
    seen = {"views": 0}

    def run(cmd, **kw):
        if cmd[:3] == ["glab", "mr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="!42 https://gl/x/-/merge_requests/42\n", stderr="")
        if cmd[:3] == ["glab", "mr", "view"]:
            seen["views"] += 1
            body = ('{"draft": false}' if seen["views"] == 1
                    else '{"draft": true}')
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mr_iid, url = open_draft_mr("/co", 1, "T", "b", "br", run)
    assert mr_iid == 42 and seen["views"] == 2


# --- I5: iid parse fallback ------------------------------------------------

def test_open_draft_mr_parses_iid_from_the_url_when_bang_is_absent():
    mr_iid, url = open_draft_mr(
        "/co", 1, "T", "b", "br",
        _glab_run(create_out="Created: https://gl/x/-/merge_requests/77\n"))
    assert mr_iid == 77
    assert url == "https://gl/x/-/merge_requests/77"


def test_open_draft_mr_raises_only_when_both_forms_miss():
    with pytest.raises(RunnerError, match="could not parse"):
        open_draft_mr("/co", 1, "T", "b", "br",
                      _glab_run(create_out="all done, nothing numeric here\n"))


def test_issue_iid_prefers_the_item_id():
    """The primary id-based parse is untouched by the work_items fix."""
    assert implementer.issue_iid(_item(iid=1775)) == 1775


@pytest.mark.parametrize("url", [
    "https://gitlab.com/performancelivestock/pb-www/-/work_items/869",
    "https://gitlab.com/performancelivestock/pb-www/-/issues/869",
])
def test_issue_iid_falls_back_to_either_url_form(url):
    """GitLab now serves issues as /-/work_items/<iid>. The web_url FALLBACK
    must accept both spellings (mirrors curator.py:377).

    Latent rather than live today -- assessor-built records always carry the
    `issue:<repo>#<iid>` id the primary parse reads -- but a record that ever
    reaches here without that id would raise instead of resolving.
    """
    item = WorkItem(schema_version=1, id="no-iid-here", repo="pb-www",
                    kind="issue", executor="implement", risk="low",
                    why="assigned issue", web_url=url, sha="", title="t")
    assert implementer.issue_iid(item) == 869


def test_issue_iid_still_raises_when_there_is_no_iid_anywhere():
    """The raise is deliberate: a wrong iid would run /rubric:do against
    someone else's issue."""
    item = WorkItem(schema_version=1, id="todo:x", repo="pb-www", kind="todo",
                    executor="triage", risk="low", why="w",
                    web_url="https://gitlab.com/dashboard/todos", sha="", title="t")
    with pytest.raises(RunnerError):
        implementer.issue_iid(item)


# --- letting go of the branch (2026-08-26 live failure) --------------------
#
# implement holds a branch for 90 minutes and its worktree is permanent, so a
# finished run that keeps the branch blocks keep-current and address-feedback
# from ever touching that MR again.

def _detaches(edges):
    return [c for c, _ in edges.calls if "checkout" in c and "--detach" in c]


def test_a_finished_implement_lets_go_of_the_branch(tmp_path):
    _, edges = _run_execute(tmp_path)
    assert len(_detaches(edges)) == 1
    assert edges.calls[-1][0] == _detaches(edges)[0]


def test_a_halted_implement_lets_go_too(tmp_path):
    """A halt parks the item on a human answer -- possibly for days. Holding
    the branch that whole time is exactly the stranding this fixes."""
    edges = _Edges(do_out="HALT_SPEC_AMBIGUITY: which tab?")
    with pytest.raises(NeedsInputError):
        _run_execute(tmp_path, edges=edges)
    assert len(_detaches(edges)) == 1


def test_a_failed_implement_lets_go_too(tmp_path):
    edges = _Edges(do_rc=1)
    with pytest.raises(RunnerError):
        _run_execute(tmp_path, edges=edges)
    assert len(_detaches(edges)) == 1


def test_a_failing_detach_never_masks_the_implement_result(tmp_path):
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    edges = _Edges()
    edges.checkout = str(_worktree(tmp_path))
    real_run = edges.run

    def run(cmd, **kw):
        if "checkout" in list(cmd) and "--detach" in list(cmd):
            edges.calls.append((list(cmd), kw))
            raise OSError("git: cannot detach")
        return real_run(cmd, **kw)

    result = execute(_item(), _cfg(tmp_path), [_box()], run_subprocess=run,
                     run_ssh=edges.ssh, http_get=edges.http)
    assert result.mr_iid == 42


# --- magi 0.2.4: no --no-rebuttal on the advisory run (2026-08-26) ---------

def test_the_advisory_magi_invocation_no_longer_suppresses_rebuttal(tmp_path):
    """FALSIFYING: re-adding the flag fails here. The existing happy-path
    assertion only substring-matched `--advisory`, so the flag was invisible
    to it -- and passing one magi 0.2.4 no longer defines is an
    unknown-argument error, not a no-op."""
    _, edges = _run_execute(tmp_path)
    advisory = [c for c, _ in edges.calls
                if c[0] == "claude" and "--advisory" in " ".join(c)]
    assert len(advisory) == 1
    assert advisory[0][2] == "/magi:magi-review !42 --advisory --draft-findings"
    assert "--no-rebuttal" not in " ".join(advisory[0])


# --- f-020 / f-023: the advisory run must be able to finish ---------------

def test_the_advisory_magi_run_uses_the_cfg_magi_timeout(tmp_path):
    """f-020. This was a hard-coded 1800s while a magi 0.2.4 tribunal takes
    40-60 minutes, so every advisory run timed out by construction -- the
    exact drift the runner's own budget was raised to avoid."""
    from worksweep.runner import MAGI_TIMEOUT_SECONDS
    _, edges = _run_execute(tmp_path)
    advisory = [(c, kw) for c, kw in edges.calls
                if c[0] == "claude" and "--advisory" in " ".join(c)]
    assert len(advisory) == 1
    assert advisory[0][1]["timeout"] == MAGI_TIMEOUT_SECONDS == 4500


def test_a_configured_magi_timeout_reaches_the_advisory_run(tmp_path):
    _, edges = _run_execute(tmp_path, cfg=_cfg(tmp_path, magi_timeout=6000))
    advisory = [(c, kw) for c, kw in edges.calls
                if c[0] == "claude" and "--advisory" in " ".join(c)]
    assert advisory[0][1]["timeout"] == 6000


import re  # noqa: E402


def _nested_same_quote_fstrings(src):
    """Lines holding an f-string whose own delimiter is reused INSIDE a
    replacement field -- the PEP 701 (3.12+) grammar.

    Scans forward from each `f'`/`f"` tracking brace depth: a matching quote
    at depth 0 simply ends the string, while one at depth > 0 is the 3.12-only
    shape. Deliberately narrow -- it looks for exactly what broke, not general
    f-string parsing.
    """
    bad = []
    for n, line in enumerate(src.splitlines(), 1):
        for m in re.finditer(r"\bf(['\"])", line):
            q, depth = m.group(1), 0
            for ch in line[m.end():]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth = max(0, depth - 1)
                elif ch == q:
                    if depth > 0:
                        bad.append((n, line.strip()))
                    break
    return bad


def test_no_f_string_reuses_its_own_quote(tmp_path):
    """f-023. `f'{a or \'\'}'` is 3.12+ only; on an older interpreter it is a
    SyntaxError at IMPORT, which takes the whole module down before any test
    can run. The local interpreter parses it happily, so this cannot be caught
    by importing -- only by looking."""
    import pathlib as _p
    # the scan catches the shape that actually broke
    assert _nested_same_quote_fstrings(
        "note = f'exited: {_tail(f\'{proc.stdout or \'\'}\', 5)}'")
    # and the module is clean of it
    src = _p.Path(implementer.__file__).read_text()
    assert _nested_same_quote_fstrings(src) == []
