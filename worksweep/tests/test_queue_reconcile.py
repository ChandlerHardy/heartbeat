import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.queue import reconcile  # noqa: E402

T0 = "2026-06-23T08:00:00Z"
T1 = "2026-06-23T09:00:00Z"


def _item(id_, sha="abc", status="proposed"):
    return WorkItem(schema_version=1, id=id_, repo="pb-www", kind="mr",
                    executor="magi-review", risk="low", why="w",
                    web_url="u", sha=sha, status=status)


def _rec(n, id_, sha="abc", status="proposed", first=T0, last=T0):
    return QueueRecord(number=n, first_seen=first, last_seen=last,
                       item=_item(id_, sha=sha, status=status))


def _by_id(records):
    return {r.item.id: r for r in records}


def test_new_items_get_sequential_numbers():
    out = reconcile([], [_item("a"), _item("b"), _item("c")], T0)
    nums = {r.item.id: r.number for r in out}
    assert nums == {"a": 1, "b": 2, "c": 3}
    for r in out:
        assert r.item.status == "proposed"
        assert r.first_seen == T0 and r.last_seen == T0


def test_new_item_added_to_existing_gets_max_plus_one():
    existing = [_rec(1, "a"), _rec(3, "b")]   # gap at 2 — numbers need not be gapless
    out = reconcile(existing, [_item("a"), _item("b"), _item("c")], T1)
    assert _by_id(out)["c"].number == 4   # max(1,3)+1


def test_existing_item_same_sha_keeps_number_and_approved_status():
    existing = [_rec(5, "a", sha="abc", status="approved", first=T0, last=T0)]
    out = reconcile(existing, [_item("a", sha="abc", status="proposed")], T1)
    r = _by_id(out)["a"]
    assert r.number == 5                 # stable number
    assert r.item.status == "approved"   # approval preserved, not reset to proposed
    assert r.first_seen == T0            # original first_seen kept
    assert r.last_seen == T1             # last_seen bumped to now


def test_proposed_item_gone_from_sweep_is_dropped():
    existing = [_rec(1, "a", status="proposed"), _rec(2, "b", status="proposed")]
    out = reconcile(existing, [_item("a")], T1)   # b vanished
    assert set(_by_id(out)) == {"a"}


def test_approved_item_gone_from_sweep_is_retained():
    existing = [_rec(1, "a", status="proposed"),
                _rec(2, "b", status="approved", first=T0, last=T0)]
    out = reconcile(existing, [_item("a")], T1)   # b vanished but is approved
    ids = _by_id(out)
    assert set(ids) == {"a", "b"}
    assert ids["b"].number == 2
    assert ids["b"].item.status == "approved"
    assert ids["b"].last_seen == T0   # not re-seen this sweep, so last_seen unchanged


def test_running_item_gone_from_sweep_is_retained():
    existing = [_rec(2, "b", status="running")]
    out = reconcile(existing, [], T1)
    assert _by_id(out)["b"].item.status == "running"


def test_same_id_new_sha_resets_to_proposed_keeps_number():
    existing = [_rec(3, "a", sha="old", status="approved", first=T0, last=T0)]
    out = reconcile(existing, [_item("a", sha="new", status="proposed")], T1)
    r = _by_id(out)["a"]
    assert r.number == 3                 # number kept
    assert r.item.sha == "new"           # sha updated
    assert r.item.status == "proposed"   # prior approval was for the old SHA -> reset
    assert r.last_seen == T1


def test_retained_numbers_are_stable_when_lower_drops():
    # #2 (proposed) drops, #3 (approved) must keep its number even though it's now
    # the only retained-by-status item — stability over density.
    existing = [_rec(1, "a"), _rec(2, "b", status="proposed"),
                _rec(3, "c", status="approved")]
    out = reconcile(existing, [_item("a")], T1)   # only a still in sweep; c approved
    ids = _by_id(out)
    assert ids["a"].number == 1
    assert ids["c"].number == 3
    assert "b" not in ids
