"""Monday standup notes from a week of GitLab activity (migrated to the mini
2026-09-08 from the OCI cron `~/bin/standup-notes.sh`).

Shape mirrors the curator: deterministic FACTS from `glab api`, one `claude -p`
pass for the prose, a validation gate the model cannot talk its way past, and
a deterministic fallback so the notes always go out. What the OCI version got
wrong and this one pins:

* **Classify before narrating.** Most pushes in a week are keep-current
  freshening merges on parked drafts. The OCI script fed raw MR rows to the
  model and the ranch-data stack was reported as "three rounds of updates"
  (2026-08-17). Here every push is classified first (`classify_push`), a
  branch with only freshening pushes is PARKED, and the model is told so.
* **Never silent.** The OCI cron's glab token expired and the only trace was
  a line in /tmp (2026-09-07). A collection failure here is a 🔴 to Discord;
  an LLM failure posts the deterministic fallback with a note.
* **No invented refs.** Every `!NNNN` / `#NNNN` in the output must exist in
  the facts, and every MR merged in the window must be mentioned.
"""
import dataclasses
import json
import re
import sys
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from . import curator
from .collectors import PROJECT_PREFIX

DISCORD_LIMIT = 1900
_TITLE = "**Monday Standup Notes**"

_FRESHEN_RE = re.compile(
    r"^Merge (remote-tracking )?branch '(origin/)?(master|main)'", re.I)
_STACK_RE = re.compile(r"^Merge remote-tracking branch 'origin/", re.I)
_URL_RE = re.compile(r"https?://|www\.", re.I)
_REF_RE = re.compile(r"(?<![\w/])([!#])(\d{2,6})\b")
_ISSUE_IN_TEXT_RE = re.compile(r"#(\d{2,6})\b|/(\d{3,6})-")


@dataclasses.dataclass
class Facts:
    after: str
    mrs: List[dict]                              # authored MRs updated in window, + "repo"
    work_pushes: List[Tuple[str, str, str]]      # (date, ref, commit_title) -- real commits
    freshening_branches: Dict[str, int]          # ref -> count of freshening/stack pushes
    reviews: List[Tuple[str, str, str]]          # (date, action, target_title)


# --- classification --------------------------------------------------------

def classify_push(commit_title: Optional[str]) -> str:
    """'freshening' (master merged in), 'stack-rebase' (another feature
    branch merged in), or 'work'. A missing title is work: the events API's
    `commit_title` is the FIRST commit of the push and can be absent; hiding
    a push is the failure this exists to prevent."""
    title = (commit_title or "").strip()
    if not title:
        return "work"
    if _FRESHEN_RE.match(title):
        return "freshening"
    if _STACK_RE.match(title):
        return "stack-rebase"
    return "work"


# --- collection ------------------------------------------------------------

def collect(run_glab: Callable[[List[str]], str], username: str,
            repos: Tuple[str, ...], after: str) -> Facts:
    """Pull the window's events (paginated until a short page) and the
    authored MRs per configured repo. Raises RuntimeError on any glab failure
    -- the caller turns that into a loud alert, never a quiet empty week."""
    work: List[Tuple[str, str, str]] = []
    fresh: Dict[str, int] = {}
    reviews: List[Tuple[str, str, str]] = []
    page = 1
    while True:
        raw = run_glab(["api", f"events?after={after}&per_page=100&page={page}"])
        events = json.loads(raw or "[]")
        for e in events:
            day = (e.get("created_at") or "")[:10]
            action = e.get("action_name") or ""
            if action == "pushed to" or action == "pushed new":
                pd = e.get("push_data") or {}
                ref = pd.get("ref") or ""
                title = pd.get("commit_title")
                kind = classify_push(title)
                if kind == "work":
                    work.append((day, ref, (title or "(untitled push)").strip()))
                else:
                    fresh[ref] = fresh.get(ref, 0) + 1
            elif action in ("approved", "commented on"):
                reviews.append((day, action, e.get("target_title") or "-"))
        if len(events) < 100:
            break
        page += 1

    mrs: List[dict] = []
    for repo in repos:
        path = (f"projects/{quote(f'{PROJECT_PREFIX}/{repo}', safe='')}/merge_requests"
                f"?author_username={username}&updated_after={after}T00:00:00Z"
                f"&per_page=50&state=all")
        for mr in json.loads(run_glab(["api", path]) or "[]"):
            mrs.append({**mr, "repo": repo})
    mrs.sort(key=lambda m: (m.get("state") != "merged", -(m.get("iid") or 0)))
    # Oldest first within the week reads as a timeline for the model.
    work.sort()
    reviews.sort()
    return Facts(after=after, mrs=mrs, work_pushes=work,
                 freshening_branches=fresh, reviews=reviews)


# --- facts text + prompt ---------------------------------------------------

def _mr_by_branch(facts: Facts) -> Dict[str, dict]:
    return {m.get("source_branch") or "": m for m in facts.mrs}


def _status_word(mr: dict, facts: Facts) -> str:
    if mr.get("state") == "merged":
        return "merged"
    if mr.get("state") == "closed":
        return "closed"
    branch = mr.get("source_branch") or ""
    touched = any(ref == branch for _, ref, _ in facts.work_pushes)
    if mr.get("draft") and not touched:
        return "PARKED (freshening pushes only this week)"
    if mr.get("draft"):
        return "in progress (draft)"
    return "in review"


def build_facts_text(facts: Facts) -> str:
    by_branch = _mr_by_branch(facts)
    lines = [f"WINDOW: since {facts.after}", "", "AUTHORED MRs (state | refs | reviewers):"]
    for m in facts.mrs:
        merged = f", merged {m['merged_at'][:10]}" if m.get("merged_at") else ""
        reviewers = ",".join(r.get("username", "") for r in m.get("reviewers") or []) or "none"
        status = _status_word(m, facts)
        target = m.get("target_branch") or ""
        stacked = ""
        if target and target not in ("master", "main"):
            parent = by_branch.get(target)
            stacked = (f" | STACKED on !{parent['iid']} (un-drafts when that merges)"
                       if parent else f" | STACKED on branch {target}")
        lines.append(f"- {m['repo']} !{m['iid']} | {status}{merged} | {m.get('title', '')} "
                     f"| branch {m.get('source_branch', '')} | reviewers: {reviewers}{stacked}")
        for day, ref, title in facts.work_pushes:
            if ref == (m.get("source_branch") or ""):
                lines.append(f"    work {day}: {title}")
    orphan = [(d, r, t) for d, r, t in facts.work_pushes if r not in by_branch]
    if orphan:
        lines += ["", "WORK PUSHES ON BRANCHES WITHOUT AN MR IN THE WINDOW:"]
        lines += [f"- {d} {r}: {t}" for d, r, t in orphan]
    n_fresh = len(facts.freshening_branches)
    lines += ["", f"MAINTENANCE: {n_fresh} branches kept current with master "
              f"(freshening / stack-rebase merges only — NOT work): "
              + ", ".join(sorted(facts.freshening_branches)) if n_fresh else
              "MAINTENANCE: no freshening pushes"]
    lines += ["", "REVIEW ACTIVITY ON OTHERS' WORK (date | action | title):"]
    lines += [f"- {d} | {a} | {t}" for d, a, t in _others_reviews(facts)] or ["- none"]
    return "\n".join(lines)


def build_prompt(facts: Facts) -> str:
    return f"""Write Chandler's Monday standup notes from the FACTS below. Output ONLY the notes, nothing else.

FORMAT (exact — one item per line, narrative lines directly under the item they explain):
Chandler:
<Plain-English title> (#issue) - <status> (!MR)
<0-3 lines: what changed and why it mattered; "Addressed feedback:" + what, when a listed work commit answers a review>
<Plain-English title> (#issue) - <status> (!MR)
...
<Parked stack title> (#a/#b/#c) - parked (drafts !N–!M), kept current with master, awaiting <blocker>
Reviews: <one line: approvals + review passes on others' MRs, or "none">

EXAMPLE of the voice and shape (Chandler's own, 2026-09-08 — copy the SHAPE, not the content):
Health growth metrics served from cache (#1705) - merged (!4066)
Hardening done before merge: cache hits and legacy fallbacks now agree on weight units and deads variant, and the bare deads row is only trusted when the Yardsheet display matches the ADG model.
Demo yard-sheet uniqueness (#1650) - in review (!4085)
Addressed feedback: deterministic ids were guessable cross-tenant, so the derivation is now HMAC-salted; duplicate-key races now converge instead of failing; publish skips/failures surface end-to-end in the admin UI.
Add-Cost removeEvent tri-state (#1607) - closed (!4083), pending architecture discussion
Leif flagged that the Mongo read-option work belongs behind pb-api rather than in pb-www; paused per his ask, consolidated the reasoning into #1820, and we'll re-cut after that discussion.
Ranch data point stack (#1588/#1590/#1597/#1598/#1599) - parked (drafts !3981–!3985), kept current with master, awaiting pb-api ranch endpoints

RULES:
- TITLES are plain English for a teammate who has not read the MR: what the change does or fixes, 4-9 words. Never a conventional-commit title, never file names, never internal jargon (no "CAS", "seam", "mutant", "wiring guard", "deterministic-_id"); say the outcome instead ("duplicate demo sheets can't be created twice", "safe_div no longer fatals on non-numeric input"). Put the issue number in parentheses after the title and the MR number in parentheses after the status.
- Status vocabulary ONLY: merged, in review, in progress (draft), parked, closed. EVERY item line ends with its MR number in parentheses. Qualify inline when it matters: "in progress (draft !4109, stacked on !4085 — un-drafts when that merges)", "closed (!4083), pending architecture discussion".
- Merged MRs lead. Every merged MR in the facts MUST appear.
- Narrative lines earn their place: a merged item gets one line on why it mattered or what hardening happened before merge; "Addressed feedback:" appears ONLY when a listed `work` commit answers a review, and says what changed in plain words. Skip the narrative when there is nothing real to say.
- Work items only. Never mention worksweep, heartbeat, infra, or personal tooling.
- The facts already classify pushes. Freshening / stack-rebase merges are NOT work: a branch whose only activity is freshening is parked — say so with its blocker (ranch-data drafts await pb-api ranch endpoints) in ONE stack line, never as "updates" or "progress".
- Cite MR numbers as !NNNN and issues as #NNNN exactly as given. NEVER invent a number, a reviewer, a person, or a detail that is not in the facts. No URLs, no markdown links, no emojis, no section headers, no bullets.
- Keep the whole thing under 1800 characters. Plain, specific, no fluff.

FACTS:
{build_facts_text(facts)}
"""


# --- validation + fallback -------------------------------------------------

def _known_refs(facts: Facts) -> Tuple[set, set]:
    """Everything citable: authored MR iids, issue numbers from titles and
    branch names, and any !N / #N that appears verbatim in the facts text
    (a commit title saying "the merged !4084 shape" makes !4084 fair to
    cite -- the live dry run on 2026-09-08 rejected exactly that)."""
    mrs = {int(m["iid"]) for m in facts.mrs if m.get("iid")}
    issues = set()
    texts = [m.get("title") or "" for m in facts.mrs] + \
            [m.get("source_branch") or "" for m in facts.mrs] + \
            [ref + " " + title for _, ref, title in facts.work_pushes] + \
            list(facts.freshening_branches)
    for text in texts:
        for a, b in _ISSUE_IN_TEXT_RE.findall(text):
            issues.add(int(a or b))
        for sigil, num in _REF_RE.findall(text):
            (mrs if sigil == "!" else issues).add(int(num))
    return mrs, issues


def _others_reviews(facts: Facts) -> List[Tuple[str, str, str]]:
    """Review activity on OTHER people's work. A reply on one's own MR is
    also a "commented on" event; it is feedback handling, not a review."""
    own = {(m.get("title") or "").strip() for m in facts.mrs}
    return [(d, a, t) for d, a, t in facts.reviews if t.strip() not in own]


def validate(output: str, facts: Facts) -> bool:
    """Deterministic gate: header present, no URLs, every !N / #N cited is a
    known ref, every merged MR mentioned, sane size."""
    if not output or not output.strip():
        print("worksweep: standup validation failed: empty output", file=sys.stderr)
        return False
    if not output.lstrip().startswith("Chandler:"):
        print("worksweep: standup validation failed: missing 'Chandler:' header", file=sys.stderr)
        return False
    if _URL_RE.search(output):
        print("worksweep: standup validation failed: contains a URL", file=sys.stderr)
        return False
    if len(output.encode("utf-8")) > 4000:
        print("worksweep: standup validation failed: over 4000 bytes", file=sys.stderr)
        return False
    mrs, issues = _known_refs(facts)
    for sigil, num in _REF_RE.findall(output):
        n = int(num)
        if sigil == "!" and n not in mrs:
            print(f"worksweep: standup validation failed: unknown MR !{n}", file=sys.stderr)
            return False
        if sigil == "#" and n not in issues:
            print(f"worksweep: standup validation failed: unknown issue #{n}", file=sys.stderr)
            return False
    for m in facts.mrs:
        if m.get("state") == "merged" and f"!{m['iid']}" not in output:
            print(f"worksweep: standup validation failed: merged !{m['iid']} not mentioned",
                  file=sys.stderr)
            return False
    return True


def _short_title(title: str) -> str:
    t = re.sub(r"^Draft:\s*", "", title or "", flags=re.I)
    t = re.sub(r"^(feat|fix|test|perf|chore|refactor)\((#?\d+|[^)]*)\):\s*", "", t)
    return t.strip() or title


def _issue_of(mr: dict) -> str:
    for text in (mr.get("title") or "", mr.get("source_branch") or ""):
        for a, b in _ISSUE_IN_TEXT_RE.findall(text):
            return f" (#{a or b})"
    return ""


def render_fallback(facts: Facts) -> str:
    """Notes with no model in the loop, in the same shape the model is asked
    for: `<title> (#issue) - <status> (!MR)`, the week's work commits under
    it, one parked-stack line, one reviews line. Titles are the MR titles
    minus their conventional-commit prefix -- the model does the plain-
    English rewrite; this path only has to be correct."""
    lines = ["Chandler:"]
    parked = []
    for m in facts.mrs:
        status = _status_word(m, facts)
        if status.startswith("PARKED"):
            parked.append(m)
            continue
        merged = f", {m['merged_at'][:10]}" if m.get("merged_at") else ""
        lines.append(f"{_short_title(m.get('title', ''))}{_issue_of(m)} - {status} (!{m['iid']}{merged})")
        for day, ref, title in facts.work_pushes:
            if ref == (m.get("source_branch") or ""):
                lines.append(f"{day}: {title[:110]}")
    if parked:
        issues = "/".join(_issue_of(m).strip(" ()") for m in parked if _issue_of(m))
        iids = sorted(m["iid"] for m in parked)
        span = f"!{iids[0]}–!{iids[-1]}" if len(iids) > 1 else f"!{iids[0]}"
        lines.append(f"Parked drafts ({issues or 'no issue refs'}) - parked (drafts {span}), "
                     f"kept current with master, awaiting their blockers")
    revs = sorted({t for _, _, t in _others_reviews(facts)})
    lines.append("Reviews: " + ("; ".join(revs) if revs else "none"))
    return "\n".join(lines)


def _chunks(text: str, limit: int) -> List[str]:
    """Split on line boundaries so no chunk exceeds `limit` UTF-8 bytes."""
    out, cur = [], ""
    for line in text.split("\n"):
        cand = line if not cur else cur + "\n" + line
        if len(cand.encode("utf-8")) > limit and cur:
            out.append(cur)
            cur = line
        else:
            cur = cand
    if cur or not out:
        out.append(cur)
    return out


# --- orchestration ---------------------------------------------------------

def run_standup(cfg, deps: Dict[str, Callable]) -> int:
    """Collect → curate → validate (fallback) → post. Never silent: a
    collection failure is a 🔴; a model failure ships the fallback with a
    note; a logged-out claude gets the sweep's own auth alert too."""
    post = deps["post"]

    def _post(text: str) -> None:
        if cfg.discord_webhook:
            post(cfg.discord_webhook, text)
        else:
            print(text)

    try:
        facts = deps["collect"]()
    except Exception as e:
        _post(f"🔴 **Standup notes NOT generated** — collecting the week's GitLab activity failed: "
              f"`{str(e)[:200]}`. Nothing was posted; write them by hand or fix the "
              f"collector and run `worksweep standup --discord` again.")
        return 1

    notes: Optional[str] = None
    note = ""
    run_llm = deps.get("llm")
    if run_llm is not None:
        try:
            out = run_llm(build_prompt(facts))
            out = out.strip() if isinstance(out, str) else ""
            if validate(out, facts):
                notes = out
            else:
                note = " (fallback: the model's draft failed validation)"
        except curator.AuthError as e:
            note = " (fallback: claude is logged out on this box)"
            _post(f"🔴 **Claude auth is DOWN on this box** — standup's `claude -p` was refused: "
                  f"`{str(e).strip().splitlines()[-1][:160]}`. Run `claude` → `/login` in the mini's "
                  f"GUI session (screen share, not ssh).")
        except Exception as e:
            print(f"worksweep: standup LLM call failed: {type(e).__name__}: {e}", file=sys.stderr)
            note = f" (fallback: model unavailable — {type(e).__name__})"
    if notes is None:
        notes = render_fallback(facts)

    body = f"{_TITLE}{note}\n\n{notes}"
    for chunk in _chunks(body, DISCORD_LIMIT):
        _post(chunk)
    return 0
