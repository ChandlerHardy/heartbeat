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

### `implement <project> #<issue>`
Trigger OCI claude to implement a specific heartbeat issue.

```bash
# Get issue details
gh issue view <number> --repo ChandlerHardy/<project> --json title,body --jq '{title: .title, body: .body}'

# Trigger implementation on OCI
ssh oci "cd /mnt/block_volume/repos/<project> && claude -p --dangerously-skip-permissions --max-turns 40 'Implement this GitHub issue, create a branch, commit, push, and open a PR.

Issue #<number>: <title>
<body>

Instructions:
- Create branch: heartbeat/$(date +%Y-%m-%d)-<slugified-title>
- Use MCP tools to understand the codebase before editing
- Implement the change with minimal scope
- Commit with descriptive message
- Push and create PR with Closes #<number> in the body'" 2>&1
```

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

### Default (no args)
Show status.

## Argument Parsing

- No args or "status" → `status`
- "discover" or "run" or "scan" → `discover`
- "interview" + project name → `interview <project>`
- "implement" + project + issue → `implement <project> #<issue>`
- "build" + project + proposal → `build <project> <proposal>`
- "merge" or "merge all" → `merge`
- "projects" or "list" → `projects`
- "add" or "add-project" → `add-project`
- Project name alone → `discover <project>`
