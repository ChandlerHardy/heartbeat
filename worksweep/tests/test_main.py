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
    # M4 Task H: main() always wires a real diverged-commits REST edge for
    # the digest path -- without this stub, 60 authored MRs means 60 real
    # `glab api ...` subprocess spawns (hermetic-suite violation; this alone
    # turned this test from ~0.05s into ~48s). 0 < stale_threshold, so no
    # stale items -- the "120 items" count below is unaffected.
    monkeypatch.setattr(wsmain.collectors, "collect_diverged_commits_count",
                        lambda repo, iid: 0)

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
    # M4 Task H: see the long-digest test above -- same hermetic-suite fix.
    monkeypatch.setattr(wsmain.collectors, "collect_diverged_commits_count",
                        lambda repo, iid: 0)

    posted = []
    monkeypatch.setattr(wsmain, "_post_discord", lambda wh, content: posted.append(content))

    assert main(["--discord"]) == 0

    records = load_queue(qp)
    assert records, "post path must persist the queue"
    joined = "\n".join(posted)
    # every persisted record's number appears in the posted digest as "<n>. "
    for r in records:
        assert f"**{r.number}.** " in joined
    # and the queue holds the same count of proposed items the digest rendered
    assert all(r.item.status == "proposed" for r in records)


# --- the dashboard's Sync edge: launchctl kickstart --------------------------

class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_kickstart_sweep_targets_the_sweep_agent_in_the_gui_domain():
    """The label must match the committed plist, and the domain must be this
    user's GUI session -- a system-domain target would not find the agent."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Completed(0)

    wsmain._kickstart_sweep(run_subprocess=fake_run)
    cmd, kw = calls[0]
    assert cmd == ["launchctl", "kickstart",
                   f"gui/{os.getuid()}/com.chandlerhardy.worksweep"]
    assert wsmain._SWEEP_AGENT_LABEL == "com.chandlerhardy.worksweep"
    # kickstart returns immediately; the sweep runs under its own agent
    assert kw["timeout"] == 15
    assert kw["capture_output"] is True
    # never hand the parent's stdin to a launchd child
    import subprocess as _sp
    assert kw["stdin"] is _sp.DEVNULL


def test_kickstart_sweep_label_matches_the_committed_plist():
    """Guards against the label drifting away from the agent that exists."""
    import plistlib, re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "etc", "mini", "com.chandlerhardy.worksweep.plist")
    raw = open(path, "rb").read()
    # plistlib/expat rejects the file's `--` inside an XML comment (Apple's own
    # parser accepts it), so read the Label out directly rather than parsing.
    label = re.search(rb"<key>Label</key>\s*<string>([^<]+)</string>", raw).group(1)
    assert label.decode() == wsmain._SWEEP_AGENT_LABEL
    assert b"--discord" in raw          # the agent really does post a digest


def test_kickstart_sweep_raises_on_a_non_zero_exit():
    """A failed kickstart must surface, not look like success -- the dashboard
    turns this into a 500 and leaves the button retryable."""
    import pytest
    with pytest.raises(RuntimeError) as e:
        wsmain._kickstart_sweep(
            run_subprocess=lambda cmd, **kw: _Completed(3, "", "Bad request"))
    assert "exited 3" in str(e.value)
    assert "Bad request" in str(e.value)


def test_kickstart_sweep_raises_when_launchctl_is_missing_or_times_out():
    import pytest
    for boom in (FileNotFoundError("launchctl"),
                 OSError("no such process"),
                 Exception("timed out")):
        with pytest.raises(RuntimeError):
            wsmain._kickstart_sweep(
                run_subprocess=lambda cmd, _b=boom, **kw: (_ for _ in ()).throw(_b))
