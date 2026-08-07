"""I3: end-to-end loop-closure integration test.

Walks the real modules (no mocked internals, only the shell/network edges
that `deps` inject) through a full cycle: sweep proposes -> intake approves
-> runner executes and completes -> a same-sha resweep leaves it done and
excluded from the digest -> a new-sha resweep re-proposes it. A sibling test
covers the companion reconcile guarantee: claimed_at survives a same-sha
reconcile of a still-running record.
"""
from __future__ import annotations

import json
import os

from worksweep import runner
from worksweep.__main__ import run_sweep
from worksweep.approvals import apply_approvals
from worksweep.config import WorksweepConfig
from worksweep.models import DiscordMessage, QueueRecord, WorkItem
from worksweep.queue import load_queue, reconcile, save_queue

NOW = "2026-08-07T12:00:00+00:00"
IID = 4020
REPO = "pb-www"
APPROVER = "chandler-123"


def _gql(sha: str, state: str = "UNREVIEWED") -> str:
    """Fabricated GraphQL sweep payload: one review-requested MR for `pb-www`,
    matching the real shape collectors.parse_graphql_sweep expects."""
    return json.dumps({"data": {"currentUser": {
        "username": "me",
        "reviewRequestedMergeRequests": {"nodes": [{
            "iid": str(IID), "title": "t", "draft": False,
            "webUrl": f"https://gl/x/-/merge_requests/{IID}",
            "diffHeadSha": sha, "updatedAt": "2026-08-07T00:00:00Z",
            "project": {"fullPath": f"performancelivestock/{REPO}"},
            "author": {"username": "other"},
            "reviewers": {"nodes": [
                {"username": "me",
                 "mergeRequestInteraction": {"reviewState": state}}]},
            "headPipeline": None,
        }]},
        "authoredMergeRequests": {"nodes": []},
    }}})


def _cfg(tmp_path) -> WorksweepConfig:
    return WorksweepConfig(
        repos=(REPO,), username="me",
        discord_webhook="https://discord.com/api/webhooks/x/y",
        checkouts_root=str(tmp_path))


def test_sweep_approve_run_reconcile_loop_closes(tmp_path):
    qpath = str(tmp_path / "queue.json")
    cfg = _cfg(tmp_path)
    posts: list = []

    def sweep_deps(raw: str) -> dict:
        return {
            "graphql": lambda: raw,
            "todos": lambda: [],
            "issues": lambda repo, user: [],
            "post": lambda hook, content: posts.append(content),
            "load": lambda: load_queue(qpath),
            "save": lambda records: save_queue(qpath, records),
            "now": lambda: NOW,
        }

    item_id = f"review:{REPO}!{IID}"
    sha1 = "sha-original"

    # 1. Sweep proposes the review item (real run_sweep, real GraphQL parsing,
    # real reconcile, real queue persistence).
    assert run_sweep(cfg, sweep_deps(_gql(sha1))) == 0
    records = load_queue(qpath)
    rec = next(r for r in records if r.item.id == item_id)
    assert rec.item.status == "proposed"
    number = rec.number

    # 2. apply_approvals (real) flips it to approved, as intake would.
    msg = DiscordMessage(id="1", author_id=APPROVER, content=f"✅ {number}",
                         timestamp=NOW)
    updated, approved = apply_approvals(records, [msg], APPROVER, NOW)
    assert number in approved
    save_queue(qpath, updated)
    assert next(r for r in updated if r.number == number).item.status == "approved"

    # 3. runner.run_once (real state machine + lockfile) completes it via a
    # stubbed execute() — no real subprocess/git/claude involved.
    runner_deps = {
        "load": lambda: load_queue(qpath),
        "save": lambda records: save_queue(qpath, records),
        "post": lambda hook, content: posts.append(content),
        "now": lambda: NOW,
        "execute": lambda item, c: (sha1, "/tmp/tribunal-report.md"),
    }
    assert runner.run_once(cfg, runner_deps,
                           lock_path=str(tmp_path / "runner.lock")) == 0
    records = load_queue(qpath)
    rec = next(r for r in records if r.number == number)
    assert rec.item.status == "done"
    assert rec.item.result_sha == sha1

    # 4. Re-sweep with the SAME sha: the record must stay done, and the
    # digest/heartbeat output must exclude it (only "done"/"error" are
    # filtered from what the formatter renders).
    posts.clear()
    assert run_sweep(cfg, sweep_deps(_gql(sha1))) == 0
    records = load_queue(qpath)
    rec = next(r for r in records if r.number == number)
    assert rec.item.status == "done"
    texts = [p for p in posts if isinstance(p, str)]
    assert len(texts) == 1
    assert texts[0].startswith("🔍")             # heartbeat: nothing needs you
    assert str(IID) not in texts[0]              # the done MR isn't mentioned

    # 5. Re-sweep with a NEW sha: the MR moved since the last review, so the
    # item must re-propose (queue.reconcile's done+new-sha "resurrect" rule).
    sha2 = "sha-new-push"
    assert run_sweep(cfg, sweep_deps(_gql(sha2))) == 0
    records = load_queue(qpath)
    rec = next(r for r in records if r.number == number)
    assert rec.item.status == "proposed"
    assert rec.item.sha == sha2


def test_claimed_at_survives_same_sha_reconcile_of_running_record():
    """Companion guarantee to the loop-closure test: while an item is
    `running` (runner mid-execute), a same-sha resweep must not lose the
    claim timestamp reap_stale needs to decide staleness."""
    claimed_at = "2026-08-07T11:50:00+00:00"
    now = "2026-08-07T12:00:00+00:00"
    item_id = f"review:{REPO}!{IID}"
    running_item = WorkItem(
        schema_version=1, id=item_id, repo=REPO, kind="review_request",
        executor="magi-review", risk="low", why="review requested",
        web_url=f"https://gl/x/-/merge_requests/{IID}", sha="sha-original",
        status="running", claimed_at=claimed_at)
    existing = [QueueRecord(number=7, item=running_item,
                            first_seen="2026-08-07T10:00:00+00:00",
                            last_seen=claimed_at)]

    fresh_item = WorkItem(
        schema_version=1, id=item_id, repo=REPO, kind="review_request",
        executor="magi-review", risk="low", why="review requested",
        web_url=f"https://gl/x/-/merge_requests/{IID}", sha="sha-original")

    out = reconcile(existing, [fresh_item], now)
    rec = next(r for r in out if r.number == 7)
    assert rec.item.status == "running"
    assert rec.item.claimed_at == claimed_at
