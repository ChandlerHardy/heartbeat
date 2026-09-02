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


def test_pipeline_dev_site_failure_fails_the_run(tmp_path):
    """SUPERSEDED by f-022 (tribunal, 2026-08-26). This used to assert the
    502 became a note on a SUCCESS result -- so the runner completed the item
    and announced a QA-complete implementation nobody had verified. The M5
    contract is that the box serves the branch, so failing to prove it is a
    failed run."""
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=_Edges(http_status=502))
    assert "502" in str(e.value)


def test_default_config_keeps_legacy_rubric_do_path(tmp_path):
    from worksweep.config import load_config
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "hb.json")
        with open(p, "w") as f:
            json.dump({"discord_webhook": "x"}, f)
        assert load_config(p).pipeline_command == ""


# --- f-021 / f-022: the pipeline path must PROVE, not assume ---------------

def test_a_stale_state_file_is_never_read_as_this_runs_work(tmp_path):
    """f-021. The worktree is permanent and `_find_pipeline_state` takes the
    first matching issue directory. A run that exits zero without writing
    state was therefore reported against a PREVIOUS run's MR -- a completed
    queue item pointing at somebody else's work."""
    edges = _Edges(write_state=None)          # this run leaves nothing behind
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    stale_dir = (tmp_path / ".worktrees" / "pb-www-implement" / ".claude"
                 / "state" / "pla-pipelines" / "1775-yesterdays-run")
    stale_dir.mkdir(parents=True)
    (stale_dir / "state.md").write_text(
        "- [x] 7. MR — https://gitlab.com/x/-/merge_requests/4001 (draft)\n")

    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=edges)
    assert "no state file" in str(e.value)


def test_the_stale_state_is_actually_removed_before_the_run(tmp_path):
    """Not merely ignored: cleared, so nothing downstream can find it."""
    edges = _Edges()
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    root = (tmp_path / ".worktrees" / "pb-www-implement" / ".claude"
            / "state" / "pla-pipelines")
    stale = root / "1775-yesterdays-run"
    stale.mkdir(parents=True)
    (stale / "state.md").write_text("- [x] 7. MR — !4001\n")

    result, _ = _run(tmp_path, edges=edges)
    assert result.mr_iid == 4099              # THIS run's MR, not 4001
    assert not (stale / "state.md").exists()


def test_state_for_another_issue_is_left_alone(tmp_path):
    """Only this issue's state is cleared -- a concurrent or earlier run for a
    different issue is none of our business."""
    edges = _Edges()
    (tmp_path / "pb-www").mkdir(exist_ok=True)
    root = (tmp_path / ".worktrees" / "pb-www-implement" / ".claude"
            / "state" / "pla-pipelines")
    other = root / "1701-other-issue"
    other.mkdir(parents=True)
    (other / "state.md").write_text("- [x] 7. MR — !4002\n")

    _run(tmp_path, edges=edges)
    assert (other / "state.md").exists()


def test_a_failed_dev_box_probe_fails_the_run(tmp_path):
    """f-022. The M5 contract is 'the box serves 200'. A probe exception was
    demoted to a note on a SUCCESS message, so the runner completed the item
    and posted a parked, QA-complete implementation nobody had verified."""
    class _Boom(_Edges):
        def http(self, url):
            self.http_calls.append(url)
            raise OSError("connection refused")

    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=_Boom())
    assert "connection refused" in str(e.value)


def test_a_non_200_dev_box_fails_the_run(tmp_path):
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=_Edges(http_status=502))
    assert "502" in str(e.value)


def test_a_healthy_box_still_completes_normally(tmp_path):
    result, edges = _run(tmp_path, edges=_Edges(http_status=200))
    assert result.mr_iid == 4099
    assert edges.http_calls == [_box().url]


# --- the Mongo/DB domain gate (team policy, 2026-08-28) -------------------
#
# LeifPedersen owns the Mongo domain, and the new rule is that schema and
# \DB\Mongo changes get his sign-off BEFORE an MR exists. An unattended run
# that opens a Draft MR touching those paths has already skipped the gate --
# the review has to happen pre-MR or it is not the gate the team agreed.

def _constraints():
    from worksweep.implementer import _PIPELINE_CONSTRAINTS
    from worksweep.models import domain_gate_text
    return _PIPELINE_CONSTRAINTS.format(box="dev2", gate=domain_gate_text())


def test_the_pipeline_prompt_names_every_gated_path():
    from worksweep.models import DOMAIN_GATE_PATHS
    text = _constraints()
    for path in DOMAIN_GATE_PATHS:
        assert path in text, path
    assert "MySQL schema" in text


def test_the_pipeline_prompt_halts_rather_than_opening_an_mr():
    """FALSIFYING. The whole point is that no MR exists yet when Leif looks."""
    from worksweep.implementer import HALT_MARKERS
    text = _constraints()
    assert any(m in text for m in HALT_MARKERS)
    assert "Leif" in text
    assert "do NOT create the MR" in text or "not create the MR" in text


def test_the_gate_is_an_explicit_exception_to_never_stop_for_input():
    """The same prompt says "Chandler is away: never stop for input". Without
    an explicit carve-out the two instructions contradict, and the louder,
    earlier one wins -- which is the one that opens the MR."""
    text = _constraints()
    assert "never stop for input" in text
    stop = text.index("never stop for input")
    gate = text.index("Leif")
    assert gate > stop                      # the exception comes after the rule
    assert "one exception" in text or "except" in text.lower()


def test_the_run_actually_receives_the_gate(tmp_path):
    """Pinned through the production path, not just the constant: a prompt
    built without the gate would pass every assertion above and still ship."""
    _, edges = _run(tmp_path)
    prompt = [c for c, _ in edges.calls if c[0] == "claude"][0][2]
    assert "phplib/local/DB/" in prompt
    assert "Leif" in prompt


# --- the gate ENFORCED, not just asked for (2026-08-28) -------------------
#
# The prompt tells the run to halt before creating an MR on Leif's domain.
# This is what happens when it does not: the MR already exists, so the only
# honest outcome is a loud failure naming what has to be unwound.

class _Touched(_Edges):
    """_Edges whose branch diff reports whatever the test says it changed."""

    def __init__(self, changed=(), diff_rc=0, **kw):
        super().__init__(**kw)
        self.changed = list(changed)
        self.diff_rc = diff_rc
        self.diff_calls = []

    def run(self, cmd, **kw):
        c = list(cmd)
        if c[3:4] == ["diff"] and "--name-only" in c:
            self.diff_calls.append(c)
            return subprocess.CompletedProcess(
                c, self.diff_rc,
                stdout="\n".join(self.changed) + "\n",
                stderr="fatal: bad revision 'origin/master'\n")
        return super().run(cmd, **kw)


def test_a_gated_file_in_the_branch_fails_the_run(tmp_path):
    """FALSIFYING. Without this the pipeline accepts the state file, confirms
    the Draft, and reports a completed implementation that skipped the gate."""
    edges = _Touched(changed=["www/home/php/x.php", "phplib/local/DB/Mongo.php"])
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=edges)
    assert "phplib/local/DB/Mongo.php" in str(e.value)
    assert "domain gate" in str(e.value)
    assert "Close it and loop Leif in" in str(e.value)


def test_the_gate_failure_is_greppable_and_not_a_generic_error(tmp_path):
    """Its own reason string: this is the one failure Chandler must act on
    differently from every other ⚠️ -- there is an MR to go close."""
    from worksweep.implementer import DOMAIN_GATE_VIOLATION
    edges = _Touched(changed=["db/migrations/x.sql"])
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=edges)
    assert DOMAIN_GATE_VIOLATION in str(e.value)


def test_the_diff_is_taken_against_the_merge_base(tmp_path):
    """Against master's tip, an unrelated master-side Mongo change would fail
    a perfectly innocent branch."""
    edges = _Touched(changed=["www/home/php/x.php"])
    _run(tmp_path, edges=edges)
    assert edges.diff_calls, "no diff was taken at all"
    assert "origin/master...HEAD" in edges.diff_calls[0]


def test_an_ungated_branch_completes_normally(tmp_path):
    result, edges = _run(tmp_path, edges=_Touched(
        changed=["www/home/php/x.php", "phplib/local/Analytics.php"]))
    assert result.mr_iid == 4099


def test_a_test_only_mongo_change_is_allowed_through(tmp_path):
    """The exclusion has to hold end to end, or "add a test for this" becomes
    an ask the executor structurally cannot satisfy."""
    result, _ = _run(tmp_path, edges=_Touched(
        changed=["test/phpunit/mongo/MongoDuplicateKeyTest.php"]))
    assert result.mr_iid == 4099


def test_the_gate_is_checked_before_the_state_file_is_accepted(tmp_path):
    """Order matters: reporting "no state file" for a gated run would send
    Chandler looking for the wrong problem."""
    edges = _Touched(changed=["phplib/local/DB/Mongo.php"], write_state=None)
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=edges)
    assert "domain gate" in str(e.value)
    assert "no state file" not in str(e.value)


def test_an_unreadable_diff_fails_rather_than_passing(tmp_path):
    """Not knowing what the branch touched is not the same as knowing it is
    clean. This check is the only thing between a schema change and an
    accepted MR, so it fails closed."""
    from worksweep.implementer import DOMAIN_GATE_VIOLATION
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=_Touched(changed=[], diff_rc=128))
    assert DOMAIN_GATE_VIOLATION in str(e.value)
    assert "could not diff" in str(e.value)


# --- in-claim resume (2026-09-01) -------------------------------------------
#
# An unattended `claude -p` pipeline run routinely ends itself mid-phase; the
# checkpoint survives, so the executor resumes INSIDE the claim while the
# checkpoint advances. Before this, every early exit cost a reap -> sweep ->
# re-approve -> next-fire cycle of 15-30 dead minutes.

_STATE_PHASE3 = "---\nphase: 3\n---\n- [x] 0. Orient\n- [x] 1. Trace\n- [x] 2. Decide\n"
_STATE_PHASE5 = "---\nphase: 5\n---\n- [x] 0. Orient\n- [x] 1. Trace\n- [x] 2. Decide\n- [x] 3. Implement\n- [x] 4. Ship gate\n"


class _ResumingEdges(_Edges):
    """Like _Edges, but each claude call writes the NEXT state in sequence."""

    def __init__(self, states, **kw):
        super().__init__(**kw)
        self.states = list(states)
        self.claude_calls = 0

    def run(self, cmd, **kw):
        c = list(cmd)
        if c[0] == "claude":
            self.write_state = self.states[
                min(self.claude_calls, len(self.states) - 1)]
            self.claude_calls += 1
        return super().run(cmd, **kw)


def test_an_early_exit_with_progress_resumes_inside_the_claim(tmp_path):
    """FALSIFYING for the whole feature: attempt 1 ends at phase 3 with no
    MR; the old executor raised there and the queue burned a ✅. Now the
    claim itself runs the pipeline again, and attempt 2's Phase-7 state
    completes normally."""
    edges = _ResumingEdges([_STATE_PHASE3, _STATE])
    result, edges = _run(tmp_path, edges=edges)
    assert edges.claude_calls == 2
    assert result.mr_iid == 4099


def test_no_progress_between_attempts_fails_instead_of_spinning(tmp_path):
    """A pipeline that cannot move is a stuck pipeline: same checkpoint twice
    means raise the honest error, never a third token-burning attempt."""
    edges = _ResumingEdges([_STATE_PHASE3, _STATE_PHASE3, _STATE_PHASE3])
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=edges)
    assert edges.claude_calls == 2
    assert "did not reach Phase 7" in str(e.value)
    assert "2 attempt(s)" in str(e.value)


def test_the_attempts_cap_bounds_the_loop(tmp_path):
    """Progress on every attempt still ends at the cap — the reap window is
    sized to implement_timeout, and an unbounded resumer would outlive it."""
    edges = _ResumingEdges([_STATE_PHASE3, _STATE_PHASE5, _STATE_PHASE5])
    with pytest.raises(RunnerError) as e:
        _run(tmp_path, edges=edges,
             cfg=_cfg(tmp_path, pipeline_resume_attempts=2))
    assert edges.claude_calls == 2
    assert "did not reach Phase 7" in str(e.value)


def test_a_first_attempt_that_reaches_phase7_never_resumes(tmp_path):
    edges = _ResumingEdges([_STATE])
    result, edges = _run(tmp_path, edges=edges)
    assert edges.claude_calls == 1
    assert result.mr_iid == 4099


def test_a_halt_on_a_resumed_attempt_still_parks(tmp_path):
    """The domain gate / needs-input contract survives the loop: a resumed
    leg that halts must park the row, not be retried into compliance."""
    class _HaltSecond(_ResumingEdges):
        def run(self, cmd, **kw):
            c = list(cmd)
            if c[0] == "claude" and self.claude_calls == 1:
                self.claude_out = "HALT_INSUFFICIENT_CONTEXT: which endpoint owns this?\n"
            return super().run(cmd, **kw)
    edges = _HaltSecond([_STATE_PHASE3, _STATE_PHASE3])
    with pytest.raises(NeedsInputError):
        _run(tmp_path, edges=edges)
    assert edges.claude_calls == 2


# --- session resume (2026-09-01): legs reopen their own session -------------
#
# The dominant resume cost was re-orientation: every fresh `claude -p` leg
# spent 10-20 minutes re-deriving its working set from artifacts. A resumed
# leg reopens the previous leg's session (--resume <id>) and remembers
# everything; the session id travels via --output-format json.

import json as _json


def _leg_json(text, session="sess-1"):
    return _json.dumps({"type": "result", "result": text,
                        "session_id": session})


class _ScriptedEdges(_Edges):
    """Each claude call takes the next (stdout, rc, state) triple."""

    def __init__(self, legs, **kw):
        super().__init__(**kw)
        self.legs = list(legs)
        self.claude_argvs = []

    def run(self, cmd, **kw):
        c = list(cmd)
        if c[0] == "claude":
            self.claude_argvs.append(c)
            stdout, rc, state = self.legs[
                min(len(self.claude_argvs) - 1, len(self.legs) - 1)]
            self.claude_out, self.claude_rc, self.write_state = stdout, rc, state
        return super().run(cmd, **kw)


def test_a_resumed_leg_reopens_the_previous_session(tmp_path):
    """FALSIFYING for the whole feature: leg 2 must carry --resume with leg
    1's session id, not restart cold with the full prompt."""
    edges = _ScriptedEdges([
        (_leg_json("ended early", "sess-abc"), 0, _STATE_PHASE3),
        (_leg_json("done", "sess-abc"), 0, _STATE),
    ])
    result, edges = _run(tmp_path, edges=edges)
    assert result.mr_iid == 4099
    assert len(edges.claude_argvs) == 2
    leg2 = edges.claude_argvs[1]
    assert leg2[1:3] == ["--resume", "sess-abc"]
    assert "Continue." in leg2[4]


def test_a_plain_text_leg_yields_no_session_and_fresh_legs(tmp_path):
    """Backwards compatibility: output that isn't the JSON envelope degrades
    to the old behaviour — raw transcript, cold resume legs."""
    edges = _ScriptedEdges([
        ("just plain text\n", 0, _STATE_PHASE3),
        ("plain again\n", 0, _STATE),
    ])
    result, edges = _run(tmp_path, edges=edges)
    assert result.mr_iid == 4099
    leg2 = edges.claude_argvs[1]
    assert "--resume" not in leg2
    assert leg2[1] == "-p"          # the full prompt, cold


def test_halt_markers_survive_the_json_envelope(tmp_path):
    """detect_halt must read the RESULT TEXT, not the JSON blob — a halt
    inside an escaped JSON string still parks the row."""
    edges = _ScriptedEdges([
        (_leg_json("...\nHALT_INSUFFICIENT_CONTEXT: which sheet?\n"), 1,
         _STATE_PHASE3),
    ])
    with pytest.raises(NeedsInputError):
        _run(tmp_path, edges=edges)


def test_a_vanished_session_retries_the_leg_fresh(tmp_path):
    """A --resume whose session is gone is a broken RESUME, not a broken
    pipeline: the same leg retries cold instead of failing the claim."""
    edges = _ScriptedEdges([
        (_leg_json("ended early", "sess-gone"), 0, _STATE_PHASE3),
        ("No conversation found with session ID sess-gone", 1, None),
        (_leg_json("done", "sess-new"), 0, _STATE),
    ])
    result, edges = _run(tmp_path, edges=edges)
    assert result.mr_iid == 4099
    assert len(edges.claude_argvs) == 3
    assert edges.claude_argvs[1][1] == "--resume"
    assert edges.claude_argvs[2][1] == "-p"      # the cold retry
