import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.approvals import parse_approval, parse_approve_all  # noqa: E402


def test_comma_list():
    assert parse_approval("✅ 1,3,5") == {1, 3, 5}


def test_loose_whitespace():
    assert parse_approval("✅  1 , 3 ,5") == {1, 3, 5}


def test_range():
    assert parse_approval("✅ 1-3") == {1, 2, 3}


def test_range_plus_singleton():
    assert parse_approval("✅ 1-3,5") == {1, 2, 3, 5}


def test_word_approve_single():
    assert parse_approval("approve 2") == {2}


def test_word_approve_multiple_space_separated():
    assert parse_approval("approve 2 4") == {2, 4}


def test_approve_is_case_insensitive():
    assert parse_approval("APPROVE 7") == {7}


def test_non_approval_numbers_only_is_empty():
    # numbers with no approval marker must NOT approve
    assert parse_approval("3 looks wrong") == set()


def test_plain_chat_is_empty():
    assert parse_approval("nice work") == set()


def test_empty_string_is_empty():
    assert parse_approval("") == set()


def test_marker_with_no_numbers_is_empty():
    assert parse_approval("✅") == set()
    assert parse_approval("approve") == set()


def test_zero_and_negative_ignored():
    # leading 0 and bare 0 are not valid item numbers; negatives never parse
    assert parse_approval("✅ 0,2") == {2}
    assert parse_approval("✅ -1,3") == {3}


def test_absurd_range_span_dropped_rest_kept():
    # span > 500 is dropped (can't blow up memory); the singleton survives
    assert parse_approval("✅ 1-100000,4") == {4}


def test_large_but_bounded_range_kept():
    # span exactly within the cap is expanded
    out = parse_approval("✅ 1-500")
    assert out == set(range(1, 501))


def test_reversed_range_ignored():
    # a descending range like 5-1 is not expanded; the marker still present so
    # other tokens parse, but this token contributes nothing
    assert parse_approval("✅ 5-1,9") == {9}


# --- `✅ all` blanket-approval predicate (decision 1/2, AC #4) ------------------

def test_approve_all_positive_forms():
    # the marker immediately followed by "all", either marker spelling, any case
    assert parse_approve_all("✅ all") is True
    assert parse_approve_all("approve ALL") is True
    assert parse_approve_all("Approve all") is True
    assert parse_approve_all("✅ all please") is True


def test_chatty_all_is_not_blanket():
    # THE false positive the adjacency regex exists to kill: marker present and
    # the word "all" present, but not adjacent -> not a blanket approval.
    assert parse_approve_all("✅ sounds good, that's all") is False
    assert parse_approve_all("approve when you can, that's all I need") is False


def test_approve_all_requires_the_marker():
    # "all" with no ✅/approve marker never approves anything
    assert parse_approve_all("all") is False
    assert parse_approve_all("all of them look fine") is False
    assert parse_approve_all("") is False


def test_explicit_numbers_beat_all():
    # decision 2: the word "all" is only a blanket when the message names NO
    # numbers. `✅ 1,3 all good` is a numbered approval, not a blanket one.
    assert parse_approve_all("✅ 1,3 all good") is False
    assert parse_approval("✅ 1,3 all good") == {1, 3}


def test_adjacent_all_with_numbers_is_still_numbers_only():
    """The case that isolates the numbers-beat-all PRECONDITION from the
    adjacency regex: here the marker IS immediately followed by "all", so the
    regex matches -- only `parse_approval(text)` being non-empty stops this
    chatty line from approving the entire queue."""
    assert parse_approve_all("✅ all good, especially 3") is False
    assert parse_approval("✅ all good, especially 3") == {3}
    assert parse_approve_all("approve all of 1 and 2") is False


def test_approve_all_requires_whitespace_after_marker():
    # deliberate consequence of the `\s+` in the pattern (spec'd, not an accident)
    assert parse_approve_all("✅all") is False


def test_all_must_be_a_whole_word():
    assert parse_approve_all("✅ allocate the reviewer") is False


# --- f-009: a refusal is not an approval (tribunal, 2026-08-26) -----------
#
# `_HAS_MARKER_RE` was a bare `✅|approve`, and "approve" is a substring of
# "disapprove". So "disapprove all" matched the blanket regex and approved the
# entire queue -- there is no un-approve path, so that is unrecoverable by
# design. Verified live during the tribunal: parse_approve_all('disapprove all')
# returned True.

def test_disapprove_all_is_not_a_blanket_approval():
    """FALSIFYING. The single most dangerous string in the system."""
    assert parse_approve_all("disapprove all") is False
    assert parse_approve_all("Disapprove ALL") is False
    assert parse_approve_all("unapprove all") is False


def test_a_negated_approval_is_not_a_blanket_approval():
    for text in ("don't approve all", "dont approve all",
                 "do not approve all", "never approve all",
                 "do not ever approve all", "won't approve all",
                 "cannot approve all", "can't approve all"):
        assert parse_approve_all(text) is False, text


def test_a_cross_mark_is_never_an_approval():
    assert parse_approve_all("❌ all") is False
    assert parse_approval("❌ 1,3") == set()


def test_disapprove_does_not_approve_numbers_either():
    """Same root cause on the numbered path: `disapprove 3` used to parse as
    an approval of item 3."""
    assert parse_approval("disapprove 3") == set()
    assert parse_approval("don't approve 1,2") == set()
    assert parse_approval("not approved yet: 7") == set()


def test_the_real_approvals_all_still_work():
    """The guard must not cost a single legitimate approval -- it is the only
    way work ever runs."""
    assert parse_approve_all("✅ all") is True
    assert parse_approve_all("approve all") is True
    assert parse_approve_all("Approve all") is True
    assert parse_approve_all("approve all please") is True
    assert parse_approval("✅ 1,3") == {1, 3}
    assert parse_approval("approve 4") == {4}
    assert parse_approval("looks good, approve 2 and 5") == {2, 5}


def test_a_word_starting_with_approve_still_counts():
    """`approved` is an ordinary way to say it and must keep working -- the
    guard is about what comes BEFORE the word, not after."""
    assert parse_approval("approved: 3") == {3}
