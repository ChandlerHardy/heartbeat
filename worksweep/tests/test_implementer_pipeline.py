"""M5: cfg.pipeline_command mode — one claude run drives the full
pla-pipeline; the executor claims, runs, and PROVES (state file -> MR ->
Draft read-back -> box 200) instead of creating the MR / running magi
itself. Every edge injected."""
import json
import os
import subprocess

import pytest

from worksweep.config import WorksweepConfig
from worksweep.devslots import DevBox
from worksweep.implementer import ImplementResult, execute
from worksweep.models import WorkItem
from worksweep.runner import NeedsInputError, RunnerError

_STATE = """# 1775 — add cost inline validation
- [x] 5. MAGI: r2 codex READY. RESOLVED.
- [x] 7. MR — https://gitlab.com/performancelivestock/pb-www/-/merge_requests/4099 (draft)
"""


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="me",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude",
                runner_timeout=1800, implement_timeout=5400,
                pipeline_command="/chandler-personal:pla-pipeline")
    base.update(kw)
    return WorksweepConfig(**base)


def _item(iid=1775):
    return WorkItem(schema_version=1, id=f"issue:pb-www#{iid}", repo="pb-www",
                    kind="issue", executor="implement", risk="low",
                    why="assigned issue", web_url=f"https://gl/x/-/issues/{iid}",
                    sha="", status="approved", title="Add cost inline validation")


def _box(name="dev2", tier="free", mr_iid=0):
    return DevBox(name=name, host="chandlerhardy-dev", path="/p/pb-www",
                  url=f"https://{name}.chandlerhardy-dev.performancebeef.com/",
                  branch="master", sha="deadbeef", tier=tier, mr_iid=mr_iid)


class _Edges:
    def __init__(self, **kw):
        self.calls = []
        self.http_calls = []
        self.checkout = ""
        self.claude_rc = kw.get("claude_rc", 0)
        self.claude_out = kw.get("claude_out", "pipeline complete\n")
        self.write_state = kw.get("write_state", _STATE)
        self.state_slug = kw.get("state_slug", "1775-add-cost-inline")
        self.mr_json = kw.get("mr_json", {"draft": True,
                                          "source_branch": "fix/1775-inline",
                                          "sha": "cafe1234"})
        self.http_status = kw.get("http_status", 200)

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        c = list(cmd)
        if c[0] == "claude":
            if self.write_state is not None:
                d = os.path.join(self.checkout, ".claude", "state",
                                 "pla-pipelines", self.state_slug)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "state.md"), "w") as f:
                    f.write(self.write_state)
            return subprocess.CompletedProcess(c, self.claude_rc,
                                               stdout=self.claude_out,
                                               stderr="")
        if c[0] == "glab" and c[1:3] == ["mr", "view"]:
            return subprocess.CompletedProcess(
                c, 0, stdout=json.dumps(self.mr_json), stderr="")
        if c[0] == "glab":
            return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(c, 0, stdout="", stderr="")

    def ssh(self, host, command):
        raise AssertionError("pipeline mode must not use the ssh edge")

    def http(self, url):
        self.http_calls.append(url)
        return self.http_status


def _run(tmp_path, edges=None, cfg=None, boxes=None):
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    edges = edges or _Edges()
    edges.checkout = str(tmp_path / ".worktrees" / "pb-www-implement")
    result = execute(_item(), cfg or _cfg(tmp_path),
                     boxes if boxes is not None else [_box()],
                     run_subprocess=edges.run, run_ssh=edges.ssh,
                     http_get=edges.http)
    return result, edges


def test_pipeline_mode_prompt_shape(tmp_path):
    _, edges = _run(tmp_path)
    claude = next(c for c, _ in edges.calls if c[0] == "claude")
    prompt = claude[2]
    assert prompt.startswith("/chandler-personal:pla-pipeline #1775 --dev 2")
    assert "never pass --lite" in prompt
    assert "Draft" in prompt
    assert "dev2.chandlerhardy-dev" in prompt        # sftp remotePath hint


def test_pipeline_mode_never_creates_mr_or_runs_magi(tmp_path):
    _, edges = _run(tmp_path)
    flat = [" ".join(c) for c, _ in edges.calls]
    assert not any("mr create" in f for f in flat)
    assert not any("magi-review" in f for f in flat)
    assert not any(f.startswith("git push") or " push origin" in f for f in flat)


def test_pipeline_mode_result_fields(tmp_path):
    result, edges = _run(tmp_path)
    assert isinstance(result, ImplementResult)
    assert result.mr_iid == 4099
    assert result.mr_url.endswith("/merge_requests/4099")
    assert result.dev_box == "dev2"
    assert result.branch == "fix/1775-inline"
    assert result.result_sha == "cafe1234"
    assert result.verdict == "SHIP"
    assert result.report_path.endswith("state.md")


def test_pipeline_missing_state_is_a_runner_error(tmp_path):
    edges = _Edges(write_state=None)
    with pytest.raises(RunnerError, match="no state file"):
        _run(tmp_path, edges=edges)


def test_pipeline_state_without_mr_is_a_runner_error(tmp_path):
    edges = _Edges(write_state="- [x] 5. MAGI: RESOLVED\n- [ ] 7. MR\n")
    with pytest.raises(RunnerError, match="names no MR"):
        _run(tmp_path, edges=edges)


def test_pipeline_halt_becomes_needs_input(tmp_path):
    edges = _Edges(claude_out="...\nHALT_INSUFFICIENT_CONTEXT: which sheet?\n",
                   claude_rc=1)
    with pytest.raises(NeedsInputError):
        _run(tmp_path, edges=edges)


def test_pipeline_nonzero_exit_is_a_runner_error(tmp_path):
    edges = _Edges(claude_rc=2)
    with pytest.raises(RunnerError, match="exited 2"):
        _run(tmp_path, edges=edges)


def test_pipeline_non_draft_mr_gets_forced_draft(tmp_path):
    class _FlipDraft(_Edges):
        def __init__(self):
            super().__init__()
            self.views = 0
            self.updated = False

        def run(self, cmd, **kw):
            c = list(cmd)
            if c[0] == "glab" and c[1:3] == ["mr", "view"]:
                self.views += 1
                self.calls.append((c, kw))
                draft = self.updated          # non-draft until update lands
                return subprocess.CompletedProcess(
                    c, 0, stdout=json.dumps({"draft": draft,
                                             "source_branch": "b",
                                             "sha": "s"}), stderr="")
            if c[0] == "glab" and c[1:3] == ["mr", "update"]:
                self.updated = True
                self.calls.append((c, kw))
                return subprocess.CompletedProcess(c, 0, stdout="", stderr="")
            return super().run(cmd, **kw)

    edges = _FlipDraft()
    _, edges = _run(tmp_path, edges=edges)
    assert edges.updated
    flat = [" ".join(c) for c, _ in edges.calls]
    assert any("mr update 4099 --draft" in f for f in flat)


def test_pipeline_dev_site_failure_is_a_note_not_an_error(tmp_path):
    edges = _Edges(http_status=502)
    result, _ = _run(tmp_path, edges=edges)
    assert "502" in result.magi_note


def test_default_config_keeps_legacy_rubric_do_path(tmp_path):
    from worksweep.config import load_config
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "hb.json")
        with open(p, "w") as f:
            json.dump({"discord_webhook": "x"}, f)
        assert load_config(p).pipeline_command == ""
