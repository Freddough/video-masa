#!/bin/bash
# ─── Video Masa Launcher ───
# Handles setup/repair detection, server startup, readiness, and diagnostics.

set -u
umask 077

SYSCTL_BIN="${VIDEOMASA_SYSCTL_BIN:-/usr/sbin/sysctl}"
ARCH_BIN="${VIDEOMASA_ARCH_BIN:-/usr/bin/arch}"
if [ "$("$SYSCTL_BIN" -in sysctl.proc_translated 2>/dev/null || true)" = "1" ]; then
    exec "$ARCH_BIN" -arm64 /bin/bash "$0" "$@"
fi

RESOURCES_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$RESOURCES_DIR/app"
VM_HOME="${VIDEOMASA_HOME:-$HOME/.videomasa}"
VENV_DIR="$VM_HOME/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
WORK_DIR="$VM_HOME/downloads"
COOKIES_DIR="$VM_HOME/cookies"
PID_FILE="$VM_HOME/server.pid"
TOKEN_FILE="$VM_HOME/server.token"
LOG_FILE="$VM_HOME/server.log"
VERSION_FILE="$VM_HOME/version"
DIAGNOSTICS_FILE="$VM_HOME/diagnostics.txt"
PORT="${VIDEOMASA_PORT:-8080}"
READY_ATTEMPTS="${VIDEOMASA_READY_ATTEMPTS:-120}"

OPEN_BIN="${VIDEOMASA_OPEN_BIN:-/usr/bin/open}"
OSASCRIPT_BIN="${VIDEOMASA_OSASCRIPT_BIN:-/usr/bin/osascript}"
CURL_BIN="${VIDEOMASA_CURL_BIN:-/usr/bin/curl}"
PBCOPY_BIN="${VIDEOMASA_PBCOPY_BIN:-/usr/bin/pbcopy}"
API_TOKEN=""

if [ -f "$RESOURCES_DIR/VERSION" ]; then
    APP_VERSION="$(tr -d '[:space:]' < "$RESOURCES_DIR/VERSION")"
else
    APP_VERSION="3.0.3"
fi

mkdir -p "$VM_HOME" "$WORK_DIR" "$COOKIES_DIR"
chmod 700 "$VM_HOME" "$WORK_DIR" "$COOKIES_DIR" 2>/dev/null || true

server_is_ready() {
    "$CURL_BIN" --fail --silent --show-error --max-time 1 \
        --header "X-Video-Masa-Token: $API_TOKEN" \
        "http://127.0.0.1:$PORT/health" 2>/dev/null |
        /usr/bin/grep -q "\"app_version\":\"$APP_VERSION\""
}

new_api_token() {
    if [ -x /usr/bin/openssl ]; then
        /usr/bin/openssl rand -hex 32
    else
        printf '%s%s' "$(/usr/bin/uuidgen | tr -d '-')" "$(/usr/bin/uuidgen | tr -d '-')"
    fi
}

open_app_url() {
    "$OPEN_BIN" "http://127.0.0.1:$PORT/?token=$API_TOKEN" >/dev/null 2>&1 || true
}

pid_matches_app() {
    _pid="$1"
    case "$_pid" in
        ''|*[!0-9]*) return 1 ;;
    esac

    kill -0 "$_pid" 2>/dev/null || return 1
    _command=$(/bin/ps -p "$_pid" -o command= 2>/dev/null || true)
    case "$_command" in
        *"$APP_DIR/app.py"*) return 0 ;;
        *) return 1 ;;
    esac
}

venv_is_healthy() {
    [ -x "$VENV_PYTHON" ] &&
        "$VENV_PYTHON" -c \
            'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1
}

launch_setup() {
    "$OSASCRIPT_BIN" - "$RESOURCES_DIR/setup.sh" <<'APPLESCRIPT'
on run argv
    set setupPath to item 1 of argv
    tell application "Terminal"
        activate
        do script "/bin/bash " & quoted form of setupPath
    end tell
end run
APPLESCRIPT
}

write_diagnostics() {
    {
        echo "Video Masa launch diagnostics"
        echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "App version: $APP_VERSION"
        echo "App directory: $APP_DIR"
        echo "State directory: $VM_HOME"
        echo "Port: $PORT"
        echo "Launcher architecture: $(/usr/bin/arch 2>/dev/null || /usr/bin/uname -m)"
        echo "Venv Python: $VENV_PYTHON"
        if [ -x "$VENV_PYTHON" ]; then
            echo "Python version: $("$VENV_PYTHON" --version 2>&1 || true)"
            echo "Python architecture: $("$VENV_PYTHON" -c 'import platform; print(platform.machine())' 2>&1 || true)"
        else
            echo "Python status: missing or not executable"
        fi
        echo "macOS: $(/usr/bin/sw_vers -productVersion 2>/dev/null || echo unknown)"
        echo
        echo "Last server log lines:"
        if [ -f "$LOG_FILE" ]; then
            tail -40 "$LOG_FILE"
        else
            echo "(no server log)"
        fi
    } > "$DIAGNOSTICS_FILE"
    chmod 600 "$DIAGNOSTICS_FILE" 2>/dev/null || true
}

show_launch_failure() {
    write_diagnostics
    _details=$(tail -30 "$LOG_FILE" 2>/dev/null || true)
    [ -n "$_details" ] || _details="The server exited before it became ready."
    _dialog_text="Video Masa could not start.

$_details

Diagnostics: $DIAGNOSTICS_FILE"

    _action=$("$OSASCRIPT_BIN" - "$_dialog_text" <<'APPLESCRIPT' || true
on run argv
    set dialogText to item 1 of argv
    try
        display dialog dialogText with title "Video Masa" buttons {"Copy Diagnostics", "Open Log", "Repair Installation"} default button "Repair Installation" with icon stop
        return button returned of result
    on error number -128
        return "Cancelled"
    end try
end run
APPLESCRIPT
)

    case "$_action" in
        "Repair Installation")
            rm -f "$VERSION_FILE" "$PID_FILE"
            launch_setup
            ;;
        "Open Log")
            "$OPEN_BIN" "$LOG_FILE" >/dev/null 2>&1 || true
            ;;
        "Copy Diagnostics")
            if [ -x "$PBCOPY_BIN" ]; then
                "$PBCOPY_BIN" < "$DIAGNOSTICS_FILE"
            fi
            ;;
    esac
}

# Reuse an existing server only when both its process identity and health match.
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if pid_matches_app "$PID"; then
        [ -f "$TOKEN_FILE" ] && API_TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")
        if [ -n "$API_TOKEN" ] && server_is_ready; then
            open_app_url
            exit 0
        fi
        kill "$PID" 2>/dev/null || true
        _stop_attempt=0
        while kill -0 "$PID" 2>/dev/null && [ "$_stop_attempt" -lt 10 ]; do
            sleep 0.25
            _stop_attempt=$((_stop_attempt + 1))
        done
    fi
    rm -f "$PID_FILE" "$TOKEN_FILE"
fi

_needs_setup=false
if ! venv_is_healthy; then
    _needs_setup=true
else
    _installed_version=""
    [ -f "$VERSION_FILE" ] && _installed_version=$(tr -d '[:space:]' < "$VERSION_FILE")
    if [ "$_installed_version" != "$APP_VERSION" ]; then
        _needs_setup=true
    fi
fi

if [ "$_needs_setup" = true ]; then
    launch_setup
    exit 0
fi

export PATH="$RESOURCES_DIR:$VENV_DIR/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export VIDEOMASA_WORK_DIR="$WORK_DIR"
export VIDEOMASA_COOKIES_DIR="$COOKIES_DIR"
export VIDEOMASA_OPEN_BROWSER="0"
export VIDEOMASA_PORT="$PORT"
API_TOKEN="$(new_api_token)"
export VIDEOMASA_API_TOKEN="$API_TOKEN"
printf '%s\n' "$API_TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE" 2>/dev/null || true

{
    echo "Starting Video Masa $APP_VERSION"
    echo "Interpreter: $VENV_PYTHON"
    echo "Application: $APP_DIR/app.py"
} > "$LOG_FILE"
chmod 600 "$LOG_FILE" 2>/dev/null || true

"$VENV_PYTHON" "$APP_DIR/app.py" >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
chmod 600 "$PID_FILE" 2>/dev/null || true

cleanup_pid_file() {
    if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$SERVER_PID" ]; then
        rm -f "$PID_FILE" "$TOKEN_FILE"
    fi
}
trap cleanup_pid_file EXIT

_attempt=0
while [ "$_attempt" -lt "$READY_ATTEMPTS" ]; do
    if server_is_ready; then
        open_app_url
        wait "$SERVER_PID" 2>/dev/null || true
        exit 0
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        wait "$SERVER_PID" 2>/dev/null || true
        show_launch_failure
        exit 1
    fi
    _attempt=$((_attempt + 1))
    sleep 0.5
done

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
echo "Timed out waiting for http://127.0.0.1:$PORT/health" >> "$LOG_FILE"
show_launch_failure
exit 1
