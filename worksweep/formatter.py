"""Render WorkItems into a numbered Discord digest. Matches the
shiplog/heartbeat-lib 1900-byte Discord cap."""
from __future__ import annotations

from typing import List

from .models import WorkItem

DISCORD_MAX_CHARS = 1900


def _truncate_bytes(s: str, max_bytes: int = DISCORD_MAX_CHARS) -> str:
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    cut = enc[:max_bytes - 3]
    while cut and (cut[-1] & 0xC0) == 0x80:  # rewind partial multibyte tail
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore") + "..."


def format_digest(items: List[WorkItem]) -> str:
    if not items:
        return "✅ Worksweep: nothing needs you right now."
    lines = [f"🔭 **Worksweep** — {len(items)} item(s) need you:"]
    for n, it in enumerate(items, 1):
        lines.append(f"{n}. `{it.executor}` {it.repo} — {it.why}\n   {it.web_url}")
    lines.append("\nReply e.g. `✅ 1,3` to approve (executors land in M2+).")
    return _truncate_bytes("\n".join(lines))
