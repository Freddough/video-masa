#!/bin/bash
# ─── Video Masa — First-Time Setup / Upgrade / Repair ───
# Builds and verifies a replacement environment before installing it atomically.

set -Eeuo pipefail
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
VERSION_FILE="$VM_HOME/version"
LOCK_DIR="$VM_HOME/setup.lock"
REQUIREMENTS_FILE="$APP_DIR/requirements.lock.txt"

if [ -f "$RESOURCES_DIR/VERSION" ]; then
    APP_VERSION="$(tr -d '[:space:]' < "$RESOURCES_DIR/VERSION")"
else
    APP_VERSION="3.1.0"
fi

TEMP_VENV=""
VERSION_TEMP=""
VENV_LINK_TEMP=""

cleanup() {
    _status=$?
    trap - EXIT INT TERM
    if [ -n "$TEMP_VENV" ] && [ -e "$TEMP_VENV" ]; then
        rm -rf "$TEMP_VENV"
    fi
    if [ -n "$VERSION_TEMP" ]; then
        rm -f "$VERSION_TEMP"
    fi
    if [ -n "$VENV_LINK_TEMP" ]; then
        rm -f "$VENV_LINK_TEMP"
    fi
    rm -rf "$LOCK_DIR"
    exit "$_status"
}
trap cleanup EXIT INT TERM

mkdir -p "$VM_HOME"
chmod 700 "$VM_HOME" 2>/dev/null || true

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    _lock_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    if [ -n "$_lock_pid" ] && kill -0 "$_lock_pid" 2>/dev/null; then
        echo "Another Video Masa setup is already running (PID $_lock_pid)."
        exit 1
    fi
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║     Video Masa — Setup / Repair            ║"
echo "╚════════════════════════════════════════════╝"
echo ""

find_python() {
    if [ -n "${VIDEOMASA_PYTHON:-}" ]; then
        _candidates=("$VIDEOMASA_PYTHON")
    else
        _candidates=(
            /opt/homebrew/bin/python3
            /usr/local/bin/python3
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
            /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
            /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
            /Library/Frameworks/Python.framework/Versions/3.10/bin/python3
            python3
            python
        )
    fi

    for candidate in "${_candidates[@]}"; do
        _resolved=$(command -v "$candidate" 2>/dev/null || true)
        [ -n "$_resolved" ] || continue
        if "$_resolved" -c \
            'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1; then
            echo "$_resolved"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python || true)

if [ -z "$PYTHON" ]; then
    echo "Python 3.10+ not found. Installing it now..."
    echo ""

    if command -v brew >/dev/null 2>&1; then
        echo "Installing Python via Homebrew..."
        brew install python
    else
        PYTHON_VERSION="3.12.8"
        PKG_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-macos11.pkg"
        PKG_PATH="/tmp/python-${PYTHON_VERSION}.pkg"

        echo "Downloading the signed Python ${PYTHON_VERSION} installer..."
        /usr/bin/curl --fail --location --progress-bar -o "$PKG_PATH" "$PKG_URL"

        echo "Verifying Python Software Foundation installer signature..."
        _signature=$(/usr/sbin/pkgutil --check-signature "$PKG_PATH" 2>&1)
        echo "$_signature"
        echo "$_signature" | grep -q "Developer ID Installer: Python Software Foundation" || {
            echo "ERROR: The downloaded installer is not signed by the Python Software Foundation."
            rm -f "$PKG_PATH"
            exit 1
        }

        echo ""
        echo "Installing Python ${PYTHON_VERSION}..."
        echo "(You may be prompted for your password)"
        sudo /usr/sbin/installer -pkg "$PKG_PATH" -target /
        rm -f "$PKG_PATH"
    fi

    export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:$PATH"
    PYTHON=$(find_python || true)

    if [ -z "$PYTHON" ]; then
        echo "ERROR: Python installation completed but Python 3.10+ was not found."
        exit 1
    fi
fi

echo "Found $("$PYTHON" --version 2>&1) at $PYTHON"
echo ""

[ -f "$REQUIREMENTS_FILE" ] || {
    echo "ERROR: Missing locked dependency file: $REQUIREMENTS_FILE"
    exit 1
}

_runtime_timestamp=$(date '+%Y%m%d-%H%M%S')
TEMP_VENV="$VM_HOME/venv.runtime-${APP_VERSION}-${_runtime_timestamp}-$$"
rm -rf "$TEMP_VENV"

echo "[1/4] Creating a temporary Python environment..."
"$PYTHON" -m venv "$TEMP_VENV"
TEMP_PYTHON="$TEMP_VENV/bin/python"
echo "       Done."
echo ""

echo "[2/4] Installing locked dependencies (this may take a few minutes)..."
"$TEMP_PYTHON" -m pip install --disable-pip-version-check --upgrade "pip==26.1.2"
"$TEMP_PYTHON" -m pip install --disable-pip-version-check --require-hashes --requirement "$REQUIREMENTS_FILE"

# Whisper invokes a command named `ffmpeg`. The pinned imageio-ffmpeg wheel
# supplies the correct macOS architecture binary, so expose it in venv/bin.
if [ -n "${VIDEOMASA_FFMPEG:-}" ]; then
    FFMPEG_EXE="$VIDEOMASA_FFMPEG"
else
    FFMPEG_EXE=$("$TEMP_PYTHON" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')
fi
[ -x "$FFMPEG_EXE" ] || {
    echo "ERROR: The pinned ffmpeg executable is missing or not executable: $FFMPEG_EXE"
    exit 1
}
ln -s "$FFMPEG_EXE" "$TEMP_VENV/bin/ffmpeg"
echo "       Done."
echo ""

echo "[3/4] Downloading the default speech model (base, ~140 MB)..."
if [ "${VIDEOMASA_SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
    "$TEMP_PYTHON" -c "import whisper; whisper.load_model('base')"
else
    echo "       Skipped by VIDEOMASA_SKIP_MODEL_DOWNLOAD."
fi
echo "       Done."
echo ""

echo "[4/4] Verifying the complete installation..."
"$TEMP_PYTHON" - "$APP_DIR/app.py" <<'PY'
from pathlib import Path
import sys

source_path = Path(sys.argv[1])
compile(source_path.read_bytes(), str(source_path), "exec")
PY
"$TEMP_PYTHON" -m pip check
"$TEMP_PYTHON" - <<'PY'
from importlib.metadata import version
import flask
import imageio_ffmpeg
import whisper
print(f"  ✓ Flask {version('Flask')}")
print(f"  ✓ imageio-ffmpeg {version('imageio-ffmpeg')}")
print(f"  ✓ whisper {version('openai-whisper')}")
PY
"$TEMP_PYTHON" -m yt_dlp --version | sed 's/^/  ✓ yt-dlp /'
"$TEMP_VENV/bin/ffmpeg" -version 2>&1 | head -1 | sed 's/^/  ✓ /'

echo ""
echo "Installing the verified environment..."
_timestamp=$(date '+%Y%m%d-%H%M%S')
_backup=""
VENV_LINK_TEMP="$VM_HOME/venv.link.$$"
ln -s "$(basename "$TEMP_VENV")" "$VENV_LINK_TEMP"

if [ -e "$VENV_DIR" ] || [ -L "$VENV_DIR" ]; then
    if [ -x "$VENV_DIR/bin/python" ] &&
        "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
        _backup="$VM_HOME/venv.previous-$_timestamp"
    else
        _backup="$VM_HOME/venv.broken-$_timestamp"
    fi
    mv "$VENV_DIR" "$_backup"
    echo "Preserved the previous environment at $_backup"
fi

if ! mv "$VENV_LINK_TEMP" "$VENV_DIR"; then
    VENV_LINK_TEMP=""
    [ -n "$_backup" ] && mv "$_backup" "$VENV_DIR"
    echo "ERROR: Could not install the verified environment."
    exit 1
fi
VENV_LINK_TEMP=""
TEMP_VENV=""

VERSION_TEMP="$VM_HOME/version.new.$$"
printf '%s\n' "$APP_VERSION" > "$VERSION_TEMP"
mv "$VERSION_TEMP" "$VERSION_FILE"
VERSION_TEMP=""
chmod 600 "$VERSION_FILE" 2>/dev/null || true

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║     Setup complete! Launching app...       ║"
echo "╚════════════════════════════════════════════╝"
echo ""

if [ "${VIDEOMASA_SKIP_RELAUNCH:-0}" != "1" ]; then
    APP_BUNDLE="$(cd "$RESOURCES_DIR/../.." && pwd)"
    /usr/bin/open "$APP_BUNDLE"
fi
