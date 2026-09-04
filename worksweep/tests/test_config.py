import json, os, sys, tempfile
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.config import load_config  # noqa: E402


def _write(tmp, obj):
    p = os.path.join(tmp, "heartbeat.json")
    with open(p, "w") as f:
        json.dump(obj, f)
    return p


def test_load_config_reads_gitlab_block():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "discord_webhook": "https://discord/hook",
            "gitlab": {"username": "chandler.hardy", "repos": ["pb-www", "pb-api"]},
        })
        cfg = load_config(p)
        assert cfg.username == "chandler.hardy"
        assert cfg.repos == ("pb-www", "pb-api")
        assert cfg.discord_webhook == "https://discord/hook"


def test_load_config_missing_gitlab_block_yields_empty_repos():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"discord_webhook": "x"})
        cfg = load_config(p)
        assert cfg.repos == () and cfg.username == ""


# FIX 5 — clear failure when the config file does not exist
def test_load_config_missing_file_raises_with_path():
    missing = "/nonexistent/dir/heartbeat.json"
    with pytest.raises(RuntimeError) as exc:
        load_config(missing)
    assert missing in str(exc.value)


# FIX 5 — clear failure on malformed JSON
def test_load_config_malformed_json_raises_with_path():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "heartbeat.json")
        with open(p, "w") as f:
            f.write("{ not valid json")
        with pytest.raises(RuntimeError) as exc:
            load_config(p)
        assert p in str(exc.value)


# M2 — the discord block populates bot_token / channel_id / discord_user_id
def test_load_config_reads_discord_block():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "chandler.hardy", "repos": ["pb-www"]},
            "discord": {"bot_token": "BOT", "channel_id": "123", "user_id": "456"},
        })
        cfg = load_config(p)
        assert cfg.bot_token == "BOT"
        assert cfg.channel_id == "123"
        assert cfg.discord_user_id == "456"


# M2 — missing discord block leaves all three fields empty (graceful)
def test_load_config_missing_discord_block_yields_empty_strings():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []}})
        cfg = load_config(p)
        assert cfg.bot_token == ""
        assert cfg.channel_id == ""
        assert cfg.discord_user_id == ""


# I4 — a non-numeric runner.timeout_seconds must raise RuntimeError (caught by
# main()'s never-silent handler), not an uncaught ValueError.
def test_load_config_non_integer_timeout_raises_runtime_error():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"timeout_seconds": "1800s"},
        })
        with pytest.raises(RuntimeError) as exc:
            load_config(p)
        assert "runner.timeout_seconds" in str(exc.value)
        assert "1800s" in str(exc.value)


# M3.5 Task C — curate defaults on, is read from the existing `runner` block
# (no separate curator_bin -- claude_bin is reused for the curator LLM edge).
def test_load_config_curate_defaults_true():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []}})
        cfg = load_config(p)
        assert cfg.curate is True
        assert cfg.claude_bin == "claude"


def test_load_config_reads_curate_false_from_runner_block():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"curate": False, "claude_bin": "/usr/local/bin/claude"},
        })
        cfg = load_config(p)
        assert cfg.curate is False
        assert cfg.claude_bin == "/usr/local/bin/claude"


def test_load_config_non_bool_curate_raises_runtime_error():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"curate": "yes"},
        })
        with pytest.raises(RuntimeError) as exc:
            load_config(p)
        assert "runner.curate" in str(exc.value)


# M4 Task F — runner.dev_boxes: dev-slot sensing config. Empty/absent -> ()
# (feature off, matches the existing "absent runner block -> graceful" pattern).
def test_load_config_dev_boxes_defaults_empty_tuple():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []}})
        cfg = load_config(p)
        assert cfg.dev_boxes == ()


def test_load_config_reads_dev_boxes_list_of_dicts():
    boxes = [
        {"name": "dev1", "host": "chandlerhardy-dev",
         "path": "/home/chandlerhardy/dev1.chandlerhardy-dev/pb-www",
         "url": "https://dev1.chandlerhardy-dev.performancebeef.com/"},
        {"name": "dev4", "host": "chandlerhardy-dev",
         "path": "/home/chandlerhardy/dev4.chandlerhardy-dev/pb-www",
         "url": "https://dev4.chandlerhardy-dev.performancebeef.com/"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"dev_boxes": boxes},
        })
        cfg = load_config(p)
        assert cfg.dev_boxes == tuple(boxes)
        assert cfg.dev_boxes[0]["name"] == "dev1"


def test_load_config_non_list_dev_boxes_raises_runtime_error():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"dev_boxes": "dev1"},
        })
        with pytest.raises(RuntimeError) as exc:
            load_config(p)
        assert "runner.dev_boxes" in str(exc.value)


# M4 Task H — runner.stale_threshold: commits-behind-master threshold for
# the keep-current sensor. Absent -> 5 (matches the plan's default).
def test_load_config_stale_threshold_defaults_to_five():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []}})
        cfg = load_config(p)
        assert cfg.stale_threshold == 5


def test_load_config_reads_stale_threshold_from_runner_block():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"stale_threshold": 10},
        })
        cfg = load_config(p)
        assert cfg.stale_threshold == 10


def test_load_config_non_integer_stale_threshold_raises_runtime_error():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {
            "gitlab": {"username": "x", "repos": []},
            "runner": {"stale_threshold": "five"},
        })
        with pytest.raises(RuntimeError) as exc:
            load_config(p)
        assert "runner.stale_threshold" in str(exc.value)


def test_load_config_auto_approve_defaults_to_keep_current():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"discord_webhook": "x"})
        assert load_config(p).auto_approve == ("keep-current",)


def test_load_config_auto_approve_empty_list_disables():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"discord_webhook": "x",
                         "runner": {"auto_approve": []}})
        assert load_config(p).auto_approve == ()


def test_load_config_auto_approve_non_list_raises():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"discord_webhook": "x",
                         "runner": {"auto_approve": "keep-current"}})
        with pytest.raises(RuntimeError, match="auto_approve"):
            load_config(p)


# 2026-09-04 (tiered feedback) — address-feedback has its own budget, read
# from runner.feedback_timeout, default 3600, non-integer rejected loudly.
def test_load_config_feedback_timeout_defaults_and_reads():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []}})
        assert load_config(p).feedback_timeout == 3600
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []},
                         "runner": {"feedback_timeout": 5400}})
        assert load_config(p).feedback_timeout == 5400


def test_load_config_non_integer_feedback_timeout_raises_runtime_error():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(tmp, {"gitlab": {"username": "x", "repos": []},
                         "runner": {"feedback_timeout": "an hour"}})
        with pytest.raises(RuntimeError) as exc:
            load_config(p)
        assert "runner.feedback_timeout" in str(exc.value)
