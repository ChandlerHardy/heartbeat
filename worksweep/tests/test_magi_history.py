"""Magi 'already reviewed' comes from queue history, seeded once from the glob."""
from worksweep.assessor import bootstrap_magi_records, has_magi_done
from worksweep.models import MergeRequest, QueueRecord, WorkItem

NOW = "2026-08-07T12:00:00+00:00"


def _done(id, sha="", result_sha="", repo="pb-www", number=1):
    return QueueRecord(number=number, first_seen=NOW, last_seen=NOW,
                       item=WorkItem(schema_version=1, id=id, repo=repo,
                                     kind="mr", executor="magi-review",
                                     risk="low", why="", web_url="", sha=sha,
                                     status="done", result_sha=result_sha))


def _mr(iid=9, sha="s9"):
    return MergeRequest(repo="pb-www", iid=iid, title="t",
                        author="chandler.hardy", web_url="u", description="",
                        sha=sha, is_draft=False, reviewers=(),
                        ci_status="unknown", updated_at="")


def test_done_at_current_sha_counts():
    recs = [_done("magi:pb-www!9@s9", sha="s9")]
    assert has_magi_done(recs, "pb-www", 9, "s9") is True


def test_done_at_stale_sha_does_not_count():
    recs = [_done("magi:pb-www!9@old", sha="old")]
    assert has_magi_done(recs, "pb-www", 9, "s9") is False


def test_executor_review_result_sha_counts():
    recs = [_done("review:pb-www!9", sha="s9", result_sha="s9")]
    assert has_magi_done(recs, "pb-www", 9, "s9") is True


def test_bootstrap_seeds_only_missing(tmp_path):
    recs = bootstrap_magi_records([], [_mr()], NOW,
                                  report_exists=lambda r, i: True)
    assert len(recs) == 1
    assert recs[0].item.done_reason == "bootstrap-glob"
    assert recs[0].item.id == "magi:pb-www!9@s9"
    # idempotent second pass
    again = bootstrap_magi_records(recs, [_mr()], NOW,
                                   report_exists=lambda r, i: True)
    assert len(again) == 1


def test_bootstrap_noop_without_reports():
    assert bootstrap_magi_records([], [_mr()], NOW,
                                  report_exists=lambda r, i: False) == []
