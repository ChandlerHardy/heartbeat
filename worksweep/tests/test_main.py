import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pytest  # noqa: E402
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


# --- the dashboard's Dismiss edge: glab mark-as-done -------------------------

def test_mark_todo_done_calls_the_write_endpoint():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Completed(0)

    wsmain._mark_todo_done(4242, run_subprocess=fake_run)
    cmd, kw = calls[0]
    assert cmd == ["glab", "api", "todos/4242/mark_as_done", "-X", "POST"]
    import subprocess as _sp
    assert kw["stdin"] is _sp.DEVNULL
    assert kw["timeout"] == 30                     # a write, not a 30s read
    assert kw["capture_output"] is True


def test_mark_todo_done_raises_so_the_dashboard_can_log_and_continue():
    import pytest
    with pytest.raises(RuntimeError) as e:
        wsmain._mark_todo_done(
            1, run_subprocess=lambda cmd, **kw: _Completed(1, "", "404 Not Found"))
    assert "404 Not Found" in str(e.value)
    with pytest.raises(RuntimeError):
        wsmain._mark_todo_done(
            1, run_subprocess=lambda cmd, **kw: (_ for _ in ()).throw(
                FileNotFoundError("glab")))


# --- the park executor's glab edge ------------------------------------------

def test_run_glab_api_sends_a_json_body_on_stdin():
    """The 2026-08 array bug: glab's own help says neither --field nor
    --raw-field parses JSON, so an MR description must go over as a raw body."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Completed(0, '{"ok":true}')

    out = wsmain._run_glab_api(
        ["api", "projects/x/merge_requests/1", "-X", "PUT", "--input", "-"],
        body='{"description":"a\\nb"}', run_subprocess=fake_run)
    cmd, kw = calls[0]
    assert cmd[0] == "glab"
    assert cmd[1:3] == ["api", "projects/x/merge_requests/1"]
    assert kw["input"] == '{"description":"a\\nb"}'
    assert kw["timeout"] == 30
    assert kw["capture_output"] is True
    assert out == '{"ok":true}'


def test_run_glab_api_reads_no_stdin_when_there_is_no_body():
    """A read must never inherit the parent's stdin under launchd."""
    import subprocess as _sp
    calls = []
    wsmain._run_glab_api(["api", "x"],
                         run_subprocess=lambda cmd, **kw: (calls.append(kw),
                                                           _Completed(0, "{}"))[1])
    assert calls[0]["stdin"] is _sp.DEVNULL
    assert calls[0]["input"] is None


def test_run_glab_api_raises_on_a_non_zero_exit():
    import pytest
    with pytest.raises(RuntimeError) as e:
        wsmain._run_glab_api(
            ["api", "x"],
            run_subprocess=lambda cmd, **kw: _Completed(1, "", "403 Forbidden"))
    assert "403 Forbidden" in str(e.value)


def test_run_glab_api_raises_when_glab_is_missing():
    import pytest
    with pytest.raises(RuntimeError):
        wsmain._run_glab_api(
            ["api", "x"],
            run_subprocess=lambda cmd, **kw: (_ for _ in ()).throw(
                FileNotFoundError("glab")))


# --- _post_discord: a delivery blip is not a sweep failure -------------------

WEBHOOK = "https://discord.com/api/webhooks/1/x"


def _http_error(code, retry_after=None):
    import email.message
    import urllib.error
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(WEBHOOK, code, f"HTTP {code}", headers, None)


class _Opener:
    """Serves a scripted sequence of outcomes to opener.open()."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0
        self.last_request = None

    def open(self, req, timeout=None):
        self.calls += 1
        self.last_request = req
        outcome = self.outcomes[self.calls - 1]
        if isinstance(outcome, Exception):
            raise outcome

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b""
        return _Resp()


def _post(outcomes, monkeypatch):
    """Run _post_discord against scripted outcomes; return (opener, sleeps)."""
    import urllib.request
    opener = _Opener(outcomes)
    sleeps = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)
    return opener, sleeps


def test_post_succeeds_first_try_without_sleeping(monkeypatch):
    opener, sleeps = _post([None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 1
    assert sleeps == []


def test_a_503_then_200_succeeds_after_one_retry(monkeypatch):
    """Falsifying: this is the live failure -- the sweep had already saved its
    work, then reported ⚠️ because one POST got a 503."""
    opener, sleeps = _post([_http_error(503), None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 2
    assert sleeps == [2]                       # first backoff only


def test_three_503s_exhaust_the_retries_and_raise(monkeypatch):
    import pytest
    opener, sleeps = _post([_http_error(503)] * 3, monkeypatch)
    with pytest.raises(RuntimeError) as e:
        wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 3                   # bounded, not forever
    assert sleeps == [2, 5]                    # slept between, not after the last
    assert str(e.value).startswith("discord post failed: ")


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_every_5xx_is_retried(monkeypatch, code):
    opener, sleeps = _post([_http_error(code), None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 2


def test_a_url_error_is_retried(monkeypatch):
    """The network-blip case: DNS hiccup, connection reset, timeout."""
    import urllib.error
    opener, sleeps = _post(
        [urllib.error.URLError("connection reset"), None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 2
    assert sleeps == [2]


def test_429_honours_retry_after_and_counts_as_an_attempt(monkeypatch):
    opener, sleeps = _post([_http_error(429, retry_after=4), None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 2
    assert sleeps == [4.0]                     # Discord's value, not our backoff


def test_429_retry_after_is_capped(monkeypatch):
    """Falsifying: an unbounded wait would park the sweep for minutes."""
    opener, sleeps = _post([_http_error(429, retry_after=600), None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert sleeps == [15.0]


@pytest.mark.parametrize("value", [None, "not-a-number", "-3",
                                   "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_429_without_a_usable_retry_after_falls_back_to_backoff(monkeypatch, value):
    opener, sleeps = _post([_http_error(429, retry_after=value), None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert sleeps == [2]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405])
def test_other_4xx_raises_immediately_with_no_retry(monkeypatch, code):
    """Falsifying: a malformed body or a revoked webhook never heals by being
    sent again -- retrying would just triple the latency of a certain failure."""
    import pytest as _pytest
    opener, sleeps = _post([_http_error(code)] * 3, monkeypatch)
    with _pytest.raises(RuntimeError) as e:
        wsmain._post_discord(WEBHOOK, "hi", sleep=sleeps.append)
    assert opener.calls == 1
    assert sleeps == []
    assert str(e.value).startswith("discord post failed: ")


def test_a_bad_webhook_still_raises_before_any_request(monkeypatch):
    """Validation and the no-redirect opener are untouched by the retry."""
    import pytest as _pytest
    opener, sleeps = _post([None], monkeypatch)
    with _pytest.raises(RuntimeError):
        wsmain._post_discord("https://evil.example.com/api/webhooks/1/x", "hi",
                             sleep=sleeps.append)
    assert opener.calls == 0
    assert sleeps == []


def test_the_no_redirect_opener_is_still_used(monkeypatch):
    import urllib.request
    seen = {}
    real = urllib.request.build_opener

    def spy(*handlers):
        seen["handlers"] = handlers
        return _Opener([None])
    monkeypatch.setattr(urllib.request, "build_opener", spy)
    wsmain._post_discord(WEBHOOK, "hi", sleep=lambda s: None)
    assert any(isinstance(h, wsmain._NoRedirect) for h in seen["handlers"])


def test_the_sleeper_defaults_to_real_time_sleep():
    """Injection is for the tests; production must actually wait."""
    import inspect
    import time as _time
    assert (inspect.signature(wsmain._post_discord)
            .parameters["sleep"].default is _time.sleep)


def test_retry_after_parsing():
    assert wsmain._retry_after_seconds(_http_error(429, 3)) == 3.0
    assert wsmain._retry_after_seconds(_http_error(429, "2.5")) == 2.5
    assert wsmain._retry_after_seconds(_http_error(429, 900)) == 15
    assert wsmain._retry_after_seconds(_http_error(429)) is None
    assert wsmain._retry_after_seconds(_http_error(503)) is None
    assert wsmain._retry_after_seconds(object()) is None


# --- mention hygiene (fix-mode round 2, warning 9) -------------------------

def test_every_post_disables_mention_parsing(monkeypatch):
    """Worksweep quotes text other people wrote -- MR titles, and now review
    thread bodies. A quoted `@everyone` must render as characters, not ring
    every phone on the server. Global, so it protects every post rather than
    the one caller that remembered."""
    import json as _json
    opener, sleeps = _post([None], monkeypatch)
    wsmain._post_discord(WEBHOOK, "leyang said @everyone ship it",
                         sleep=sleeps.append)
    body = _json.loads(opener.last_request.data.decode("utf-8"))
    assert body["allowed_mentions"] == {"parse": []}
    assert body["content"] == "leyang said @everyone ship it"
