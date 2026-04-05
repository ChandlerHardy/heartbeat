---
name: heartbeat
description: "Run heartbeat commands from your local session. Triggers discovery, implementation, interviews, and status checks on OCI. Use for: heartbeat, run heartbeat, discover, heartbeat status, heartbeat interview, implement proposal."
user-invocable: true
---

# Heartbeat — Local Control

Run heartbeat operations from your Mac terminal session. All heavy work runs on OCI via SSH.

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

**This command runs LOCALLY in the current session** (not on OCI). You're already in Claude Code with full MCP tools — use them directly.

**By issue number:** `/heartbeat implement gnomestead #15`

1. Get issue details:
   ```bash
   gh issue view <number> --repo ChandlerHardy/<project> --json title,body
   ```

2. Find the local repo path. Project name → path mapping:
   - gnomestead-web → `~/workspaces/gnomestead/gnomestead-web`
   - gnomestead / gnomestead-ios → `~/workspaces/gnomestead/gnomestead-ios`
   - Others → `~/repos/<project>`

3. `cd` to the repo, create a branch, implement the change, commit, push, and create a PR:
   - Branch: `heartbeat/YYYY-MM-DD-<slugified-title>`
   - Use your MCP tools and full workflow to implement properly
   - PR body should include `Closes #<number>`

**Quick wins (default):** `/heartbeat implement gnomestead` or `/heartbeat implement gnomestead quick-wins`

1. Find quick-win issues:
   ```bash
   gh issue list --repo ChandlerHardy/<project> --state open --label "heartbeat,quick-win" --json number,title,body --jq '.[:2]'
   ```

2. Implement each one locally (same as by-issue-number flow, one at a time, max 2).

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
Clone a GitHub repo to OCI and add it to heartbeat config.

```bash
ssh oci "cd /mnt/block_volume/repos && git clone git@github.com:<github-repo>.git <name>"
# Then update heartbeat.json via jq
```

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
