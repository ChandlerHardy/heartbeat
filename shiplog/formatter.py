"""Format ShipLog output as markdown and Discord messages."""

from __future__ import annotations

from typing import List

from .classifier import group_by_category
from .models import RepoSnapshot, ShipLogReport


CATEGORY_EMOJI = {
    "feat": "✨",
    "fix": "🐛",
    "refactor": "♻️",
    "perf": "⚡",
    "docs": "📝",
    "test": "✅",
    "chore": "🔧",
    "style": "💅",
    "revert": "⏪",
    "other": "•",
}

CATEGORY_LABEL = {
    "feat": "Features",
    "fix": "Fixes",
    "refactor": "Refactors",
    "perf": "Performance",
    "docs": "Docs",
    "test": "Tests",
    "chore": "Chores",
    "style": "Style",
    "revert": "Reverts",
    "other": "Other",
}

# Discord's hard message limit is 2000 chars. We truncate well below that
# so the formatted digest and the final `…` sentinel both fit under the
# cap with headroom for markdown mention expansion. Shared by
# shiplog/__main__.py::_send_discord and format_discord() below.
DISCORD_MAX_CHARS = 1900


def _fmt_date(dt):
    return dt.strftime("%Y-%m-%d")


def _snapshot_section(snapshot: RepoSnapshot, ascii_only: bool = False) -> List[str]:
    lines: List[str] = []
    header = f"### {snapshot.name}"
    if snapshot.merged_count == 0 and snapshot.commit_count == 0:
        return []
    lines.append(header)
    lines.append("")
    meta_bits: List[str] = []
    if snapshot.merged_count:
        meta_bits.append(f"{snapshot.merged_count} merged")
    if snapshot.commit_count:
        meta_bits.append(f"{snapshot.commit_count} commits")
    if snapshot.open_pr_count:
        meta_bits.append(f"{snapshot.open_pr_count} open PRs")
    if snapshot.open_issue_count:
        meta_bits.append(f"{snapshot.open_issue_count} open issues")
    if snapshot.releases:
        meta_bits.append(f"releases: {', '.join(snapshot.releases)}")
    if meta_bits:
        lines.append("_" + " · ".join(meta_bits) + "_")
        lines.append("")

    groups = group_by_category(list(snapshot.merged_prs))
    for category, prs in groups.items():
        emoji = "" if ascii_only else (CATEGORY_EMOJI.get(category, "") + " ")
        lines.append(f"**{emoji}{CATEGORY_LABEL.get(category, category.title())}**")
        for pr in prs:
            lines.append(f"- [#{pr.number}]({pr.url}) {pr.short_title}")
        lines.append("")
    return lines


def format_markdown(report: ShipLogReport, ascii_only: bool = False) -> str:
    """Produce a full markdown digest suitable for the weekly report archive."""
    lines: List[str] = []
    lines.append(
        f"# ShipLog — {_fmt_date(report.window_start)} → {_fmt_date(report.window_end)}"
    )
    lines.append("")
    lines.append(
        f"**Totals:** {report.total_merged} merged PRs, {report.total_commits} commits "
        f"across {len(report.active_repos)} active repo(s)."
    )
    lines.append("")

    if not report.active_repos:
        lines.append("_No activity in this window._")
        lines.append("")
    else:
        for snapshot in report.active_repos:
            lines.extend(_snapshot_section(snapshot, ascii_only=ascii_only))

    if report.errors:
        lines.append("## Collection errors")
        lines.append("")
        for err in report.errors:
            lines.append(f"- {err}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_discord(report: ShipLogReport, max_chars: int = DISCORD_MAX_CHARS) -> str:
    """Produce a compact Discord-friendly digest (under max_chars)."""
    lines: List[str] = []
    lines.append(
        f"📊 **ShipLog** ({_fmt_date(report.window_start)} → {_fmt_date(report.window_end)})"
    )
    lines.append(
        f"**Totals:** {report.total_merged} merged · {report.total_commits} commits · "
        f"{len(report.active_repos)} active repos"
    )
    lines.append("")

    for snapshot in report.active_repos:
        groups = group_by_category(list(snapshot.merged_prs))
        counts = " · ".join(
            f"{CATEGORY_EMOJI.get(c, '•')} {len(prs)}"
            for c, prs in groups.items()
        ) or "(commits only)"
        lines.append(f"**{snapshot.name}** — {counts}")
        for pr in snapshot.merged_prs[:3]:
            lines.append(f"  └ #{pr.number} {pr.short_title}")
        if snapshot.merged_count > 3:
            lines.append(f"  └ +{snapshot.merged_count - 3} more")

    out = "\n".join(lines)
    return truncate_to_bytes(out, max_chars)


def truncate_to_bytes(s: str, max_bytes: int) -> str:
    """Truncate *s* so its UTF-8 encoding is ≤ *max_bytes* bytes.

    Python ``str`` length counts Unicode code points, not UTF-8 bytes. A
    Discord digest heavy with emoji (3 bytes per codepoint) or CJK text
    (3–4 bytes) can pass a code-point length check yet exceed Discord's
    2000-byte wire limit and be rejected with HTTP 400. Encode first,
    truncate at the byte level, rewind any partial multibyte tail so the
    result decodes cleanly on a codepoint boundary, then append a
    one-codepoint ellipsis.
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    # Reserve 3 bytes for the "…" ellipsis (0xE2 0x80 0xA6 in UTF-8).
    cut = encoded[: max_bytes - 3]
    # Strip any trailing continuation bytes (10xxxxxx)...
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    # ...then strip an unaccompanied multibyte leader if the cut landed
    # mid-codepoint. Any byte ≥ 0x80 after the continuation sweep must be
    # a leader with its continuations already chopped off.
    if cut and cut[-1] >= 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8") + "…"


# Backwards-compat alias for any external caller that imported the
# underscore-prefixed name while the contract was still considered private.
_truncate_to_bytes = truncate_to_bytes
