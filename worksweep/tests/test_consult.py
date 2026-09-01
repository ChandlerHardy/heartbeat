"""Send-to-Fable: the consult module and the runner's consult pass.

The lane's contract: a parked (`needs-input`) row's question gets ONE
read-only claude pass whose structured recommendation lands back on the row;
the human accepts it into a ruling or ignores it. Advisory by construction —
these tests pin the read-only tool scope, the strict-but-forgiving JSON
contract, and the pass's requested -> done/error lifecycle.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from worksweep import consult
from worksweep.config import WorksweepConfig
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import RunnerError, _run_consult_pass, _set_consult

NOW = "2026-09-01T16:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(repos=("pb-www",), username="chandler.hardy",
                discord_webhook="https://discord.com/api/webhooks/x/y",
                checkouts_root=str(tmp_path), claude_bin="claude")
    base.update(kw)
    return WorksweepConfig(**base)


def _item(status="needs-input", consult="", consult_rec="", kind="feedback",
          **kw):
    base = dict(schema_version=1, id="feedback:pb-www!4098", repo="pb-www",
                kind=kind, executor="address-feedback", risk="low",
                why="2 unaddressed threads",
                web_url="https://gl/x/-/merge_requests/4098", sha="s",
                status=status, branch="fix/1607",
                error_summary="2 threads need your call - the TypeError gate",
                consult=consult, consult_rec=consult_rec)
    base.update(kw)
    return WorkItem(**base)


def _rec(n, **kw):
    return QueueRecord(number=n, first_seen=NOW, last_seen=NOW,
                       item=_item(**kw))


REC_JSON = json.dumps({"decision": "Decline the change in this MR.",
                       "why": "The window predates the MR.",
                       "rejected": "Fixing inline — reopens the gate."})


# --- parse_rec: strict JSON, held loosely ------------------------------------

def test_a_clean_json_answer_parses():
    rec = consult.parse_rec(REC_JSON)
    assert rec["decision"] == "Decline the change in this MR."
    assert rec["why"] and rec["rejected"]


def test_prose_wrapped_json_still_parses():
    """claude -p wraps answers in prose/fences often enough that failing the
    run over formatting would make the button flaky by design."""
    raw = f"Here is my recommendation:\n```json\n{REC_JSON}\n```\nGood luck!"
    assert consult.parse_rec(raw)["decision"].startswith("Decline")


def test_an_answer_without_a_decision_fails_the_run():
    """FALSIFYING: a rec the human could accept must always carry a decision
    — an empty one accepted into a ruling would hand the executor nothing."""
    with pytest.raises(RunnerError):
        consult.parse_rec(json.dumps({"why": "thoughts", "rejected": ""}))
    with pytest.raises(RunnerError):
        consult.parse_rec("no json here at all")


def test_missing_optional_fields_degrade_to_empty():
    rec = consult.parse_rec(json.dumps({"decision": "Escalate to a session."}))
    assert rec == {"decision": "Escalate to a session.",
                   "why": "", "rejected": ""}


# --- render_rec: one row-sized string ---------------------------------------

def test_the_rec_renders_as_labelled_sentences():
    text = consult.render_rec({"decision": "Do X.", "why": "Because Y.",
                               "rejected": "Z loses because W."})
    assert text == "Do X.  ·  Why: Because Y.  ·  Rejected: Z loses because W."


def test_an_oversized_rec_is_truncated_for_the_row():
    text = consult.render_rec({"decision": "d" * 2000, "why": "", "rejected": ""})
    assert len(text) <= consult._REC_MAX
    assert text.endswith("…")


# --- the prompt --------------------------------------------------------------

def test_the_prompt_carries_the_question_and_the_fence_rule():
    p = consult.render_consult_prompt(_item(), "")
    assert "2 threads need your call" in p
    assert consult._FENCE_TOKEN in p
    assert "STRICT JSON" in p
    assert "read-only" in p.lower() or "Read/Grep/Glob" in p


def test_thread_bodies_arrive_fenced_and_only_open_ones():
    threads = json.dumps([
        {"id": "t-open", "notes": [
            {"body": "please fix", "system": False, "resolvable": True,
             "resolved": False, "author": {"username": "edmundlim"}}]},
        {"id": "t-closed", "notes": [
            {"body": "done already", "system": False, "resolvable": True,
             "resolved": True, "author": {"username": "edmundlim"}}]},
    ])
    with patch("worksweep.consult._fetch_threads", return_value=(threads,)):
        block = consult._threads_block(_item(), run_glab=lambda *a, **k: "")
    assert "t-open" in block and "please fix" in block
    assert consult._fence_begin("t-open") in block
    assert "t-closed" not in block


def test_a_non_feedback_item_gets_no_thread_block():
    assert consult._threads_block(_item(kind="mr", id="mr:pb-www!1"),
                                  run_glab=lambda *a, **k: "") == ""


def test_a_failed_thread_fetch_degrades_to_no_block():
    """Best-effort: the parked question itself usually carries the ask."""
    def boom(*a, **k):
        raise RuntimeError("gitlab down")
    with patch("worksweep.consult._fetch_threads", side_effect=boom):
        assert consult._threads_block(_item(), run_glab=boom) == ""


# --- execute_consult: the run edge ------------------------------------------

def _fake_subprocess(stdout=REC_JSON, returncode=0, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


def test_execute_returns_the_rendered_rec(tmp_path):
    seen = []
    with patch.object(consult.checkouts, "worktree_for",
                      return_value=str(tmp_path)):
        text = consult.execute_consult(_item(), _cfg(tmp_path),
                                       run_subprocess=_fake_subprocess(seen=seen),
                                       run_glab=None)
    assert text.startswith("Decline the change")
    assert "Why:" in text


def test_the_run_is_read_only_and_never_gets_bash(tmp_path):
    """FALSIFYING: the fenced thread bodies are attacker-writable, so the
    consult's tool scope is the security boundary. Bash in the argv is the
    one-line regression this pins against."""
    seen = []
    with patch.object(consult.checkouts, "worktree_for",
                      return_value=str(tmp_path)):
        consult.execute_consult(_item(), _cfg(tmp_path),
                                run_subprocess=_fake_subprocess(seen=seen),
                                run_glab=None)
    claude_cmd = next(c for c in seen if c[0] == "claude")
    tools = claude_cmd[claude_cmd.index("--allowedTools") + 1]
    assert tools == "Read,Grep,Glob"
    assert "Bash" not in tools and "Edit" not in tools and "Write" not in tools


def test_a_configured_model_reaches_the_argv(tmp_path):
    seen = []
    with patch.object(consult.checkouts, "worktree_for",
                      return_value=str(tmp_path)):
        consult.execute_consult(_item(),
                                _cfg(tmp_path, consult_model="claude-fable-5"),
                                run_subprocess=_fake_subprocess(seen=seen),
                                run_glab=None)
    claude_cmd = next(c for c in seen if c[0] == "claude")
    assert claude_cmd[claude_cmd.index("--model") + 1] == "claude-fable-5"


def test_a_failed_run_raises(tmp_path):
    with patch.object(consult.checkouts, "worktree_for",
                      return_value=str(tmp_path)):
        with pytest.raises(RunnerError):
            consult.execute_consult(
                _item(), _cfg(tmp_path),
                run_subprocess=_fake_subprocess(returncode=1, stdout="boom"),
                run_glab=None)


# --- the runner's consult pass ----------------------------------------------

def _deps(records, execute=None, posts=None):
    posts = posts if posts is not None else []
    state = {"records": list(records), "saves": []}
    d = {"load": lambda: list(state["records"]),
         "save": lambda recs: (state.update(records=list(recs)),
                               state["saves"].append(list(recs)))[0],
         "post": lambda hook, content: posts.append(content),
         "now": lambda: NOW}
    if execute is not None:
        d["execute_consult"] = execute
    return d, posts, state


def _by_num(state, n):
    return next(r for r in state["records"] if r.number == n)


def test_a_requested_consult_lands_its_rec_on_the_row(tmp_path):
    deps, posts, state = _deps([_rec(7, consult="requested")],
                               execute=lambda item, cfg: "Do X.  ·  Why: Y.")
    rc = _run_consult_pass(_cfg(tmp_path), deps,
                           str(tmp_path / "consult.lock"))
    assert rc == 0
    row = _by_num(state, 7)
    assert row.item.consult == "done"
    assert row.item.consult_rec == "Do X.  ·  Why: Y."
    assert row.item.status == "needs-input"          # the human still decides
    assert any("🔮" in p and "#7" in p for p in posts)


def test_a_failed_consult_flips_to_error_and_posts(tmp_path):
    def boom(item, cfg):
        raise RunnerError("claude fell over")
    deps, posts, state = _deps([_rec(7, consult="requested")], execute=boom)
    rc = _run_consult_pass(_cfg(tmp_path), deps,
                           str(tmp_path / "consult.lock"))
    assert rc == 1
    assert _by_num(state, 7).item.consult == "error"
    assert any("⚠️" in p and "#7" in p for p in posts)


def test_an_unwired_runner_is_inert():
    """Old callers/tests carry no execute_consult dep; the pass must be a
    no-op, never a crash-and-post."""
    deps, posts, state = _deps([_rec(7, consult="requested")], execute=None)
    assert _run_consult_pass(None, deps, "/nonexistent/consult.lock") == 0
    assert posts == []


def test_nothing_requested_means_nothing_runs(tmp_path):
    calls = []
    deps, posts, state = _deps(
        [_rec(7, consult=""), _rec(8, consult="done", consult_rec="r")],
        execute=lambda item, cfg: calls.append(item) or "rec")
    assert _run_consult_pass(_cfg(tmp_path), deps,
                             str(tmp_path / "consult.lock")) == 0
    assert calls == []


def test_a_rec_is_never_written_onto_a_row_that_moved_on():
    """FALSIFYING: the row was approved/dismissed mid-consult. Writing the rec
    anyway would offer an Accept for a question nobody is asking any more."""
    deps, posts, state = _deps([_rec(7, status="approved",
                                     consult="requested")])
    deps["execute_consult"] = lambda item, cfg: "stale rec"
    assert _set_consult(deps, 7, "done", "stale rec") is False
    assert state["saves"] == []
    assert _by_num(state, 7).item.consult_rec == ""
