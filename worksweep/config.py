"""Load the worksweep slice of ~/etc/heartbeat.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorksweepConfig:
    repos: tuple
    username: str
    discord_webhook: str
    # M2 intake: the read-only bot identity + target channel + the only author
    # allowed to approve. Absent `discord` block -> all three are "" (graceful).
    bot_token: str = ""
    channel_id: str = ""
    discord_user_id: str = ""
    # M3 runner: parent dir of per-repo clones + the claude binary + the hard
    # timeout cap for one magi-review executor run. Absent `runner` block ->
    # checkouts_root="" (execute() then fails fast with a clear RunnerError).
    checkouts_root: str = ""
    claude_bin: str = "claude"
    runner_timeout: int = 1800


def load_config(path: str | None = None) -> WorksweepConfig:
    path = path or os.path.expanduser("~/etc/heartbeat.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"config not found: {path}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config is not valid JSON ({path}): {e}")
    gl = data.get("gitlab") or {}
    dc = data.get("discord") or {}
    rn = data.get("runner") or {}
    timeout_raw = rn.get("timeout_seconds", 1800)
    try:
        runner_timeout = int(timeout_raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"config runner.timeout_seconds must be an integer, got {timeout_raw!r}")
    return WorksweepConfig(
        repos=tuple(gl.get("repos") or []),
        username=gl.get("username", ""),
        discord_webhook=data.get("discord_webhook", ""),
        bot_token=dc.get("bot_token", ""),
        channel_id=dc.get("channel_id", ""),
        discord_user_id=dc.get("user_id", ""),
        checkouts_root=rn.get("checkouts_root", ""),
        claude_bin=rn.get("claude_bin", "claude"),
        runner_timeout=runner_timeout,
    )
