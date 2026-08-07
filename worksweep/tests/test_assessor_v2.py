"""Review-state buckets -> items/resolutions; new own-MR item kinds."""
from worksweep.assessor import (
    assess_own_mr, assess_review_request, resolutions)
from worksweep.models import MergeRequest


def _mr(**kw):
    base = dict(repo="pb-www", iid=9, title="t", author="other",
                web_url="https://gl/x/-/merge_requests/9", description="",
                sha="s9", is_draft=False, reviewers=("chandler.hardy",),
                ci_status="unknown", updated_at="")
    base.update(kw)
    return MergeRequest(**base)


def test_unreviewed_emits_review_item():
    items = assess_review_request(_mr(my_review_state="UNREVIEWED"), "chandler.hardy")
    assert [i.id for i in items] == ["review:pb-www!9"]
    assert items[0].executor == "magi-review"


def test_reviewed_emits_nothing():
    for state in ("REVIEWED", "REQUESTED_CHANGES", "APPROVED"):
        assert assess_review_request(_mr(my_review_state=state), "chandler.hardy") == []


def test_draft_review_request_included_and_tagged():
    items = assess_review_request(
        _mr(my_review_state="UNREVIEWED", is_draft=True), "chandler.hardy")
    assert len(items) == 1 and "(draft)" in items[0].why


def test_resolutions_for_waiting_states():
    mrs = [_mr(iid=1, my_review_state="REVIEWED"),
           _mr(iid=2, my_review_state="UNREVIEWED"),
           _mr(iid=3, my_review_state="REQUESTED_CHANGES")]
    assert resolutions(mrs, "chandler.hardy") == {
        "review:pb-www!1": "already-reviewed",
        "review:pb-www!3": "already-reviewed"}


def test_own_mr_feedback_and_ci_items():
    mr = _mr(author="chandler.hardy", changes_requested=True,
             unresolved_count=2, ci_status="failed")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: True)
    ids = {i.id for i in items}
    assert "feedback:pb-www!9" in ids
    assert "ci:pb-www!9" in ids
    assert "magi:pb-www!9@s9" not in ids  # has_magi True suppresses


def test_own_mr_magi_item_when_no_history():
    mr = _mr(author="chandler.hardy")
    items = assess_own_mr(mr, "chandler.hardy", has_magi=lambda r, i, s: False)
    assert any(i.id == "magi:pb-www!9@s9" for i in items)
