#!/bin/bash
# Heartbeat — stale branch cleanup
# Deletes remote heartbeat/* branches whose PRs are merged or closed.
set -euo pipefail

CONFIG="$HOME/etc/heartbeat.json"
TODAY=$(date +%Y-%m-%d)
LOG_PREFIX="[heartbeat-cleanup $TODAY]"
PROJECT_COUNT=$(jq '.projects | length' "$CONFIG")

# Parse --project flag to filter to a single project
FILTER_PROJECT=""
DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) FILTER_PROJECT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) shift ;;
  esac
done

log() { echo "$LOG_PREFIX $1" >&2; }

get_github_repo() {
  local dir="$1"
  git -C "$dir" remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||'
}

cleanup_project() {
  local name="$1"
  local path_dir="$2"
  local repo
  repo=$(get_github_repo "$path_dir")

  if [[ -z "$repo" ]]; then
    log "SKIP $name — no GitHub remote"
    return
  fi

  log "Checking $name ($repo)..."

  # List remote heartbeat/* branches
  local branches
  branches=$(git -C "$path_dir" ls-remote --heads origin 'refs/heads/heartbeat/*' 2>/dev/null | awk '{print $2}' | sed 's|refs/heads/||') || true

  if [[ -z "$branches" ]]; then
    log "  No heartbeat branches found"
    return
  fi

  local deleted=0
  local kept=0

  while IFS= read -r branch; do
    # Find PRs for this branch (merged or closed)
    local pr_state
    pr_state=$(gh pr list --repo "$repo" --head "$branch" --state all --json state --jq '.[0].state // empty' 2>/dev/null) || true

    if [[ "$pr_state" == "MERGED" || "$pr_state" == "CLOSED" ]]; then
      if [[ "$DRY_RUN" == true ]]; then
        log "  [dry-run] Would delete $branch (PR $pr_state)"
      else
        git -C "$path_dir" push origin --delete "$branch" 2>/dev/null && \
          log "  Deleted $branch (PR $pr_state)" || \
          log "  Failed to delete $branch"
      fi
      ((deleted++))
    elif [[ -z "$pr_state" ]]; then
      # No PR found — branch may be orphaned; skip (safe default)
      log "  Skipped $branch (no PR found)"
      ((kept++))
    else
      log "  Kept $branch (PR $pr_state)"
      ((kept++))
    fi
  done <<< "$branches"

  log "  $name: deleted=$deleted kept=$kept"
}

# Main loop — iterate projects from heartbeat.json
for i in $(seq 0 $((PROJECT_COUNT - 1))); do
  NAME=$(jq -r ".projects[$i].name" "$CONFIG")
  PATH_DIR=$(jq -r ".projects[$i].path" "$CONFIG")

  # Filter if --project specified
  if [[ -n "$FILTER_PROJECT" && "$NAME" != "$FILTER_PROJECT" ]]; then
    continue
  fi

  if [[ ! -d "$PATH_DIR" ]]; then
    log "SKIP $NAME — directory not found: $PATH_DIR"
    continue
  fi

  cleanup_project "$NAME" "$PATH_DIR"
done

log "Cleanup complete."
