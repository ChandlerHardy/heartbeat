"""Worksweep entry point: collect -> assess -> dedupe -> format -> output.

Read-only M1. `--dry-run` prints to stdout; `--discord` posts the digest.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict

# Only ever POST the digest to a Discord-owned host. The webhook comes from
# ~/etc/heartbeat.json (semi-trusted); this allowlist + no-redirect opener stop
# a tampered/typo'd webhook from exfiltrating the digest elsewhere.
_ALLOWED_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")

from . import assessor, collectors
from .config import WorksweepConfig, load_config
from .formatter import format_digest


def build_digest(collect_fns: Dict[str, Callable], cfg: WorksweepConfig,
                 has_magi: Callable[[str, int], bool]) -> str:
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
    return format_digest(assessor.dedupe(items))


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, no Discord")
    ap.add_argument("--discord", action="store_true", help="post digest to Discord")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except RuntimeError as e:
        print(f"worksweep: {e}", file=sys.stderr)
        return 1
    collect_fns = {
        "my_mrs": collectors.collect_my_mrs,
        "review_requests": collectors.collect_review_requests,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
    }
    digest = build_digest(
        collect_fns, cfg,
        has_magi=lambda repo, iid: assessor.has_magi_report(repo, iid))

    if args.discord and not args.dry_run:
        if not cfg.discord_webhook:
            print("worksweep: no discord_webhook configured", file=sys.stderr)
            return 1
        try:
            _post_discord(cfg.discord_webhook, digest)
        except Exception as e:
            print(f"worksweep: {e}", file=sys.stderr)
            return 1
    else:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
