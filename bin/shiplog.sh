#!/bin/bash
# ShipLog — weekly retrospective digest
# Runs via cron on OCI every Sunday morning, or on-demand from anywhere.
#
# Usage:
#   ./shiplog.sh                   # 7-day window, archive + discord
#   ./shiplog.sh --days 14         # override window
#   ./shiplog.sh --project name    # limit to one project
#   ./shiplog.sh --dry-run         # stdout only, no archive / discord

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse --dry-run off the front.
EXTRA_ARGS=()
SEND_DISCORD=1
ARCHIVE=1
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      SEND_DISCORD=0
      ARCHIVE=0
      ;;
    --no-discord)
      SEND_DISCORD=0
      ;;
    --no-archive)
      ARCHIVE=0
      ;;
    *)
      EXTRA_ARGS+=("$arg")
      ;;
  esac
done

CMD=(python3 -m shiplog)
if [[ $ARCHIVE -eq 1 ]]; then
  CMD+=(--archive)
fi
if [[ $SEND_DISCORD -eq 1 ]]; then
  CMD+=(--discord)
fi
# Empty-array expansion: under `set -u` on macOS /bin/bash 3.2, a bare
# "${EXTRA_ARGS[@]}" on an empty array is treated as an unset variable. The
# ${arr[@]+"${arr[@]}"} idiom expands to nothing when unset and to the
# quoted elements otherwise. Harmless on bash 4.4+.
CMD+=(${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"})

cd "$REPO_ROOT"
"${CMD[@]}"
