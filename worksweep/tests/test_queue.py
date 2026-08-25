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


def test_save_queue_uses_a_unique_temp_name_per_write(tmp_path):
    """F3: a FIXED `path + '.tmp'` is shared by every writer -- two concurrent
    writers interleave their bytes into the same temp file and os.replace then
    publishes the mixture as the whole queue."""
    import glob, threading
    from worksweep.queue import save_queue, load_queue
    from worksweep.models import WorkItem, QueueRecord

    qp = os.path.join(str(tmp_path), "queue.json")

    def _recs(n, tag):
        return [QueueRecord(number=i, first_seen="t", last_seen=tag,
                            item=WorkItem(schema_version=1, id=f"{tag}{i}",
                                          repo="pb-www", kind="mr",
                                          executor="magi-review", risk="low",
                                          why="w" * 400, web_url="u", sha="s"))
                for i in range(1, n + 1)]

    errors = []

    def writer(tag):
        try:
            for _ in range(15):
                save_queue(qp, _recs(60, tag))
        except Exception as e:                       # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,))
               for t in ("aaaa", "bbbb", "cccc")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    # the published file is always ONE writer's complete payload, never a mix
    records = load_queue(qp)
    assert len(records) == 60
    assert len({r.last_seen for r in records}) == 1
    # and no temp files are left lying around
    assert glob.glob(os.path.join(str(tmp_path), ".queue-*")) == []
    assert not os.path.exists(qp + ".tmp")
