"""curator.py: prompt building, deterministic validator, and the injected
run_llm orchestration edge. All LLM calls are injected — no subprocess runs
in these tests except the make_run_llm edge tests, which inject
run_subprocess (same pattern as runner.execute)."""
import subprocess

import pytest

from worksweep.curator import (
    build_prompt, curate, make_run_llm, partition_counts, validate,
)
from worksweep.models import QueueRecord, WorkItem

NOW = "2026-08-17T12:00:00+00:00"


def _wi(kind="review_request", executor="magi-review", status="proposed",
       repo="pb-www", why="review requested", iid=4061, sha="abc"):
    return WorkItem(schema_version=1, id=f"x{iid}", repo=repo, kind=kind,
                    executor=executor, risk="low", why=why,
                    web_url=f"https://gitlab.com/x/-/merge_requests/{iid}",
                    sha=sha, status=status)


def _rec(number, wi, first_seen=NOW):
    return QueueRecord(number=number, first_seen=first_seen, last_seen=NOW, item=wi)


# --- build_prompt --------------------------------------------------------

def test_build_prompt_includes_one_line_per_record_with_all_fields():
    # Task E: record lines gain a trailing `| title` field (blank when the
    # WorkItem has no title, as here).
    recs = [_rec(1, _wi(why="review requested"))]
    prompt = build_prompt(recs, NOW)
    line = "1 | review_request | magi-review | pb-www | 4061 | review requested | 0 | proposed | "
    assert line in prompt


def test_build_prompt_computes_age_days_from_first_seen():
    old = "2026-08-05T12:00:00+00:00"  # 12 days before NOW
    recs = [_rec(2, _wi(iid=99), first_seen=old)]
    prompt = build_prompt(recs, NOW)
    assert "2 | review_request | magi-review | pb-www | 99 | review requested | 12 | proposed | " in prompt


def test_build_prompt_unparseable_first_seen_leaves_age_blank():
    recs = [_rec(3, _wi(iid=5), first_seen="not-a-date")]
    prompt = build_prompt(recs, NOW)
    assert "3 | review_request | magi-review | pb-www | 5 | review requested |  | proposed | " in prompt


# --- validate --------------------------------------------------------------

def _queue(*, magi_status="proposed"):
    return [
        _rec(1, _wi(iid=4061, executor="magi-review", status=magi_status)),
        _rec(2, _wi(iid=4062, kind="feedback", executor="triage",
                    status="proposed", why="changes requested")),
        _rec(43, _wi(iid=4063, kind="mr", executor="mr-hygiene",
                     status="proposed", why="missing dev link")),
        _rec(44, _wi(iid=4064, kind="todo", executor="triage",
                     status="proposed", why="mentioned")),
    ]


def test_validate_accepts_good_briefing():
    out = ("**Needs your review:**\n1. pb-www !4061 — review requested\n\n"
           "**Feedback / CI:**\n2. pb-www !4062 — changes requested\n\n"
           "2 low-priority items held in queue: 43, 44")
    assert validate(out, _queue()) is True


def test_validate_rejects_invented_number():
    out = ("**Needs your review:**\n1. pb-www !4061 — review requested\n"
           "2 low-priority items held in queue: 43, 44, 999")
    assert validate(out, _queue()) is False


def test_validate_rejects_missing_magi_review_number():
    # #1 is proposed magi-review but never referenced anywhere in the output.
    out = "2 low-priority items held in queue: 43, 44"
    assert validate(out, _queue()) is False


def test_validate_accepts_when_magi_review_item_is_approved_too():
    out = "1. pb-www !4061 — ready\n3 low-priority items held in queue: 2, 43, 44"
    assert validate(out, _queue(magi_status="approved")) is True


def test_validate_rejects_oversized_output():
    out = "1. pb-www !4061 — " + ("x" * 2000)
    assert validate(out, _queue()) is False


def test_validate_rejects_empty_output():
    assert validate("", _queue()) is False
    assert validate("   \n  ", _queue()) is False


def test_validate_strips_age_tokens_before_scanning():
    # (12d) is an age marker, not a queue number -- 12 is not in the queue's
    # allowed set, but the stripping rule must exempt it.
    out = "1. pb-www !4061 — review requested (12d)"
    assert validate(out, _queue()) is True


def test_validate_strips_held_count_tally_before_scanning():
    # The leading count in "N low-priority items held in queue: ..." is a
    # tally, not a queue number, and routinely won't coincide with one.
    out = "5 low-priority items held in queue: 43, 44"
    assert validate(out, _queue()) is False  # magi #1 still unreferenced
    out2 = "1. pb-www !4061 — x\n5 low-priority items held in queue: 43, 44"
    assert validate(out2, _queue()) is True


# Critical fix: a link/URL is a hard rejection, not stripped-then-allowed.
# This is the injection bound -- an untrusted MR/issue title riding into the
# prompt via `why` can make the LLM emit arbitrary prose, but it must never
# be able to turn that into a clickable link posted straight to Discord.
def test_validate_rejects_bare_https_url():
    out = "1. pb-www !4061 -- review requested, see https://evil.example.com/x"
    assert validate(out, _queue()) is False


def test_validate_rejects_markdown_link():
    out = "1. pb-www [!4061](https://gitlab.com/x/-/merge_requests/4061) -- review requested"
    assert validate(out, _queue()) is False


def test_validate_accepts_clean_output_with_no_links():
    out = "1. pb-www !4061 -- review requested\n1 low-priority items held in queue: 43"
    assert validate(out, _queue()) is True


def test_validator_accepts_why_digit_reuse_documented_risk():
    """Accepted residual risk (see _allowed_numbers docstring): the
    why-digit whitelist is global across all records, not scoped per-record.
    The prompt's line format echoes `why` verbatim; a count that already
    exists in our own trusted input (here: "2 unresolved threads") is
    accepted as a faithful quote even though 2 isn't a queue/ref number in
    this fixture. This is deliberately not hardened further -- an invented
    small-number reference is cosmetic (a ✅ reply against a number nobody
    holds status on is a no-op in intake), and the one hard invariant
    (every proposed/approved magi-review number is referenced) is checked
    independently and unaffected by this widening. This test pins the
    current, accepted behavior."""
    recs = [_rec(1, _wi(iid=4061, kind="feedback", executor="triage",
                        status="proposed", why="2 unresolved threads"))]
    out = "1. pb-www !4061 -- 2 unresolved threads"
    assert validate(out, recs) is True


def test_validate_still_rejects_a_number_absent_from_every_why_text():
    recs = [_rec(1, _wi(iid=4061, kind="feedback", executor="triage",
                        status="proposed", why="changes requested"))]
    out = "1. pb-www !4061 -- 7 unresolved threads"
    assert validate(out, recs) is False


# --- curate ------------------------------------------------------------

def test_curate_returns_validated_llm_output():
    recs = _queue()
    good = "1. pb-www !4061 — review requested\n3 low-priority items held in queue: 2, 43, 44"
    result = curate(recs, NOW, run_llm=lambda prompt: good)
    assert result == good


def test_curate_returns_none_when_run_llm_raises():
    recs = _queue()
    def boom(prompt):
        raise RuntimeError("claude timed out")
    assert curate(recs, NOW, run_llm=boom) is None


def test_curate_returns_none_when_output_fails_validation():
    recs = _queue()
    bad = "1. pb-www !4061 — review requested\nheld: 999"
    assert curate(recs, NOW, run_llm=lambda prompt: bad) is None


def test_curate_returns_none_for_empty_records():
    assert curate([], NOW, run_llm=lambda prompt: "anything") is None


def test_curate_passes_a_prompt_containing_all_queue_numbers():
    recs = _queue()
    seen = {}
    def capture(prompt):
        seen["prompt"] = prompt
        return "1. pb-www !4061 — x\n3 low-priority items held in queue: 2, 43, 44"
    curate(recs, NOW, run_llm=capture)
    for n in (1, 2, 43, 44):
        assert f"{n} | " in seen["prompt"]


# --- partition_counts ----------------------------------------------------

def test_partition_counts_splits_actionable_from_held():
    recs = _queue()
    n, m = partition_counts(recs)
    # #1 (magi-review proposed) and #2 (triage feedback) are actionable leads;
    # #43 (mr-hygiene) and #44 (todo) are held.
    assert (n, m) == (2, 2)


# --- make_run_llm (subprocess edge, injected run_subprocess) -------------

class _Cfg:
    claude_bin = "claude"


def test_make_run_llm_returns_stdout_on_success():
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="curated text\n", stderr="")
    run_llm = make_run_llm(_Cfg(), run_subprocess=fake_run)
    assert run_llm("prompt text") == "curated text\n"


def test_make_run_llm_invokes_claude_dash_p_with_prompt():
    calls = []
    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    run_llm = make_run_llm(_Cfg(), run_subprocess=fake_run)
    run_llm("the prompt")
    cmd, kw = calls[0]
    assert cmd == ("claude", "-p", "the prompt")
    assert kw["timeout"] == 120
    assert "cwd" in kw and kw["cwd"]


def test_make_run_llm_raises_on_nonzero_exit():
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
    run_llm = make_run_llm(_Cfg(), run_subprocess=fake_run)
    with pytest.raises(Exception, match="boom"):
        run_llm("prompt")


def test_make_run_llm_raises_on_timeout():
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 120))
    run_llm = make_run_llm(_Cfg(), run_subprocess=fake_run)
    with pytest.raises(Exception, match="120"):
        run_llm("prompt")
