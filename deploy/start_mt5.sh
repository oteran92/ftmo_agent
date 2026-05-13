#!/usr/bin/env bash
# Start MT5 headlessly via Xvfb + Wine.
# Designed to be called by systemd or manually.

set -e

WINEPREFIX=/root/.wine_mt5
MT5_EXE="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
DISPLAY_NUM=99
LOGFILE=/var/log/mt5-wine.log

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"; }

log "=== MT5 Wine Start ==="

# Kill any existing virtual display
pkill Xvfb 2>/dev/null || true
sleep 1

# Start Xvfb
log "Starting Xvfb :$DISPLAY_NUM"
Xvfb ":$DISPLAY_NUM" -screen 0 1024x768x24 -ac +extension GLX +render &
XVFB_PID=$!
sleep 3

# Verify Xvfb started
if ! kill -0 $XVFB_PID 2>/dev/null; then
    log "ERROR: Xvfb failed to start"
    exit 1
fi
log "Xvfb started (PID $XVFB_PID)"

# Verify MT5 binary
if [ ! -f "$MT5_EXE" ]; then
    log "ERROR: MT5 not found at $MT5_EXE"
    log "Run the install script first: bash /root/install_mt5.sh"
    exit 1
fi

# Start MT5
export DISPLAY=":$DISPLAY_NUM"
export WINEPREFIX
export WINEARCH=win64
export WINEDEBUG=-all

log "Starting MT5..."
wine "$MT5_EXE" /portable >> "$LOGFILE" 2>&1 &
MT5_PID=$!
log "MT5 launched (Wine PID $MT5_PID)"
echo $MT5_PID > /run/mt5-wine.pid
wait $MT5_PID
log "MT5 exited with code $?"
