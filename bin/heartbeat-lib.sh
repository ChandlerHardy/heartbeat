#!/bin/bash
# heartbeat-lib.sh — shared functions for heartbeat scripts
# Sourced by heartbeat.sh, heartbeat-weekly.sh, heartbeat-cleanup.sh, and
# tests. No side effects on source.

# get_github_repo — resolve owner/name from a local git clone's origin.
# Used by every heartbeat script; previously duplicated in three places.
get_github_repo() {
  local dir="$1"
  git -C "$dir" remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||'
}

# send_discord — post a Discord message. Expects $DISCORD_WEBHOOK in the
# caller's environment. Truncates to under the 2000-char Discord limit.
# Previously duplicated verbatim in heartbeat.sh and heartbeat-weekly.sh.
send_discord() {
  local message="$1"
  printf '%s' "$message" | DISCORD_WEBHOOK="${DISCORD_WEBHOOK:-}" python3 -c '
import json, sys, os, urllib.request
content = sys.stdin.read()
if not content.strip():
    exit(0)
webhook = os.environ.get("DISCORD_WEBHOOK") or ""
if not webhook:
    sys.stderr.write("send_discord: DISCORD_WEBHOOK not set\n")
    exit(0)
if len(content) > 1990:
    content = content[:1987] + "..."
data = json.dumps({"content": content}).encode()
req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", "User-Agent": "HeartbeatBot/1.0"})
urllib.request.urlopen(req)
'
}

# SENESCHAL_TOKEN_HELPER: path to the Python helper that mints a GitHub
# App installation token for the Seneschal bot. Override via env var for
# testing.
SENESCHAL_TOKEN_HELPER="${SENESCHAL_TOKEN_HELPER:-$HOME/seneschal/venv/bin/python $HOME/seneschal/seneschal_token.py}"

# gh_as_seneschal — run `gh` with a Seneschal installation token for the
# given repo, falling back to the user's normal gh auth if minting fails
# (App not installed on that repo, missing PEM, network error). The
# fallback keeps heartbeat working on repos where Seneschal isn't
# installed yet.
#
# Usage:
#   gh_as_seneschal owner/repo issue create --repo owner/repo --title "..." ...
#   gh_as_seneschal owner/repo pr create --title "..." ...
#
# The repo is passed once for token minting; the rest of the args are
# forwarded to `gh` unchanged.
gh_as_seneschal() {
  local repo="$1"
  shift
  local token=""
  if [ -n "$repo" ]; then
    token=$($SENESCHAL_TOKEN_HELPER "$repo" 2>/dev/null || true)
  fi
  if [ -n "$token" ]; then
    GH_TOKEN="$token" gh "$@"
  else
    gh "$@"
  fi
}

# log_run — append a JSON line to the history file
# Usage: log_run <project> <findings> <implemented> <skipped> <prs> <errors_json> <history_file>
log_run() {
  local project="$1"
  local findings_count="$2"
  local implemented_count="$3"
  local skipped_count="$4"
  local prs_created="$5"
  local errors_json="$6"
  local history_file="$7"

  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

  # schema_version: bump if any field is renamed or its type changes.
  # Reader contract: tools/heartbeat-dashboard/internal/config/history.go
  # and bin/heartbeat-lib.sh::summarize_history must agree on field names.
  local line
  line=$(jq -cn \
    --arg ts "$timestamp" \
    --arg proj "$project" \
    --argjson findings "$findings_count" \
    --argjson implemented "$implemented_count" \
    --argjson skipped "$skipped_count" \
    --argjson prs "$prs_created" \
    --argjson errors "$errors_json" \
    '{
      schema_version: 1,
      timestamp: $ts,
      project: $proj,
      findings_count: $findings,
      implemented_count: $implemented,
      skipped_count: $skipped,
      prs_created: $prs,
      errors: $errors
    }')

  echo "$line" >> "$history_file"
}

# summarize_history — read the last N lines of a JSONL file and print a summary
# Usage: summarize_history <history_file> <last_n>
summarize_history() {
  local history_file="$1"
  local last_n="${2:-7}"

  if [ ! -f "$history_file" ]; then
    echo "No history found at $history_file"
    return
  fi

  local data
  data=$(tail -n "$last_n" "$history_file")

  local total_runs total_findings total_implemented total_skipped total_prs total_errors
  total_runs=$(echo "$data" | wc -l | tr -d ' ')
  total_findings=$(echo "$data" | jq -s '[.[].findings_count] | add // 0')
  total_implemented=$(echo "$data" | jq -s '[.[].implemented_count] | add // 0')
  total_skipped=$(echo "$data" | jq -s '[.[].skipped_count] | add // 0')
  total_prs=$(echo "$data" | jq -s '[.[].prs_created] | add // 0')
  total_errors=$(echo "$data" | jq -s '[.[].errors | length] | add // 0')

  echo "Last ${total_runs} runs: ${total_findings} findings, ${total_implemented} implemented, ${total_skipped} skipped, ${total_prs} PRs created, ${total_errors} errors"
}
