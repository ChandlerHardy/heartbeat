import json, os, sys, tempfile
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
