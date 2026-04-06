---
name: heartbeat
description: "Run heartbeat commands locally. Discovery, implementation, interviews, and status checks. OCI is only used for the nightly cron and Discord bot. Use for: heartbeat, run heartbeat, discover, heartbeat status, heartbeat interview, implement proposal."
user-invocable: true
---

# Heartbeat — Local-First

All interactive work runs locally via subagents. OCI is reserved for the nightly cron bot and Discord integration.

## Project Name Mapping

| User says | Local path | OCI name | GitHub repo |
|-----------|-----------|----------|-------------|
| gnomestead, gnomestead-ios, gnomestead backend | `~/workspaces/gnomestead/gnomestead-ios` | `gnomestead-ios` | `ChandlerHardy/gnomestead` |
| gnomestead-web, gnomestead frontend | `~/workspaces/gnomestead/gnomestead-web` | `gnomestead-web` | `ChandlerHardy/gnomestead-web` |
| crooked-finger, crooked finger | `~/repos/crooked-finger` | `crooked-finger` | `ChandlerHardy/crooked-finger` |
| portfolio-website, portfolio, website | `~/repos/portfolio-website` | `portfolio-website` | `ChandlerHardy/portfolio-website` |
| elucidate-chess, elucidate, chess | `~/repos/elucidate-chess` | `elucidate-chess` | `ChandlerHardy/elucidate-chess` |
| greenline | `~/repos/greenline` | `greenline` | `ChandlerHardy/greenline` |
| snapcal | `~/repos/snapcal` | `snapcal` | `ChandlerHardy/snapcal` |
| heartbeat | `~/repos/heartbeat` | `heartbeat` | `ChandlerHardy/heartbeat` |

**Note:** gnomestead backend GitHub repo is `ChandlerHardy/gnomestead` but the directory is `gnomestead-ios`.

## Commands

Parse the user's intent from the arguments:

### `discover [project]`
Run discovery on one or all projects.

```bash
# All projects
ssh oci "~/bin/heartbeat.sh" 2>&1
# Single project (filter by name)
ssh oci "~/bin/heartbeat.sh --project <name>" 2>&1
```

If `--project` isn't supported yet, run the full heartbeat and filter output.

### `status`
Show open PRs and issues across all heartbeat-tracked projects.

```bash
for repo in ChandlerHardy/crooked-finger ChandlerHardy/portfolio-website ChandlerHardy/gnomestead-web ChandlerHardy/gnomestead; do
  echo "=== $(echo $repo | cut -d/ -f2) ==="
  gh pr list --repo $repo --state open --search "heartbeat" --json number,title,reviewDecision --jq '.[] | "#\(.number) \(.reviewDecision) \(.title)"'
  gh issue list --repo $repo --state open --label heartbeat --json number,title --jq '.[] | "#\(.number) \(.title)"'
done
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
Implement directly using subagents. **Never dispatch to OCI for interactive sessions.**

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

### `merge`
Merge all approved heartbeat PRs.

```bash
for repo in ChandlerHardy/crooked-finger ChandlerHardy/portfolio-website ChandlerHardy/gnomestead-web ChandlerHardy/gnomestead; do
  gh pr list --repo $repo --state open --search "heartbeat" --json number,reviews --jq '.[] | select(.reviews | map(select(.state == "APPROVED")) | length > 0) | .number' | while read pr; do
    gh pr merge $pr --repo $repo --merge --delete-branch
  done
done
```

### `build <project> <proposal>`
Deep implementation via subagent. For larger proposals.

Launch an implementer subagent with the proposal details, working from the local repo with `isolation: "worktree"`. Give it thorough context about the codebase and the proposal.

### `projects`
List all projects in the heartbeat config.

```bash
ssh oci "jq -r '.projects[] | \"\(.name) — \(.path)\"' ~/etc/heartbeat.json"
```

### `add-project <name> <github-repo>`
Add a new project to the heartbeat system. Handles all three registration points:

**Step 1: Clone on OCI and add to heartbeat.json**
```bash
# Clone repo
ssh oci "cd /mnt/block_volume/repos && git clone git@github.com:<github-repo>.git <local-dir-name>"

# Add to heartbeat.json
ssh oci "jq '.projects += [{\"name\": \"<name>\", \"path\": \"/mnt/block_volume/repos/<local-dir-name>\", \"stale_days\": 14}]' ~/etc/heartbeat.json > /tmp/hb.json && mv /tmp/hb.json ~/etc/heartbeat.json"
```

**Step 2: Add to REPO_NAME_MAP in code-reviewer (if GitHub name != OCI dir name)**
If the GitHub repo name differs from the local directory name (like gnomestead → gnomestead-ios), update the REPO_NAME_MAP dict in `code-reviewer/app.py` and deploy:
```python
REPO_NAME_MAP = {
    "gnomestead": "gnomestead-ios",
    "<github-name>": "<local-dir-name>",  # add this
}
```
Then: `scp code-reviewer/app.py oci:~/code-reviewer/app.py && ssh oci "sudo systemctl restart code-reviewer"`

**Step 3: Update the skill name mapping table**
Add a row to the "Project Name Mapping" table at the top of this file, commit, and push:
```bash
cd ~/repos/heartbeat && git add -A && git commit -m "Add <name> to heartbeat" && git push origin main
```

**Step 4: Create product context**
```bash
ssh oci "mkdir -p /mnt/block_volume/repos/<local-dir-name>/docs"
```
Then suggest running `/heartbeat interview <name>` to populate `docs/product-context.md`.

Report all steps taken and prompt for the vision interview.

### `burn [project] [--until N%]`
Autonomous discover → implement → merge loop. **Interactive session only** — not for OCI bot/cron.

Loop until session usage hits `--until` (default 80%):
1. Check usage via claude-in-chrome (`claude.ai/settings/usage` tab must be open)
2. Run discovery on OCI: `ssh oci "~/bin/heartbeat.sh --project <oci-name>"`
3. Merge any approved PRs
4. Implement new issues via subagents with `isolation: "worktree"` (2-3 parallel)
5. Merge approved PRs after code reviewer runs
6. Check usage → if under target, loop to step 2

Rules:
- Implementation is always local via subagents — never OCI `claude -p`
- Discovery still runs on OCI (it has MCP tools for exploration)
- If discovery returns 0 findings, move to next project or stop
- If no project specified, rotate through all active projects
- Print running merge count and usage after each round

### Default (no args or "help")
Show the command menu, then status:

```
Heartbeat Commands:
  /heartbeat status                     — open PRs and backlog issues
  /heartbeat discover [project]         — run discovery scan
  /heartbeat interview <project>        — vision interview → update product context
  /heartbeat implement <project>        — implement quick-wins
  /heartbeat implement <project> #N     — implement specific issue
  /heartbeat build <project> <proposal> — deep implementation
  /heartbeat burn [project] [--until N%] — autonomous loop until usage target
  /heartbeat cleanup [project]          — delete stale heartbeat branches
  /heartbeat merge                      — merge all approved PRs
  /heartbeat projects                   — list tracked projects
  /heartbeat add-project <name> <repo>  — add new project
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
- "merge" or "merge all" → `merge`
- "projects" or "list" → `projects`
- "add" or "add-project" → `add-project`
- Project name alone → `discover <project>`
