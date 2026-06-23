import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, Todo, Issue  # noqa: E402
from worksweep.assessor import assess_mr, assess_todo, dedupe  # noqa: E402


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="me", web_url="u",
                description="", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


def test_mine_without_magi_proposes_magi_review():
    items = assess_mr(_mr(author="chandler.hardy"), "chandler.hardy",
                      has_magi=lambda r, s: False)
    assert any(i.executor == "magi-review" for i in items)


def test_mine_with_magi_does_not_propose_magi_review():
    items = assess_mr(_mr(author="chandler.hardy"), "chandler.hardy",
                      has_magi=lambda r, s: True)
    assert not any(i.executor == "magi-review" for i in items)


def test_missing_dev_url_proposes_hygiene():
    items = assess_mr(_mr(author="chandler.hardy", description="no link"),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert any(i.executor == "mr-hygiene" for i in items)


def test_present_dev_url_no_hygiene():
    desc = "see https://x-dev4.performancebeef.com/y"
    items = assess_mr(_mr(author="chandler.hardy", description=desc),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert not any(i.executor == "mr-hygiene" for i in items)


def test_review_request_when_im_reviewer_not_author():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",)),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert any(i.executor == "review" for i in items)


def test_draft_review_request_is_skipped():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",), is_draft=True),
                      "chandler.hardy", has_magi=lambda r, s: True)
    assert not any(i.executor == "review" for i in items)


def test_dedupe_by_id():
    a = assess_todo(Todo(target="MergeRequest", action="review_requested",
                         web_url="https://gitlab.com/x/-/merge_requests/9"))
    again = assess_todo(Todo(target="MergeRequest", action="review_requested",
                             web_url="https://gitlab.com/x/-/merge_requests/9"))
    assert len(dedupe(a + again)) == 1
