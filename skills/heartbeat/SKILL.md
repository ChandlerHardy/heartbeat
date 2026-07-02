---
name: heartbeat
description: "Run heartbeat commands locally. Discovery, implementation, interviews, and status checks. All interactive work runs in the current session — OCI is only for the nightly cron bot. Use for: heartbeat, run heartbeat, discover, heartbeat status, heartbeat interview, implement proposal."
user-invocable: true
---

# Heartbeat — Fully Local

All interactive heartbeat work runs in the current session using local tools and subagents. OCI is only for the unattended nightly cron and Discord bot — never SSH to OCI from interactive commands.

> **Current status (2026-07):** the nightly `heartbeat.sh` cron and `heartbeat-bot.service` on OCI
> are **disabled** — only the weekly GitHub digest (`heartbeat-weekly.sh`, Sun 14:05 UTC) runs.
> Re-enable steps are in MEMORY.md § Scheduled Tasks. Interactive commands below are unaffected.

## Dashboard

**GitHub Projects board:** https://github.com/users/ChandlerHardy/projects/1

Issues and PRs are auto-added to the board with status tracking:
- **Discovered** — found by automation, not yet triaged
- **Triaged** — reviewed, pending action
- **Implemented** — code written, PR created
- **Merged** — merged to main
- **Rejected** — not pursuing

Labels: `heartbeat`, `discovered`, `implemented`, `rejected`, `ai-digest`, `server-health`, `standup-notes`

## Project Name Mapping

| User says | Local path | GitHub repo |
|-----------|-----------|-------------|
| gnomestead, gnomestead-ios, gnomestead backend | `~/workspaces/gnomestead/gnomestead-ios` | `ChandlerHardy/gnomestead` |
| gnomestead-web, gnomestead frontend | `~/workspaces/gnomestead/gnomestead-web` | `ChandlerHardy/gnomestead-web` |
| crooked-finger, crooked finger | `~/repos/crooked-finger` | `ChandlerHardy/crooked-finger` |
| elucidate-chess, elucidate, chess | `~/repos/elucidate-chess` | `ChandlerHardy/elucidate-chess` |
| greenline | `~/repos/greenline` | `ChandlerHardy/greenline` |
| snapcal | `~/repos/snapcal` | `ChandlerHardy/snapcal` |
| heartbeat | `~/repos/heartbeat` | `ChandlerHardy/heartbeat` |

**Note:** gnomestead backend GitHub repo is `ChandlerHardy/gnomestead` but the directory is `gnomestead-ios`.

## Commands

Parse the user's intent from the arguments:

### `discover [project]`
Run discovery locally via an Explore subagent. Scans the codebase for bugs, tech debt, DX improvements, and feature opportunities.

**Single project:**
1. Resolve the local path from the project name mapping
2. Read `docs/product-context.md`, `CLAUDE.md`, `README.md` for product context
3. Launch an `Explore` subagent against the local path with this brief:
   - Phase 1 (code quality): bugs, dead code, inconsistent patterns, performance, security, test gaps
   - Phase 2 (features): what's missing, what's half-finished, what would advance the product
   - Check git log for recent activity and avoid proposing in-progress work
   - Check open issues (`gh issue list` for GitHub, `glab issue list` for GitLab) to avoid duplicates
4. Format findings as a structured report with: title, category, effort, impact, files, what, why

**All projects:**
Launch parallel Explore subagents, one per active project. Combine results.

After discovery, create issues with `heartbeat` + category labels and add to the project board with "Discovered" status.

### `status`
Show open PRs and issues across all heartbeat-tracked projects. Run locally via `gh`.

```bash
# Filter PRs by `heartbeat/` branch prefix — heartbeat's pr create doesn't
# apply a label, and `--search "heartbeat"` would match any PR body/comment
# mentioning the word. Branch prefix is the canonical marker. Issues DO get
# the heartbeat label (applied by heartbeat.sh::create_issue_if_new), so
# --label is correct for the issue half.
for repo in ChandlerHardy/crooked-finger ChandlerHardy/gnomestead-web ChandlerHardy/gnomestead ChandlerHardy/heartbeat ChandlerHardy/elucidate-chess ChandlerHardy/greenline ChandlerHardy/snapcal; do
  name=$(echo $repo | cut -d/ -f2)
  prs=$(gh pr list --repo $repo --state open --json number,title,headRefName,reviewDecision \
    --jq '.[] | select(.headRefName | startswith("heartbeat/")) | "  PR #\(.number) [\(.reviewDecision // "pending")] \(.title)"' 2>/dev/null)
  issues=$(gh issue list --repo $repo --state open --label heartbeat --json number,title --jq '.[] | "  #\(.number) \(.title)"' 2>/dev/null)
  if [ -n "$prs" ] || [ -n "$issues" ]; then
    echo "=== $name ==="
    [ -n "$prs" ] && echo "$prs"
    [ -n "$issues" ] && echo "$issues"
  fi
done
echo ""
echo "Dashboard: https://github.com/users/ChandlerHardy/projects/1"
```

### `interview <project>`
Run an interactive vision interview to refine a project's product-context.md.

1. Read the current product context from the local repo:
   ```bash
   cat <local-path>/docs/product-context.md 2>/dev/null
   ```

2. Run a Socratic interview with the user. Ask 5-7 questions:
   - What's the ONE thing that would make this product 10x better?
   - Who is your ideal user? What problem are they solving?
   - What do competitors do that you wish you had?
   - What feature would make users come back daily?
   - What's the next milestone you want to hit?
   - What should this product NOT try to be?

3. After the interview, synthesize the answers into an updated product-context.md.
   Show the user the updated context and ask for approval.

4. On approval, write locally, commit, and push:
   ```bash
   cd <local-path> && git add docs/product-context.md && git commit -m 'Update product context from vision interview' && git push origin main
   ```

### `implement <project> [quick-wins | #<issue>]`
Implement directly using subagents.

**By issue number:** `/heartbeat implement gnomestead #15`
1. Get issue details: `gh issue view <number> --repo ChandlerHardy/<project>`
2. Launch an implementer subagent (with `isolation: "worktree"` for the local repo):
   - Read existing code to understand patterns
   - Create branch: `heartbeat/$(date +%Y-%m-%d)-<slugified-title>`
   - Implement with minimal scope
   - Commit, push, create PR with `Closes #<number>`

**Quick wins (default):** `/heartbeat implement gnomestead`
- Find issues: `gh issue list --repo ChandlerHardy/<project> --state open --label "heartbeat,quick-win"`
- Implement up to 2 in parallel, one subagent per issue, using `isolation: "worktree"`

**Important:** One issue per subagent. Use worktree isolation for parallel work on the same repo.

**Subagent selection:**
- `general-purpose` — quick wins, single-file fixes, config changes, dead code removal. Fast, low overhead.
- `implementer` — features needing tests, multi-file changes, anything benefiting from TDD. Thorough but slower.

Default to `general-purpose` for quick-wins and tech-debt. Use `implementer` for features and anything the code reviewer is likely to flag for missing tests.

### `merge`
Merge all approved heartbeat PRs.

```bash
# Same branch-prefix filter as `status` — don't use `--search "heartbeat"`
# because it matches any PR body/comment mentioning the word. Only APPROVED
# PRs are merged (reviewDecision is the aggregate; some PRs may have review
# events that are COMMENT or CHANGES_REQUESTED from seneschal rounds).
for repo in ChandlerHardy/crooked-finger ChandlerHardy/gnomestead-web ChandlerHardy/gnomestead ChandlerHardy/elucidate-chess ChandlerHardy/greenline ChandlerHardy/snapcal; do
  gh pr list --repo $repo --state open --json number,headRefName,reviewDecision \
    --jq '.[] | select((.headRefName | startswith("heartbeat/")) and .reviewDecision == "APPROVED") | .number' \
    | while read pr; do
      gh pr merge $pr --repo $repo --merge --delete-branch
    done
done
```

### `build <project> <proposal>`
Deep implementation via subagent. For larger proposals.

Launch an implementer subagent with the proposal details, working from the local repo with `isolation: "worktree"`. Give it thorough context about the codebase and the proposal.

### `projects`
List all projects from the name mapping table above. Show local path and whether the directory exists.

```bash
for path in ~/workspaces/gnomestead/gnomestead-ios ~/workspaces/gnomestead/gnomestead-web ~/repos/crooked-finger ~/repos/elucidate-chess ~/repos/greenline ~/repos/snapcal ~/repos/heartbeat; do
  name=$(basename "$path")
  if [ -d "$path/.git" ]; then
    last=$(git -C "$path" log -1 --format="%ar" 2>/dev/null || echo "unknown")
    echo "  $name — $path (last commit: $last)"
  else
    echo "  $name — $path (not found)"
  fi
done
```

### `add-project <name> <github-repo>`
Add a new project to the heartbeat system. Three steps:

**Step 1: Add to the project name mapping table** in this skill file.

**Step 2: Clone on OCI and add to heartbeat.json** (for nightly cron only):
```bash
ssh oci "cd /mnt/block_volume/repos && git clone git@github.com:<github-repo>.git <local-dir-name>"
ssh oci "jq '.projects += [{\"name\": \"<name>\", \"path\": \"/mnt/block_volume/repos/<local-dir-name>\", \"stale_days\": 14}]' ~/etc/heartbeat.json > /tmp/hb.json && mv /tmp/hb.json ~/etc/heartbeat.json"
```

**Step 3: Add to REPO_NAME_MAP in `~/repos/seneschal/app.py`** (if GitHub name != OCI dir name —
Seneschal was extracted from this repo into its own; the map no longer lives here):
```python
REPO_NAME_MAP = {
    "gnomestead": "gnomestead-ios",
    "<github-name>": "<local-dir-name>",
}
```
Then redeploy the Seneschal webhook handler from ITS repo:
```bash
cd ~/repos/seneschal && ./install.sh oci    # ships app.py to OCI and restarts seneschal.service
```

**Step 4: Create product context**
Suggest running `/heartbeat interview <name>` to populate `docs/product-context.md`.

Commit skill changes and push.

### `burn [project] [--until N%]`
Autonomous discover → implement → merge loop in the current session.

Loop until session usage hits `--until` (default 80%):
1. Check usage via claude-in-chrome (`claude.ai/settings/usage` tab must be open)
2. Run discovery locally via Explore subagent
3. Create issues from findings (GitHub projects only)
4. Merge any approved PRs
5. Implement new issues via subagents with `isolation: "worktree"` (2-3 parallel)
6. Merge approved PRs after code reviewer runs
7. Check usage → if under target, loop to step 2

Rules:
- Everything runs locally — no OCI dispatch
- If discovery returns 0 findings, move to next project or stop
- If no project specified, rotate through all active projects
- Print running merge count and usage after each round

### `history [project] [--last N]`
Show a summary of recent heartbeat runs from the structured log on OCI (this is the one command that reads from OCI, since the nightly cron writes there).

```bash
HISTORY=$(ssh oci "cat ~/heartbeat-reports/history.jsonl 2>/dev/null")
```

Then summarize locally. Default: last 7 entries. If `--last N` specified, use N.

**If a project is specified**, filter to only that project's entries:
```bash
HISTORY=$(ssh oci "cat ~/heartbeat-reports/history.jsonl 2>/dev/null" | jq -c "select(.project == \"<name>\")")
```

**Summary format:**
```
Last 7 runs: 23 findings, 14 implemented, 9 skipped, 9 PRs created, 0 errors
```

If there are errors, list the most recent ones.

**Detailed mode** (`--detail`): show each run as a row:
```
2026-04-06T02:00:00Z  crooked-finger     5 findings  3 impl  1 skip  2 PRs  0 err
```

### `brainstorm [--focus <theme>]`
Runs `bin/heartbeat-brainstorm.sh` to propose ENTIRELY NEW project ideas that complement the existing portfolio (not features for existing projects). Reads each tracked project's `docs/product-context.md` and calls `claude -p` with a system prompt that explicitly bans saturated categories (dev tools "because dev tools are hot", generic SaaS clones) and requires concrete pitches with failure modes.

```bash
bin/heartbeat-brainstorm.sh                     # all projects, archive
bin/heartbeat-brainstorm.sh --focus="consumer"  # steer the brainstorm
bin/heartbeat-brainstorm.sh --discord           # post preview to Discord
```

Output: `~/heartbeat-reports/brainstorm-YYYY-MM-DD.md`. Intended to run weekly as part of the Sunday cron.

### Default (no args or "help")
Print the command menu **as a fresh response — verbatim, in your own message, even when this skill file is already inlined in the user's prompt context.** The user invoked `help` to get a clean menu; they cannot easily extract one from a multi-hundred-line file dump. "It's already on screen" is not a substitute for printing it. After the menu, run `status`.

```
Heartbeat Commands:
  /heartbeat status                     — open PRs and backlog issues
  /heartbeat discover [project]         — run discovery scan
  /heartbeat interview <project>        — vision interview → update product context
  /heartbeat implement <project>        — implement quick-wins
  /heartbeat implement <project> #N     — implement specific issue
  /heartbeat build <project> <proposal> — deep implementation
  /heartbeat brainstorm                 — propose NEW project ideas (reads all product contexts)
  /heartbeat burn [project] [--until N%] — autonomous loop until usage target
  /heartbeat cleanup [project]          — delete stale heartbeat branches
  /heartbeat history [project]          — summarize recent run metrics
  /heartbeat merge                      — merge all approved PRs
  /heartbeat projects                   — list tracked projects
  /heartbeat add-project <name> <repo>  — add new project
  /heartbeat dashboard                  — launch local read-only dashboard on :8765
  /heartbeat config <list|add|remove>   — manage projects in heartbeat.json
```

### `dashboard`
Launch the local heartbeat-dashboard web UI on http://127.0.0.1:8765. Binary at `~/bin/heartbeat-dashboard`, builds from `tools/heartbeat-dashboard/`. Read-only; shows projects config, run history from `~/heartbeat-reports/history.jsonl`, and live issue/PR counts via `gh`.

### `config <list|add|remove|show>`
Edit `heartbeat.json` via `bin/heartbeat-config.sh`:

```bash
heartbeat-config list                               # tabular project list
heartbeat-config add gnomestead /path/to/dir 14     # add project
heartbeat-config remove old-project                 # remove project
heartbeat-config show                               # raw JSON
```

Then show status output.

## Argument Parsing

- No args or "status" → `status`
- "discover" or "run" or "scan" → `discover`
- "interview" + project name → `interview <project>`
- "implement" + project + issue → `implement <project> #<issue>`
- "implement" + project (+ "quick-wins" or nothing) → `implement <project> quick-wins`
- "build" + project + proposal → `build <project> <proposal>`
- "burn" + optional project + optional --until → `burn`
- "cleanup" or "clean" + optional project → `cleanup`
- "history" + optional project + optional --last N → `history`
- "merge" or "merge all" → `merge`
- "projects" or "list" → `projects`
- "add" or "add-project" → `add-project`
- Project name alone → `discover <project>`
