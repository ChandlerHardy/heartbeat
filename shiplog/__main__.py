"""ShipLog CLI entrypoint.

Usage:
    python3 -m shiplog --days 7 --archive --discord
    python3 -m shiplog --config ~/etc/heartbeat.json --days 7
    python3 -m shiplog --project crooked-finger  # single repo
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collectors import collect_snapshot
from .formatter import DISCORD_MAX_CHARS, format_discord, format_markdown
from .models import ShipLogReport


DEFAULT_CONFIG = os.path.expanduser("~/etc/heartbeat.json")
DEFAULT_ARCHIVE = os.path.expanduser("~/heartbeat-reports")


def _get_github_repo(repo_path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        url = result.stdout.strip()
        # git@github.com:owner/repo.git or https://github.com/owner/repo.git
        for marker in ("github.com:", "github.com/"):
            if marker in url:
                tail = url.split(marker, 1)[1]
                if tail.endswith(".git"):
                    tail = tail[:-4]
                return tail
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return ""


def _load_config(path: str) -> dict:
    if not os.path.exists(path):
        return {"projects": [], "discord_webhook": ""}
    try:
        with open(path) as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        # Corrupt config file (hand edit, interrupted write, etc.) — fall
        # back to the empty skeleton so ShipLog still emits a report and
        # surface the parse error via stderr so the operator sees it.
        print(
            f"shiplog: config at {path} is not valid JSON ({exc}); "
            "falling back to empty project list",
            file=sys.stderr,
        )
        return {"projects": [], "discord_webhook": ""}
    except OSError as exc:
        print(
            f"shiplog: unable to read config at {path} ({exc}); "
            "falling back to empty project list",
            file=sys.stderr,
        )
        return {"projects": [], "discord_webhook": ""}


def _atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` without a partial-write window.

    A naked `Path.write_text()` truncates the file on open, so a crash mid-
    write (SIGKILL, power loss, full disk) leaves the archive at zero bytes
    and that partial state becomes permanent. Stage to a temp sibling and
    rename — `os.replace` is atomic on the same filesystem.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _send_discord(webhook: str, content: str) -> None:
    if not webhook or not content.strip():
        return
    # formatter.truncate_to_bytes owns the byte-level truncation contract;
    # delegate here too instead of re-slicing by codepoint, which would bypass
    # the multibyte-safety the formatter already applies.
    from .formatter import truncate_to_bytes
    data = json.dumps({"content": truncate_to_bytes(content, DISCORD_MAX_CHARS)}).encode()
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ShipLog/1.0",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"shiplog: discord post failed: {e}", file=sys.stderr)


def build_report(
    config: dict,
    days: int,
    only_project: str | None = None,
) -> ShipLogReport:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    report = ShipLogReport(window_start=since, window_end=now)

    for project in config.get("projects", []):
        name = project.get("name", "")
        repo_path = project.get("path", "")
        if only_project and name != only_project:
            continue
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            report.errors.append(f"{name}: not a git repo at {repo_path}")
            continue
        repo = _get_github_repo(repo_path)
        if not repo:
            report.errors.append(f"{name}: no GitHub remote")
            continue
        snapshot = collect_snapshot(name=name, repo=repo, repo_path=repo_path, since=since)
        report.snapshots.append(snapshot)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shiplog",
        description="Weekly retrospective digest for personal repos.",
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default 7)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"Path to heartbeat.json (default: {DEFAULT_CONFIG})")
    parser.add_argument("--archive", action="store_true", help=f"Write markdown to {DEFAULT_ARCHIVE}/shiplog-YYYY-MM-DD.md")
    parser.add_argument("--archive-dir", default=DEFAULT_ARCHIVE, help="Override archive directory")
    parser.add_argument("--discord", action="store_true", help="Post Discord digest (uses webhook from config)")
    parser.add_argument("--project", default=None, help="Limit to one project by name (for testing)")
    parser.add_argument("--ascii", action="store_true", help="ASCII-only markdown output (no emoji)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON to stdout instead of markdown")
    args = parser.parse_args(argv)

    config = _load_config(args.config)
    report = build_report(config, args.days, only_project=args.project)

    if args.json:
        payload = {
            # Bump when any field is renamed, removed, or its type changes so
            # downstream consumers (cron, n8n, dashboards) can detect drift.
            "schema_version": 1,
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "total_merged": report.total_merged,
            "total_commits": report.total_commits,
            "snapshots": [
                {
                    "name": s.name,
                    "repo": s.repo,
                    "merged": [
                        {"number": p.number, "title": p.title, "url": p.url, "author": p.author}
                        for p in s.merged_prs
                    ],
                    "commits": s.commit_count,
                    "open_prs": s.open_pr_count,
                    "open_issues": s.open_issue_count,
                    "releases": list(s.releases),
                }
                for s in report.snapshots
            ],
            "errors": report.errors,
        }
        print(json.dumps(payload, indent=2))
    else:
        md = format_markdown(report, ascii_only=args.ascii)
        print(md)
        if args.archive:
            Path(args.archive_dir).mkdir(parents=True, exist_ok=True)
            date_str = report.window_end.strftime("%Y-%m-%d")
            archive_path = Path(args.archive_dir) / f"shiplog-{date_str}.md"
            # Don't clobber a same-day re-run. Disambiguate with HHMMSS so
            # two runs in the same minute don't land on the same path and
            # silently overwrite each other (the earlier stamp was %H%M and
            # gave minute-granularity only).
            if archive_path.exists():
                stamp = report.window_end.strftime("%H%M%S")
                archive_path = Path(args.archive_dir) / f"shiplog-{date_str}-{stamp}.md"
            _atomic_write_text(archive_path, md)
            print(f"shiplog: archived to {archive_path}", file=sys.stderr)

    if args.discord:
        webhook = config.get("discord_webhook", "")
        discord_msg = format_discord(report)
        _send_discord(webhook, discord_msg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
