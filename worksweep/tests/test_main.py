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
def test_main_returns_1_on_discord_post_failure(monkeypatch):
    cfg = WorksweepConfig(repos=(), username="me",
                          discord_webhook="https://discord/hook")
    monkeypatch.setattr(wsmain, "load_config", lambda *a, **k: cfg)

    def boom(webhook, content):
        raise RuntimeError("post failed")

    monkeypatch.setattr(wsmain, "_post_discord", boom)
    assert main(["--discord"]) == 1
