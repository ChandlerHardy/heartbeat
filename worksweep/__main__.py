"""Worksweep entry point: collect -> assess -> dedupe -> format -> output.

M1: read-only digest. `--dry-run` prints to stdout; `--discord` posts.
M2: `--discord`/`--dry-run` reconcile the sweep into a persistent queue and
render the digest from the queue's stable numbers; the new `intake` subcommand
polls Discord for Chandler's approval replies and flips matched items to
`approved` (no executors — approval is status-only).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

# Only ever POST the digest to a Discord-owned host. The webhook comes from
# ~/etc/heartbeat.json (semi-trusted); this allowlist + no-redirect opener stop
# a tampered/typo'd webhook from exfiltrating the digest elsewhere.
_ALLOWED_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")

from . import assessor, collectors
from .approvals import apply_approvals
from .config import WorksweepConfig, load_config
from .discord_read import fetch_messages
from .formatter import (
    format_digest, format_messages,
    format_digest_from_records, format_messages_from_records,
)
from .queue import load_queue, reconcile, save_queue

_QUEUE_DEFAULT = os.path.expanduser("~/.worksweep/queue.json")
_CURSOR_DEFAULT = os.path.expanduser("~/.worksweep/intake-cursor")


def _queue_path() -> str:
    """Path to the persistent queue (overridable in tests)."""
    return _QUEUE_DEFAULT


def _cursor_path() -> str:
    """Path to the intake last-seen-message-id cursor (overridable in tests)."""
    return _CURSOR_DEFAULT


def _now() -> str:
    """Current UTC timestamp (ISO8601). The CLI edge — pure fns take `now` in."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read_cursor() -> Optional[str]:
    try:
        with open(_cursor_path()) as f:
            val = f.read().strip()
            return val or None
    except FileNotFoundError:
        return None


def _write_cursor(message_id: str) -> None:
    path = _cursor_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(message_id)
    os.replace(tmp, path)


def collect_and_assess(collect_fns: Dict[str, Callable], cfg: WorksweepConfig,
                       has_magi: Callable[[str, int], bool]) -> list:
    """Collect signals -> assess -> dedupe. Returns the WorkItem list."""
    items = []
    for repo in cfg.repos:
        try:
            for mr in collect_fns["my_mrs"](repo, cfg.username):
                items += assessor.assess_mr(mr, cfg.username, has_magi)
            for mr in collect_fns["review_requests"](repo, cfg.username):
                items += assessor.assess_mr(mr, cfg.username, has_magi)
            for iss in collect_fns["issues"](repo, cfg.username):
                items += assessor.assess_issue(iss)
        except Exception as e:  # one repo's failure must not abort the sweep
            print(f"worksweep: skipping repo {repo}: {e}", file=sys.stderr)
            continue
    try:
        for td in collect_fns["todos"]():
            items += assessor.assess_todo(td)
    except Exception as e:
        print(f"worksweep: todos collection failed: {e}", file=sys.stderr)
    return assessor.dedupe(items)


def build_digest(collect_fns: Dict[str, Callable], cfg: WorksweepConfig,
                 has_magi: Callable[[str, int], bool]) -> str:
    """Full digest as a single string (stdout view)."""
    return format_digest(collect_and_assess(collect_fns, cfg, has_magi))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so a 30x can't bounce the POST off-host."""
    def redirect_request(self, *args, **kwargs):
        return None


def _validate_webhook(webhook: str) -> None:
    """Raise RuntimeError unless the webhook is an https Discord-host URL."""
    parts = urllib.parse.urlparse(webhook)
    if parts.scheme != "https":
        raise RuntimeError(f"discord webhook must be https, got {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_WEBHOOK_HOSTS and not any(
            host.endswith("." + h) for h in _ALLOWED_WEBHOOK_HOSTS):
        raise RuntimeError(f"refusing to post to non-Discord host: {host!r}")


def _post_discord(webhook: str, content: str) -> None:
    """POST the digest to Discord. Raises RuntimeError on a bad host or network failure."""
    _validate_webhook(webhook)
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "WorksweepBot/1.0"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=15) as resp:
            resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"discord post failed: {e}")


def _run_intake(cfg: WorksweepConfig) -> int:
    """Poll Discord for approval replies and flip matched queue items.

    Read-only Discord (one GET) + an optional confirmation POST via the M1
    webhook. No executor runs — approval is status-only.
    """
    if not cfg.bot_token or not cfg.channel_id:
        print("worksweep: intake needs a discord bot_token + channel_id "
              "(set the `discord` block in ~/etc/heartbeat.json)", file=sys.stderr)
        return 1

    qpath = _queue_path()
    records = load_queue(qpath)
    after = _read_cursor()
    try:
        messages = fetch_messages(cfg.channel_id, cfg.bot_token, after=after)
    except Exception as e:
        print(f"worksweep: discord fetch failed: {e}", file=sys.stderr)
        return 1

    now = _now()
    updated, approved = apply_approvals(records, messages, cfg.discord_user_id, now)
    if approved != set():
        save_queue(qpath, updated)

    # Advance the cursor to the newest message id seen so the next poll only
    # reads newer messages (Discord ids are monotonically increasing snowflakes).
    if messages:
        newest = max(messages, key=lambda m: int(m.id) if m.id.isdigit() else 0)
        if newest.id:
            _write_cursor(newest.id)

    if approved:
        nums = sorted(approved)
        by_num = {r.number: r for r in updated}
        details = ", ".join(
            f"{n} ({by_num[n].item.executor} {by_num[n].item.repo})".strip()
            for n in nums if n in by_num)
        confirm = f"✅ Approved: {details}"
        if cfg.discord_webhook:
            try:
                _post_discord(cfg.discord_webhook, confirm)
            except Exception as e:
                print(f"worksweep: confirmation post failed: {e}", file=sys.stderr)
        else:
            print(confirm)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("command", nargs="?", choices=["intake"],
                    help="`intake` polls Discord for approval replies")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, no Discord")
    ap.add_argument("--discord", action="store_true", help="post digest to Discord")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except RuntimeError as e:
        print(f"worksweep: {e}", file=sys.stderr)
        return 1

    if args.command == "intake":
        return _run_intake(cfg)

    collect_fns = {
        "my_mrs": collectors.collect_my_mrs,
        "review_requests": collectors.collect_review_requests,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
    }
    items = collect_and_assess(
        collect_fns, cfg,
        has_magi=lambda repo, iid: assessor.has_magi_report(repo, iid))

    # Reconcile the fresh sweep into the persistent queue so the rendered digest
    # uses stable, persisted numbers — the number a user replies to maps to the
    # same WorkItem next sweep. The queue is the source of truth for numbering.
    records = reconcile(load_queue(_queue_path()), items, _now())
    try:
        save_queue(_queue_path(), records)
    except OSError as e:
        print(f"worksweep: could not persist queue: {e}", file=sys.stderr)

    if args.discord and not args.dry_run:
        if not cfg.discord_webhook:
            print("worksweep: no discord_webhook configured", file=sys.stderr)
            return 1
        try:
            for message in format_messages_from_records(records):
                _post_discord(cfg.discord_webhook, message)
        except Exception as e:
            print(f"worksweep: {e}", file=sys.stderr)
            return 1
    else:
        print(format_digest_from_records(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
