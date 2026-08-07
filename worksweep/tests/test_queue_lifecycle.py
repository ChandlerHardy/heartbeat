"""reconcile v2: resolutions, terminal retention, error retry, done resurrect."""
import dataclasses
import datetime

from worksweep.models import QueueRecord, WorkItem
from worksweep.queue import reconcile

NOW = "2026-08-07T12:00:00+00:00"


def _item(id="review:pb-www!1", sha="s1", status="proposed", **kw):
    return WorkItem(schema_version=1, id=id, repo="pb-www",
                    kind="review_request", executor="magi-review", risk="low",
                    why="review requested", web_url="https://x/1", sha=sha,
                    status=status, **kw)


def _rec(item, number=1, last_seen="2026-08-06T00:00:00+00:00"):
    return QueueRecord(number=number, item=item,
                       first_seen="2026-08-01T00:00:00+00:00", last_seen=last_seen)


def test_resolved_id_flips_done_and_is_retained():
    out = reconcile([_rec(_item(status="proposed"))], [], NOW,
                    resolved={"review:pb-www!1": "already-reviewed"})
    assert out[0].item.status == "done"
    assert out[0].item.done_reason == "already-reviewed"
    assert out[0].last_seen == NOW


def test_resolved_does_not_touch_terminal_records():
    done = _item(status="done", done_reason="executor-completed")
    out = reconcile([_rec(done)], [], NOW,
                    resolved={"review:pb-www!1": "already-reviewed"})
    assert out[0].item.done_reason == "executor-completed"


def test_done_retained_when_gone_from_sweep():
    out = reconcile([_rec(_item(status="done"))], [], NOW)
    assert len(out) == 1 and out[0].item.status == "done"


def test_error_reproposed_when_signal_persists():
    prior = _rec(_item(status="error", error_summary="boom"))
    out = reconcile([prior], [_item()], NOW)
    assert out[0].item.status == "proposed"
    assert out[0].number == 1  # number stable


def test_done_same_sha_stays_done():
    prior = _rec(_item(status="done", result_sha="s1"))
    out = reconcile([prior], [_item(sha="s1")], NOW)
    assert out[0].item.status == "done"


def test_done_new_sha_resurrects_proposed():
    prior = _rec(_item(status="done", result_sha="s1"))
    out = reconcile([prior], [_item(sha="s2")], NOW)
    assert out[0].item.status == "proposed" and out[0].item.sha == "s2"


def test_compaction_drops_old_terminal_records():
    old = (datetime.datetime.fromisoformat(NOW)
           - datetime.timedelta(days=91)).isoformat()
    stale_done = _rec(_item(id="review:pb-www!2", status="done"),
                      number=2, last_seen=old)
    fresh_done = _rec(_item(status="done"))
    out = reconcile([stale_done, fresh_done], [], NOW)
    assert [r.number for r in out] == [1]


def test_compaction_never_drops_unparseable_timestamps():
    weird = _rec(_item(status="done"), last_seen="not-a-date")
    assert len(reconcile([weird], [], NOW)) == 1
