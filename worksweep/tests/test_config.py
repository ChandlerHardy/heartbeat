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
