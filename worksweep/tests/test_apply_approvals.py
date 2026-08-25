import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord, DiscordMessage  # noqa: E402
from worksweep.approvals import (  # noqa: E402
    apply_approvals, approve_all, approve_numbers)

USER = "chandler-123"
OTHER = "colleague-999"
T0 = "2026-06-23T08:00:00Z"
T1 = "2026-06-23T09:00:00Z"


def _rec(n, status="proposed"):
    return QueueRecord(number=n, first_seen=T0, last_seen=T0,
                       item=WorkItem(schema_version=1, id=f"id{n}", repo="pb-www",
                                     kind="mr", executor="magi-review", risk="low",
                                     why="w", web_url="u", sha="abc", status=status))


def _msg(author_id, content, mid="1"):
    return DiscordMessage(id=mid, author_id=author_id, content=content, timestamp=T1)


def _by_num(records):
    return {r.number: r for r in records}


def test_approval_from_user_flips_named_proposed_items():
    recs = [_rec(1), _rec(2), _rec(3)]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1,3")], USER, T1)
    by = _by_num(out)
    assert by[1].item.status == "approved"
    assert by[3].item.status == "approved"
    assert by[2].item.status == "proposed"
    assert approved == {1, 3}


def test_approved_record_last_seen_is_bumped():
    out, _ = apply_approvals([_rec(1)], [_msg(USER, "✅ 1")], USER, T1)
    assert _by_num(out)[1].last_seen == T1


def test_approval_from_other_author_is_ignored():
    recs = [_rec(1), _rec(2)]
    out, approved = apply_approvals(recs, [_msg(OTHER, "✅ 1,2")], USER, T1)
    assert approved == set()
    for r in out:
        assert r.item.status == "proposed"


def test_number_with_no_matching_record_is_noop():
    out, approved = apply_approvals([_rec(1)], [_msg(USER, "✅ 1,9")], USER, T1)
    assert approved == {1}                      # 9 has no record -> not approved
    assert _by_num(out)[1].item.status == "approved"


def test_already_approved_is_idempotent_and_not_in_returned_set():
    # #1 is already approved; re-approving must keep it approved and NOT report it
    # as newly approved (so the confirmation only names freshly flipped items).
    recs = [_rec(1, status="approved"), _rec(2)]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1,2")], USER, T1)
    by = _by_num(out)
    assert by[1].item.status == "approved"
    assert by[2].item.status == "approved"
    assert approved == {2}


def test_multiple_messages_union_only_user_authored():
    recs = [_rec(1), _rec(2), _rec(3)]
    msgs = [_msg(USER, "✅ 1", mid="1"),
            _msg(OTHER, "✅ 2", mid="2"),
            _msg(USER, "approve 3", mid="3")]
    out, approved = apply_approvals(recs, msgs, USER, T1)
    assert approved == {1, 3}
    assert _by_num(out)[2].item.status == "proposed"


def test_non_approval_user_message_changes_nothing():
    recs = [_rec(1)]
    out, approved = apply_approvals(recs, [_msg(USER, "looks good")], USER, T1)
    assert approved == set()
    assert _by_num(out)[1].item.status == "proposed"


# --- `✅ all` blanket approval (decisions 1/2, AC #1-#3, #5, #8, #20) ----------

# Every status the queue can hold, so the blanket path is asserted against the
# WHOLE state space rather than the one status it is allowed to touch.
_ALL_STATUSES = ("proposed", "needs-input", "running", "approved", "done", "error")


def _one_of_each():
    """One record per status, numbered 1..6 in _ALL_STATUSES order."""
    return [_rec(i, status=s) for i, s in enumerate(_ALL_STATUSES, start=1)]


def test_approve_all_flips_every_proposed_item():
    """AC #8 (falsifying): delete parse_approve_all, or make apply_approvals
    ignore it, and the records asserted `approved` here read `proposed`."""
    recs = [_rec(1), _rec(2), _rec(3)]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ all")], USER, T1)
    by = _by_num(out)
    assert {n: by[n].item.status for n in (1, 2, 3)} == {
        1: "approved", 2: "approved", 3: "approved"}
    assert approved == {1, 2, 3}


def test_approve_all_leaves_every_other_status_byte_identical():
    """AC #2: the blanket set is ("proposed",), NOT _APPROVABLE. Seeds all six
    statuses and pins the exact flipped set and the exact untouched set."""
    recs = _one_of_each()
    before = {r.number: r for r in recs}
    out, approved = apply_approvals(recs, [_msg(USER, "✅ all")], USER, T1)
    by = _by_num(out)

    # exactly one record flipped: the `proposed` one (#1)
    assert approved == {1}
    assert by[1].item.status == "approved"
    # every other record is the SAME object, byte-identical (last_seen included)
    for n in (2, 3, 4, 5, 6):
        assert by[n] == before[n]
    assert {by[n].item.status for n in (2, 3, 4, 5, 6)} == {
        "needs-input", "running", "approved", "done", "error"}


def test_approve_all_does_not_release_a_parked_needs_input_item():
    # decision 1 stated as its own falsifiable line: "yes to everything" is not
    # "ignore all questions". A numbered ✅ still releases it (test below).
    recs = [_rec(1, status="needs-input")]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ all")], USER, T1)
    assert approved == set()
    assert _by_num(out)[1].item.status == "needs-input"


def test_numbered_approval_still_releases_needs_input():
    # the asymmetry guard: AC #2 must not weaken the numbered path (behaviour 4)
    recs = [_rec(1, status="needs-input")]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1")], USER, T1)
    assert approved == {1}
    assert _by_num(out)[1].item.status == "approved"


def test_explicit_numbers_beat_all():
    """AC #3: `✅ 1,3 all good` over records 1-3 yields exactly {1, 3}."""
    recs = [_rec(1), _rec(2), _rec(3)]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1,3 all good")], USER, T1)
    by = _by_num(out)
    assert approved == {1, 3}
    assert {n: by[n].item.status for n in (1, 2, 3)} == {
        1: "approved", 2: "proposed", 3: "approved"}


def test_adjacent_all_with_numbers_approves_only_those_numbers():
    """AC #3 at the apply level, on the message shape that actually exercises
    the precondition: "✅ all good, especially 3" names a number, so it is a
    numbered approval -- records 1 and 2 stay proposed."""
    recs = [_rec(1), _rec(2), _rec(3)]
    out, approved = apply_approvals(
        recs, [_msg(USER, "✅ all good, especially 3")], USER, T1)
    by = _by_num(out)
    assert approved == {3}
    assert {n: by[n].item.status for n in (1, 2, 3)} == {
        1: "proposed", 2: "proposed", 3: "approved"}


def test_approve_all_from_other_author_changes_nothing():
    """AC #5: the author gate wraps the blanket check too."""
    recs = _one_of_each()
    before = list(recs)
    out, approved = apply_approvals(recs, [_msg(OTHER, "✅ all")], USER, T1)
    assert approved == set()
    assert out == before


def test_approve_all_preserves_number_and_first_seen():
    # behaviour 6 must survive on the blanket path too
    recs = [_rec(7)]
    out, approved = apply_approvals(recs, [_msg(USER, "approve all")], USER, T1)
    r = _by_num(out)[7]
    assert (r.number, r.first_seen, r.last_seen) == (7, T0, T1)


def test_chatty_all_does_not_blanket_approve():
    """The regression the adjacency regex buys: a casual sign-off must not
    approve the queue."""
    recs = _one_of_each()
    before = list(recs)
    out, approved = apply_approvals(
        recs, [_msg(USER, "✅ sounds good, that's all")], USER, T1)
    assert approved == set()
    assert out == before


def test_blanket_and_numbered_messages_compose():
    # a numbered message releases the parked item; a separate blanket message
    # sweeps the proposed ones. Both land in `newly` for the confirmation.
    recs = [_rec(1), _rec(2, status="needs-input"), _rec(3), _rec(4, status="running")]
    msgs = [_msg(USER, "✅ 2", mid="1"), _msg(USER, "✅ all", mid="2")]
    out, approved = apply_approvals(recs, msgs, USER, T1)
    by = _by_num(out)
    assert approved == {1, 2, 3}
    assert {n: by[n].item.status for n in (1, 2, 3, 4)} == {
        1: "approved", 2: "approved", 3: "approved", 4: "running"}


# --- decision 8: one definition of "approvable" (AC #20) -----------------------

def test_approve_all_matches_apply_approvals_byte_for_byte():
    """AC #20: the dashboard's approve_all and an equivalent Discord `✅ all`
    must produce identical records -- if they can diverge, the status rules
    exist in two places."""
    via_discord, discord_newly = apply_approvals(
        _one_of_each(), [_msg(USER, "✅ all")], USER, T1)
    via_dashboard, dashboard_newly = approve_all(_one_of_each(), T1)
    assert via_dashboard == via_discord
    assert dashboard_newly == discord_newly == {1}


def test_approve_numbers_matches_apply_approvals_byte_for_byte():
    """AC #20, numbered route: the dashboard's checked boxes and `✅ 1,2` must
    produce identical records -- including releasing the needs-input item."""
    via_discord, discord_newly = apply_approvals(
        _one_of_each(), [_msg(USER, "✅ 1,2,3")], USER, T1)
    via_dashboard, dashboard_newly = approve_numbers(_one_of_each(), {1, 2, 3}, T1)
    assert via_dashboard == via_discord
    assert dashboard_newly == discord_newly == {1, 2}   # 3 is `running`


# --- F1: blanket approval gates on RUNNABLE executors -------------------------

# Everything the assessor can emit. The runner claims only the first three
# (runner.pick_claim at runner.py:353 and :441); the rest are FYI rows a human
# handles by hand.
_RUNNABLE = ("magi-review", "keep-current", "implement", "park")
_NOT_RUNNABLE = ("triage", "mr-hygiene", "none")


def _erec(n, executor, status="proposed"):
    return QueueRecord(number=n, first_seen=T0, last_seen=T0,
                       item=WorkItem(schema_version=1, id=f"id{n}", repo="pb-www",
                                     kind="mr", executor=executor, risk="low",
                                     why="w", web_url="u", sha="abc", status=status))


def test_runnable_executors_matches_the_runner_claim_gate():
    """The constant and the runner's real claim gate must not drift.

    If someone teaches the runner a fourth executor, this fails and points at
    RUNNABLE_EXECUTORS -- otherwise `✅ all` would silently keep refusing it.
    """
    from worksweep import runner
    from worksweep.models import RUNNABLE_EXECUTORS
    assert set(RUNNABLE_EXECUTORS) == set(runner._ALL_EXECUTORS)
    # and the two claim call sites together cover exactly that set
    assert set(RUNNABLE_EXECUTORS) == {runner._MAGI, runner._KEEP_CURRENT,
                                       runner._IMPLEMENT, runner._PARK}


def test_blanket_approval_skips_non_runnable_executors():
    """F1 (falsifying): a blanket-approved triage/mr-hygiene/none record is a
    permanently stuck zombie -- nothing claims it and there is no un-approve
    path. Drop the executor gate and this goes red."""
    runnable = list(enumerate(_RUNNABLE, start=1))
    not_runnable = list(enumerate(_NOT_RUNNABLE, start=len(_RUNNABLE) + 1))
    recs = ([_erec(i, ex) for i, ex in runnable]
            + [_erec(i, ex) for i, ex in not_runnable])
    out, approved = apply_approvals(recs, [_msg(USER, "✅ all")], USER, T1)
    by = _by_num(out)
    assert approved == {i for i, _ in runnable}
    assert {by[i].item.status for i, _ in runnable} == {"approved"}
    # every non-runnable one is untouched and stays actionable
    assert {by[i].item.status for i, _ in not_runnable} == {"proposed"}


def test_numbered_approval_still_approves_a_non_runnable_item():
    """F1 keeps the numbered path as-is: naming an item is a deliberate human
    choice, and the human can see exactly what they typed."""
    recs = [_erec(1, "triage"), _erec(2, "mr-hygiene")]
    out, approved = apply_approvals(recs, [_msg(USER, "✅ 1,2")], USER, T1)
    assert approved == {1, 2}
    assert {r.item.status for r in out} == {"approved"}


def test_is_blanket_eligible_is_the_single_gate():
    from worksweep.approvals import is_blanket_eligible
    for ex in _RUNNABLE:
        assert is_blanket_eligible(_erec(1, ex).item) is True
        assert is_blanket_eligible(_erec(1, ex, "needs-input").item) is False
        assert is_blanket_eligible(_erec(1, ex, "running").item) is False
    for ex in _NOT_RUNNABLE:
        assert is_blanket_eligible(_erec(1, ex).item) is False


# --- F2: the blanket set can be scoped to the numbers the client rendered ----

def test_approve_all_scoped_to_client_numbers():
    """F2: the dashboard sends the numbers its page actually displayed, so the
    user approves exactly the set they consented to."""
    recs = [_erec(1, "magi-review"), _erec(2, "magi-review"), _erec(3, "magi-review")]
    out, approved = approve_all(recs, T1, numbers={1, 3})
    by = _by_num(out)
    assert approved == {1, 3}
    assert by[2].item.status == "proposed"


def test_approve_all_scope_is_intersected_with_current_eligibility():
    """F2 (falsifying): a number the client rendered but that is no longer
    eligible must NOT be flipped -- the server re-checks against disk state."""
    recs = [_erec(1, "magi-review"),                       # still eligible
            _erec(2, "magi-review", "running"),            # runner claimed it since
            _erec(3, "triage"),                            # never blanket-eligible
            _erec(4, "magi-review")]                       # eligible, not rendered
    out, approved = approve_all(recs, T1, numbers={1, 2, 3})
    by = _by_num(out)
    assert approved == {1}
    assert {n: by[n].item.status for n in (1, 2, 3, 4)} == {
        1: "approved", 2: "running", 3: "proposed", 4: "proposed"}


def test_approve_all_with_no_scope_still_means_every_eligible_record():
    """The Discord `✅ all` path passes no scope."""
    recs = [_erec(1, "magi-review"), _erec(2, "implement"), _erec(3, "triage")]
    out, approved = approve_all(recs, T1)
    assert approved == {1, 2}


def test_empty_client_scope_approves_nothing():
    """An empty rendered set must not be read as 'no scope' -- that would turn
    a page showing nothing approvable into a full blanket approval."""
    recs = [_erec(1, "magi-review"), _erec(2, "magi-review")]
    out, approved = approve_all(recs, T1, numbers=set())
    assert approved == set()
    assert {r.item.status for r in out} == {"proposed"}
