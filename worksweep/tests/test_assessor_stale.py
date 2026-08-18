"""M4 Task H: assess_stale — pure threshold gating for the keep-current sensor."""
from worksweep.assessor import assess_stale
from worksweep.models import MergeRequest


def _mr(**kw):
    base = dict(repo="pb-www", iid=4020, title="Feed schedule tweak",
               author="me", web_url="https://gl/x/-/merge_requests/4020",
               description="", sha="s4020", is_draft=False, reviewers=(),
               ci_status="success", updated_at="",
               source_branch="feat/1701-thing")
    base.update(kw)
    return MergeRequest(**base)


def test_below_threshold_emits_nothing():
    assert assess_stale(_mr(), diverged=4, threshold=5) == []


def test_at_threshold_emits_one_stale_item():
    items = assess_stale(_mr(), diverged=5, threshold=5)
    assert [i.id for i in items] == ["stale:pb-www!4020"]


def test_above_threshold_emits_one_stale_item():
    items = assess_stale(_mr(), diverged=12, threshold=5)
    assert len(items) == 1


def test_stale_item_shape():
    item = assess_stale(_mr(), diverged=7, threshold=5)[0]
    assert item.repo == "pb-www"
    assert item.kind == "stale"
    assert item.executor == "keep-current"
    assert item.risk == "low"
    assert item.why == "7 commits behind master"
    assert item.web_url == "https://gl/x/-/merge_requests/4020"
    assert item.sha == "s4020"
    assert item.title == "Feed schedule tweak"
    assert item.branch == "feat/1701-thing"


def test_zero_diverged_emits_nothing():
    assert assess_stale(_mr(), diverged=0, threshold=5) == []


def test_custom_threshold_respected():
    assert assess_stale(_mr(), diverged=9, threshold=10) == []
    assert len(assess_stale(_mr(), diverged=10, threshold=10)) == 1
