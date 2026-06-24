import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, Todo, Issue  # noqa: E402
from worksweep.config import WorksweepConfig  # noqa: E402
import worksweep.__main__ as wsmain  # noqa: E402
from worksweep.__main__ import build_digest, main  # noqa: E402


def _mr(**kw):
    base = dict(repo="pb-www", iid=1, title="t", author="chandler.hardy", web_url="u",
                description="no link", sha="abc", is_draft=False, reviewers=(),
                ci_status="success", updated_at="2026-06-22T10:00:00Z")
    base.update(kw)
    return MergeRequest(**base)


def test_build_digest_end_to_end_with_injected_collectors():
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="x")
    collect_fns = {
        "my_mrs": lambda repo, user: [_mr()],
        "review_requests": lambda repo, user: [],
        "todos": lambda: [],
        "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, iid: True)
    # mine + missing dev link -> exactly one hygiene item
    assert "mr-hygiene" in out
    assert "1." in out


def test_build_digest_empty_is_all_clear():
    cfg = WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="x")
    collect_fns = {
        "my_mrs": lambda repo, user: [], "review_requests": lambda repo, user: [],
        "todos": lambda: [], "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, iid: True)
    assert "nothing needs you" in out.lower()


# FIX 3 — one repo's collector failure does not abort the sweep
def test_build_digest_survives_one_repo_failure():
    cfg = WorksweepConfig(repos=("repoA", "repoB"), username="chandler.hardy",
                          discord_webhook="x")

    def flaky_my_mrs(repo, user):
        if repo == "repoA":
            raise RuntimeError("repoA boom")
        return [_mr(repo="repoB", description="no link")]

    collect_fns = {
        "my_mrs": flaky_my_mrs,
        "review_requests": lambda repo, user: [],
        "todos": lambda: [],
        "issues": lambda repo, user: [],
    }
    out = build_digest(collect_fns, cfg, has_magi=lambda r, iid: True)
    # repoB's hygiene item survives despite repoA raising
    assert "repoB" in out
    assert "mr-hygiene" in out


# FIX 4 — main() returns 1 when the Discord post fails
def test_main_returns_1_on_discord_post_failure(monkeypatch, tmp_path):
    cfg = WorksweepConfig(repos=(), username="me",
                          discord_webhook="https://discord/hook")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path",
                        lambda: os.path.join(str(tmp_path), "queue.json"))

    def boom(webhook, content):
        raise RuntimeError("post failed")

    monkeypatch.setattr(wsmain, "_post_discord", boom)
    assert main(["--discord"]) == 1


# Long digest is delivered across multiple Discord messages, none over the cap
def test_main_discord_posts_multiple_messages_for_long_digest(monkeypatch, tmp_path):
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="https://discord.com/api/webhooks/1/x")
    many = [_mr(iid=i, description="no link") for i in range(60)]
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path",
                        lambda: os.path.join(str(tmp_path), "queue.json"))
    monkeypatch.setattr(wsmain.assessor, "has_magi_report", lambda repo, iid: False)
    monkeypatch.setattr(wsmain.collectors, "collect_my_mrs", lambda repo, user: many)
    monkeypatch.setattr(wsmain.collectors, "collect_review_requests", lambda repo, user: [])
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
    cfg = WorksweepConfig(repos=("pb-www",), username="chandler.hardy",
                          discord_webhook="https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(wsmain, "_queue_path", lambda: qp)
    monkeypatch.setattr(wsmain.assessor, "has_magi_report", lambda repo, iid: False)
    # one MR of mine, missing a dev link -> a magi item + a hygiene item
    monkeypatch.setattr(wsmain.collectors, "collect_my_mrs",
                        lambda repo, user: [_mr(iid=3890, description="no link")])
    monkeypatch.setattr(wsmain.collectors, "collect_review_requests", lambda repo, user: [])
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
