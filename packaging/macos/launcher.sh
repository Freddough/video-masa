#!/bin/bash
# ─── Video Masa Launcher ───
# Handles first-run setup detection, upgrades, server startup, and browser opening.
set -e

RESOURCES_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$RESOURCES_DIR/app"
VM_HOME="$HOME/.videomasa"
VENV_DIR="$VM_HOME/venv"
WORK_DIR="$VM_HOME/downloads"
COOKIES_DIR="$VM_HOME/cookies"
PID_FILE="$VM_HOME/server.pid"
LOG_FILE="$VM_HOME/server.log"
VERSION_FILE="$VM_HOME/version"
PORT=8080

# App version — bump this with each release
APP_VERSION="3.0"

# ─── If server is already running, just open the browser ───
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        open "http://localhost:$PORT"
        exit 0
    else
        # Stale PID file — clean up
        rm -f "$PID_FILE"
    fi
fi

mkdir -p "$VM_HOME" "$WORK_DIR" "$COOKIES_DIR"

# ─── Determine if setup is needed ───
_needs_setup=false

if [ ! -d "$VENV_DIR" ]; then
    # No venv at all — first install
    _needs_setup=true
elif [ -f "$VENV_DIR/bin/python" ]; then
    _py_minor=$("$VENV_DIR/bin/python" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | cut -d. -f2)
    if [ -n "$_py_minor" ] && [ "$_py_minor" -lt 10 ]; then
        # Venv was created with Python < 3.10 — too old
        rm -rf "$VENV_DIR"
        _needs_setup=true
    fi
fi

# Check if app was upgraded since last setup
if [ "$_needs_setup" = false ]; then
    _installed_version=""
    [ -f "$VERSION_FILE" ] && _installed_version=$(cat "$VERSION_FILE")
    if [ "$_installed_version" != "$APP_VERSION" ]; then
        # Version mismatch — wipe venv so setup reinstalls deps
        rm -rf "$VENV_DIR"
        _needs_setup=true
    fi
fi

if [ "$_needs_setup" = true ]; then
    osascript <<EOF
tell application "Terminal"
    activate
    do script "bash '${RESOURCES_DIR}/setup.sh'"
end tell
EOF
    exit 0
fi

# ─── Normal launch: start server + open browser ───
export PATH="$RESOURCES_DIR:$PATH"
export VIDEOMASA_WORK_DIR="$WORK_DIR"
export VIDEOMASA_COOKIES_DIR="$COOKIES_DIR"
export VIDEOMASA_OPEN_BROWSER="1"
export VIDEOMASA_PORT="$PORT"

source "$VENV_DIR/bin/activate"
cd "$APP_DIR"
python app.py > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Clean up PID file when server exits
(
    wait "$SERVER_PID" 2>/dev/null
    rm -f "$PID_FILE"
) &

exit 0
