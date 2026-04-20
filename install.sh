#!/bin/bash
# Deploy heartbeat (personal automation + ShipLog) to OCI server.
# Usage: ./install.sh [oci-host]
#
# NOTE: The Seneschal PR-review bot is no longer deployed by this script.
# It has moved to its own repository:
#
#   https://github.com/ChandlerHardy/seneschal
#
# Deploy Seneschal with that repo's install.sh.
set -euo pipefail

HOST="${1:-oci}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying heartbeat to ${HOST}..."

# Heartbeat scripts (discovery + scheduling + CLI)
scp "$REPO_DIR/bin/heartbeat.sh" "${HOST}:~/bin/heartbeat.sh"
scp "$REPO_DIR/bin/heartbeat-lib.sh" "${HOST}:~/bin/heartbeat-lib.sh"
scp "$REPO_DIR/bin/heartbeat-weekly.sh" "${HOST}:~/bin/heartbeat-weekly.sh"
scp "$REPO_DIR/bin/heartbeat-cleanup.sh" "${HOST}:~/bin/heartbeat-cleanup.sh"
scp "$REPO_DIR/bin/shiplog.sh" "${HOST}:~/bin/shiplog.sh"
scp "$REPO_DIR/bin/heartbeat-brainstorm.sh" "${HOST}:~/bin/heartbeat-brainstorm.sh"
scp "$REPO_DIR/bin/heartbeat-config.sh" "${HOST}:~/bin/heartbeat-config.sh"
scp "$REPO_DIR/bin/hb" "${HOST}:~/bin/hb"
ssh "$HOST" "chmod +x ~/bin/heartbeat.sh ~/bin/heartbeat-weekly.sh ~/bin/heartbeat-cleanup.sh ~/bin/shiplog.sh ~/bin/heartbeat-brainstorm.sh ~/bin/heartbeat-config.sh ~/bin/hb"

# ShipLog — Python package deployed to ~/shiplog
ssh "$HOST" "mkdir -p ~/shiplog"
for f in __init__.py __main__.py models.py classifier.py formatter.py collectors.py; do
  scp "$REPO_DIR/shiplog/$f" "${HOST}:~/shiplog/$f"
done

# Verify
ssh "$HOST" "bash -n ~/bin/heartbeat.sh && echo 'heartbeat.sh: OK'"
ssh "$HOST" "bash -n ~/bin/heartbeat-weekly.sh && echo 'heartbeat-weekly.sh: OK'"
ssh "$HOST" "bash -n ~/bin/shiplog.sh && echo 'shiplog.sh: OK'"
ssh "$HOST" "python3 -c 'import sys; sys.path.insert(0, \"/home/ubuntu\"); import shiplog; print(\"shiplog import: OK\")' || echo 'shiplog import: FAIL'"

echo ""
echo "Heartbeat deploy complete."
echo ""
echo "Note: Seneschal (the PR-review bot) is now a separate repo."
echo "Deploy it with: cd ~/repos/seneschal && ./install.sh $HOST"
echo ""
echo "Note: claude-burn is a local Mac tool and is not deployed to OCI."
echo "  Build locally: cd tools/claude-burn && go build -o ~/bin/claude-burn ./cmd/claude-burn"
