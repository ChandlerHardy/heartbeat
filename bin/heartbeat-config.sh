#!/bin/bash
# heartbeat-config.sh
#
# Safely add/remove/list projects in heartbeat.json without hand-editing.
#
# Usage:
#   heartbeat-config list
#   heartbeat-config add <name> <path> [stale_days]
#   heartbeat-config remove <name>
#   heartbeat-config show
#
# By default writes to $HOME/etc/heartbeat.json. Override with CONFIG env var.

set -euo pipefail

CONFIG="${CONFIG:-$HOME/etc/heartbeat.json}"

die() { echo "heartbeat-config: $*" >&2; exit 1; }

ensure_config() {
  if [ ! -f "$CONFIG" ]; then
    die "config file not found: $CONFIG (set CONFIG= to override)"
  fi
}

cmd_list() {
  ensure_config
  jq -r '.projects[] | "\(.name)\t\(.path)\t\(.stale_days)d"' "$CONFIG" | column -t -s $'\t'
}

cmd_show() {
  ensure_config
  jq . "$CONFIG"
}

cmd_add() {
  local name="${1:-}" path="${2:-}" stale="${3:-14}"
  [ -z "$name" ] && die "usage: add <name> <path> [stale_days]"
  [ -z "$path" ] && die "usage: add <name> <path> [stale_days]"
  ensure_config

  # Check for duplicates. Use --arg so a name containing `"` cannot
  # corrupt the jq query string.
  if jq -e --arg n "$name" '.projects[] | select(.name == $n)' "$CONFIG" > /dev/null 2>&1; then
    die "project '$name' already exists (use 'remove $name' first)"
  fi

  local tmp
  tmp=$(mktemp)
  jq --arg n "$name" --arg p "$path" --argjson s "$stale" \
    '.projects += [{"name": $n, "path": $p, "stale_days": $s}]' "$CONFIG" > "$tmp"
  mv "$tmp" "$CONFIG"
  echo "Added $name (path: $path, stale_days: $stale)"
}

cmd_remove() {
  local name="${1:-}"
  [ -z "$name" ] && die "usage: remove <name>"
  ensure_config

  if ! jq -e --arg n "$name" '.projects[] | select(.name == $n)' "$CONFIG" > /dev/null 2>&1; then
    die "project '$name' not found"
  fi

  local tmp
  tmp=$(mktemp)
  jq --arg n "$name" '.projects |= map(select(.name != $n))' "$CONFIG" > "$tmp"
  mv "$tmp" "$CONFIG"
  echo "Removed $name"
}

main() {
  local subcmd="${1:-list}"
  shift || true
  case "$subcmd" in
    list) cmd_list "$@" ;;
    show) cmd_show "$@" ;;
    add) cmd_add "$@" ;;
    remove) cmd_remove "$@" ;;
    -h|--help|help)
      sed -n '2,12p' "$0" | sed 's/^# //;s/^#//'
      ;;
    *)
      die "unknown command: $subcmd (try 'help')"
      ;;
  esac
}

main "$@"
