#!/usr/bin/env bash
# Auto-deploy: pulls latest code and syncs dell_fan_control.py to /opt/ if changed.
# No dependencies beyond git itself.
#
# Usage (foreground, polls every 30 s):
#   ./watch_and_deploy.sh
#
# Or as a systemd service (see bottom of script for setup).

REPO_DIR="$HOME/workspace/dell_fan_control"
SCRIPT="dell_fan_control.py"
DEST="/opt/dell_fan_control.py"

sync_if_changed() {
    cd "${REPO_DIR}" || exit 1

    # Pull latest from origin
    git pull --quiet origin main 2>/dev/null

    current=$(sha256sum "${SCRIPT}" 2>/dev/null | awk '{print $1}')
    last=$(cat /tmp/dell_fan_control.last_hash 2>/dev/null)

    if [[ -n "${current}" && "${current}" != "${last}" ]]; then
        echo "[$(date '+%H:%M:%S')] ${SCRIPT} changed → synced to ${DEST}"
        sudo cp "${SCRIPT}" "${DEST}" && sudo chmod +x "${DEST}"
        echo "${current}" > /tmp/dell_fan_control.last_hash
    fi
}

echo "Watching ${REPO_DIR}/${SCRIPT} → ${DEST} (polls every 30 s, Ctrl+C to stop)"

while true; do
    sync_if_changed
    sleep 30
done

# ── Optional: run as a systemd service ────────────────────────────────────────
# Run these on the server:
#   sudo cp watch_and_deploy.sh /usr/local/bin/
#   sudo tee /etc/systemd/system/dell-fan-sync.service << 'EOF'
# [Unit]
# Description=Auto-sync dell_fan_control.py to /opt on git changes
# After=network.target
#
# [Service]
# Type=simple
# User=dsilva
# WorkingDirectory=/home/dsilva/workspace/dell_fan_control
# ExecStart=/usr/local/bin/watch_and_deploy.sh
# Restart=always
# RestartSec=5
#
# [Install]
# WantedBy=multi-user.target
# EOF
#   sudo systemctl daemon-reload && sudo systemctl enable --now dell-fan-sync
