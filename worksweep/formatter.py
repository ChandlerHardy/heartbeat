"""Render WorkItems into Discord digest message(s).

`format_messages` splits the digest into a list of messages each within the
1900-byte Discord cap (matches heartbeat-lib's send_discord) — so a long sweep
is delivered in full across several messages instead of being truncated.
`format_digest` returns the whole digest as one string for stdout/dry-run.
"""
from __future__ import annotations

from typing import List

from .models import WorkItem

DISCORD_MAX_CHARS = 1900

_HEADER = "🔭 **Worksweep**"
_FOOTER = "Reply e.g. `✅ 1,3` to approve (executors land in M2+)."
_ALL_CLEAR = "✅ Worksweep: nothing needs you right now."


def _truncate_bytes(s: str, max_bytes: int = DISCORD_MAX_CHARS) -> str:
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    cut = enc[:max_bytes - 3]
    while cut and (cut[-1] & 0xC0) == 0x80:  # rewind partial multibyte tail
        cut = cut[:-1]
    return cut.decode("utf-8", "ignore") + "..."


def _item_line(n: int, it: WorkItem) -> str:
    # One compact line per item. The web_url is a masked link ([#ref](url)) so
    # Discord renders no embed card (25 raw URLs would spawn 25 previews) and the
    # ref number stays visible. The leading `n.` is the approval handle for M2.
    parts = [f"{n}.", f"`{it.executor}`"]
    if it.repo:
        parts.append(it.repo)
    if it.web_url:
        ref = it.web_url.rstrip("/").rsplit("/", 1)[-1]
        parts.append(f"[#{ref}]({it.web_url})")
    return f"{' '.join(parts)} — {it.why}"


def format_messages(items: List[WorkItem],
                    max_bytes: int = DISCORD_MAX_CHARS) -> List[str]:
    """Split the digest into messages each <= max_bytes (byte-safe)."""
    if not items:
        return [_ALL_CLEAR]
    head = f"{_HEADER} — {len(items)} item(s) need you:"
    cont = f"{_HEADER} *(cont.)*"
    msgs: List[str] = []
    cur = head
    for n, it in enumerate(items, 1):
        # Reserve headroom so even an oversized single line fits under a
        # continuation header without breaching the cap.
        line = _truncate_bytes(_item_line(n, it), max_bytes - 80)
        candidate = f"{cur}\n{line}"
        if len(candidate.encode("utf-8")) > max_bytes:
            msgs.append(cur)
            cur = f"{cont}\n{line}"
        else:
            cur = candidate
    with_footer = f"{cur}\n\n{_FOOTER}"
    if len(with_footer.encode("utf-8")) <= max_bytes:
        msgs.append(with_footer)
    else:
        msgs.append(cur)
        msgs.append(_FOOTER)
    return msgs


def format_digest(items: List[WorkItem]) -> str:
    """The whole digest as a single string (uncapped) — for stdout/dry-run."""
    if not items:
        return _ALL_CLEAR
    lines = [f"{_HEADER} — {len(items)} item(s) need you:"]
    lines += [_item_line(n, it) for n, it in enumerate(items, 1)]
    lines.append(f"\n{_FOOTER}")
    return "\n".join(lines)
