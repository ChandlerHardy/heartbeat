"""New lifecycle/review-state fields default cleanly and round-trip the queue."""
import dataclasses

from worksweep.models import MergeRequest, QueueRecord, WorkItem
from worksweep.queue import load_queue, save_queue


def _item(**kw):
    base = dict(schema_version=1, id="review:pb-www!1", repo="pb-www",
                kind="review_request", executor="magi-review", risk="low",
                why="review requested", web_url="https://x/1", sha="abc")
    base.update(kw)
    return WorkItem(**base)


def test_workitem_new_fields_default_empty():
    it = _item()
    assert (it.claimed_at, it.done_reason, it.result_sha,
            it.report_path, it.error_summary) == ("", "", "", "", "")


def test_workitem_roundtrips_queue_with_new_fields(tmp_path):
    p = str(tmp_path / "q.json")
    it = _item(status="done", done_reason="executor-completed",
               result_sha="abc", report_path="/r.md", claimed_at="t1")
    save_queue(p, [QueueRecord(number=1, item=it, first_seen="t0", last_seen="t1")])
    loaded = load_queue(p)
    assert loaded[0].item == it


def test_old_queue_record_without_new_fields_loads(tmp_path):
    # A queue file written by M2 code lacks the new keys entirely.
    p = str(tmp_path / "q.json")
    old = _item()
    d = dataclasses.asdict(old)
    for k in ("claimed_at", "done_reason", "result_sha", "report_path", "error_summary"):
        d.pop(k)
    import json
    (tmp_path / "q.json").write_text(json.dumps(
        [{"number": 1, "first_seen": "t0", "last_seen": "t0", "item": d}]))
    assert load_queue(p)[0].item.id == "review:pb-www!1"


def test_mergerequest_review_state_fields_default():
    mr = MergeRequest(repo="pb-www", iid=1, title="t", author="a",
                      web_url="u", description="", sha="s", is_draft=False,
                      reviewers=(), ci_status="unknown", updated_at="")
    assert (mr.my_review_state, mr.changes_requested, mr.unresolved_count) == ("", False, 0)
