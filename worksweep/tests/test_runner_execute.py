"""execute() subprocess contract + run_once orchestration (all edges injected)."""
import dataclasses
import os
import subprocess

import pytest

from worksweep.config import WorksweepConfig, load_config
from worksweep.models import QueueRecord, WorkItem
from worksweep.runner import RunnerError, execute, extract_verdict, find_report, run_once

NOW = "2026-08-07T12:00:00+00:00"


def _cfg(tmp_path, **kw):
    base = dict(
        repos=("pb-www",), username="me",
        discord_webhook="https://discord.com/api/webhooks/x/y",
        checkouts_root=str(tmp_path), claude_bin="claude", runner_timeout=1800)
    base.update(kw)
    return WorksweepConfig(**base)


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
    assert "/magi:magi-review !4020 --advisory --draft-findings" in calls[1][0]
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


# I2 — rc 0 but no tribunal report file must NOT be a silent success: it must
# raise RunnerError so run_once flips the claim to error and posts a warning,
# instead of recording a permanent "done" with an empty report_path.
def test_execute_success_with_no_report_raises(tmp_path):
    os.makedirs(tmp_path / "pb-www")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with pytest.raises(RunnerError, match="no tribunal report"):
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


def test_run_once_no_report_flips_item_to_error(tmp_path):
    """I2: a real execute() (via fake subprocess, rc 0, no report file written)
    must surface as an error claim, not a silent 'done' with no report."""
    os.makedirs(tmp_path / "pb-www")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    posts, saves = [], []
    deps = {"load": lambda: [_approved()],
            "save": lambda recs: saves.append(recs),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW,
            "execute": lambda item, cfg: execute(item, cfg, run_subprocess=fake_run)}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 1
    assert saves[-1][0].item.status == "error"
    assert "no tribunal report" in saves[-1][0].item.error_summary
    assert any(p.startswith("⚠️") for p in posts)


def test_run_once_preserves_concurrent_intake_approval(tmp_path):
    """C1: run_once must not clobber a concurrent intake approval that lands
    while #1 is mid-execute. deps["load"] is a stateful fake: the FIRST call
    (at the top of run_once) returns the pre-execute snapshot (#1 approved,
    #2 not yet approved); the SECOND call (post-execute re-load, simulating
    intake having run and saved during the 30-min execute window) returns #2
    now approved too. After run_once, #1 must be done AND #2 must still show
    approved — the post-execute save must not stomp #2's approval by writing
    back the stale pre-execute snapshot."""
    def _with_status(rec, status):
        return dataclasses.replace(rec, item=dataclasses.replace(rec.item, status=status))

    # Pre-execute snapshot: #1 approved (the runner's target), #2 not yet
    # approved. Post-execute (fresh) snapshot: intake ran mid-execute and
    # flipped #2 to approved — the change a stale-snapshot save would lose.
    pre = [_approved(1), _with_status(_approved(2), "proposed")]
    post_execute = [_approved(1), _with_status(_approved(2), "approved")]
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return pre if calls["n"] == 1 else post_execute

    saves = []
    posts = []
    deps = {"load": fake_load,
            "save": lambda recs: saves.append(recs),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW,
            "execute": lambda item, cfg: ("s1", "/r.md")}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 0

    final = saves[-1]
    by_num = {r.number: r for r in final}
    assert by_num[1].item.status == "done"
    assert by_num[2].item.status == "approved"


def test_run_once_lost_claim_after_fresh_reload_posts_warning(tmp_path):
    """C1 edge case: if the claimed record vanished from the queue by the time
    the post-execute fresh reload runs (queue rewritten out from under us),
    run_once must not crash or silently drop it — it posts a ⚠️ naming the
    lost record instead of saving a phantom update."""
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return [_approved(1)] if calls["n"] == 1 else []

    saves = []
    posts = []
    deps = {"load": fake_load,
            "save": lambda recs: saves.append(recs),
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW,
            "execute": lambda item, cfg: ("s1", "/r.md")}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 0
    assert any("#1" in p and "⚠️" in p for p in posts)
    # the claim save (pre-execute) happened, but there is no second (phantom)
    # post-execute save once the fresh reload can't find #1 anymore
    assert len(saves) == 1
    assert saves[0][0].number == 1 and saves[0][0].item.status == "running"


def test_run_once_nothing_approved_is_quiet(tmp_path):
    posts = []
    deps = {"load": lambda: [], "save": lambda r: None,
            "post": lambda hook, content: posts.append(content),
            "now": lambda: NOW, "execute": lambda i, c: ("", "")}
    assert run_once(_cfg(tmp_path), deps,
                    lock_path=str(tmp_path / "runner.lock")) == 0
    assert posts == []   # runner is event-only, no heartbeat spam every 10 min


def test_execute_passes_devnull_stdin_to_claude(tmp_path):
    """claude -p exits 1 ('no stdin data received in 3s') when spawned without a
    stdin under launchd/subprocess — found live 2026-08-18 on the first real ✅.
    Every claude -p edge must pass stdin=DEVNULL."""
    import subprocess as sp
    os.makedirs(tmp_path / "pb-www")
    seen = {}

    def fake_run(cmd, **kw):
        if cmd[0] == "claude":
            seen["stdin"] = kw.get("stdin")
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-x.md").write_text("## Verdict\nok\n")
    execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)
    assert seen["stdin"] is sp.DEVNULL


def test_run_dry_run_never_saves_or_posts(monkeypatch, tmp_path, capsys):
    """`worksweep run --dry-run` is a preview: it must not persist a claim/done
    onto the live queue nor post to Discord (2026-08-18 incident)."""
    from worksweep import __main__ as m
    from worksweep.config import WorksweepConfig
    from worksweep.models import QueueRecord, WorkItem
    from worksweep.queue import save_queue, load_queue
    qpath = str(tmp_path / "queue.json")
    rec = QueueRecord(number=1, first_seen="t", last_seen="t",
                      item=WorkItem(schema_version=1, id="review:pb-www!1", repo="pb-www",
                                    kind="review_request", executor="magi-review", risk="low",
                                    why="", web_url="https://gl/x/-/merge_requests/1", sha="s",
                                    status="approved"))
    save_queue(qpath, [rec])
    monkeypatch.setattr(m, "_queue_path", lambda: qpath)
    monkeypatch.setattr(m, "load_config", lambda: WorksweepConfig(
        repos=("pb-www",), username="me", discord_webhook="https://discord.com/api/webhooks/x/y"))
    posted = []
    monkeypatch.setattr(m, "_post_discord", lambda h, c: posted.append(c))
    monkeypatch.setattr(m._runner, "acquire_lock", lambda p: True, raising=False) if hasattr(m, "_runner") else None
    rc = m.main(["run", "--dry-run"])
    assert rc == 0
    assert posted == []                                   # nothing reached Discord
    assert load_queue(qpath)[0].item.status == "approved"  # live queue untouched
    assert "dry-run" in capsys.readouterr().out


# --- magi 0.2.4: the rebuttal round is mandatory now (2026-08-26) ----------
#
# Unattended runs used to skip rebuttal because there was nobody to wait for.
# 0.2.4 performs it mechanically, so `--no-rebuttal` is gone -- and a full
# tribunal that legitimately runs 40-60 minutes no longer fits the windows
# that were sized for a 30-minute one.

def test_the_magi_invocation_no_longer_suppresses_rebuttal(tmp_path):
    """FALSIFYING: re-adding the flag fails here. Passing a flag magi 0.2.4
    does not define is not a no-op -- it is an unknown-argument error, so
    every unattended review would fail."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www").mkdir()
    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-2026-08-07.md"
     ).write_text("## Verdict\nSHIP\n")
    execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)
    prompt = calls[1][0][2]
    assert prompt == "/magi:magi-review !4020 --advisory --draft-findings"
    assert "--no-rebuttal" not in prompt


def test_the_magi_run_gets_its_own_timeout_not_the_generic_one(tmp_path):
    """A full tribunal with rebuttal runs 40-60 min. `cfg.runner_timeout` is
    30 min AND is shared with address-feedback, whose own contract is that it
    finishes inside the 45-minute reap window -- so magi cannot just borrow it."""
    from worksweep.runner import MAGI_TIMEOUT_SECONDS
    calls = []

    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www").mkdir()
    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-2026-08-07.md"
     ).write_text("## Verdict\nSHIP\n")
    cfg = _cfg(tmp_path)
    execute(_approved().item, cfg, run_subprocess=fake_run)
    assert calls[1][1]["timeout"] == MAGI_TIMEOUT_SECONDS == 4500
    assert calls[1][1]["timeout"] > cfg.runner_timeout


def test_a_configured_magi_timeout_is_honoured(tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www").mkdir()
    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-2026-08-07.md"
     ).write_text("## Verdict\nSHIP\n")
    execute(_approved().item, _cfg(tmp_path, magi_timeout=6000),
            run_subprocess=fake_run)
    assert calls[1][1]["timeout"] == 6000


def test_the_timeout_message_names_the_magi_budget(tmp_path):
    from worksweep.runner import MAGI_TIMEOUT_SECONDS

    def fake_run(cmd, **kw):
        if cmd[0] == "claude":
            raise subprocess.TimeoutExpired(cmd, 4500)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www").mkdir()
    with pytest.raises(RunnerError) as e:
        execute(_approved().item, _cfg(tmp_path), run_subprocess=fake_run)
    assert str(MAGI_TIMEOUT_SECONDS) in str(e.value)

def _approved_own_mr(number=1):
    """A magi row on an AUTHORED MR -- assess_own_mr and the post-feedback
    chain both emit kind="mr" (only assess_review_request emits
    kind="review_request"), so this fixture stands for both sources."""
    return QueueRecord(
        number=number, first_seen=NOW, last_seen=NOW,
        item=WorkItem(schema_version=1, id=f"magi:pb-www!{number}@s1",
                      repo="pb-www", kind="mr",
                      executor="magi-review", risk="low", why="",
                      web_url="https://gl/x/-/merge_requests/4020",
                      sha="s1", status="approved"))


def test_an_authored_mr_review_is_advisory_and_never_stages_drafts(tmp_path):
    """Chandler, 2026-08-26: "we don't review our own MRs, we just fix the
    problems" -- a magi run on an authored MR (kind="mr": assessed OR
    post-feedback-chained) produces the report ONLY. `--draft-findings`
    would stage self-addressed review comments on our own MR.

    2026-09-04 (#256, !4110): without `--advisory` the skill runs its FULL
    fix loop -- it dispatched an implementer into the shared, read-only
    magi checkout to fix eleven of its own fresh Warnings, and nothing in
    this lane pushes or posts that work. One round, report only; the fix
    is a separate, consented row."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((tuple(cmd), kw.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    (tmp_path / "pb-www").mkdir()
    (tmp_path / "pb-www" / ".magi").mkdir()
    (tmp_path / "pb-www" / ".magi" / "tribunal-report-mr-4020-2026-08-07.md"
     ).write_text("## Verdict\nSHIP\n")
    execute(_approved_own_mr().item, _cfg(tmp_path), run_subprocess=fake_run)
    prompt = calls[1][0][2]
    assert prompt == "/magi:magi-review !4020 --advisory"
    assert "--draft-findings" not in prompt
