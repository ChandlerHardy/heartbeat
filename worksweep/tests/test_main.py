import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest  # noqa: E402
from worksweep.config import WorksweepConfig  # noqa: E402
import worksweep.__main__ as wsmain  # noqa: E402
from worksweep.__main__ import main  # noqa: E402


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="chandler.hardy", web_url="u",
                description="no link", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


# FIX 4 — main() returns 1 when the Discord post fails
def test_main_returns_1_on_discord_post_failure(monkeypatch, tmp_path):
    cfg = WorksweepConfig(repos=(), username="me",
                          discord_webhook="https://discord/hook")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path",
                        lambda: os.path.join(str(tmp_path), "queue.json"))
    monkeypatch.setattr(wsmain.collectors, "run_graphql_sweep", lambda: "")
    monkeypatch.setattr(wsmain.collectors, "parse_graphql_sweep",
                        lambda raw, user, repos: ([], [], []))
    monkeypatch.setattr(wsmain.collectors, "collect_todos", lambda: [])

    def boom(webhook, content):
        raise RuntimeError("post failed")

    monkeypatch.setattr(wsmain, "_post_discord", boom)
    assert main(["--discord"]) == 1


# --discord with no configured webhook must hard-fail before run_sweep, not
# silently degrade to stdout printing (never-silent contract: a cron caller
# checking the exit code must see the failure).
def test_main_discord_without_webhook_hard_fails(monkeypatch, tmp_path, capsys):
    cfg = WorksweepConfig(repos=(), username="me", discord_webhook="")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path",
                        lambda: os.path.join(str(tmp_path), "queue.json"))

    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))
    # graphql/todos must never even be consulted -- the guard trips first.
    monkeypatch.setattr(wsmain.collectors, "run_graphql_sweep",
                        lambda: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr(wsmain.collectors, "collect_todos",
                        lambda: (_ for _ in ()).throw(AssertionError("should not run")))

    assert main(["--discord"]) == 1
    assert posted == []
    err = capsys.readouterr().err
    assert "no discord_webhook configured" in err


# Long digest is delivered across multiple Discord messages, none over the cap
def test_main_discord_posts_multiple_messages_for_long_digest(monkeypatch, tmp_path):
    # curate=False: this test is about the raw formatter's pagination, not
    # curation -- and disabling it keeps this test from shelling out to a
    # real `claude` (main() wires a real curator LLM edge for non-dry-run).
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="https://discord.com/api/webhooks/1/x",
                          curate=False)
    many = [_mr(iid=i, description="no link") for i in range(60)]
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path",
                        lambda: os.path.join(str(tmp_path), "queue.json"))
    monkeypatch.setattr(wsmain.assessor, "has_magi_report", lambda repo, iid: False)
    monkeypatch.setattr(wsmain.collectors, "run_graphql_sweep", lambda: "")
    monkeypatch.setattr(wsmain.collectors, "parse_graphql_sweep",
                        lambda raw, user, repos: ([], many, []))
    monkeypatch.setattr(wsmain.collectors, "collect_todos", lambda: [])
    monkeypatch.setattr(wsmain.collectors, "collect_issues", lambda repo, user: [])

    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))
    assert main(["--discord"]) == 0
    assert len(posted) > 1  # 60 MRs -> 120 items -> spans multiple messages
    for m in posted:
        assert len(m.encode("utf-8")) <= 1900


# HARDENING — webhook host allowlist (reject SSRF/exfil targets)
def test_validate_webhook_accepts_discord_hosts():
    wsmain._validate_webhook("https://discord.com/api/webhooks/123/abc")
    wsmain._validate_webhook("https://canary.discord.com/api/webhooks/1/x")
    wsmain._validate_webhook("https://discordapp.com/api/webhooks/1/x")


def test_validate_webhook_rejects_non_discord_host():
    import pytest
    with pytest.raises(RuntimeError):
        wsmain._validate_webhook("https://evil.example.com/api/webhooks/1/x")


def test_validate_webhook_rejects_non_https():
    import pytest
    with pytest.raises(RuntimeError):
        wsmain._validate_webhook("http://discord.com/api/webhooks/1/x")


def test_validate_webhook_rejects_lookalike_host():
    import pytest
    with pytest.raises(RuntimeError):
        # discord.com.evil.com must NOT pass a naive substring check
        wsmain._validate_webhook("https://discord.com.evil.com/api/webhooks/1/x")


# M2 — the --discord post path persists the queue and the posted number equals
# the persisted QueueRecord.number (the load-bearing numbering contract).
def test_post_persists_queue_and_posted_number_matches_record_number(monkeypatch, tmp_path):
    from worksweep.queue import load_queue
    qp = os.path.join(str(tmp_path), "queue.json")
    # curate=False: this test is about the raw formatter's number contract,
    # not curation -- and disabling it keeps this test from shelling out to
    # a real `claude` (main() wires a real curator LLM edge for non-dry-run).
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="https://discord.com/api/webhooks/1/x",
                          curate=False)
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain.assessor, "has_magi_report", lambda repo, iid: False)
    # one MR of mine, missing a dev link -> a magi item + a hygiene item
    monkeypatch.setattr(wsmain.collectors, "run_graphql_sweep", lambda: "")
    monkeypatch.setattr(
        wsmain.collectors, "parse_graphql_sweep",
        lambda raw, user, repos: ([], [_mr(iid=3890, description="no link")], []))
    monkeypatch.setattr(wsmain.collectors, "collect_todos", lambda: [])
    monkeypatch.setattr(wsmain.collectors, "collect_issues", lambda repo, user: [])

    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))

    assert main(["--discord"]) == 0

    records = load_queue(qp)
    assert records, "post path must persist the queue"
    joined = "\n".join(posted)
    # every persisted record's number appears in the posted digest as "<n>. "
    for r in records:
        assert f"{r.number}. " in joined
    # and the queue holds the same count of proposed items the digest rendered
    assert all(r.item.status == "proposed" for r in records)
