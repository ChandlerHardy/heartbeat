# claude-burn

Local Claude Code usage telemetry. Reads session JSONL files from
`~/.claude/projects/` and prints a formatted report showing where your
quota actually goes.

## Why

Claude Code's built-in usage view tells you *how much* you've used this
session or this week. It doesn't tell you *where*. claude-burn answers:

- Which projects are burning the most tokens?
- Which model am I actually using (Opus vs Sonnet vs Haiku)?
- How good is my cache hit rate?
- Which days burned the most quota?

## Install

```bash
cd tools/claude-burn
go build -o ~/bin/claude-burn ./cmd/claude-burn
```

Or run directly from source:

```bash
go run ./cmd/claude-burn --days 7
```

## Usage

```
claude-burn [flags]

Flags:
  -root string
        Claude Code projects root (default: ~/.claude/projects)
  -days int
        Lookback window in days (default 7, 0 = unbounded)
  -top int
        Show top N projects and last N days (default 15)
  -project string
        Filter to one project by directory name (e.g. "gnomestead")
  -no-days
        Hide the daily breakdown
  -ascii
        ASCII-only output
  -version
        Print version and exit
```

## Example output

```
claude-burn - Claude Code usage report
=============================================
Window:       2026-04-05 18:03  ->  2026-04-12 16:13
Messages:     5032
Projects:     7
Models:       3

Totals
---------------------------------------------
  Input tokens:                   68.5k
  Output tokens:                  2.53M
  Cache creation:                24.86M
  Cache read:                   985.04M
  Total tokens:                1012.50M
  Billable (excl read):          27.46M
  Cache hit rate:                 97.3%

By model
---------------------------------------------
MODEL              MESSAGES  BILLABLE  TOTAL
claude-opus-4-6    5025      27.38M    1012.43M
claude-sonnet-4-6  2         70.8k     70.8k

By project
---------------------------------------------
PROJECT                SESSIONS  MESSAGES  BILLABLE  LAST ACTIVE
~/workspaces/example-a  3         2215      13.60M    1d ago
~/repos                7         1727      8.17M     1m ago
~/repos/nightwork      1         379       2.76M     3d ago
~/repos/career-ops     1         244       1.38M     4d ago

Last 14 days
---------------------------------------------
DAY         BILLABLE  BAR
2026-04-05  1.35M     ####
2026-04-06  3.55M     ###########
2026-04-07  6.86M     ######################
2026-04-08  2.49M     ########
2026-04-09  7.63M     #########################
```

## How tokens are counted

Each `assistant` message in a Claude Code session log has a `usage` dict
with four buckets: `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, and `cache_read_input_tokens`.

claude-burn reports two totals:

- **Total** — sum of all four buckets (raw tokens processed).
- **Billable** — input + output + cache creation. Excludes cache reads,
  which are cheap/free on most plans and are a better "actual burn"
  estimate for rate-limit visibility.

## Cache hit rate

Cache hit rate = cache_read_tokens / total_tokens.

A healthy session with prompt caching enabled typically runs >80%. If
yours is lower, you're re-sending large prompts unnecessarily.

## Project directory decoding

Claude Code encodes project paths by replacing slashes with hyphens, so
`/Users/you/repos/my-tool` becomes `-Users-you-repos-my-tool`. This is
ambiguous when repo names themselves contain hyphens.

claude-burn tries progressively more conservative splits (longest prefix
that actually exists on disk) to recover the real path.
