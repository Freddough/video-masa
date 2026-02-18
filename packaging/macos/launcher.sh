#!/bin/bash
# ─── Video Masa Launcher ───
# Handles first-run setup detection, server startup, and browser opening.
set -e

RESOURCES_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$RESOURCES_DIR/app"
VM_HOME="$HOME/.videomasa"
VENV_DIR="$VM_HOME/venv"
WORK_DIR="$VM_HOME/downloads"
PID_FILE="$VM_HOME/server.pid"
LOG_FILE="$VM_HOME/server.log"
PORT=8080

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

mkdir -p "$VM_HOME" "$WORK_DIR"

# ─── First-run: no venv yet → open Terminal with setup script ───
if [ ! -d "$VENV_DIR" ]; then
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
