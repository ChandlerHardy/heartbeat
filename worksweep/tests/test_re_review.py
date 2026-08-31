"""The re-review sensor: when an author addresses Chandler's review feedback
(moves the branch after his review), a row appears — the reverse-direction
twin of the plain-note sensor. Origin: ck-www !401 sat in a mutual wait
(2026-08-31) and pb-www !4076's version-7 fixes surfaced only because the
author mentioned them.

Design: a reviewed-state sidecar (~/.worksweep/reviewed-state.json) remembers
the head sha each reviewed MR was at when Chandler's review state went
waiting. A later sweep seeing a different head proposes `re-review:{repo}!{iid}`
(kind re_review, executor magi-review — the targeted advisory pass). First
sight of a reviewed MR SEEDS the sidecar quietly (no retroactive storm).
Dismissing the row records the row's sha as reviewed — same evidence-keyed
semantics as seen-notes: "I have dealt with THIS head", never "mute this MR".
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep import assessor, reviewedstate  # noqa: E402
from worksweep.models import MergeRequest, WorkItem  # noqa: E402
from worksweep.queue import is_dismissable  # noqa: E402


def _mr(sha="aaaa1111", state="REVIEWED", reviewers=("chandler.hardy",),
        iid=401, repo="ck-www"):
    return MergeRequest(
        repo=repo, iid=iid, title="t", author="randywu",
        web_url=f"https://gitlab.com/x/-/merge_requests/{iid}",
        description="", sha=sha, is_draft=False, reviewers=reviewers,
        ci_status="success", updated_at="2026-08-31T10:00:00Z",
        my_review_state=state)


# ---------------------------------------------------------------- sidecar

def test_sidecar_roundtrip_and_missing_file(tmp_path):
    p = str(tmp_path / "reviewed-state.json")
    assert reviewedstate.load_state(p) == {}
    reviewedstate.record_state(p, "ck-www!401", "aaaa1111", "2026-08-31T10:00:00Z")
    reviewedstate.record_state(p, "pb-www!4076", "bbbb2222", "2026-08-31T10:00:00Z")
    assert reviewedstate.load_state(p) == {"ck-www!401": "aaaa1111",
                                           "pb-www!4076": "bbbb2222"}
    # re-recording the same key replaces, never duplicates
    reviewedstate.record_state(p, "ck-www!401", "cccc3333", "2026-08-31T11:00:00Z")
    assert reviewedstate.load_state(p)["ck-www!401"] == "cccc3333"


def test_sidecar_malformed_file_reads_empty(tmp_path):
    p = tmp_path / "reviewed-state.json"
    p.write_text("{not json")
    assert reviewedstate.load_state(str(p)) == {}


def test_sidecar_prunes_stale_entries(tmp_path):
    p = str(tmp_path / "reviewed-state.json")
    reviewedstate.record_state(p, "old!1", "aaaa1111", "2026-01-01T00:00:00Z")
    reviewedstate.record_state(p, "new!2", "bbbb2222", "2026-08-30T00:00:00Z")
    assert reviewedstate.load_state(p, now="2026-08-31T00:00:00Z") == {
        "new!2": "bbbb2222"}


# ---------------------------------------------------------------- assess

def test_head_moved_after_review_fires_a_row():
    rows = assessor.assess_re_review(_mr(sha="cccc3333"), "chandler.hardy",
                                     reviewed_sha="aaaa1111")
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "re-review:ck-www!401"
    assert row.kind == "re_review"
    assert row.executor == "magi-review"
    assert "aaaa1111"[:8] in row.why and "cccc3333"[:8] in row.why
    assert row.sha == "cccc3333"


def test_unmoved_head_fires_nothing():
    assert assessor.assess_re_review(_mr(sha="aaaa1111"), "chandler.hardy",
                                     reviewed_sha="aaaa1111") == []


def test_unknown_reviewed_sha_fires_nothing():
    # First sight seeds the sidecar; it must never fire retroactively.
    assert assessor.assess_re_review(_mr(), "chandler.hardy",
                                     reviewed_sha="") == []


def test_actionable_states_are_the_review_request_lane_not_ours():
    for state in ("UNREVIEWED", "REVIEW_STARTED", "UNAPPROVED", ""):
        assert assessor.assess_re_review(_mr(state=state), "chandler.hardy",
                                         reviewed_sha="aaaa1111") == [], state


def test_approved_mrs_fire_nothing():
    # Post-LGTM pushes either auto-unapprove (-> review_request lane) or are
    # the maintainer's business; a re-review nag on an approved MR is noise.
    assert assessor.assess_re_review(_mr(state="APPROVED", sha="cccc3333"),
                                     "chandler.hardy",
                                     reviewed_sha="aaaa1111") == []


def test_non_reviewer_fires_nothing():
    assert assessor.assess_re_review(_mr(reviewers=("leyang",), sha="cccc3333"),
                                     "chandler.hardy",
                                     reviewed_sha="aaaa1111") == []


# ------------------------------------------------------------- resolution

def test_re_review_resolves_when_state_goes_actionable_or_approved():
    mrs = [_mr(state="UNREVIEWED"), _mr(state="APPROVED", iid=402)]
    out = assessor.re_review_resolutions(mrs, "chandler.hardy",
                                         {"ck-www!401": "aaaa1111",
                                          "ck-www!402": "aaaa1111"})
    assert out["re-review:ck-www!401"] == "review-lane-active"
    assert out["re-review:ck-www!402"] == "approved"


def test_re_review_resolves_when_sha_catches_up():
    out = assessor.re_review_resolutions([_mr(sha="aaaa1111")],
                                         "chandler.hardy",
                                         {"ck-www!401": "aaaa1111"})
    assert out["re-review:ck-www!401"] == "re-reviewed"


def test_waiting_mr_with_moved_head_is_not_resolved():
    out = assessor.re_review_resolutions([_mr(sha="cccc3333")],
                                         "chandler.hardy",
                                         {"ck-www!401": "aaaa1111"})
    assert "re-review:ck-www!401" not in out


# ------------------------------------------------------------ dismissal

def _row(sha="cccc3333"):
    return WorkItem(schema_version=1, id="re-review:ck-www!401", repo="ck-www",
                    kind="re_review", executor="magi-review", risk="low",
                    why="author moved the branch", web_url="u", sha=sha,
                    title="t", status="proposed")


def test_re_review_rows_are_dismissable_despite_runnable_executor():
    assert is_dismissable(_row())


def test_other_magi_review_rows_stay_undismissable():
    review = WorkItem(schema_version=1, id="review:ck-www!401", repo="ck-www",
                      kind="review_request", executor="magi-review",
                      risk="low", why="review requested", web_url="u",
                      sha="s", title="t", status="proposed")
    assert not is_dismissable(review)
