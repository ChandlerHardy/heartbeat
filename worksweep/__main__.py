"""Worksweep entry point: collect -> assess -> dedupe -> format -> output.

M1: read-only digest. `--dry-run` prints to stdout; `--discord` posts.
M2: `--discord`/`--dry-run` reconcile the sweep into a persistent queue and
render the digest from the queue's stable numbers; the `intake` subcommand
polls Discord for Chandler's approval replies and flips matched items to
`approved` (no executors — approval is status-only).
M3: the sweep runs one GraphQL query (mirroring the GitLab dashboard) instead
of the old per-repo REST calls; `run_sweep(cfg, deps)` is the dependency-
injected core so tests never shell out, and `main()` just wires real deps
(`glab`, Discord, the queue file) around it. Message contract: every sweep
posts exactly one digest (or 🔍 heartbeat, or ⚠️ error) — never silence.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

# Only ever POST the digest to a Discord-owned host. The webhook comes from
# ~/etc/heartbeat.json (semi-trusted); this allowlist + no-redirect opener stop
# a tampered/typo'd webhook from exfiltrating the digest elsewhere.
_ALLOWED_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")

_SSH_TIMEOUT_SECONDS = 20
# M4 Task G: syncing a branch onto a dev box does a real `git fetch` over the
# network on the far side — the 20s read-only probe budget is far too short.
_SSH_SYNC_TIMEOUT_SECONDS = 300
_HTTP_PROBE_TIMEOUT_SECONDS = 20

from . import assessor, collectors, curator, devslots, implementer
from .approvals import apply_approvals
from .config import WorksweepConfig, load_config
from .discord_read import fetch_messages
from .formatter import (
    DISCORD_MAX_CHARS, _FOOTER, _HEADER, _truncate_bytes,
    format_messages_from_records,
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


def run_ssh(host: str, command: str, timeout: int = _SSH_TIMEOUT_SECONDS) -> str:
    """M4 Task F ssh edge (read-only): run `command` on `host` via the `ssh`
    binary and return stdout. Raises RuntimeError on timeout, a missing ssh
    binary, or a non-zero exit -- mirrors collectors._run_glab. Callers
    (devslots.probe) catch this per-box and degrade to an unknown branch/sha
    rather than losing the whole sweep to one unreachable box."""
    try:
        # stdin=DEVNULL (the `ssh -n` equivalent): without it ssh hands the
        # parent's stdin to the remote command, which under launchd is the
        # same non-TTY hazard fixed for `claude -p` in c0e7791.
        result = subprocess.run(["ssh", host, command],
                                stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ssh {host} timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("ssh not found on PATH")
    if result.returncode != 0:
        raise RuntimeError(f"ssh {host} failed: {(result.stderr or '').strip()}")
    return result.stdout


def http_status(url: str, timeout: int = _HTTP_PROBE_TIMEOUT_SECONDS) -> int:
    """M4 Task G http edge: GET `url` and return its status code. Used only to
    prove a dev box still serves 200 after a sync, so an HTTP error response
    is a RESULT (its code), not an exception; anything that isn't an HTTP
    response at all raises and implementer.sync_to_box turns it into a
    RunnerError."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "WorksweepBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _implement_boxes(cfg: WorksweepConfig) -> list:
    """Probe + classify the dev boxes for one implement claim.

    Deliberately re-runs the GraphQL sweep instead of trusting the queue: the
    tier of a box depends on the CURRENT state of the MR sitting on its branch
    (merged? approved and handed off? under live review?), and the runner
    fires on its own cadence, minutes to hours after the digest that proposed
    the item. Raises on failure — the runner's implement pass catches it and
    turns it into an error status + ⚠️ rather than guessing a box is free.
    """
    if not cfg.dev_boxes:
        return []
    boxes = devslots.probe(list(cfg.dev_boxes), run_ssh)
    review_mrs, authored, assigned = collectors.parse_graphql_sweep(
        collectors.run_graphql_sweep(), cfg.username, cfg.repos)
    records = load_queue(_queue_path())
    claimed = frozenset(r.item.dev_box for r in records
                        if r.item.dev_box and r.item.executor == "implement"
                        and r.item.status in ("running", "approved"))
    return implementer.annotate_boxes(boxes, review_mrs + authored + assigned,
                                      cfg.username, claimed=claimed)


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


def run_sweep(cfg: WorksweepConfig, deps: Dict[str, Callable]) -> int:
    """One sweep under the message contract: digest, 🔍 heartbeat, or ⚠️ error —
    never silence. All I/O arrives via `deps` so tests stay hermetic."""
    post = deps["post"]

    def _post_all(messages) -> None:
        if cfg.discord_webhook:
            for m in messages:
                post(cfg.discord_webhook, m)
        else:
            for m in messages:
                print(m)

    try:
        raw = deps["graphql"]()
        review_mrs, authored, assigned = collectors.parse_graphql_sweep(
            raw, cfg.username, cfg.repos)

        items = []
        for mr in review_mrs:
            items += assessor.assess_review_request(mr, cfg.username)
        records0 = deps["load"]()
        records0 = assessor.bootstrap_magi_records(
            records0, authored, deps["now"]())
        for mr in authored:
            items += assessor.assess_own_mr(
                mr, cfg.username,
                has_magi=lambda r, i, s: assessor.has_magi_done(records0, r, i, s))
        # An assigned-to-me MR not already surfaced as a review-requested or
        # authored item this sweep gets a lightweight triage item.
        tracked_mr_ids = {(mr.repo, mr.iid) for mr in review_mrs + authored}
        for mr in assigned:
            items += assessor.assess_assigned_mr(mr, cfg.username, tracked_mr_ids)
        covered_issues = assessor.covered_issue_iids(authored)
        for repo in cfg.repos:
            try:
                for iss in deps["issues"](repo, cfg.username):
                    items += assessor.assess_issue(iss, covered=covered_issues)
            except Exception as e:
                print(f"worksweep: issues for {repo} failed: {e}", file=sys.stderr)
        items = assessor.dedupe(items)

        # GitLab todos are noisier than the GraphQL sweep's authoritative
        # buckets (review/authored/assigned) -- drop a todo whose action is
        # already covered by a bucket, or whose (fragment/slash-normalized)
        # URL matches an item or MR already surfaced this sweep, so the
        # digest doesn't show the same thing twice under two executors.
        try:
            todos_raw = deps["todos"]()
        except Exception as e:
            print(f"worksweep: todos collection failed: {e}", file=sys.stderr)
            todos_raw = []
        surviving_todos = assessor.filter_todos(
            todos_raw, items, review_mrs + authored + assigned)
        for td in surviving_todos:
            items += assessor.assess_todo(td)
        items = assessor.dedupe(items)

        resolved = assessor.resolutions(review_mrs, cfg.username, authored)
        records = reconcile(records0, items, deps["now"](), resolved=resolved)
        try:
            deps["save"](records)
        except OSError as e:
            print(f"worksweep: could not persist queue: {e}", file=sys.stderr)

        # done/error records are terminal — they've already been reported (or
        # failed and are excluded from re-proposal) so they'd only clutter the
        # digest. The actionable check must use the same filtered set the
        # formatter renders, or "nothing needs you" and an empty digest could
        # disagree.
        actionable = [r for r in records if r.item.status not in ("done", "error")]

        # M4 Task F: dev-slot sensing. Entirely opt-in (cfg.dev_boxes empty
        # -> deps["ssh"] is never touched, matching every pre-M4 caller/test)
        # and never fatal to the sweep — a probe failure just means no
        # preamble line this round, not a failed digest.
        slot_line: Optional[str] = None
        if cfg.dev_boxes:
            ssh_edge = deps.get("ssh")
            if ssh_edge is None:
                print("worksweep: runner.dev_boxes configured but no ssh dep "
                      "provided — skipping dev-slot sensing", file=sys.stderr)
            else:
                try:
                    boxes = devslots.probe(list(cfg.dev_boxes), ssh_edge)
                    claimed = frozenset(
                        r.item.dev_box for r in records
                        if r.item.dev_box and r.item.executor == "implement"
                        and r.item.status in ("running", "approved"))
                    tiers = devslots.classify(
                        boxes, review_mrs + authored + assigned, cfg.username,
                        claimed=claimed)
                    slot_line = devslots.summary_line(tiers)
                except Exception as e:
                    print(f"worksweep: dev-slot sensing failed: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    slot_line = None

        if actionable:
            curated = None
            run_llm = deps.get("llm")
            if cfg.curate and run_llm is not None:
                curated = curator.curate(actionable, deps["now"](), run_llm,
                                         preamble=slot_line)
            if curated is not None:
                n, m = curator.partition_counts(actionable)
                head = (f"{_HEADER} (curated) — {n} actionable / {m} held:\n"
                        + (f"{slot_line}\n" if slot_line else ""))
                tail = f"\n{_FOOTER}"
                # Fixed parts (header, slot line, footer) must always survive:
                # give the LLM body whatever budget remains and truncate ONLY
                # the body. Truncating the assembled string from the end would
                # silently eat the ✅-instructions footer once a multi-box slot
                # line is present (Task F review finding, 2026-08-18).
                fixed = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
                body_budget = max(200, DISCORD_MAX_CHARS - fixed)
                _post_all([head + _truncate_bytes(curated, body_budget) + tail])
            else:
                _post_all(format_messages_from_records(
                    actionable, now=deps["now"](), preamble=slot_line))
        else:
            _post_all([f"🔍 Worksweep: nothing needs you "
                       f"(checked {len(review_mrs)} review requests, "
                       f"{len(authored)} authored MRs)"])
        return 0
    except Exception as e:
        msg = f"⚠️ Worksweep sweep failed: {type(e).__name__}: {e}"
        try:
            if cfg.discord_webhook:
                post(cfg.discord_webhook, msg)
        except Exception as post_err:
            print(f"worksweep: error post also failed: {post_err}", file=sys.stderr)
        print(msg, file=sys.stderr)
        return 1


def _execute_implement(item, cfg, boxes):
    """Real implement edge: subprocess + a longer-budget ssh + an http probe."""
    return implementer.execute(
        item, cfg, boxes, run_subprocess=subprocess.run,
        run_ssh=lambda host, command: run_ssh(
            host, command, timeout=_SSH_SYNC_TIMEOUT_SECONDS),
        http_get=http_status)


def _dry_run_implement(item, cfg, boxes):
    box = boxes[0] if boxes else None
    iid = implementer.issue_iid(item)
    return implementer.ImplementResult(
        iid=iid, mr_iid=0, mr_url="", dev_url=getattr(box, "url", ""),
        dev_box=getattr(box, "name", ""),
        branch=implementer.branch_name(iid, item.title or ""),
        report_path="(dry-run)", verdict="", result_sha=item.sha,
        magi_note="dry-run: nothing was pushed, opened or synced")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="worksweep")
    ap.add_argument("command", nargs="?", choices=["intake", "run"],
                    help="`intake` polls Discord for approval replies; "
                         "`run` executes one approved magi-review item")
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

    if args.command == "run":
        from . import runner as _runner
        deps = {
            "load": lambda: load_queue(_queue_path()),
            "save": lambda records: save_queue(_queue_path(), records),
            "post": _post_discord,
            "now": _now,
            "execute": (lambda item, c: (item.sha, "(dry-run)"))
                       if args.dry_run else _runner.execute,
            # M4 Task G. Box probing stays real under --dry-run (read-only ssh,
            # nothing to preview around); the executor itself does not, since
            # it pushes, opens an MR and rewrites a dev box.
            "boxes": lambda: _implement_boxes(cfg),
            "execute_implement": (_dry_run_implement if args.dry_run
                                  else _execute_implement),
        }
        return _runner.run_once(cfg, deps)

    if args.discord and not args.dry_run and not cfg.discord_webhook:
        print("worksweep: no discord_webhook configured", file=sys.stderr)
        return 1

    deps = {
        "graphql": collectors.run_graphql_sweep,
        "todos": collectors.collect_todos,
        "issues": collectors.collect_issues,
        "post": _post_discord,
        "load": lambda: load_queue(_queue_path()),
        "save": lambda records: save_queue(_queue_path(), records),
        "now": _now,
    }
    if args.dry_run or not args.discord:
        deps["post"] = lambda hook, content: print(content)
    # --dry-run must never shell out to the curator LLM (it's meant to be a
    # side-effect-free preview); every other invocation gets the real edge.
    if not args.dry_run:
        deps["llm"] = curator.make_run_llm(cfg)
    # M4 Task F: dev-slot ssh probing is read-only (git branch/rev-parse on
    # the box), so --dry-run still runs it for real -- unlike the LLM/post
    # edges above, there's no side effect to preview around. run_sweep only
    # ever calls this when cfg.dev_boxes is non-empty.
    deps["ssh"] = run_ssh
    return run_sweep(cfg, deps)


if __name__ == "__main__":
    raise SystemExit(main())
