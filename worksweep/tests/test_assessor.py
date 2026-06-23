import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, Todo, Issue  # noqa: E402
from worksweep.assessor import (  # noqa: E402
    assess_mr, assess_todo, assess_issue, dedupe, has_magi_report,
)


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="me", web_url="u",
                description="", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


def test_mine_without_magi_proposes_magi_review():
    items = assess_mr(_mr(author="chandler.hardy"), "chandler.hardy",
                      has_magi=lambda r, iid: False)
    assert any(i.executor == "magi-review" for i in items)


def test_mine_with_magi_does_not_propose_magi_review():
    items = assess_mr(_mr(author="chandler.hardy"), "chandler.hardy",
                      has_magi=lambda r, iid: True)
    assert not any(i.executor == "magi-review" for i in items)


def test_missing_dev_url_proposes_hygiene():
    items = assess_mr(_mr(author="chandler.hardy", description="no link"),
                      "chandler.hardy", has_magi=lambda r, iid: True)
    assert any(i.executor == "mr-hygiene" for i in items)


def test_present_dev_url_no_hygiene():
    desc = "see https://x-dev4.performancebeef.com/y"
    items = assess_mr(_mr(author="chandler.hardy", description=desc),
                      "chandler.hardy", has_magi=lambda r, iid: True)
    assert not any(i.executor == "mr-hygiene" for i in items)


def test_review_request_when_im_reviewer_not_author():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",)),
                      "chandler.hardy", has_magi=lambda r, iid: True)
    assert any(i.executor == "review" for i in items)


def test_draft_review_request_is_skipped():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",), is_draft=True),
                      "chandler.hardy", has_magi=lambda r, iid: True)
    assert not any(i.executor == "review" for i in items)


def test_dedupe_by_id():
    a = assess_todo(Todo(target="MergeRequest", action="review_requested",
                         web_url="https://gitlab.com/x/-/merge_requests/9"))
    again = assess_todo(Todo(target="MergeRequest", action="review_requested",
                             web_url="https://gitlab.com/x/-/merge_requests/9"))
    assert len(dedupe(a + again)) == 1


# FIX 8 — honest CI status in review-request branch
def test_review_request_unknown_ci_omits_ci_clause():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",),
                          ci_status="unknown"),
                      "chandler.hardy", has_magi=lambda r, iid: True)
    review = [i for i in items if i.executor == "review"]
    assert len(review) == 1
    assert review[0].why == "review requested"
    assert "CI" not in review[0].why


def test_review_request_success_ci_mentions_green():
    items = assess_mr(_mr(author="leyang", reviewers=("chandler.hardy",),
                          ci_status="success"),
                      "chandler.hardy", has_magi=lambda r, iid: True)
    review = [i for i in items if i.executor == "review"]
    assert len(review) == 1
    assert "CI green" in review[0].why


# FIX 9 — todo id includes action so distinct actions don't collide
def test_dedupe_keeps_distinct_actions_same_url():
    url = "https://gitlab.com/x/-/merge_requests/9"
    a = assess_todo(Todo(target="MergeRequest", action="review_requested", web_url=url))
    b = assess_todo(Todo(target="MergeRequest", action="mentioned", web_url=url))
    assert len(dedupe(a + b)) == 2


# FIX 10 — assess_todo exact fields
def test_assess_todo_exact_fields():
    it = assess_todo(Todo(target="Issue", action="assigned", web_url="u"))[0]
    assert it.executor == "triage"
    assert it.kind == "todo"
    assert it.why == "assigned on Issue"


# FIX 10 — assess_issue exact fields
def test_assess_issue_exact_fields():
    it = assess_issue(Issue(repo="pb-www", iid=42, title="bug", web_url="u"))[0]
    assert it.kind == "issue"
    assert it.executor == "triage"
    assert it.why == "assigned issue: bug"
    assert it.id == "issue:pb-www#42"


# FIX 1 — real has_magi_report keyed on (repo, iid), repo-aware glob
def test_has_magi_report_matches_repo_and_iid(tmp_path, monkeypatch):
    import worksweep.assessor as a
    base = tmp_path / "pla0"
    monkeypatch.setattr(a, "MAGI_REPORTS_BASE", str(base))
    magi_dir = base / "pb-www" / ".magi"
    magi_dir.mkdir(parents=True)
    (magi_dir / "tribunal-report-mr-3920-x.md").write_text("report")
    assert has_magi_report("pb-www", 3920) is True
    assert has_magi_report("pb-www", 1) is False
    assert has_magi_report("pb-api", 3920) is False
