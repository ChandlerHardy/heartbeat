#!/bin/bash
# Deploy heartbeat to OCI server
# Usage: ./install.sh [oci-host]
set -euo pipefail

HOST="${1:-oci}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying heartbeat to ${HOST}..."

# Scripts
scp "$REPO_DIR/bin/heartbeat.sh" "${HOST}:~/bin/heartbeat.sh"
scp "$REPO_DIR/bin/heartbeat-weekly.sh" "${HOST}:~/bin/heartbeat-weekly.sh"
ssh "$HOST" "chmod +x ~/bin/heartbeat.sh ~/bin/heartbeat-weekly.sh"

# Code reviewer
scp "$REPO_DIR/code-reviewer/app.py" "${HOST}:~/code-reviewer/app.py"
scp "$REPO_DIR/code-reviewer/requirements.txt" "${HOST}:~/code-reviewer/requirements.txt"

# Systemd (requires sudo)
scp "$REPO_DIR/systemd/code-reviewer.service" "/tmp/code-reviewer.service"
ssh "$HOST" "sudo cp /tmp/code-reviewer.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart code-reviewer"

# Verify
ssh "$HOST" "systemctl is-active code-reviewer && echo 'code-reviewer: OK'"
ssh "$HOST" "bash -n ~/bin/heartbeat.sh && echo 'heartbeat.sh: OK'"
ssh "$HOST" "bash -n ~/bin/heartbeat-weekly.sh && echo 'heartbeat-weekly.sh: OK'"

echo "Deploy complete."
