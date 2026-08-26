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
    # M4 Task G: hard cap for ONE `/rubric:do` implement run (90 min). The
    # runner reaps an implement claim at implement_timeout + 15 min, so this
    # value also sets the stuck-claim window.
    implement_timeout: int = 5400
    # 2026-08-26 (magi 0.2.4): unattended tribunals now run the rebuttal round
    # mechanically instead of skipping it, so a healthy review takes 40-60 min
    # where it used to take under 30. magi-review therefore gets its own budget
    # rather than borrowing `runner_timeout` -- which is also address-feedback's,
    # and that one is contractually inside the 45-minute reap window.
    magi_timeout: int = 4500
    # M3.5 Task C: gate for the LLM digest-curation pass. Reuses claude_bin
    # (no separate curator_bin) -- both are just "claude" runs in a
    # subprocess, one against a checkout, one against the repo root.
    curate: bool = True
    # M4 Task F: dev boxes the implement executor can claim, each
    # {"name","host","path","url"}. Absent/empty `runner.dev_boxes` -> ()
    # (dev-slot sensing off, matches the M3 "absent block -> graceful" pattern).
    dev_boxes: tuple = ()
    # M4 Task H: an authored MR whose branch is this many (or more) commits
    # behind master gets a `keep-current` item. GitLab's REST
    # `diverged_commits_count` isn't in the GraphQL MR node, so this is
    # sensed with one REST call per authored MR -- see collectors.
    stale_threshold: int = 5
    # M5 (2026-08-24): when non-empty, the implement executor stops running
    # `/rubric:do` + its own MR/magi steps and instead has ONE claude run
    # drive this command end-to-end (the full pla-pipeline: implement ->
    # ship gate -> full-magi fix loop -> park+QA -> Draft MR), passing the
    # claimed slot via --dev. The executor then verifies the outcome (state
    # file, Draft read-back, box 200) instead of producing it.
    pipeline_command: str = ""
    # Executors whose freshly-proposed items skip the Discord ✅ gate and go
    # straight to `approved` at sweep time. keep-current is the only default:
    # a master merge is low-risk, aborts cleanly on conflict, and Chandler
    # okayed full autonomy for it (2026-08-24). Config `runner.auto_approve`
    # (a list of executor names, [] to disable) overrides.
    auto_approve: tuple = ("keep-current",)


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
    magi_raw = rn.get("magi_timeout", 4500)
    try:
        magi_timeout = int(magi_raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"config runner.magi_timeout must be an integer, got {magi_raw!r}")
    implement_raw = rn.get("implement_timeout", 5400)
    try:
        implement_timeout = int(implement_raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"config runner.implement_timeout must be an integer, "
            f"got {implement_raw!r}")
    curate_raw = rn.get("curate", True)
    if not isinstance(curate_raw, bool):
        raise RuntimeError(
            f"config runner.curate must be a boolean, got {curate_raw!r}")
    dev_boxes_raw = rn.get("dev_boxes") or []
    if not isinstance(dev_boxes_raw, list):
        raise RuntimeError(
            f"config runner.dev_boxes must be a list, got {type(dev_boxes_raw).__name__}")
    stale_raw = rn.get("stale_threshold", 5)
    try:
        stale_threshold = int(stale_raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"config runner.stale_threshold must be an integer, "
            f"got {stale_raw!r}")
    pipeline_raw = rn.get("pipeline_command", "")
    if not isinstance(pipeline_raw, str):
        raise RuntimeError(
            f"config runner.pipeline_command must be a string, "
            f"got {pipeline_raw!r}")
    aa_raw = rn.get("auto_approve", ["keep-current"])
    if not isinstance(aa_raw, list) or not all(isinstance(x, str) for x in aa_raw):
        raise RuntimeError(
            f"config runner.auto_approve must be a list of executor names, "
            f"got {aa_raw!r}")
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
        implement_timeout=implement_timeout,
        magi_timeout=magi_timeout,
        curate=curate_raw,
        dev_boxes=tuple(dev_boxes_raw),
        stale_threshold=stale_threshold,
        pipeline_command=pipeline_raw,
        auto_approve=tuple(aa_raw),
    )
