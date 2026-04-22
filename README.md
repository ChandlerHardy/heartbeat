<p align="center">
  <img src="docs/banner.png" alt="Heartbeat — autonomous discovery and implementation for portfolio projects" width="960">
</p>

# Heartbeat

Autonomous discovery, implementation scheduling, and weekly digests for personal portfolio projects. Runs on OCI, controllable from Discord or local Claude Code sessions.

> **Note:** The Seneschal PR-review bot has moved to its own repository:
> **[github.com/ChandlerHardy/seneschal](https://github.com/ChandlerHardy/seneschal)**
>
> Deploy it from that repo. This repo keeps the heartbeat discovery loop,
> ShipLog weekly digests, and the `/heartbeat` slash commands.

## How It Works

```
Discovery → GitHub Issues → Implementation → PR → Seneschal review → Merge
    ↑                                                                    ↓
    └──────────── Product Context (vision interviews) ───────────────────┘
```

**Daily (2 AM CDT):** Heartbeat scans each project in two phases:
1. **Code quality** — bugs, tech debt, DX improvements → quick wins auto-implemented
2. **Feature discovery** — reads `docs/product-context.md`, proposes features that advance the product

**On demand:** PR review is handled by [Seneschal](https://github.com/ChandlerHardy/seneschal) — deployed separately and triggered by commenting `/seneschal review` on any PR on a repo with the Seneschal GitHub App installed.

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

2. **If GitHub repo name ≠ OCI dir name** (like `gnomestead` → `gnomestead-ios`), update `REPO_NAME_MAP` in the Seneschal repo's `app.py` and redeploy Seneschal.

3. **Update skill mapping** — add a row to the name mapping table in `skills/heartbeat/SKILL.md`.

4. **Create product context** — run `/heartbeat interview <name>` or write `docs/product-context.md` manually.

## Architecture

```
Mac (you)                        OCI Server
─────────                        ──────────
/heartbeat skill ──── ssh ────→  ~/bin/heartbeat.sh (discovery + implementation)
  or Discord bot                 ~/bin/heartbeat-weekly.sh (weekly digest)
                                 ~/seneschal/app.py (webhook; manual trigger only)
                                 ~/heartbeat-bot/bot.py (Discord commands)

GitHub
──────
Webhook (PR events + issue comments) → seneschal (port 9100, nginx proxy)
Issues (labeled: heartbeat)      created by discovery, closed by PR merge
Reviews posted by seneschal-cr[bot] in response to a `/seneschal review`
  comment (or manual /seneschal-review from the operator's CLI session)
```

## Components

| File | What | Where |
|------|------|-------|
| `bin/hb` | **Unified CLI** — thin dispatcher over everything below | Any host |
| `bin/heartbeat.sh` | Daily discovery + quick-win implementation | OCI cron, 2 AM CDT |
| `bin/heartbeat-weekly.sh` | Weekly heartbeat-only digest to Discord | OCI cron, Sunday 9 AM CDT |
| `bin/shiplog.sh` | Full weekly "what I shipped" digest (all PRs) | OCI cron, Sunday 9 AM CDT |
| `bin/heartbeat-brainstorm.sh` | Propose NEW project ideas via Claude (reads all product contexts) | OCI or Mac |
| `bin/heartbeat-config.sh` | Add/remove projects in `heartbeat.json` safely | Any host with `jq` |
| `bin/heartbeat-backfill-projects.sh` | Populate the GH Projects board from existing issues/PRs | Mac (needs `gh` with `project` scope) |
| `code-reviewer/` | Seneschal GitHub App webhook: pre-review analysis + Claude review; runs ONLY on `/seneschal review` comments or manual CLI trigger | OCI `~/seneschal/`, systemd `seneschal` |
| `shiplog/` | ShipLog Python package (called by bin/shiplog.sh) | OCI `~/shiplog` |
| `tools/claude-burn/` | Local Go CLI for Claude Code usage telemetry | Mac `~/bin/claude-burn` |
| `tools/heartbeat-dashboard/` | Local Go web UI for config + run history (secondary to GH Projects board) | Mac `~/bin/heartbeat-dashboard` |
| `skills/heartbeat/SKILL.md` | Local Claude Code `/heartbeat` commands | Mac `~/.claude/skills/heartbeat` |
| `systemd/seneschal.service` | Persistent bot service | OCI systemd |

**Not in this repo (OCI-only, has secrets):**
- `~/heartbeat-bot/bot.py` — Discord bot (slash commands, token in systemd env)
- `~/etc/heartbeat.json` — project config with Discord webhook URL
- `~/seneschal/ch-code-reviewer.pem` — GitHub App private key (filename is legacy, slug locked at App creation)
- `~/seneschal/webhook-secret.txt` — GitHub webhook HMAC secret (chmod 600)

## ch-code-reviewer pre-review analysis

Before running the Claude review, the code-reviewer now runs a full
pre-review analysis pipeline that posts its findings as a separate
comment and feeds structured context into the Claude prompt:

| Module | What it does |
|--------|--------------|
| `risk.py` | Scores PR risk (low/medium/high) from size, touched paths, sensitive files (auth, migrations, deps, secrets) |
| `scope.py` | Detects scope drift when a PR touches unrelated top-level dirs without a broad-refactor title |
| `test_gaps.py` | Parses the diff, extracts new public symbols (Python, Go, JS/TS, Swift, PHP, Vue), flags any without a test reference |
| `related_prs.py` | Finds other open PRs touching the same files to warn of merge conflicts |
| `context_loader.py` | Blast radius — uses ripgrep to find callers of newly-added symbols and inlines them into the review prompt (the "Greptile trick" without the embedding pipeline) |
| `repo_config.py` | Loads optional `.ch-code-reviewer.yml` for per-repo rules, ignore_paths, and review style |
| `review_memory.py` | Per-repo `.ch-code-reviewer-memory.md` that accumulates recurring feedback patterns and feeds them into future review prompts |
| `summary.py` | 1-sentence diff summary at the top of the review |
| `title_check.py` | Flags vague PR titles (wip, fix, update) and nudges toward conventional commits |
| `findings.py` | Severity-tagged findings (BLOCKER / WARNING / NIT / INFO) sorted most important first |
| `analyzer.py` | Coordinator that runs all modules and produces a PRAnalysis with labels, body, and prompt addendum |

Labels applied to PRs: `risk:low|medium|high`, `scope:drifted`,
`tests:missing`, `review:blocker`.

**Per-repo config** (`.ch-code-reviewer.yml` at repo root, optional):

```yaml
rules:
  - "Use Realm for persistent storage"
  - "Prefer cobra over flag for CLI"
ignore_paths:
  - docs/
  - examples/
max_risk_for_auto_fix: medium
review_style: blunt
```

**Per-repo memory** (`.ch-code-reviewer-memory.md` at repo root, optional):
Just a flat markdown file of `- bullet rules`. The bot reads them into
every future review prompt. You edit them freely — the bot will never
overwrite your edits on load.

## ShipLog — weekly retrospective digest

Complements `heartbeat-weekly.sh`. Where the existing weekly digest only
reports on heartbeat-labeled PRs, ShipLog captures ALL shipped work
across every tracked repo and classifies it conventional-commit style
(feat / fix / refactor / docs / chore / ...).

```bash
python3 -m shiplog --days 7 --archive --discord      # full pipeline
python3 -m shiplog --days 14 --project crooked-finger  # single repo test
python3 -m shiplog --json                              # machine-readable
./bin/shiplog.sh --dry-run                             # stdout only
```

Output includes merged PRs grouped by category, commit counts per repo,
open PRs, open issues, and any releases cut in the window. Markdown
archive goes to `~/heartbeat-reports/shiplog-YYYY-MM-DD.md`; Discord
message is a compact version with top-3 PRs per project.

## claude-burn — local usage telemetry

Go CLI tool (no external deps) that reads Claude Code session JSONL
files from `~/.claude/projects/` and prints a formatted report: usage
by project, by model, and by day. Answers "where does my quota
actually go?"

```bash
cd tools/claude-burn && go build -o ~/bin/claude-burn ./cmd/claude-burn
claude-burn --days 7 --top 10
```

See `tools/claude-burn/README.md` for details.

## Dashboards

**Primary: GitHub Projects board** — <https://github.com/users/ChandlerHardy/projects/1>

The nightly `heartbeat.sh` auto-adds discovered issues and their PRs to
this board via GraphQL. If the board ever looks empty, the most likely
cause is that the `gh` token on OCI is missing the `project` scope:

```bash
# On OCI:
gh auth refresh -h github.com -s project

# To backfill historical items from anywhere with a properly-scoped gh:
./bin/heartbeat-backfill-projects.sh
```

**Secondary: local Go dashboard** (`tools/heartbeat-dashboard/`)

A read-only web UI for viewing things the GitHub Projects board can't
show: the `heartbeat.json` config, local run history from
`~/heartbeat-reports/history.jsonl`, and per-project filesystem state.
Runs on `http://127.0.0.1:8765`.

```bash
cd tools/heartbeat-dashboard
go build -o ~/bin/heartbeat-dashboard ./cmd/heartbeat-dashboard
heartbeat-dashboard
```

## Editing the heartbeat config

`bin/heartbeat-config.sh` provides safe list/add/remove operations on
`heartbeat.json` without hand-editing JSON:

```bash
heartbeat-config list
heartbeat-config add gnomestead ~/workspaces/gnomestead/gnomestead-ios
heartbeat-config remove old-project
heartbeat-config show
```

## Unified CLI (`hb`)

`bin/hb` is a thin dispatcher that wraps every heartbeat subsystem under
one command so you don't have to remember which script does what.

```bash
hb status                      # open PRs and issues across all repos
hb projects                    # list tracked projects
hb runs --last 20              # recent heartbeat run history
hb board                       # open the GH Projects board
hb dashboard                   # launch local read-only web UI
hb add gnomestead ~/path       # edit heartbeat.json
hb brainstorm                  # propose NEW project ideas via Claude
hb backfill                    # populate the GH Projects board
hb shiplog --days 7            # weekly retrospective digest
hb burn --days 7               # Claude Code usage telemetry
hb help                        # full command reference
```

`hb` does **not** require Claude Code or the Discord bot to be running —
it's a pure shell dispatcher. For Claude-Code-integrated flows (implement,
interview, build), use the `/heartbeat` slash command which uses subagents.

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

## Review triggers

All webhook-driven Seneschal runs are **off by default**. The bot only
reviews PRs you explicitly ask it to.

**Invocation paths:**

| Trigger | How to fire | Pipeline |
|---|---|---|
| PR comment | Comment exactly `/seneschal review` on its own line in a PR | Single-pass: analyzer + one `claude -p --max-turns 25` call → formal review posted as `seneschal-cr[bot]` |
| Operator CLI | Run `/seneschal-review <N>` in a local `claude -p` session | Full multi-persona: six reviewer personas in parallel + state-file aggregation + posted via `~/bin/seneschal-post` |

**Comment-trigger constraints:**

- Must be on its own line (regex is `^/seneschal\s+review\s*$` under `re.MULTILINE`).
- Case-sensitive — `/Seneschal review` does not match.
- Author must be in `COMMENT_TRIGGER_AUTHORS` (currently `{"ChandlerHardy"}`).
- Any `*[bot]` author is dropped before matching, so the bot's own review bodies cannot accidentally retrigger themselves.

**Kill switches and opt-ins:**

| Flag | Where | Default | Effect |
|---|---|---|---|
| `SENESCHAL_AUTOREVIEW` | systemd `Environment=` | unset (off) | When set to `1`/`true`/`yes`, re-enables automatic review on `pull_request.opened` and `pull_request.synchronize` events |
| `config.auto_fix` | `.ch-code-reviewer.yml` in the reviewed repo | False (code default) | When true, a REQUEST_CHANGES verdict from the single-pass path triggers a `claude -p --max-turns 40` auto-fix run that commits and pushes a fix. Opt in per repo only |
| `AUTOFIX_TRUSTED_AUTHORS` | `app.py` constant | `{"ChandlerHardy"}` | Additional allowlist guarding `auto_fix`: even when a repo opts in, only PRs authored by these logins can trigger the fix loop |

**Deliberately designed to fail safe**: a fresh install of the webhook
handler with an empty environment will never auto-review and never
auto-fix. Every expensive path requires an explicit opt-in that has to
be typed somewhere.

## MCP Tools (OCI)

All `claude -p` sessions have access to:
- **jcodemunch** — code analysis, symbol search, file outlines
- **context7** — framework documentation (Next.js, FastAPI, etc.)
- **codebase-memory-mcp** — architecture memory, code search

## Safety

- Branch-only (never commits to main directly)
- SKIP escape hatch — if implementation is too complex, claude writes SKIP and reverts
- Max 2 quick wins per project per run
- Seneschal auto-review and auto-fix **both default to off**; see "Review triggers" above
- When opted in, max 3 auto-fix attempts per PR (tracked via `[auto-fix` comment prefix)
- Auto-fix gated on `AUTOFIX_TRUSTED_AUTHORS` allowlist in addition to the per-repo `auto_fix` opt-in
- Skips stale repos (no commits in 14 days)
- Idempotent (won't re-create existing branches)
- `--max-turns` on all claude invocations (25-60 depending on task)
- All invocations use `claude -p --dangerously-skip-permissions`

## Deploy

```bash
./install.sh oci    # Deploy scripts + seneschal webhook to OCI
```

Deploys `bin/*.sh`, the Seneschal Python package (`code-reviewer/*.py` →
`~/seneschal/`), the reviewer persona definitions + `/seneschal-review`
slash command into `~/.claude/`, the ShipLog package, and the
`seneschal.service` systemd unit. Secrets (PEM, webhook HMAC) must be
in place at `~/seneschal/ch-code-reviewer.pem` and
`~/seneschal/webhook-secret.txt` before first run.

To re-enable automatic review on PR open/push: uncomment the
`Environment=SENESCHAL_AUTOREVIEW=1` line in
`systemd/seneschal.service`, redeploy, and restart. To enable auto-fix
on a specific repo, drop an `.ch-code-reviewer.yml` at its root with
`auto_fix: true`.
