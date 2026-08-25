#!/bin/bash
# Worksweep — read-only GitLab digest of MRs/reviews/todos/issues.
#   ./worksweep.sh --dry-run    # stdout only (default if no --discord)
#   ./worksweep.sh --discord    # post the digest to Discord
#   ./worksweep.sh intake       # poll Discord for ✅ approval replies (M2)
#   ./worksweep.sh run          # execute one approved magi-review item (M3)
#   ./worksweep.sh dashboard    # serve the queue view + approval buttons (:8787)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
exec python3 -m worksweep "$@"
