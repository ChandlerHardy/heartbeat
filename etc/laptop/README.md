# etc/laptop — the MacBook's scheduled-job estate

Installed agents (`~/Library/LaunchAgents/`), watched by `dev.sync-watch` (daily
09:30, alerts Discord only on problems — silence means healthy):

| Agent | Schedule | Freshness signal |
|---|---|---|
| dev.ferdinand-sync | daily 7:00 | ~/heartbeat-reports/ferdinand-sync.log |
| dev.runbook | daily 9:00 | ~/.claude/dev-docs/runbook-auto.log (its REAL log; the launchd one is an empty decoy) |
| dev.infra-sync | daily 9:15 | ~/.claude/dev-docs/infra-sync.log |
| dev.commit-pulse.eod | daily 16:00 | ~/heartbeat-reports/commit-pulse-eod.log |
| dev.seneschal.rsync | hourly | run-stamp ~/heartbeat-reports/stamps/dev.seneschal.rsync (rsync is quiet on success; plist wraps it to touch the stamp) |
| dev.workflow.github-sync | Sundays 10:00 | ~/heartbeat-reports/dev-workflow-sync.log |

2026-08-31: the three /tmp log paths moved to ~/heartbeat-reports/ (macOS purges
/tmp); sync-watch.sh + its plist added after the OCI standup cron was found dead
for 11 weeks with nothing watching it. Webhook lives at
~/.config/heartbeat/discord-webhook (0600, never committed).

Install: `cp sync-watch.sh ~/bin/ && cp dev.sync-watch.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/dev.sync-watch.plist`
