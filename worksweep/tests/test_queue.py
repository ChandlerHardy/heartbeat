import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import WorkItem, QueueRecord  # noqa: E402
from worksweep.queue import load_queue, save_queue   # noqa: E402


def _rec(n, id_, status="proposed"):
    return QueueRecord(number=n, first_seen="2026-06-23T08:00:00Z",
                       last_seen="2026-06-23T08:00:00Z",
                       item=WorkItem(schema_version=1, id=id_, repo="pb-www", kind="mr",
                                     executor="magi-review", risk="low", why="w",
                                     web_url="u", sha="abc", status=status))


def test_load_missing_file_returns_empty():
    assert load_queue("/nonexistent/dir/queue.json") == []


def test_save_then_load_roundtrips():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sub", "queue.json")   # parent dir does not exist yet
        recs = [_rec(1, "magi:pb-www!1@abc"), _rec(2, "review:pb-www!2", status="approved")]
        save_queue(p, recs)
        out = load_queue(p)
        assert [r.number for r in out] == [1, 2]
        assert out[1].item.status == "approved"
        assert out[0].item.id == "magi:pb-www!1@abc"


def test_save_is_atomic_no_partial_on_existing():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        save_queue(p, [_rec(1, "a")])
        save_queue(p, [_rec(1, "a"), _rec(2, "b")])   # overwrite
        assert len(load_queue(p)) == 2
        # no leftover temp files in the dir
        assert [f for f in os.listdir(tmp) if f.endswith(".json")] == ["queue.json"]


def test_load_malformed_json_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        with open(p, "w") as f:
            f.write("{ not valid json")
        assert load_queue(p) == []


def test_roundtrip_preserves_first_and_last_seen():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "queue.json")
        rec = QueueRecord(number=7, first_seen="2026-06-01T00:00:00Z",
                          last_seen="2026-06-23T00:00:00Z",
                          item=WorkItem(schema_version=1, id="x", repo="r", kind="mr",
                                        executor="review", risk="low", why="w",
                                        web_url="u", sha="def"))
        save_queue(p, [rec])
        out = load_queue(p)
        assert out[0].first_seen == "2026-06-01T00:00:00Z"
        assert out[0].last_seen == "2026-06-23T00:00:00Z"
        assert out[0].item.sha == "def"
