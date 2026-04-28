#!/bin/bash
# setup_droplet.sh — Run from YOUR terminal to configure the DigitalOcean Droplet.
# The new Droplet already auto-installs via cloud-init on first boot.
# This script is only needed to UPDATE credentials or redeploy code.
# Usage: bash scripts/setup_droplet.sh

set -e

DROPLET_IP="165.22.25.204"
REPO_URL="https://github.com/oteran92/ftmo_agent"

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

# ── Extract current refresh token from MSAL cache (no manual steps needed) ──────
MS_REFRESH_TOKEN=$(python3 -c "
import json
try:
    cache = json.load(open('$HOME/.m365_mcp_token_cache.json'))
    for v in cache.get('RefreshToken', {}).values():
        if '121bab41' in v.get('home_account_id', ''):
            print(v.get('secret', ''))
            break
except Exception as e:
    print('')
" 2>/dev/null)

if [ -z "$MS_REFRESH_TOKEN" ]; then
    echo "[WARN] Could not extract MS refresh token — email alerts will be disabled"
else
    echo "[OK] MS refresh token extracted (${#MS_REFRESH_TOKEN} chars)"
fi

# ── SSH helper ──────────────────────────────────────────────────────────────────
run_remote() {
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 root@$DROPLET_IP "$@"
}

echo ""
echo "=== Waiting for SSH to be ready ==="
for i in $(seq 1 12); do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@$DROPLET_IP 'echo ok' 2>/dev/null; then
        echo "[OK] SSH ready"
        break
    fi
    echo "  Attempt $i/12 — waiting 10s..."
    sleep 10
done

echo ""
echo "=== Step 1: Ensure packages installed ==="
run_remote "which python3 && python3 --version && which git" || \
    run_remote "apt-get update -qq && apt-get install -y -qq python3 python3-pip git"

echo ""
echo "=== Step 2: Clone or update repository ==="
run_remote "
if [ -d /opt/ftmo_agent ]; then
    cd /opt/ftmo_agent && git pull
else
    git clone $REPO_URL /opt/ftmo_agent
fi
"

echo ""
echo "=== Step 3: Install Python dependencies (venv) ==="
run_remote "
apt-get install -y -qq python3.12-venv python3-full
python3 -m venv /opt/ftmo_venv
/opt/ftmo_venv/bin/pip install -q -r /opt/ftmo_agent/requirements.txt
"

echo ""
echo "=== Step 4: Create .env on server ==="
# Write .env using printf to avoid heredoc quoting issues with special chars
run_remote "printf '%s\n' \
'ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}' \
'TWELVEDATA_API_KEY=${TWELVEDATA_API_KEY}' \
'MS_CLIENT_ID=a47b745d-3383-4e35-81d2-5629063bd358' \
'MS_TENANT_ID=bb39cf77-e2e2-403c-a430-7cbc12114f78' \
'MS_REFRESH_TOKEN=${MS_REFRESH_TOKEN}' \
'ALERT_EMAIL_FROM=vote@eroica.io' \
'ALERT_EMAIL_TO=vote@eroica.io' \
> /opt/ftmo_agent/.env"
echo "[OK] .env created on server"

echo ""
echo "=== Step 5: Configure systemd service ==="
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
ExecStart=/opt/ftmo_venv/bin/python -u monitor.py
Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF"

run_remote "
systemctl daemon-reload
systemctl enable ftmo-monitor
systemctl restart ftmo-monitor
sleep 3
systemctl status ftmo-monitor --no-pager | head -20
"

echo ""
echo "=== SETUP COMPLETE ==="
echo "Droplet IP:  $DROPLET_IP"
echo "Service:     ftmo-monitor.service (auto-starts on reboot)"
echo ""
echo "Useful commands:"
echo "  ssh root@$DROPLET_IP 'journalctl -u ftmo-monitor -f'     # live logs"
echo "  ssh root@$DROPLET_IP 'systemctl status ftmo-monitor'     # status"
echo "  ssh root@$DROPLET_IP 'cat /var/log/ftmo-setup.log'       # cloud-init log"
