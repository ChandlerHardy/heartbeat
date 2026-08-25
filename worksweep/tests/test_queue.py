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


# --- dismiss: the dashboard's retire-a-manual-row transition -----------------

def _drec(n, executor="triage", status="proposed", kind="todo", sha="",
          ident=None):
    from worksweep.models import WorkItem, QueueRecord
    return QueueRecord(
        number=n, first_seen="2026-08-01T00:00:00Z", last_seen="2026-08-01T00:00:00Z",
        item=WorkItem(schema_version=1,
                      id=ident or f"todo:assigned:https://gl/x/-/work_items/{n}",
                      repo="", kind=kind, executor=executor, risk="low", why="w",
                      web_url=f"https://gl/x/-/work_items/{n}", sha=sha,
                      status=status))


def test_is_dismissable_matrix():
    from worksweep.queue import is_dismissable
    for ex in ("triage", "mr-hygiene", "none", "review"):
        assert is_dismissable(_drec(1, executor=ex).item) is True
        assert is_dismissable(_drec(1, executor=ex, status="done").item) is False
        assert is_dismissable(_drec(1, executor=ex, status="error").item) is False
    # anything the runner claims is approve-territory, never dismiss-territory
    for ex in ("magi-review", "keep-current", "implement"):
        assert is_dismissable(_drec(1, executor=ex).item) is False


def test_dismiss_flips_only_the_named_dismissable_record():
    from worksweep.queue import dismiss
    recs = [_drec(1), _drec(2), _drec(3, executor="magi-review")]
    out, flipped = dismiss(recs, 1, "2026-08-25T09:00:00Z")
    assert flipped is not None and flipped.number == 1
    by = {r.number: r for r in out}
    assert (by[1].item.status, by[1].item.done_reason) == ("done", "dismissed")
    assert by[1].first_seen == "2026-08-01T00:00:00Z"      # preserved
    assert by[1].last_seen == "2026-08-25T09:00:00Z"       # bumped
    assert by[2].item.status == "proposed"
    assert by[3].item.status == "proposed"


def test_dismiss_returns_none_for_ineligible_or_missing_records():
    from worksweep.queue import dismiss
    recs = [_drec(1, executor="magi-review"), _drec(2, status="done")]
    for number in (1, 2, 99):
        out, flipped = dismiss(recs, number, "2026-08-25T09:00:00Z")
        assert flipped is None, number
        assert out == recs, number


def test_a_dismissed_todo_stays_dismissed_when_the_gitlab_todo_is_cleared():
    """The todo drops out of the sweep entirely; `done` is retained, not
    re-proposed."""
    from worksweep.queue import dismiss, reconcile
    recs = [_drec(1)]
    out, _ = dismiss(recs, 1, "2026-08-25T09:00:00Z")
    after = reconcile(out, [], "2026-08-25T13:00:00Z")
    assert len(after) == 1
    assert (after[0].item.status, after[0].item.done_reason) == ("done", "dismissed")


def test_a_dismissed_todo_stays_dismissed_even_if_gitlab_still_lists_it():
    """The case that actually matters here: worksweep cannot mark the GitLab
    todo done (no id is carried), so the todo comes back in every sweep. It
    must NOT resurrect -- todo items have sha "" on both sides, so reconcile's
    same-sha branch keeps the record `done`."""
    from worksweep.queue import dismiss, reconcile
    recs = [_drec(1)]
    out, _ = dismiss(recs, 1, "2026-08-25T09:00:00Z")
    still_pending = out[0].item.__class__(
        **{**out[0].item.__dict__, "status": "proposed", "done_reason": ""})
    after = reconcile(out, [still_pending], "2026-08-25T13:00:00Z")
    assert len(after) == 1
    assert after[0].item.status == "done"
    assert after[0].item.done_reason == "dismissed"
    assert after[0].number == 1                    # and keeps its handle
