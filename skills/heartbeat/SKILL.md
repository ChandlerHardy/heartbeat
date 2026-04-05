---
name: heartbeat
description: "Run heartbeat commands from your local session. Triggers discovery, implementation, interviews, and status checks on OCI. Use for: heartbeat, run heartbeat, discover, heartbeat status, heartbeat interview, implement proposal."
user-invocable: true
---

# Heartbeat — Local Control

Run heartbeat operations from your Mac terminal session. All heavy work runs on OCI via SSH.

## Project Name Mapping

Users may say short/informal names. Map to the correct OCI path and GitHub repo:

| User says | OCI path | GitHub repo |
|-----------|----------|-------------|
| gnomestead, gnomestead-ios, gnomestead backend | `/mnt/block_volume/repos/gnomestead-ios` | `ChandlerHardy/gnomestead` |
| gnomestead-web, gnomestead frontend | `/mnt/block_volume/repos/gnomestead-web` | `ChandlerHardy/gnomestead-web` |
| crooked-finger, crooked finger | `/mnt/block_volume/repos/crooked-finger` | `ChandlerHardy/crooked-finger` |
| portfolio-website, portfolio, website | `/mnt/block_volume/repos/portfolio-website` | `ChandlerHardy/portfolio-website` |
| elucidate-chess, elucidate, chess | `/mnt/block_volume/repos/elucidate-chess` | `ChandlerHardy/elucidate-chess` |
| greenline | `/mnt/block_volume/repos/greenline` | `ChandlerHardy/greenline` |
| snapcal | `/mnt/block_volume/repos/snapcal` | `ChandlerHardy/snapcal` |

**Important:** The gnomestead backend GitHub repo is `ChandlerHardy/gnomestead` but the OCI directory is `gnomestead-ios`. Always use the correct mapping.

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

1. Read the current product context from OCI:
   ```bash
   ssh oci "cat /mnt/block_volume/repos/<project>/docs/product-context.md 2>/dev/null"
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

4. On approval, write to OCI:
   ```bash
   # Write updated context
   ssh oci "cat > /mnt/block_volume/repos/<project>/docs/product-context.md << 'EOF'
   <updated content>
   EOF"
   # Commit
   ssh oci "cd /mnt/block_volume/repos/<project> && git add docs/product-context.md && git commit -m 'Update product context from vision interview' && git push origin main"
   ```

### `implement <project> [quick-wins | #<issue>]`
Trigger OCI implementation. Two modes:

**Always dispatches to OCI** so it keeps running after you close the laptop. The full build → review → fix cycle runs autonomously on the server.

**By issue number:** `/heartbeat implement gnomestead #15`

```bash
# Get issue details
ISSUE=$(gh issue view <number> --repo ChandlerHardy/<project> --json title,body --jq '.')
TITLE=$(echo "$ISSUE" | jq -r '.title')
BODY=$(echo "$ISSUE" | jq -r '.body')

# Dispatch to OCI (runs in background, survives disconnect)
ssh oci "nohup bash -l -c 'cd /mnt/block_volume/repos/<local-name> && echo \"Implement this GitHub issue, create a branch, commit, push, and open a PR.

Issue #<number>: $TITLE
$BODY

Instructions:
- Create branch: heartbeat/\$(date +%Y-%m-%d)-<slugified-title>
- Use MCP tools to understand the codebase before editing
- Implement the change with minimal scope
- Commit with descriptive message
- Push and create PR with Closes #<number> in the body\" | claude -p --dangerously-skip-permissions --max-turns 40' > /tmp/heartbeat-implement-<number>.log 2>&1 &"
```

Report: "Dispatched to OCI. The build → review → fix cycle will run autonomously. Check progress with `/heartbeat status` or `ssh oci 'tail -f /tmp/heartbeat-implement-<number>.log'`."

**Quick wins (default):** `/heartbeat implement gnomestead` or `/heartbeat implement gnomestead quick-wins`

```bash
# Find quick-win issues
gh issue list --repo ChandlerHardy/<project> --state open --label "heartbeat,quick-win" --json number,title --jq '.[:2]'

# Dispatch each to OCI (same as by-issue flow, one at a time, max 2)
```

When no mode is specified, default to quick-wins.

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
Deep implementation — full autonomous agent for a larger proposal (not just quick wins).
Same as Discord `/heartbeat build`. Runs on OCI with extended timeout.

```bash
ssh oci "cd /mnt/block_volume/repos/<project> && claude -p --dangerously-skip-permissions --max-turns 60 '<proposal details + implementation instructions>'" 2>&1
```

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
- "merge" or "merge all" → `merge`
- "projects" or "list" → `projects`
- "add" or "add-project" → `add-project`
- Project name alone → `discover <project>`
