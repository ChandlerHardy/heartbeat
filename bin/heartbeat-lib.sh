#!/bin/bash
# heartbeat-lib.sh — shared functions for heartbeat scripts
# Sourced by heartbeat.sh and tests. No side effects on source.

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
