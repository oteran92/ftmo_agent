#!/usr/bin/env bash
# Deploy auto_executor.py and updated monitor.py to the Droplet,
# install the MT5 Wine systemd service, and restart the monitor.
#
# Usage: bash deploy/deploy_autoexec.sh

set -e

SERVER="root@165.245.244.196"
REMOTE_DIR="/root/ftmo_agent"

echo "=== Syncing files to Droplet ==="
rsync -avz --exclude '__pycache__' --exclude '.git' --exclude '.env' \
  skills/auto_executor.py \
  monitor.py \
  "${SERVER}:${REMOTE_DIR}/skills/"

rsync -avz monitor.py "${SERVER}:${REMOTE_DIR}/"

echo "=== Copying systemd service ==="
scp deploy/mt5-wine.service "${SERVER}:/etc/systemd/system/mt5-wine.service"
scp deploy/start_mt5.sh "${SERVER}:/root/start_mt5.sh"
ssh "$SERVER" "chmod +x /root/start_mt5.sh"

echo "=== Reloading systemd ==="
ssh "$SERVER" "systemctl daemon-reload"

echo "=== Restarting ftmo-monitor ==="
ssh "$SERVER" "systemctl restart ftmo-monitor && systemctl status ftmo-monitor --no-pager | head -10"

echo ""
echo "Done. MT5 Wine service is registered but NOT started yet."
echo "Start it manually after configuring your FTMO account in MT5:"
echo "  ssh $SERVER"
echo "  bash /root/start_mt5.sh  # first time (configure account)"
echo "  systemctl enable --now mt5-wine"
