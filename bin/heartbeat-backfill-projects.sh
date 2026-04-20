#!/bin/bash
# heartbeat-backfill-projects.sh
#
# One-shot: add every open heartbeat-labeled issue and PR across tracked repos
# to the Heartbeat Dashboard GitHub Projects board.
#
# Why this exists: heartbeat.sh calls add_to_project() on each new issue, but
# on OCI the `gh` auth is missing the `project` scope and the mutation
# silently fails. This script runs from a Mac with full scope to populate
# the board with existing work. Re-run safely — GitHub dedupes items.
#
# To permanently fix OCI:
#   ssh oci
#   gh auth refresh -h github.com -s project
# Then the nightly heartbeat.sh add_to_project calls will start landing.

set -euo pipefail

PROJECT_BOARD_ID="PVT_kwHOAVEBTs4BT23z"
PROJECT_STATUS_FIELD_ID="PVTSSF_lAHOAVEBTs4BT23zzhBC39Y"
STATUS_DISCOVERED="30d3a08c"
STATUS_IMPLEMENTED="da2d3b98"

# Project single-select field (added in this PR so you can group by project).
PROJECT_FIELD_ID="PVTSSF_lAHOAVEBTs4BT23zzhBjJ_A"

project_option_for_repo() {
  case "${1##*/}" in
    crooked-finger)    echo "b0246adc" ;;
    portfolio-website) echo "15f18ca0" ;;
    gnomestead-web)    echo "7dd582f2" ;;
    gnomestead)        echo "e192cae1" ;;
    elucidate-chess)   echo "25dc8dd3" ;;
    greenline)         echo "3a5c6315" ;;
    snapcal)           echo "c06ab3f9" ;;
    heartbeat)         echo "8e047e62" ;;
    *)                 echo "" ;;
  esac
}

set_project_field() {
  local item_id="$1"
  local option_id="$2"
  [ -z "$option_id" ] && return 0
  gh api graphql -f query='mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: {singleSelectOptionId: $optionId}}) {
      projectV2Item { id }
    }
  }' -f projectId="$PROJECT_BOARD_ID" -f itemId="$item_id" -f fieldId="$PROJECT_FIELD_ID" -f optionId="$option_id" > /dev/null 2>&1
}

REPOS=(
  ChandlerHardy/crooked-finger
  ChandlerHardy/portfolio-website
  ChandlerHardy/gnomestead-web
  ChandlerHardy/gnomestead
  ChandlerHardy/elucidate-chess
  ChandlerHardy/greenline
  ChandlerHardy/snapcal
  ChandlerHardy/heartbeat
)

add_item() {
  local node_id="$1"
  local status="$2"
  local label="$3"
  local repo_name="$4"  # NEW: repo name for Project field lookup

  local item_id
  item_id=$(gh api graphql -f query='mutation($projectId: ID!, $contentId: ID!) {
    addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
      item { id }
    }
  }' -f projectId="$PROJECT_BOARD_ID" -f contentId="$node_id" --jq '.data.addProjectV2ItemById.item.id' 2>/dev/null)

  if [ -z "$item_id" ] || [ "$item_id" = "null" ]; then
    echo "  ! could not add $label"
    return 1
  fi

  gh api graphql -f query='mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input: {projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: {singleSelectOptionId: $optionId}}) {
      projectV2Item { id }
    }
  }' -f projectId="$PROJECT_BOARD_ID" -f itemId="$item_id" -f fieldId="$PROJECT_STATUS_FIELD_ID" -f optionId="$status" > /dev/null 2>&1

  # Also set the Project field so the board can group by project.
  local option_id
  option_id=$(project_option_for_repo "$repo_name")
  set_project_field "$item_id" "$option_id"

  echo "  + $label"
  return 0
}

ISSUES_ADDED=0
PRS_ADDED=0

for repo in "${REPOS[@]}"; do
  echo "=== $repo ==="

  # Open heartbeat issues -> Discovered
  issues=$(gh issue list --repo "$repo" --state open --label heartbeat --json number,title,url --limit 100 2>/dev/null || echo "[]")
  count=$(echo "$issues" | jq 'length')
  if [ "$count" != "0" ]; then
    # Process substitution keeps the loop body in the parent shell so we can
    # increment the success counter — piping into `while` puts it in a
    # subshell and any counter changes evaporate, masking failed mutations.
    while read -r item; do
      num=$(echo "$item" | jq -r .number)
      title=$(echo "$item" | jq -r .title)
      node_id=$(gh api "repos/$repo/issues/$num" --jq .node_id 2>/dev/null)
      if [ -n "$node_id" ] && [ "$node_id" != "null" ]; then
        if add_item "$node_id" "$STATUS_DISCOVERED" "#$num $title" "$repo"; then
          ISSUES_ADDED=$((ISSUES_ADDED + 1))
        fi
      fi
    done < <(echo "$issues" | jq -c '.[]')
  fi

  # Open heartbeat PRs -> Implemented
  prs=$(gh pr list --repo "$repo" --state open --label "heartbeat" --json number,title,url --limit 100 2>/dev/null || echo "[]")
  pr_count=$(echo "$prs" | jq 'length')
  if [ "$pr_count" != "0" ]; then
    while read -r item; do
      num=$(echo "$item" | jq -r .number)
      title=$(echo "$item" | jq -r .title)
      node_id=$(gh api "repos/$repo/pulls/$num" --jq .node_id 2>/dev/null)
      if [ -n "$node_id" ] && [ "$node_id" != "null" ]; then
        if add_item "$node_id" "$STATUS_IMPLEMENTED" "PR #$num $title" "$repo"; then
          PRS_ADDED=$((PRS_ADDED + 1))
        fi
      fi
    done < <(echo "$prs" | jq -c '.[]')
  fi
done

echo ""
echo "Backfill complete."
echo "  Board: https://github.com/users/ChandlerHardy/projects/1"
