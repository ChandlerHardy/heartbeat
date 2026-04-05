# Heartbeat

Autonomous discovery, implementation, and review system for portfolio projects. Runs on OCI, controllable from Discord or local Claude Code sessions.

## How It Works

```
Discovery → GitHub Issues → Implementation → PR → Bot Review → Auto-Fix → Approve/Escalate
    ↑                                                                              ↓
    └──────────── Product Context (vision interviews) ─────────────────────────────┘
```

**Daily (2 AM CDT):** Heartbeat scans each project in two phases:
1. **Code quality** — bugs, tech debt, DX improvements → quick wins auto-implemented
2. **Feature discovery** — reads `docs/product-context.md`, proposes features that advance the product

**On every PR push:** Code reviewer bot formally reviews (APPROVE/REQUEST_CHANGES), auto-fixes issues via `claude -p`, re-reviews until passing or 3 attempts exhausted.

**Weekly (Sunday 9 AM CDT):** Digest summarizing merged PRs, open PRs, and backlog across all projects.

## Commands

Available from both Discord (`/heartbeat <cmd>`) and local Claude Code (`/heartbeat <cmd>`):

| Command | Description |
|---------|-------------|
| `status` | Show open PRs and backlog issues across all projects |
| `discover [project]` | Run discovery scan (all projects or one) |
| `interview <project>` | Interactive vision interview → updates product-context.md |
| `implement <project>` | Implement quick-win issues on OCI |
| `implement <project> #N` | Implement a specific issue on OCI |
| `build <project> <proposal>` | Deep implementation for larger proposals |
| `merge` | Merge all approved heartbeat PRs |
| `projects` | List all tracked projects |
| `add-project <name> <repo>` | Add a new project (clone, config, mapping, product context) |

All `implement` and `build` commands dispatch to OCI — they keep running after you close your laptop.

## Adding a New Project

Use `/heartbeat add-project <name> <github-repo>` or manually:

1. **Clone on OCI + update config:**
   ```bash
   ssh oci "cd /mnt/block_volume/repos && git clone git@github.com:ChandlerHardy/<repo>.git <name>"
   ssh oci "jq '.projects += [{\"name\": \"<name>\", \"path\": \"/mnt/block_volume/repos/<name>\", \"stale_days\": 14}]' ~/etc/heartbeat.json > /tmp/hb.json && mv /tmp/hb.json ~/etc/heartbeat.json"
   ```

2. **If GitHub repo name ≠ OCI dir name** (like `gnomestead` → `gnomestead-ios`), update `REPO_NAME_MAP` in `code-reviewer/app.py` and deploy.

3. **Update skill mapping** — add a row to the name mapping table in `skills/heartbeat/SKILL.md`.

4. **Create product context** — run `/heartbeat interview <name>` or write `docs/product-context.md` manually.

## Architecture

```
Mac (you)                        OCI Server
─────────                        ──────────
/heartbeat skill ──── ssh ────→  ~/bin/heartbeat.sh (discovery + implementation)
  or Discord bot                 ~/bin/heartbeat-weekly.sh (weekly digest)
                                 ~/code-reviewer/app.py (webhook + auto-fix)
                                 ~/heartbeat-bot/bot.py (Discord commands)

GitHub
──────
Webhook (PR events) ──────────→  code-reviewer (port 9100, nginx proxy)
Issues (labeled: heartbeat)      created by discovery, closed by PR merge
Reviews (APPROVE/REQUEST_CHANGES) posted by ch-code-reviewer GitHub App
```

## Components

| File | What | Where |
|------|------|-------|
| `bin/heartbeat.sh` | Daily discovery + quick-win implementation | OCI cron, 2 AM CDT |
| `bin/heartbeat-weekly.sh` | Weekly digest to Discord | OCI cron, Sunday 9 AM CDT |
| `code-reviewer/app.py` | GitHub App webhook: review + auto-fix cycle | OCI systemd `code-reviewer` |
| `skills/heartbeat/SKILL.md` | Local Claude Code `/heartbeat` commands | Mac `~/.claude/skills/heartbeat` |
| `systemd/code-reviewer.service` | Persistent bot service | OCI systemd |

**Not in this repo (OCI-only, has secrets):**
- `~/heartbeat-bot/bot.py` — Discord bot (slash commands, token in systemd env)
- `~/etc/heartbeat.json` — project config with Discord webhook URL
- `~/code-reviewer/ch-code-reviewer.pem` — GitHub App private key
- `~/code-reviewer/webhook-secret.txt` — GitHub webhook secret

## Product Context

Each project has a `docs/product-context.md` that tells the discovery agent what the product is, who it's for, and what would advance it. This is what makes heartbeat propose *features*, not just code fixes.

Update via `/heartbeat interview <project>` — an interactive Socratic interview (5-7 questions about vision, users, goals) that refines the product context. The next discovery run picks up the updated context automatically.

**Projects with product context:**
- crooked-finger, portfolio-website, gnomestead-web, gnomestead-ios
- elucidate-chess (uses `docs/ELUCIDATE_VISION.md` instead)

## Project Name Mapping

| Name | OCI path | GitHub repo |
|------|----------|-------------|
| gnomestead / gnomestead-ios | `gnomestead-ios` | `ChandlerHardy/gnomestead` |
| gnomestead-web | `gnomestead-web` | `ChandlerHardy/gnomestead-web` |
| crooked-finger | `crooked-finger` | `ChandlerHardy/crooked-finger` |
| portfolio-website | `portfolio-website` | `ChandlerHardy/portfolio-website` |
| elucidate-chess | `elucidate-chess` | `ChandlerHardy/elucidate-chess` |
| greenline | `greenline` | `ChandlerHardy/greenline` |
| snapcal | `snapcal` | `ChandlerHardy/snapcal` |

## Review Cycle

```
heartbeat creates PR
        ↓
ch-code-reviewer bot reviews (APPROVE or REQUEST_CHANGES)
        ↓
REQUEST_CHANGES → auto-fix (claude -p with review feedback + diff)
        ↓
push fix → webhook fires → bot re-reviews
        ↓
repeat up to 3x → APPROVE (merge) or escalate (manual intervention)
```

## MCP Tools (OCI)

All `claude -p` sessions have access to:
- **jcodemunch** — code analysis, symbol search, file outlines
- **context7** — framework documentation (Next.js, FastAPI, etc.)
- **codebase-memory-mcp** — architecture memory, code search

## Safety

- Branch-only (never commits to main directly)
- SKIP escape hatch — if implementation is too complex, claude writes SKIP and reverts
- Max 2 quick wins per project per run
- Max 3 auto-fix attempts per PR
- Skips stale repos (no commits in 14 days)
- Idempotent (won't re-create existing branches)
- `--max-turns` on all claude invocations (25-60 depending on task)
- All invocations use `claude -p --dangerously-skip-permissions`

## Deploy

```bash
./install.sh oci    # Deploy scripts + code-reviewer to OCI
```

Secrets must be configured manually on OCI (not in this repo).
