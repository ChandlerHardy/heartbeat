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


# --- M4 Task H: `stale` (keep-current) items in rule 2 --------------------

def test_partition_counts_treats_stale_as_actionable_not_held():
    recs = _queue() + [_rec(45, _wi(iid=4065, kind="stale",
                                    executor="keep-current", status="proposed",
                                    why="7 commits behind master"))]
    n, m = partition_counts(recs)
    assert (n, m) == (3, 2)          # stale joins the actionable count


def test_partition_counts_ignores_done_stale_items():
    recs = _queue() + [_rec(45, _wi(iid=4065, kind="stale",
                                    executor="keep-current", status="done",
                                    why="7 commits behind master"))]
    n, m = partition_counts(recs)
    assert (n, m) == (2, 3)          # done -> not a live ask, falls to held


def test_stale_kind_documented_in_rule_2():
    from worksweep.curator import _INSTRUCTIONS
    assert "`stale`" in _INSTRUCTIONS
    assert "keep-current" in _INSTRUCTIONS


def test_validate_accepts_stale_item_number_without_requiring_it():
    """A stale item is whitelisted (its own queue number is always allowed —
    _allowed_numbers is global, not kind-filtered) but NOT required: a
    briefing that omits it entirely still validates, unlike a proposed
    magi-review item."""
    recs = _queue() + [_rec(45, _wi(iid=4065, kind="stale",
                                    executor="keep-current", status="proposed",
                                    why="7 commits behind master"))]
    out = ("**Needs your review:**\n1. pb-www !4061 — review requested\n\n"
          "**Feedback / CI:**\n2. pb-www !4062 — changes requested\n"
          "45. pb-www !4065 — 7 commits behind master\n\n"
          "2 low-priority items held in queue: 43, 44")
    assert validate(out, recs) is True
    # omitting #45 entirely is still valid -- stale items are not required
    out_without_45 = ("**Needs your review:**\n1. pb-www !4061 — review requested\n\n"
                      "**Feedback / CI:**\n2. pb-www !4062 — changes requested\n\n"
                      "2 low-priority items held in queue: 43, 44")
    assert validate(out_without_45, recs) is True


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


# --- M4 Task F: dev-slot preamble ------------------------------------------

_SLOT_LINE = "Dev slots: dev1 free · dev4, dev5 reclaimable (approved, awaiting merge) · dev0 live"


def test_build_prompt_includes_preamble_context_when_given():
    recs = [_rec(1, _wi())]
    prompt = build_prompt(recs, NOW, preamble=_SLOT_LINE)
    assert _SLOT_LINE in prompt
    # Context must precede the queue table, not get mixed into a record line.
    assert prompt.index(_SLOT_LINE) < prompt.index("1 | review_request")


def test_build_prompt_omits_preamble_block_when_none():
    recs = [_rec(1, _wi())]
    prompt = build_prompt(recs, NOW)
    assert "Dev slots:" not in prompt


def test_curate_passes_preamble_through_to_prompt():
    recs = [_rec(1, _wi())]
    seen = {}

    def fake_llm(prompt):
        seen["prompt"] = prompt
        return "Needs your review:\n1. pb-www !4061 -- t -- review requested"

    out = curate(recs, NOW, fake_llm, preamble=_SLOT_LINE)
    assert out is not None
    assert _SLOT_LINE in seen["prompt"]


def test_curate_preamble_does_not_break_validation():
    recs = [_rec(1, _wi())]
    # An LLM output that never echoes the preamble must still validate fine
    # -- preamble is context for the LLM, not a required output token.
    out = curate(recs, NOW,
                 lambda p: "Needs your review:\n1. pb-www !4061 -- t -- review requested",
                 preamble=_SLOT_LINE)
    assert out is not None


# --- assigned issues are first-class (2026-08-18 regression) ---------------

def _issue_rec(number, iid, title):
    from worksweep.models import QueueRecord, WorkItem
    return QueueRecord(number=number, first_seen="2026-08-17T00:00:00+00:00",
                       last_seen="2026-08-17T00:00:00+00:00",
                       item=WorkItem(schema_version=1, id=f"issue:pb-www#{iid}",
                                     repo="pb-www", kind="issue", executor="implement",
                                     risk="low", why=f"assigned issue: {title}",
                                     web_url=f"https://gitlab.com/x/-/work_items/{iid}",
                                     sha="", status="proposed", title=title))


def test_validate_rejects_output_that_drops_an_assigned_issue():
    from worksweep.curator import validate
    recs = [_issue_rec(175, 1775, "Discrepancy in estimated days left")]
    # LLM output that folded the issue into nothing (no 175 anywhere)
    assert validate("Needs your review:\n(none)\n0 low-priority items held in queue:", recs) is False


def test_validate_accepts_output_listing_the_assigned_issue():
    from worksweep.curator import validate
    recs = [_issue_rec(175, 1775, "Discrepancy in estimated days left")]
    out = "Assigned issues:\n175. pb-www #1775 -- Discrepancy in estimated days left -- ✅ to implement"
    assert validate(out, recs) is True


def test_partition_counts_treats_assigned_issue_as_actionable():
    from worksweep.curator import partition_counts
    n, m = partition_counts([_issue_rec(175, 1775, "x")])
    assert (n, m) == (1, 0)


def test_instructions_name_assigned_issues_section():
    from worksweep.curator import _INSTRUCTIONS
    assert "Assigned issues:" in _INSTRUCTIONS
    assert "NEVER fold it into the low-priority line" in _INSTRUCTIONS


def test_make_run_llm_passes_devnull_stdin():
    import subprocess as sp
    from worksweep.curator import make_run_llm
    from worksweep.config import WorksweepConfig
    seen = {}
    def fake_run(cmd, **kw):
        seen["stdin"] = kw.get("stdin")
        return sp.CompletedProcess(cmd, 0, stdout="ok", stderr="")
    cfg = WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="")
    make_run_llm(cfg, run_subprocess=fake_run)("prompt")
    assert seen["stdin"] is sp.DEVNULL


# --- linkify (deterministic links after validation) --------------------------

def _rec_with_url(number, id_, url, kind="review_request", executor="magi-review"):
    from worksweep.models import QueueRecord, WorkItem
    return QueueRecord(number=number, first_seen="t", last_seen="t",
                       item=WorkItem(schema_version=1, id=id_, repo="pb-www", kind=kind,
                                     executor=executor, risk="low", why="", web_url=url,
                                     sha="s", status="proposed"))


def test_linkify_mr_and_issue_refs():
    from worksweep.curator import linkify
    recs = [_rec_with_url(153, "review:pb-www!4010", "https://gitlab.com/g/pb-www/-/merge_requests/4010"),
            _rec_with_url(175, "issue:pb-www#1775", "https://gitlab.com/g/pb-www/-/work_items/1775",
                          kind="issue", executor="implement")]
    out = linkify("153. pb-www !4010 -- x\n175. pb-www #1775 -- y\n9 held: 66, 112", recs)
    assert "[!4010](<https://gitlab.com/g/pb-www/-/merge_requests/4010>)" in out
    assert "[#1775](<https://gitlab.com/g/pb-www/-/work_items/1775>)" in out
    assert "9 held: 66, 112" in out          # bare queue numbers untouched


def test_linkify_leaves_unknown_refs_and_does_not_double_link():
    from worksweep.curator import linkify
    recs = [_rec_with_url(1, "review:pb-www!4010", "https://gl/-/merge_requests/4010")]
    once = linkify("see !4010 and !9999", recs)
    assert once.count("[!4010]") == 1 and "!9999" in once and "[!9999]" not in once
    assert linkify(once, recs) == once        # idempotent


def test_linkify_never_invents_urls_from_text():
    from worksweep.curator import linkify
    # a record whose web_url is empty contributes no link
    recs = [_rec_with_url(1, "review:pb-www!4010", "")]
    assert linkify("!4010", recs) == "!4010"
