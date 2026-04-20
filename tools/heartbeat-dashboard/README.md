# heartbeat-dashboard

Local web UI for the heartbeat automation system.

> **Note:** For work tracking (issues, PRs, status), the primary
> dashboard is the [GitHub Projects board][board]. This Go dashboard
> exists only to show things GitHub Projects can't — the
> `heartbeat.json` config, local run history from
> `~/heartbeat-reports/history.jsonl`, and per-project filesystem state.

[board]: https://github.com/users/ChandlerHardy/projects/1

## What it shows

- **Overview** — tracked projects, recent run stats, last run timestamp
- **Projects** — each configured project's local state + live GitHub
  issue/PR counts (fetched via `gh`)
- **Runs** — chronological list of heartbeat runs from `history.jsonl`
- **Config** — current `heartbeat.json` contents, redacted Discord webhook

## Install

```bash
cd tools/heartbeat-dashboard
go build -o ~/bin/heartbeat-dashboard ./cmd/heartbeat-dashboard
```

Zero external Go dependencies (stdlib only).

## Run

```bash
heartbeat-dashboard                            # auto-detects ~/etc/heartbeat.json
heartbeat-dashboard --config ./test.json       # custom config
heartbeat-dashboard --port 9000                # custom port (default 8765)
heartbeat-dashboard --history ./runs.jsonl     # custom history file
```

Then open http://127.0.0.1:8765 in a browser.

## Design choices

- **Read-only.** The dashboard deliberately does not write to config
  files. Use `bin/heartbeat-config.sh` to edit `heartbeat.json`, or edit
  it directly with a text editor, then hit the Refresh button on any
  page.
- **Localhost only.** Default bind is `127.0.0.1` — no auth, no TLS, no
  CORS. This is a dev tool, not a production service.
- **Graceful degradation.** If `gh` CLI is unauthenticated or offline,
  issue/PR counts show as `-` but the rest of the dashboard still works.
- **Embedded templates and CSS.** Single binary, no runtime file
  dependencies beyond the config file(s) you point at.

## When to use this vs the GitHub Projects board

| Use Projects board when | Use this dashboard when |
|-------------------------|-------------------------|
| Viewing heartbeat issues/PRs by status | Viewing the heartbeat.json config |
| Triaging discovered work | Inspecting run history / errors |
| Planning which items to implement | Editing the config via CLI |
| Sharing state with others | Per-project local state checks |

## Backfilling the Projects board

If the board looks empty, the nightly heartbeat.sh has been failing to
add items (usually because the OCI `gh` token is missing the `project`
scope). Fix:

```bash
# On OCI:
gh auth refresh -h github.com -s project

# From your Mac, to backfill historical items:
./bin/heartbeat-backfill-projects.sh
```
