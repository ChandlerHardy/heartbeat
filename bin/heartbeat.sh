#!/bin/bash
# Heartbeat — autonomous discovery & implementation for portfolio projects
# Runs daily via cron. Discovers improvements, implements quick wins, notifies Discord.
set -euo pipefail

CONFIG="$HOME/etc/heartbeat.json"
TODAY=$(date +%Y-%m-%d)
LOG_PREFIX="[heartbeat $TODAY]"
DISCORD_WEBHOOK=$(jq -r '.discord_webhook' "$CONFIG")
MAX_QW=$(jq -r '.max_quick_wins_per_project' "$CONFIG")
BACKLOG_THRESHOLD=$(jq -r '.backlog_threshold // 5' "$CONFIG")
PROJECT_COUNT=$(jq '.projects | length' "$CONFIG")
TMPDIR="/tmp/heartbeat-${TODAY}"
mkdir -p "$TMPDIR"
mkdir -p "$HOME/heartbeat-reports"
HISTORY_FILE="$HOME/heartbeat-reports/history.jsonl"

# Source shared functions (log_run, summarize_history)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/heartbeat-lib.sh"

# Parse --project flag to filter to a single project
FILTER_PROJECT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) FILTER_PROJECT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

log() { echo "$LOG_PREFIX $1" >&2; }

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//;s/-$//' | cut -c1-50
}

get_github_repo() {
  local dir="$1"
  git -C "$dir" remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||'
}

ensure_labels() {
  local repo="$1"
  for label in heartbeat quick-win feature tech-debt dx; do
    gh label create "$label" --repo "$repo" --force 2>/dev/null || true
  done
}

create_issue_if_new() {
  local repo="$1"
  local title="$2"
  local category="$3"
  local effort="$4"
  local impact="$5"
  local files="$6"
  local what="$7"
  local why="$8"

  local existing
  existing=$(gh issue list --repo "$repo" --search "in:title $title" --state open --json number --jq '.[0].number' 2>/dev/null)
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    echo "$existing"
    return
  fi

  local issue_body
  issue_body=$(cat <<ISSUEBODY
**Category:** $category | **Effort:** $effort | **Impact:** $impact
**Files:** $files

## What
$what

## Why
$why

---
*Discovered by Heartbeat on $TODAY*
ISSUEBODY
)

  local issue_number
  issue_number=$(gh issue create --repo "$repo" \
    --title "[heartbeat] $title" \
    --body "$issue_body" \
    --label "heartbeat,$category,discovered" \
    2>/dev/null | grep -oE '[0-9]+$')

  log "    Created issue #$issue_number: $title"

  # Add to project board with "Discovered" status
  add_to_project "repos/$repo/issues/$issue_number" "$STATUS_DISCOVERED"

  echo "$issue_number"
}

PROJECT_BOARD_ID="PVT_kwHOAVEBTs4BT23z"
PROJECT_STATUS_FIELD_ID="PVTSSF_lAHOAVEBTs4BT23zzhBC39Y"
# Status option IDs from the Heartbeat Dashboard project board
STATUS_DISCOVERED="30d3a08c"
STATUS_TRIAGED="4b2b540d"
STATUS_IMPLEMENTED="da2d3b98"
STATUS_MERGED="76df93ef"
STATUS_REJECTED="dab08eb6"

add_to_project() {
  local issue_or_pr_url="$1"
  local status_option_id="$2"

  # Get the node ID from the issue/PR URL
  local node_id
  node_id=$(gh api "$issue_or_pr_url" --jq '.node_id' 2>/dev/null)
  if [ -z "$node_id" ] || [ "$node_id" = "null" ]; then
    log "    Warning: could not get node_id for $issue_or_pr_url"
    return
  fi

  # Add item to project board
  local item_id
  item_id=$(gh api graphql -f query='mutation($projectId: ID!, $contentId: ID!) {
    addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
      item { id }
    }
  }' -f projectId="$PROJECT_BOARD_ID" -f contentId="$node_id" --jq '.data.addProjectV2ItemById.item.id' 2>/dev/null)

  if [ -z "$item_id" ] || [ "$item_id" = "null" ]; then
    log "    Warning: could not add to project board"
    return
  fi

  # Set status
  gh api graphql -f query='mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: {singleSelectOptionId: $optionId}}) {
      projectV2Item { id }
    }
  }' -f projectId="$PROJECT_BOARD_ID" -f itemId="$item_id" -f fieldId="$PROJECT_STATUS_FIELD_ID" -f optionId="$status_option_id" > /dev/null 2>&1

  log "    Added to project board (status: $status_option_id)"
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

# Write a prompt to a temp file (avoids shell quoting issues)
write_prompt() {
  local name="$1"
  local content="$2"
  local file="${TMPDIR}/${name}.txt"
  printf '%s' "$content" > "$file"
  echo "$file"
}

# Run claude -p with file-based prompts (no shell quoting issues)
run_claude() {
  local dir="$1"
  local prompt_file="$2"
  local sys_prompt_file="${3:-}"
  local max_turns="${4:-25}"
  local extra_flags="${5:-}"

  local cmd="cat '${prompt_file}' | claude -p --dangerously-skip-permissions --max-turns ${max_turns} ${extra_flags}"

  if [ -n "$sys_prompt_file" ]; then
    cmd="${cmd} --append-system-prompt \"\$(cat '${sys_prompt_file}')\""
  fi

  bash -l -c "cd '$dir' && $cmd"
}

run_discovery() {
  local name="$1"
  local dir="$2"
  local raw_file="${TMPDIR}/${name}-raw.json"
  local findings_file="${TMPDIR}/${name}-findings.json"

  cd "$dir"
  mkdir -p docs/proposals

  # Gather product context from whatever docs exist
  local product_context=""
  for ctx_file in docs/product-context.md docs/specs/*.md docs/ELUCIDATE_VISION.md docs/CHESS_TRAINER_PROJECT.md; do
    if [ -f "$ctx_file" ]; then
      product_context="${product_context}\n--- ${ctx_file} ---\n$(head -100 "$ctx_file")\n"
    fi
  done
  # Fall back to CLAUDE.md and README for product context
  if [ -z "$product_context" ]; then
    [ -f CLAUDE.md ] && product_context="$(head -50 CLAUDE.md)"
    [ -f README.md ] && product_context="${product_context}\n$(head -50 README.md)"
  fi

  local discovery_prompt
  discovery_prompt=$(write_prompt "${name}-discovery" "You are a product-aware discovery agent for the '$name' project.

Your job has TWO phases:

## Phase 1: Code Quality (2-3 findings)
Scan the codebase for bugs, tech debt, and DX improvements. These are the quick wins.

## Phase 2: Feature Discovery (2-4 findings)
Read the product context below, understand what this project IS and WHO it's for, then propose features that would meaningfully advance it. Think like a product engineer:
- What's the next feature a user would expect?
- What's missing that would make this product more complete?
- What would differentiate this from competitors?
- What existing feature could be deepened or improved?

Do NOT propose generic features (dark mode, analytics, i18n) unless they're specifically relevant.
DO propose features grounded in the product's actual purpose and user needs.

## Product Context
${product_context}

## Instructions
Use your MCP tools BEFORE making suggestions:
- Use jcodemunch to understand the codebase structure and what's already built
- Use context7 to check framework capabilities for proposed features
- Use codebase-memory-mcp to understand the architecture
- Check git log to avoid proposing work already done or in progress

Categorize each finding as: quick-win (<1hr, low risk) | feature | tech-debt | dx

CRITICAL: You MUST write your findings as valid JSON to the file: ${findings_file}
Use the Write tool or bash to write the file. Do NOT output JSON to stdout — write it to the file.

The JSON schema:
{
  \"project\": \"$name\",
  \"findings\": [
    {
      \"title\": \"Short descriptive title\",
      \"category\": \"quick-win\",
      \"effort\": \"30min\",
      \"impact\": \"low | medium | high\",
      \"files\": [\"path/to/file\"],
      \"what\": \"What to change\",
      \"why\": \"Why it matters\"
    }
  ]
}

Rules:
- Phase 1 findings: be specific about files and what needs to change
- Phase 2 findings: describe the feature clearly, list files that would need to be created or modified
- quick-win means: single file, low risk, obvious improvement
- feature means: new capability that advances the product (may need multiple files)
- Check git log and branches to avoid proposing work already in progress
- 5-7 findings total, mix of both phases, prioritized by impact
- Do NOT propose anything that duplicates these existing open issues:
${EXISTING_TITLES}")

  local discovery_system
  discovery_system=$(write_prompt "${name}-discovery-sys" "You are a product engineer, not just a code scanner. You have MCP tools — use them:
- jcodemunch: search_symbols, get_file_outline, get_file_tree, find_dead_code, get_repo_health
- context7: resolve-library-id + query-docs for current framework documentation
- codebase-memory-mcp: search_code, get_architecture, detect_changes
ALWAYS investigate the codebase AND read the product context before proposing.
For feature proposals, think about what would make this product MORE USEFUL to its target users.
Generic suggestions that ignore the actual product or code are worthless.")

  # Claude writes findings to $findings_file via Write/bash tool.
  # Capture stdout too in case it outputs JSON there instead.
  run_claude "$dir" "$discovery_prompt" "$discovery_system" 30 > "$raw_file" 2>/dev/null || true

  # Check if Claude wrote the file directly (preferred path)
  if jq -e '.findings' "$findings_file" > /dev/null 2>&1; then
    log "  Findings file written directly by Claude"
  else
    # Fallback: parse stdout for JSON
    local raw_text
    raw_text=$(cat "$raw_file" 2>/dev/null)

    # If --output-format json was used, unwrap .result
    local unwrapped
    unwrapped=$(echo "$raw_text" | jq -r '.result // empty' 2>/dev/null)
    [ -z "$unwrapped" ] && unwrapped="$raw_text"

    if echo "$unwrapped" | jq -e '.findings' > /dev/null 2>&1; then
      echo "$unwrapped" | jq '.' > "$findings_file"
    else
      log "  Discovery failed — could not parse JSON output"
      echo '{"findings":[]}' > "$findings_file"
    fi
  fi

  cp "$findings_file" "docs/proposals/${TODAY}-heartbeat.json"
  git add "docs/proposals/${TODAY}-heartbeat.json" 2>/dev/null
  git commit -m "heartbeat: discovery findings for $TODAY" --quiet 2>/dev/null || true
  git push --quiet origin HEAD 2>/dev/null || true

  echo "$findings_file"
}

implement_quick_win() {
  local dir="$1"
  local title="$2"
  local files="$3"
  local what="$4"
  local why="$5"
  local issue_num="$6"

  cd "$dir"
  local slug
  slug=$(slugify "$title")
  local branch="heartbeat/${TODAY}-${slug}"

  if git branch -a 2>/dev/null | grep -q "$branch"; then
    log "    SKIP — branch already exists: $branch"
    echo "EXISTS"
    return
  fi

  git checkout -b "$branch" 2>/dev/null

  local impl_prompt
  impl_prompt=$(write_prompt "impl-${slug}" "Implement this change, then commit with a descriptive message.

Title: $title
Files: $files
What: $what
Why: $why

Instructions:
1. BEFORE editing, use MCP tools to understand the code:
   - Use jcodemunch to find related symbols and understand file structure
   - Use context7 to check framework docs for correct API usage
   - Read surrounding files to match existing conventions
2. Make the change with minimal scope
3. Run existing tests if they exist — if any fail, revert and write SKIP to stdout
4. Commit with message format: fix: <title> (or feat: / chore: as appropriate)
5. If this is bigger than a quick win, write SKIP to stdout and exit without changes")

  local impl_system
  impl_system=$(write_prompt "impl-${slug}-sys" "You are an autonomous code implementer. You have MCP tools — USE THEM:
- jcodemunch: search_symbols, get_file_outline, find_references, get_file_tree
- context7: resolve-library-id + query-docs for framework documentation
- codebase-memory-mcp: search_code, get_architecture
RULES:
- ALWAYS search the codebase before editing. Never guess at types or signatures.
- Run existing tests before AND after changes. If any test fails, revert and write SKIP.
- Validate at system boundaries (user input, API endpoints). Trust internal code.
- Do not add error handling for impossible scenarios.
- Do not refactor surrounding code. Minimal change only.
- After committing, verify with a quick build check if possible (e.g. npx tsc --noEmit).")

  local output
  output=$(run_claude "$dir" "$impl_prompt" "$impl_system" 40 2>/dev/null)

  if echo "$output" | grep -qi "SKIP"; then
    log "    Claude SKIPped — reverting branch"
    git checkout main 2>/dev/null || git checkout master 2>/dev/null
    git branch -D "$branch" 2>/dev/null
    echo "SKIPPED"
    return
  fi

  local has_commits
  has_commits=$(git log main..HEAD --oneline -1 2>/dev/null || git log master..HEAD --oneline -1 2>/dev/null)
  if [ -z "$has_commits" ]; then
    log "    No commits made — reverting branch"
    git checkout main 2>/dev/null || git checkout master 2>/dev/null
    git branch -D "$branch" 2>/dev/null
    echo "SKIPPED"
    return
  fi

  git push origin "$branch" --quiet 2>/dev/null

  local pr_url
  pr_url=$(gh pr create \
    --title "heartbeat: $title" \
    --body "$(cat <<PRBODY
## Heartbeat Auto-Implementation

**What:** $what
**Why:** $why
**Files:** $files

---
*Automatically discovered and implemented by Heartbeat on $TODAY.*
*Review and merge at your convenience.*

Closes #$issue_num
PRBODY
)" \
    --base main \
    --head "$branch" 2>/dev/null) || log "    PR creation failed (may already exist)"

  # Update issue status to "Implemented" on project board
  local repo
  repo=$(get_github_repo "$dir")
  add_to_project "repos/$repo/issues/$issue_num" "$STATUS_IMPLEMENTED"

  # Also add the PR itself to the board
  if [ -n "$pr_url" ]; then
    local pr_num
    pr_num=$(echo "$pr_url" | grep -oE '[0-9]+$')
    add_to_project "repos/$repo/pulls/$pr_num" "$STATUS_IMPLEMENTED"
  fi

  git checkout main 2>/dev/null || git checkout master 2>/dev/null
  log "    Pushed branch + PR: $branch"
  echo "IMPLEMENTED"
}

# === MAIN LOOP ===
log "Starting heartbeat run — $PROJECT_COUNT projects configured"

send_discord "**Heartbeat Report — ${TODAY}**"

SUMMARY=""

for i in $(seq 0 $((PROJECT_COUNT - 1))); do
  NAME=$(jq -r ".projects[$i].name" "$CONFIG")
  PATH_DIR=$(jq -r ".projects[$i].path" "$CONFIG")
  STALE_DAYS=$(jq -r ".projects[$i].stale_days" "$CONFIG")

  # Skip if --project was specified and this isn't the target
  if [ -n "$FILTER_PROJECT" ] && [ "$NAME" != "$FILTER_PROJECT" ]; then
    continue
  fi

  log "Processing: $NAME"

  # Run-level counters for structured logging
  RUN_FINDINGS=0
  RUN_IMPLEMENTED=0
  RUN_SKIPPED=0
  RUN_PRS=0
  RUN_ERRORS="[]"

  if [ ! -d "$PATH_DIR/.git" ]; then
    log "  SKIP — not a git repo: $PATH_DIR"
    SUMMARY="${SUMMARY}**${NAME}** — skipped (not found)\n"
    RUN_ERRORS=$(echo "$RUN_ERRORS" | jq -c --arg e "not a git repo: $PATH_DIR" '. + [$e]')
    log_run "$NAME" "$RUN_FINDINGS" "$RUN_IMPLEMENTED" "$RUN_SKIPPED" "$RUN_PRS" "$RUN_ERRORS" "$HISTORY_FILE"
    continue
  fi

  cd "$PATH_DIR"

  git pull --quiet origin main 2>/dev/null || git pull --quiet origin master 2>/dev/null || true

  RECENT=$(git log --since="${STALE_DAYS} days ago" --oneline -1 2>/dev/null)
  if [ -z "$RECENT" ]; then
    log "  SKIP — no commits in $STALE_DAYS days"
    SUMMARY="${SUMMARY}**${NAME}** — skipped (no activity in ${STALE_DAYS} days)\n"
    log_run "$NAME" "$RUN_FINDINGS" "$RUN_IMPLEMENTED" "$RUN_SKIPPED" "$RUN_PRS" "$RUN_ERRORS" "$HISTORY_FILE"
    continue
  fi

  log "  Active — recent commit: $RECENT"

  GITHUB_REPO=$(get_github_repo "$PATH_DIR")
  ensure_labels "$GITHUB_REPO"

  # === BACKLOG CHECK: skip discovery if too many open heartbeat issues ===
  OPEN_HEARTBEAT_ISSUES=$(gh issue list --repo "$GITHUB_REPO" --label "heartbeat" --state open --json title --jq '. | length' 2>/dev/null || echo "0")
  if [ "$OPEN_HEARTBEAT_ISSUES" -gt "$BACKLOG_THRESHOLD" ]; then
    log "  SKIP discovery — $OPEN_HEARTBEAT_ISSUES open heartbeat issues (threshold: $BACKLOG_THRESHOLD)"
    SUMMARY="${SUMMARY}**${NAME}** — skipped discovery (backlog: ${OPEN_HEARTBEAT_ISSUES}/${BACKLOG_THRESHOLD})\n"
    log_run "$NAME" "$RUN_FINDINGS" "$RUN_IMPLEMENTED" "$RUN_SKIPPED" "$RUN_PRS" "$RUN_ERRORS" "$HISTORY_FILE"
    continue
  fi

  # Collect existing heartbeat issue titles to avoid re-proposing tracked work
  EXISTING_TITLES=$(gh issue list --repo "$GITHUB_REPO" --label "heartbeat" --state open --json title --jq '.[].title' 2>/dev/null || echo "")
  export EXISTING_TITLES

  # === PHASE 1: DISCOVERY ===
  FINDINGS_FILE=$(run_discovery "$NAME" "$PATH_DIR")
  FINDING_COUNT=$(jq '.findings | length' "$FINDINGS_FILE" 2>/dev/null || echo "0")
  log "  Found $FINDING_COUNT opportunities"

  RUN_FINDINGS=$FINDING_COUNT

  if [ "$FINDING_COUNT" = "0" ]; then
    SUMMARY="${SUMMARY}**${NAME}** — no findings\n"
    log_run "$NAME" "$RUN_FINDINGS" "$RUN_IMPLEMENTED" "$RUN_SKIPPED" "$RUN_PRS" "$RUN_ERRORS" "$HISTORY_FILE"
    continue
  fi

  # === PHASE 2: IMPLEMENT QUICK WINS ===
  PROJECT_MSG="**${NAME}** (${FINDING_COUNT} findings)\n"
  QW_COUNT=0

  for j in $(seq 0 $((FINDING_COUNT - 1))); do
    CATEGORY=$(jq -r ".findings[$j].category" "$FINDINGS_FILE" 2>/dev/null || echo "unknown")
    TITLE=$(jq -r ".findings[$j].title" "$FINDINGS_FILE" 2>/dev/null || echo "unknown")
    EFFORT=$(jq -r ".findings[$j].effort" "$FINDINGS_FILE" 2>/dev/null || echo "unknown")
    IMPACT=$(jq -r ".findings[$j].impact" "$FINDINGS_FILE" 2>/dev/null || echo "unknown")
    FILES=$(jq -r ".findings[$j].files | join(\", \")" "$FINDINGS_FILE" 2>/dev/null || echo "")
    WHAT=$(jq -r ".findings[$j].what" "$FINDINGS_FILE" 2>/dev/null || echo "")
    WHY=$(jq -r ".findings[$j].why" "$FINDINGS_FILE" 2>/dev/null || echo "")

    if [ "$CATEGORY" = "quick-win" ] && [ "$QW_COUNT" -lt "$MAX_QW" ]; then
      ISSUE_NUM=$(create_issue_if_new "$GITHUB_REPO" "$TITLE" "$CATEGORY" "$EFFORT" "$IMPACT" "$FILES" "$WHAT" "$WHY") || {
        RUN_ERRORS=$(echo "$RUN_ERRORS" | jq -c --arg e "create_issue_if_new failed for: $TITLE" '. + [$e]')
        continue
      }
      log "  Implementing quick-win: $TITLE (issue #$ISSUE_NUM)"
      RESULT=$(implement_quick_win "$PATH_DIR" "$TITLE" "$FILES" "$WHAT" "$WHY" "$ISSUE_NUM") || {
        RUN_ERRORS=$(echo "$RUN_ERRORS" | jq -c --arg e "implement_quick_win failed for: $TITLE" '. + [$e]')
        RESULT="FAILED"
      }

      if [ "$RESULT" = "IMPLEMENTED" ]; then
        SLUG=$(slugify "$TITLE")
        PROJECT_MSG="${PROJECT_MSG}> ✅ **Implemented**: ${TITLE}\n> Branch: \`heartbeat/${TODAY}-${SLUG}\` — PR created\n"
        QW_COUNT=$((QW_COUNT + 1))
        RUN_IMPLEMENTED=$((RUN_IMPLEMENTED + 1))
        RUN_PRS=$((RUN_PRS + 1))
      elif [ "$RESULT" = "EXISTS" ]; then
        PROJECT_MSG="${PROJECT_MSG}> ⏭️ **Already done**: ${TITLE}\n"
        RUN_SKIPPED=$((RUN_SKIPPED + 1))
      else
        PROJECT_MSG="${PROJECT_MSG}> ⏭️ **Skipped**: ${TITLE} (more complex than expected)\n"
        RUN_SKIPPED=$((RUN_SKIPPED + 1))
      fi
    else
      ISSUE_NUM=$(create_issue_if_new "$GITHUB_REPO" "$TITLE" "$CATEGORY" "$EFFORT" "$IMPACT" "$FILES" "$WHAT" "$WHY") || {
        RUN_ERRORS=$(echo "$RUN_ERRORS" | jq -c --arg e "create_issue_if_new failed for: $TITLE" '. + [$e]')
        continue
      }
      PROJECT_MSG="${PROJECT_MSG}> 📋 **${TITLE}** [${CATEGORY}, ${IMPACT} impact, ~${EFFORT}] — issue #${ISSUE_NUM}\n> ${WHAT}\n"
    fi
  done

  send_discord "$(echo -e "$PROJECT_MSG")"
  SUMMARY="${SUMMARY}${NAME}: ${FINDING_COUNT} findings\n"

  log_run "$NAME" "$RUN_FINDINGS" "$RUN_IMPLEMENTED" "$RUN_SKIPPED" "$RUN_PRS" "$RUN_ERRORS" "$HISTORY_FILE"

done

echo -e "# Heartbeat Report — ${TODAY}\n\n${SUMMARY}" > "$HOME/heartbeat-reports/${TODAY}.md"

rm -rf "$TMPDIR"

log "Heartbeat complete"
