"""
Create the FTMO Monitor Droplet on DigitalOcean.
Reads the MS refresh token from the local MSAL cache automatically.
Usage: python3 scripts/create_droplet.py
"""
import json
import os
import sys
from pathlib import Path

# ── Load secrets from local environment ────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
env = {}
for line in env_file.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v

ANTHROPIC_KEY = env["ANTHROPIC_API_KEY"]
TWELVEDATA_KEY = env["TWELVEDATA_API_KEY"]
DO_TOKEN = env["DIGITALOCEAN_TOKEN"]
MS_CLIENT_ID = env.get("MS_CLIENT_ID", "")
MS_TENANT_ID = env.get("MS_TENANT_ID", "")
ALERT_EMAIL_FROM = env.get("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO = env.get("ALERT_EMAIL_TO", "")

# Extract MS refresh token from MSAL cache (same one used by the Outlook MCP)
# The account is identified by its home_account_id prefix stored in .env as MS_ACCOUNT_ID.
# Falls back to MS_REFRESH_TOKEN from .env if cache is unavailable.
MS_ACCOUNT_ID_PREFIX = env.get("MS_ACCOUNT_ID_PREFIX", "")
MSAL_CACHE = Path.home() / ".m365_mcp_token_cache.json"
MS_REFRESH_TOKEN = env.get("MS_REFRESH_TOKEN", "")
try:
    cache = json.loads(MSAL_CACHE.read_text())
    for v in cache.get("RefreshToken", {}).values():
        home_id = v.get("home_account_id", "")
        if MS_ACCOUNT_ID_PREFIX and home_id.startswith(MS_ACCOUNT_ID_PREFIX):
            MS_REFRESH_TOKEN = v.get("secret", "")
            break
except Exception as e:
    print(f"[WARN] Could not read MSAL cache: {e}")

if not MS_REFRESH_TOKEN:
    print("[ERROR] MS refresh token not found — email alerts will be disabled")

if MS_REFRESH_TOKEN:
    print(f"MS refresh token: {MS_REFRESH_TOKEN[:8]}... ({len(MS_REFRESH_TOKEN)} chars)")
else:
    print("[ERROR] MS refresh token not found — email alerts will be disabled")

# ── Build cloud-init user_data ──────────────────────────────────────────────────
USER_DATA = f"""#!/bin/bash
exec > /var/log/ftmo-setup.log 2>&1
set -e
echo "Setup started at $(date)"
apt-get update -qq
apt-get install -y -qq python3 python3-pip git
echo "Packages ready"
git clone https://github.com/oteran92/ftmo_agent /opt/ftmo_agent
echo "Repo cloned"
cd /opt/ftmo_agent
pip3 install -q -r requirements.txt
echo "Dependencies ready"
mkdir -p /opt/ftmo_agent/data
cat > /opt/ftmo_agent/.env << 'ENVEOF'
ANTHROPIC_API_KEY={ANTHROPIC_KEY}
TWELVEDATA_API_KEY={TWELVEDATA_KEY}
MS_CLIENT_ID={MS_CLIENT_ID}
MS_TENANT_ID={MS_TENANT_ID}
MS_REFRESH_TOKEN={MS_REFRESH_TOKEN}
ALERT_EMAIL_FROM={ALERT_EMAIL_FROM}
ALERT_EMAIL_TO={ALERT_EMAIL_TO}
ENVEOF
echo ".env created"
cat > /etc/systemd/system/ftmo-monitor.service << 'SVCEOF'
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
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable ftmo-monitor
systemctl start ftmo-monitor
echo "Service started at $(date)"
"""

# ── Create Droplet via DO API ───────────────────────────────────────────────────
import urllib.request

payload = json.dumps({
    "name": "ftmo-monitor",
    "region": "fra1",
    "size": "s-1vcpu-1gb",
    "image": "ubuntu-24-04-x64",
    "ssh_keys": [53619064],
    "monitoring": True,
    "user_data": USER_DATA,
    "tags": ["ftmo-agent"],
}).encode()

req = urllib.request.Request(
    "https://api.digitalocean.com/v2/droplets",
    data=payload,
    headers={
        "Authorization": f"Bearer {DO_TOKEN}",
        "Content-Type": "application/json",
    },
    method="POST",
)

with urllib.request.urlopen(req) as resp:
    result = json.loads(resp.read())

droplet = result["droplet"]
print(f"\n=== Droplet Created ===")
print(f"ID: {droplet['id']}")
print(f"Name: {droplet['name']}")
print(f"Region: {droplet['region']['name']}")
print(f"Status: {droplet['status']}")
print(f"\nWait 3-5 minutes for cloud-init to complete.")
    print(f"The monitor will send a startup email to {ALERT_EMAIL_TO} when ready.")
