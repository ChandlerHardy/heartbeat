#!/bin/bash
# Tests for log_run() and summarize_history() functions
set -euo pipefail

PASS=0
FAIL=0
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label — expected '$expected', got '$actual'"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label — expected to contain '$needle' in: $haystack"
    FAIL=$((FAIL + 1))
  fi
}

# Source only the functions we need from heartbeat.sh
# We stub out external deps (jq config reads, discord, etc.) by defining
# just the log_run and summarize_history functions directly.
# This avoids needing the full heartbeat environment.

HISTORY_FILE="${TMPDIR_TEST}/history.jsonl"

# --- Source the functions ---
# We extract log_run and summarize_history from the script.
# Since heartbeat.sh has set -euo pipefail and reads CONFIG at top level,
# we define the functions inline here matching what we will implement.

log() { echo "$1" >&2; }

# This is what log_run SHOULD look like — we source the real script after
# implementing it. For now, test against the expected interface.

# We will source the real functions by extracting them.
# For the RED phase, define stubs that will fail:

log_run() { :; }
summarize_history() { :; }

# Try sourcing the real functions file if it exists
FUNC_FILE="$(cd "$(dirname "$0")/../bin" && pwd)/heartbeat-lib.sh"
if [ -f "$FUNC_FILE" ]; then
  source "$FUNC_FILE"
fi

echo "=== Test: log_run writes valid JSONL ==="

log_run "test-project" 5 3 1 2 '["timeout on test run"]' "$HISTORY_FILE"

if [ ! -f "$HISTORY_FILE" ]; then
  echo "  FAIL: history.jsonl was not created"
  FAIL=$((FAIL + 1))
else
  LINE_COUNT=$(wc -l < "$HISTORY_FILE" | tr -d ' ')
  assert_eq "file has exactly 1 line" "1" "$LINE_COUNT"

  # Validate JSON structure
  VALID_JSON=$(jq -e '.' "$HISTORY_FILE" > /dev/null 2>&1 && echo "yes" || echo "no")
  assert_eq "line is valid JSON" "yes" "$VALID_JSON"

  # Check fields
  PROJECT=$(jq -r '.project' "$HISTORY_FILE")
  assert_eq "project field" "test-project" "$PROJECT"

  FINDINGS=$(jq -r '.findings_count' "$HISTORY_FILE")
  assert_eq "findings_count field" "5" "$FINDINGS"

  IMPLEMENTED=$(jq -r '.implemented_count' "$HISTORY_FILE")
  assert_eq "implemented_count field" "3" "$IMPLEMENTED"

  SKIPPED=$(jq -r '.skipped_count' "$HISTORY_FILE")
  assert_eq "skipped_count field" "1" "$SKIPPED"

  PRS=$(jq -r '.prs_created' "$HISTORY_FILE")
  assert_eq "prs_created field" "2" "$PRS"

  ERRORS=$(jq -r '.errors | length' "$HISTORY_FILE")
  assert_eq "errors array has 1 entry" "1" "$ERRORS"

  ERROR_MSG=$(jq -r '.errors[0]' "$HISTORY_FILE")
  assert_eq "error message content" "timeout on test run" "$ERROR_MSG"

  # Timestamp should be ISO-8601-ish
  TS=$(jq -r '.timestamp' "$HISTORY_FILE")
  TS_MATCH=$(echo "$TS" | grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2}' || echo "0")
  assert_eq "timestamp looks like a date" "1" "$TS_MATCH"
fi

echo ""
echo "=== Test: log_run appends (does not overwrite) ==="

log_run "second-project" 2 1 0 1 '[]' "$HISTORY_FILE"

LINE_COUNT=$(wc -l < "$HISTORY_FILE" | tr -d ' ')
assert_eq "file has 2 lines after second call" "2" "$LINE_COUNT"

SECOND_PROJECT=$(tail -1 "$HISTORY_FILE" | jq -r '.project')
assert_eq "second line has correct project" "second-project" "$SECOND_PROJECT"

echo ""
echo "=== Test: log_run with empty errors ==="

EMPTY_ERR_FILE="${TMPDIR_TEST}/empty-err.jsonl"
log_run "clean-project" 3 3 0 1 '[]' "$EMPTY_ERR_FILE"

ERRORS_LEN=$(jq -r '.errors | length' "$EMPTY_ERR_FILE")
assert_eq "empty errors array" "0" "$ERRORS_LEN"

echo ""
echo "=== Test: summarize_history output ==="

SUMMARY_FILE="${TMPDIR_TEST}/summary-test.jsonl"
# Write 3 lines simulating 3 runs
for i in 1 2 3; do
  log_run "proj-${i}" "$((i * 2))" "$i" "$((i - 1))" "$i" '[]' "$SUMMARY_FILE"
done

SUMMARY=$(summarize_history "$SUMMARY_FILE" 3)

# Should contain aggregate numbers: findings=2+4+6=12, implemented=1+2+3=6, prs=1+2+3=6
assert_contains "summary mentions findings total" "12 findings" "$SUMMARY"
assert_contains "summary mentions implemented total" "6 implemented" "$SUMMARY"
assert_contains "summary mentions prs total" "6 PRs" "$SUMMARY"
assert_contains "summary mentions run count" "3 runs" "$SUMMARY"

echo ""
echo "=== Test: summarize_history with limit ==="

LIMIT_FILE="${TMPDIR_TEST}/limit-test.jsonl"
for i in 1 2 3 4 5; do
  log_run "proj" "$((i * 10))" "$i" 0 "$i" '[]' "$LIMIT_FILE"
done

# Only last 2 runs: findings=40+50=90, implemented=4+5=9, prs=4+5=9
SUMMARY2=$(summarize_history "$LIMIT_FILE" 2)
assert_contains "limited summary run count" "2 runs" "$SUMMARY2"
assert_contains "limited summary findings" "90 findings" "$SUMMARY2"

echo ""
echo "=== Test: summarize_history counts errors ==="

ERR_FILE="${TMPDIR_TEST}/err-test.jsonl"
log_run "proj" 5 2 1 1 '["err1","err2"]' "$ERR_FILE"
log_run "proj" 3 1 0 1 '["err3"]' "$ERR_FILE"

SUMMARY3=$(summarize_history "$ERR_FILE" 10)
assert_contains "error count in summary" "3 errors" "$SUMMARY3"

echo ""
echo "=== Test: gh_as_seneschal is defined ==="

if declare -f gh_as_seneschal > /dev/null; then
  echo "  PASS: gh_as_seneschal function exists"
  PASS=$((PASS + 1))
else
  echo "  FAIL: gh_as_seneschal function not defined"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Test: gh_as_seneschal falls back to user gh when token helper fails ==="

# Point SENESCHAL_TOKEN_HELPER at a script that always fails so the helper
# falls back to the user's gh auth path. We stub `gh` to record how it was
# called.
SENESCHAL_TOKEN_HELPER="false"
gh() {
  if [ -n "${GH_TOKEN:-}" ]; then
    echo "called-with-token"
  else
    echo "called-without-token"
  fi
}

GH_TOKEN="" RESULT=$(gh_as_seneschal "owner/repo" issue create --title test)
assert_eq "fallback path used when minting fails" "called-without-token" "$RESULT"

unset -f gh

echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
