"""Worksweep entry point: collect -> assess -> dedupe -> format -> output.

Read-only M1. `--dry-run` prints to stdout; `--discord` posts the digest.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Callable, Dict

from . import assessor, collectors
from .config import WorksweepConfig, load_config
from .formatter import format_digest


def build_digest(collect_fns: Dict[str, Callable], cfg: WorksweepConfig,
                 has_magi: Callable[[str, str], bool]) -> str:
    items = []
    for repo in cfg.repos:
        for mr in collect_fns["my_mrs"](repo, cfg.username):
            items += assessor.assess_mr(mr, cfg.username, has_magi)
        for mr in collect_fns["review_requests"](repo, cfg.username):
            items += assessor.assess_mr(mr, cfg.username, has_magi)
        for iss in collect_fns["issues"](repo, cfg.username):
            items += assessor.assess_issue(iss)
    for td in collect_fns["todos"]():
        items += assessor.assess_todo(td)
    return format_digest(assessor.dedupe(items))


def _post_discord(webhook: str, content: str) -> None:
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "WorksweepBot/1.0"})
    urllib.request.urlopen(req, timeout=15)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, no Discord")
    ap.add_argument("--discord", action="store_true", help="post digest to Discord")
    args = ap.parse_args(argv)

    cfg = load_config()
    collect_fns = {
        "my_mrs": collectors.collect_my_mrs,
        "review_requests": collectors.collect_review_requests,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
    }
    digest = build_digest(
        collect_fns, cfg,
        has_magi=lambda r, s: assessor.has_magi_report(r, s))

    if args.discord and not args.dry_run:
        if not cfg.discord_webhook:
            print("worksweep: no discord_webhook configured", file=sys.stderr)
            return 1
        _post_discord(cfg.discord_webhook, digest)
    else:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
