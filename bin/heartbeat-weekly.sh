#!/bin/bash
# Heartbeat Weekly Digest — summarizes the week's activity across portfolio projects
set -euo pipefail

CONFIG="$HOME/etc/heartbeat.json"
DISCORD_WEBHOOK=$(jq -r '.discord_webhook' "$CONFIG")
PROJECT_COUNT=$(jq '.projects | length' "$CONFIG")
WEEK_START=$(date -d "7 days ago" +%Y-%m-%d)
TODAY=$(date +%Y-%m-%d)
mkdir -p "$HOME/heartbeat-reports"

# Source shared helpers (get_github_repo, send_discord).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/heartbeat-lib.sh"
export DISCORD_WEBHOOK

REPORT="**📊 Heartbeat Weekly Digest** (${WEEK_START} → ${TODAY})\n\n"
TOTAL_MERGED=0
TOTAL_OPEN=0
TOTAL_ISSUES=0

for i in $(seq 0 $((PROJECT_COUNT - 1))); do
  NAME=$(jq -r ".projects[$i].name" "$CONFIG")
  PATH_DIR=$(jq -r ".projects[$i].path" "$CONFIG")

  if [ ! -d "$PATH_DIR/.git" ]; then
    continue
  fi

  REPO=$(get_github_repo "$PATH_DIR")
  if [ -z "$REPO" ]; then
    continue
  fi

  # Count merged PRs this week
  MERGED=$(gh pr list --repo "$REPO" --state merged --search "heartbeat" --json mergedAt --jq "[.[] | select(.mergedAt >= \"${WEEK_START}\")] | length" 2>/dev/null || echo "0")
  
  # Count open PRs
  OPEN=$(gh pr list --repo "$REPO" --state open --search "heartbeat" --json number --jq 'length' 2>/dev/null || echo "0")
  
  # Count open issues
  ISSUES=$(gh issue list --repo "$REPO" --state open --label "heartbeat" --json number --jq 'length' 2>/dev/null || echo "0")

  if [ "$MERGED" != "0" ] || [ "$OPEN" != "0" ] || [ "$ISSUES" != "0" ]; then
    REPORT="${REPORT}**${NAME}**: ${MERGED} merged, ${OPEN} open PRs, ${ISSUES} backlog issues\n"
  fi

  TOTAL_MERGED=$((TOTAL_MERGED + MERGED))
  TOTAL_OPEN=$((TOTAL_OPEN + OPEN))
  TOTAL_ISSUES=$((TOTAL_ISSUES + ISSUES))
done

REPORT="${REPORT}\n**Totals**: ${TOTAL_MERGED} merged | ${TOTAL_OPEN} open PRs | ${TOTAL_ISSUES} backlog items"

send_discord "$(echo -e "$REPORT")"
echo -e "$REPORT" > "$HOME/heartbeat-reports/weekly-${TODAY}.md"

echo "[heartbeat-weekly] Digest sent" >&2
