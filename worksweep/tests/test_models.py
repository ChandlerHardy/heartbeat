import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, WorkItem  # noqa: E402


def test_merge_request_dev_url_present_true():
    mr = MergeRequest(
        repo="pb-www", iid=3920, title="t", author="leyang",
        web_url="https://gitlab.com/x/-/merge_requests/3920",
        description="## Dev link\n**https://leyang-dev4.performancebeef.com/x** ready",
        sha="abc", is_draft=False, reviewers=("chandler.hardy",),
        ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )
    assert mr.dev_url_present is True


def test_merge_request_dev_url_present_false():
    mr = MergeRequest(
        repo="pb-www", iid=1, title="t", author="me", web_url="u",
        description="no link here", sha="abc", is_draft=False,
        reviewers=(), ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )
    assert mr.dev_url_present is False


def _mr(description):
    return MergeRequest(
        repo="pb-www", iid=1, title="t", author="me", web_url="u",
        description=description, sha="abc", is_draft=False, reviewers=(),
        ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )


# FIX 11 — require a -/. immediately before "dev"
def test_dev_url_requires_boundary_before_dev():
    # bare "dev" prefix on the label must NOT match
    assert _mr("https://unintendeddev.performancebeef.com/x").dev_url_present is False


def test_dev_url_hyphen_boundary_matches():
    assert _mr("https://foo-dev4.performancebeef.com/x").dev_url_present is True


def test_workitem_defaults_status_proposed():
    wi = WorkItem(schema_version=1, id="magi:pb-www!1@abc", repo="pb-www",
                  kind="mr", executor="magi-review", risk="low",
                  why="no magi review", web_url="u", sha="abc")
    assert wi.status == "proposed"
