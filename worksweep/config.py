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
    return WorksweepConfig(
        repos=tuple(gl.get("repos") or []),
        username=gl.get("username", ""),
        discord_webhook=data.get("discord_webhook", ""),
    )
