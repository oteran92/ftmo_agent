#!/bin/bash
# setup_droplet.sh — Run this from YOUR terminal (not Cursor) to configure the DigitalOcean droplet
# Usage: bash scripts/setup_droplet.sh
#
# This script:
#   1. Installs Python3 + pip on the Droplet
#   2. Clones the ftmo_agent repo
#   3. Installs dependencies
#   4. Creates the .env file with your credentials
#   5. Configures a systemd service to run monitor.py 24/7

set -e

DROPLET_IP="64.225.79.205"
REPO_URL="https://github.com/$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||' | sed 's|\.git||')"

echo "=== FTMO Monitor — Droplet Setup ==="
echo "Droplet: $DROPLET_IP"
echo ""

# ── Load local .env ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    source "$SCRIPT_DIR/.env"
    echo "[OK] Loaded .env from $SCRIPT_DIR"
else
    echo "[ERROR] .env not found at $SCRIPT_DIR/.env"
    exit 1
fi

# ── SSH helper ──────────────────────────────────────────────────────────────────
run_remote() {
    ssh -o StrictHostKeyChecking=no root@$DROPLET_IP "$@"
}

echo ""
echo "=== Step 1: Install system packages ==="
run_remote "apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv git"
echo "[OK] Python3 + git installed"

echo ""
echo "=== Step 2: Clone repository ==="
# If repo URL not detected, use the known path
if [[ "$REPO_URL" == "https://github.com//" ]]; then
    echo "Enter your GitHub repo URL (e.g. https://github.com/yourusername/ftmo_agent):"
    read REPO_URL
fi

run_remote "
if [ -d /opt/ftmo_agent ]; then
    cd /opt/ftmo_agent && git pull
else
    git clone $REPO_URL /opt/ftmo_agent
fi
"
echo "[OK] Repository ready at /opt/ftmo_agent"

echo ""
echo "=== Step 3: Install Python dependencies ==="
run_remote "cd /opt/ftmo_agent && pip3 install -q -r requirements.txt"
echo "[OK] Dependencies installed"

echo ""
echo "=== Step 4: Create .env on server ==="
run_remote "cat > /opt/ftmo_agent/.env << 'ENVEOF'
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
TWELVEDATA_API_KEY=${TWELVEDATA_API_KEY}
MS_TENANT_ID=${MS_TENANT_ID:-}
MS_CLIENT_ID=${MS_CLIENT_ID:-}
MS_CLIENT_SECRET=${MS_CLIENT_SECRET:-}
ALERT_EMAIL_FROM=${ALERT_EMAIL_FROM:-}
ALERT_EMAIL_TO=${ALERT_EMAIL_TO:-}
ENVEOF"
echo "[OK] .env created on server"

echo ""
echo "=== Step 5: Create systemd service ==="
run_remote "cat > /etc/systemd/system/ftmo-monitor.service << 'SVCEOF'
[Unit]
Description=FTMO Monitor — autonomous H4 market scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftmo_agent
EnvironmentFile=/opt/ftmo_agent/.env
ExecStart=/usr/bin/python3 -u monitor.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF"

run_remote "
systemctl daemon-reload
systemctl enable ftmo-monitor
systemctl start ftmo-monitor
sleep 3
systemctl status ftmo-monitor --no-pager | head -20
"
echo ""
echo "=== SETUP COMPLETE ==="
echo "Droplet IP:  $DROPLET_IP"
echo "Service:     ftmo-monitor (auto-starts on reboot)"
echo ""
echo "Useful commands (run from your terminal):"
echo "  ssh root@$DROPLET_IP 'journalctl -u ftmo-monitor -f'    # view live logs"
echo "  ssh root@$DROPLET_IP 'systemctl status ftmo-monitor'    # check status"
echo "  ssh root@$DROPLET_IP 'systemctl restart ftmo-monitor'   # restart"
