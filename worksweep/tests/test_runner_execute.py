"""execute() subprocess contract + run_once orchestration (all edges injected)."""
import os
import subprocess

import pytest

from worksweep.config import WorksweepConfig, load_config
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import RunnerError, execute, extract_verdict, find_report, run_once

NOW = "2026-08-07T12:00:00+00:00"


def _cfg(tmp_path):
    return WorksweepConfig(
        repos=("pb-www",), username="me",
        discord_webhook="https://discord.com/api/webhooks/x/y",
        checkouts_root=str(tmp_path), claude_bin="claude", runner_timeout=1800)


def _approved(number=1):
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"review:pb-www!{number}",
                      repo="pb-www", kind="review_request",
                      executor="magi-review", risk="low", why="",
                      web_url="https://gl/x/-/merge_requests/4020",
                      sha="s1", status="approved"))


def test_runner_config_block(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text('{"gitlab": {"repos": ["pb-www"], "username": "me"},'
                 '"discord_webhook": "https://discord.com/api/webhooks/x/y",'
                 '"runner": {"checkouts_root": "/co", "timeout_seconds": 900}}')
    cfg = load_config(str(p))
    assert cfg.checkouts_root == "/co"
    assert cfg.runner_timeout == 900
    assert cfg.claude_bin == "claude"


def test_execute_invokes_fetch_then_claude(tmp_path):
    os.makedirs(tmp_path / "pb-www")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-2026-08-07.md"
     ).write_text("## Verdict\nSHIP\n")
    sha, report = execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)
    assert sha == "s1" and report.endswith("tribunal-report-mr-4020-2026-08-07.md")
    assert calls[0][0][:3] == ("git", "-C", str(tmp_path / "pb-www"))
    assert calls[1][0][0] == "claude"
    assert "/magi:magi-review !4020" in calls[1][0]
    assert calls[1][1] == str(tmp_path / "pb-www")


def test_execute_missing_checkout_raises(tmp_path):
    with pytest.raises(RunnerError, match="checkout"):
        execute(_approved().item, _cfg(tmp_path))


def test_execute_nonzero_claude_raises(tmp_path):
    os.makedirs(tmp_path / "pb-www")

    def fake_run(cmd, **kw):
        rc = 1 if cmd[0] == "claude" else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom\n")

    with pytest.raises(RunnerError, match="boom"):
        execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)


def test_find_report_picks_newest(tmp_path):
    magi = tmp_path / ".magi"
    magi.mkdir()
    a = magi / "tribunal-report-mr-7-2026-08-01.md"
    b = magi / "tribunal-report-mr-7-2026-08-07.md"
    a.write_text("old"); b.write_text("new")
    os.utime(a, (1, 1))
    assert find_report(str(tmp_path), 7) == str(b)
    assert find_report(str(tmp_path), 8) is None


def test_extract_verdict_section(tmp_path):
    r = tmp_path / "r.md"
    r.write_text("# T\n\n## Verdict\nline1\nline2\n\n## Next\nx\n")
    v = extract_verdict(str(r))
    assert "line1" in v and "## Next" not in v


def test_run_once_happy_path(tmp_path):
    posts, saves = [], []
    deps = {"load": lambda: [_approved()],
            "save": lambda recs: saves.append(recs),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW,
            "execute": lambda item, cfg: ("s1", "/r.md")}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 0
    final = saves[-1]
    assert final[0].item.status == "done"
    assert any("magi-review" in p for p in posts)


def test_run_once_failure_posts_warning(tmp_path):
    posts, saves = [], []

    def boom(item, cfg):
        raise RunnerError("claude timed out")

    deps = {"load": lambda: [_approved()], "save": lambda r: saves.append(r),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW, "execute": boom}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 1
    assert saves[-1][0].item.status == "error"
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_nonrunner_exception_still_fails_and_posts(tmp_path):
    """FileNotFoundError (e.g. `claude`/git missing from launchd's minimal
    PATH) must not propagate uncaught — the claim must flip to error and
    Discord must get a ⚠️, not silence until the 45-min reap."""
    posts, saves = [], []

    def boom(item, cfg):
        raise OSError("[Errno 2] No such file or directory: 'claude'")

    deps = {"load": lambda: [_approved()], "save": lambda r: saves.append(r),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW, "execute": boom}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 1
    assert saves[-1][0].item.status == "error"
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_nothing_approved_is_quiet(tmp_path):
    posts = []
    deps = {"load": lambda: [], "save": lambda r: None,
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW, "execute": lambda i, c: ("", "")}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 0
    assert posts == []   # runner is event-only, no heartbeat spam every 10 min
