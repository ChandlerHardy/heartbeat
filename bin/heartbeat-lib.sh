#!/bin/bash
# heartbeat-lib.sh — shared functions for heartbeat scripts
# Sourced by heartbeat.sh, heartbeat-weekly.sh, heartbeat-cleanup.sh, and
# tests. No side effects on source.

# GitHub Projects board identifiers. Duplicated between heartbeat.sh and
# heartbeat-backfill-projects.sh until they both sourced this lib;
# centralize here so an ID rotation only needs one edit. If the board is
# deleted/rotated, refresh values via:
#   gh api graphql -f query='query{ user(login:"ChandlerHardy"){ projectV2(number:1){ id } } }'
PROJECT_BOARD_ID="PVT_kwHOAVEBTs4BT23z"
PROJECT_STATUS_FIELD_ID="PVTSSF_lAHOAVEBTs4BT23zzhBC39Y"
STATUS_DISCOVERED="30d3a08c"
STATUS_TRIAGED="4b2b540d"
STATUS_IMPLEMENTED="da2d3b98"
STATUS_MERGED="76df93ef"
STATUS_REJECTED="dab08eb6"

# get_github_repo — resolve owner/name from a local git clone's origin.
# Used by every heartbeat script; previously duplicated in three places.
get_github_repo() {
  local dir="$1"
  git -C "$dir" remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||'
}

# sha256_prefix — read stdin, emit the first 8 hex chars of its SHA-256.
# Portable across OCI Oracle Linux (sha256sum, GNU coreutils) and macOS
# (shasum, BSD); falls back to openssl which is present almost everywhere.
# Previously heartbeat.sh hardcoded `shasum -a 256` which silently failed on
# OCI — `set -euo pipefail` aborted the caller before the sentinel was
# written, so ensure_labels re-fired 56 gh-label API calls every night.
sha256_prefix() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print substr($1,1,8)}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print substr($1,1,8)}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 | awk '{print substr($NF,1,8)}'
  else
    echo "sha256_prefix: no sha256 tool (sha256sum/shasum/openssl) available" >&2
    return 1
  fi
}

# sanitize_lt — read stdin, replace every `<` with `‹` (U+2039) on stdout.
# Used to neutralize closing-tag breakout in prompt data blocks
# (<existing_issues>, <product_context>, <portfolio>, <finding>). POSIX `tr`
# is byte-oriented and produces truncated UTF-8 when set2 is multibyte
# (replaces `<` with only the first byte of `‹`), which breaks on OCI under
# LC_ALL=C. `sed` passes replacement bytes through verbatim regardless of
# locale — verified on both macOS and LC_ALL=C Linux.
sanitize_lt() {
  sed 's/</‹/g'
}

# neutralize_mentions — read stdin, replace `@` (U+0040) with `＠` (U+FF20,
# fullwidth commercial at) on stdout. Visually indistinguishable at most
# font sizes but NOT interpreted by GitHub's @-mention parser, so an
# attacker-controlled issue title can't pipe unwitting users into a
# notification tsunami via an auto-PR title. Same locale-safety reasoning
# as sanitize_lt — use sed, not tr.
neutralize_mentions() {
  sed 's/@/＠/g'
}

# neutralize_backticks — read stdin, replace `` ` `` (U+0060) with `｀`
# (U+FF40, fullwidth grave accent). The auto-PR body wraps user-derived
# fields in ```text fences so markdown doesn't render attacker content —
# but fences are closable. A finding whose `what`/`why` contains three
# backticks alone closes the fence and re-enables markdown rendering
# downstream. Neutralizing every backtick means no content can close the
# fence. Same sed/locale reasoning as the other sanitizers.
neutralize_backticks() {
  sed 's/`/｀/g'
}

# send_discord — post a Discord message. Expects $DISCORD_WEBHOOK in the
# caller's environment. Truncates to under the 2000-char Discord limit.
# Previously duplicated verbatim in heartbeat.sh and heartbeat-weekly.sh.
#
# The truncation cap here matches shiplog/formatter.py's DISCORD_MAX_CHARS
# (1900, leaving ~100 chars of headroom for markdown mention expansion).
# Keep the two values in sync — a mismatched cap means a message that fits
# one send path gets silently truncated at a different boundary on another.
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
# Discord enforces a 2000-character limit on the message content, but the
# wire payload is UTF-8 bytes — Python str length counts codepoints, so an
# emoji-heavy message at 1900 codepoints can be ~5700 bytes after encoding
# and get rejected with a 400. Truncate at the BYTE level, rewinding any
# partial multibyte tail, so the decoded payload is always valid UTF-8 and
# always fits the wire limit.
MAX_BYTES = 1900
encoded = content.encode("utf-8")
if len(encoded) > MAX_BYTES:
    cut = encoded[:MAX_BYTES - 3]  # reserve 3 bytes for the ellipsis
    # Rewind any continuation-byte tail, then drop an unaccompanied
    # multibyte leader so the result ends on a codepoint boundary.
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    if cut and cut[-1] >= 0x80:
        cut = cut[:-1]
    content = cut.decode("utf-8") + "…"
data = json.dumps({"content": content}).encode()
req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", "User-Agent": "HeartbeatBot/1.0"})
urllib.request.urlopen(req)
'
}

# SENESCHAL_TOKEN_HELPER: command that mints a GitHub App installation token
# for the Seneschal bot, printing the token to stdout. Set by the operator to
# a full command line (e.g. "/opt/seneschal/venv/bin/python /opt/seneschal/
# seneschal_token.py"); unset means "no bot identity, use the operator's gh
# auth". Heartbeat must not hardcode a path into a sibling repo's deployment
# layout — seneschal lives in its own repo now and its install location is
# the operator's choice.
: "${SENESCHAL_TOKEN_HELPER:=}"

# gh_as_seneschal — run `gh` with a Seneschal installation token for the
# given repo. Behavior depends on SENESCHAL_TOKEN_HELPER:
#   - unset: run as plain `gh` (operator identity) — normal for repos where
#     Seneschal isn't installed or deployments that don't use the bot at all.
#   - set but mint fails (App not installed, PEM missing, network error):
#     warn to stderr and fall back to `gh`, so the operator notices an
#     unexpected identity downgrade instead of silently authoring bot-labeled
#     issues under their personal token.
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
  if [ -n "$repo" ] && [ -n "$SENESCHAL_TOKEN_HELPER" ]; then
    # shellcheck disable=SC2086 # helper is an operator-provided command line
    token=$($SENESCHAL_TOKEN_HELPER "$repo" 2>/dev/null || true)
    if [ -z "$token" ]; then
      printf 'gh_as_seneschal: SENESCHAL_TOKEN_HELPER set but mint failed for %s; falling back to operator gh auth\n' "$repo" >&2
    fi
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

  # Serialize the append: without flock, two concurrent writers on macOS
  # could interleave mid-line and produce a corrupt JSONL record. With
  # flock the write is ordered; without it the script-level lock is the
  # only protection.
  #
  # Fail-soft: if any step in the flock path fails (EACCES after a
  # root-owned logrotate, ENOENT if the dir is being recreated, disk
  # full), log to stderr and return 0 so the caller's main loop isn't
  # aborted by `set -euo pipefail`. A missed history line is recoverable
  # at the next run; a crashed nightly mid-project is not.
  if command -v flock >/dev/null 2>&1; then
    if ! (
      exec 8>>"$history_file" 2>/dev/null || exit 1
      flock 8 2>/dev/null || exit 1
      echo "$line" >&8
    ); then
      echo "log_run: failed to append to $history_file (continuing)" >&2
    fi
  else
    echo "$line" >> "$history_file" 2>/dev/null || \
      echo "log_run: failed to append to $history_file (continuing)" >&2
  fi
  return 0
}

# summarize_history — read the last N lines of a JSONL file and print a summary.
# Default matches `hb runs` (10) so "recent runs" means the same thing across
# the end-of-night summary and the `hb runs` readout.
# Usage: summarize_history <history_file> <last_n>
summarize_history() {
  local history_file="$1"
  local last_n="${2:-10}"

  if [ ! -f "$history_file" ]; then
    echo "No history found at $history_file"
    return
  fi

  local data
  data=$(tail -n "$last_n" "$history_file")

  # Compute all five totals in one jq pass (previously forked jq five times
  # over the same input). The `add // 0` pattern handles empty runs.
  local totals
  totals=$(echo "$data" | jq -s '{
    findings: ([.[].findings_count] | add // 0),
    implemented: ([.[].implemented_count] | add // 0),
    skipped: ([.[].skipped_count] | add // 0),
    prs: ([.[].prs_created] | add // 0),
    errors: ([.[].errors | length] | add // 0)
  } | "\(.findings) \(.implemented) \(.skipped) \(.prs) \(.errors)"' -r)
  local total_runs total_findings total_implemented total_skipped total_prs total_errors
  total_runs=$(echo "$data" | wc -l | tr -d ' ')
  read -r total_findings total_implemented total_skipped total_prs total_errors <<< "$totals"

  echo "Last ${total_runs} runs: ${total_findings} findings, ${total_implemented} implemented, ${total_skipped} skipped, ${total_prs} PRs created, ${total_errors} errors"
}
