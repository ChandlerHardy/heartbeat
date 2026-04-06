#!/bin/bash
# Heartbeat Weekly Digest — summarizes the week's activity across portfolio projects
set -euo pipefail

CONFIG="$HOME/etc/heartbeat.json"
DISCORD_WEBHOOK=$(jq -r '.discord_webhook' "$CONFIG")
PROJECT_COUNT=$(jq '.projects | length' "$CONFIG")
WEEK_START=$(date -d "7 days ago" +%Y-%m-%d)
TODAY=$(date +%Y-%m-%d)
mkdir -p "$HOME/heartbeat-reports"

get_github_repo() {
  local dir="$1"
  git -C "$dir" remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||'
}

send_discord() {
  local message="$1"
  printf '%s' "$message" | DISCORD_WEBHOOK="$DISCORD_WEBHOOK" python3 -c '
import json, sys, os, urllib.request
content = sys.stdin.read()
if not content.strip():
    exit(0)
if len(content) > 1990:
    content = content[:1987] + "..."
data = json.dumps({"content": content}).encode()
req = urllib.request.Request(os.environ["DISCORD_WEBHOOK"], data=data, headers={"Content-Type": "application/json", "User-Agent": "HeartbeatBot/1.0"})
urllib.request.urlopen(req)
'
}

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
