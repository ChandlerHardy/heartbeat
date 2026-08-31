#!/bin/bash
# sync-watch — watchdog for the laptop's scheduled-job estate.
#
# Inverse of worksweep's never-silence digest: this posts to Discord ONLY when
# something is wrong (stale log or non-zero last exit). Silence means healthy.
# Origin (2026-08-31): the OCI standup cron sat disabled for 11 weeks unnoticed,
# and ferdinand-sync had no watcher and logged to purge-prone /tmp — success and
# absence looked identical from the outside.
#
# Each entry: launchd label | freshness file | max age in hours (threshold sized
# to the job's schedule plus slack for a sleeping laptop). The freshness file is
# the job's own log for jobs that write on success, or a run-stamp under
# $H/stamps/ for quiet-on-success jobs (seneschal.rsync writes ONLY errors, and
# runbook's launchd log is an empty decoy — its real log is runbook-auto.log;
# both burned the first version of this watcher with false alarms, 2026-08-31).
set -u

H="$HOME/heartbeat-reports"
WEBHOOK_FILE="$HOME/.config/heartbeat/discord-webhook"
SELF_LOG="$H/sync-watch.log"

JOBS=(
  "dev.ferdinand-sync|$H/ferdinand-sync.log|30"
  "dev.infra-sync|$HOME/.claude/dev-docs/infra-sync.log|30"
  "dev.runbook|$HOME/.claude/dev-docs/runbook-auto.log|30"
  "dev.commit-pulse.eod|$H/commit-pulse-eod.log|30"
  "dev.seneschal.rsync|$H/stamps/dev.seneschal.rsync|6"
  "dev.workflow.github-sync|$H/dev-workflow-sync.log|192"
)

problems=()
now=$(date +%s)

for entry in "${JOBS[@]}"; do
  IFS='|' read -r label log max_h <<<"$entry"

  # launchctl list <label> prints a plist; LastExitStatus is absent until the
  # job has run this boot, which is fine — freshness covers the never-ran case.
  if ! listing=$(launchctl list "$label" 2>/dev/null); then
    problems+=("$label: NOT LOADED (launchctl has no such agent)")
    continue
  fi
  exit_status=$(printf '%s' "$listing" | awk -F'= |;' '/LastExitStatus/ {gsub(/[^0-9-]/,"",$2); print $2}')
  if [ -n "${exit_status:-}" ] && [ "$exit_status" != "0" ]; then
    problems+=("$label: last exit $exit_status")
  fi

  if [ ! -f "$log" ]; then
    problems+=("$label: log missing ($log)")
    continue
  fi
  mtime=$(stat -f %m "$log")
  age_h=$(( (now - mtime) / 3600 ))
  if [ "$age_h" -gt "$max_h" ]; then
    problems+=("$label: log stale ${age_h}h (threshold ${max_h}h)")
  fi
done

ts=$(date '+%Y-%m-%d %H:%M')
if [ ${#problems[@]} -eq 0 ]; then
  echo "$ts: all ${#JOBS[@]} jobs healthy" >> "$SELF_LOG"
  exit 0
fi

echo "$ts: ${#problems[@]} problem(s): ${problems[*]}" >> "$SELF_LOG"

body="⚠️ **sync-watch (laptop)** — ${#problems[@]} scheduled job(s) unhealthy:"
for p in "${problems[@]}"; do body="$body
• $p"; done

if [ -r "$WEBHOOK_FILE" ]; then
  payload=$(python3 -c 'import json,sys; print(json.dumps({"content": sys.stdin.read()}))' <<<"$body")
  curl -s -m 15 -o /dev/null -H 'Content-Type: application/json' -d "$payload" "$(cat "$WEBHOOK_FILE")" \
    || echo "$ts: DISCORD POST FAILED" >> "$SELF_LOG"
else
  echo "$ts: no webhook file at $WEBHOOK_FILE — alert not delivered" >> "$SELF_LOG"
fi
