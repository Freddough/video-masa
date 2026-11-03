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
LOCK_HASH_FILE="$VM_HOME/lock-hash"
LOCK_DIR="$VM_HOME/setup.lock"
REQUIREMENTS_FILE="$APP_DIR/requirements.lock.txt"

if [ -f "$RESOURCES_DIR/VERSION" ]; then
    APP_VERSION="$(tr -d '[:space:]' < "$RESOURCES_DIR/VERSION")"
else
    APP_VERSION="3.3.0"
fi

# A private Python means a Homebrew upgrade can't break the app, and nobody needs
# developer tools or an administrator password. Hashes are from this release's SHA256SUMS.
PYTHON_RELEASE="20261026"
PYTHON_VERSION="3.12.16"
PYTHON_SHA256_ARM64="353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e"
PYTHON_SHA256_X86_64="37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734"
CURL_BIN="${VIDEOMASA_CURL_BIN:-/usr/bin/curl}"

TEMP_VENV=""
TEMP_PYTHON_DIR=""
VERSION_TEMP=""
VENV_LINK_TEMP=""

cleanup() {
    _status=$?
    trap - EXIT INT TERM
    if [ -n "$TEMP_VENV" ] && [ -e "$TEMP_VENV" ]; then
        rm -rf "$TEMP_VENV"
    fi
    if [ -n "$TEMP_PYTHON_DIR" ]; then
        rm -rf "$TEMP_PYTHON_DIR"
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

# Tell the app's window what's happening. The details go to the log.
status() {
    echo "$*"
    if [ -n "${VIDEOMASA_STATUS_FD:-}" ]; then
        echo "@@STATUS $*" >&"$VIDEOMASA_STATUS_FD"
    fi
}

install_private_python() {
    case "$(/usr/bin/uname -m)" in
        arm64)
            _platform="aarch64-apple-darwin"
            _expected_sha256="$PYTHON_SHA256_ARM64"
            ;;
        x86_64)
            _platform="x86_64-apple-darwin"
            _expected_sha256="$PYTHON_SHA256_X86_64"
            ;;
        *)
            echo "ERROR: Unsupported Mac processor: $(/usr/bin/uname -m)"
            exit 1
            ;;
    esac
    _expected_sha256="${VIDEOMASA_PYTHON_ARCHIVE_SHA256:-$_expected_sha256}"
    PRIVATE_PYTHON_NAME="$PYTHON_VERSION-$PYTHON_RELEASE"
    _python_home="$VM_HOME/python/$PRIVATE_PYTHON_NAME"
    PYTHON="$_python_home/bin/python3"
    if [ -x "$PYTHON" ] && "$PYTHON" -c 'import ssl, venv' >/dev/null 2>&1; then
        return 0
    fi

    status "Downloading Python..."
    mkdir -p "$VM_HOME/python"
    TEMP_PYTHON_DIR=$(/usr/bin/mktemp -d "$VM_HOME/python/.download.XXXXXX")
    _archive="$TEMP_PYTHON_DIR/python.tar.gz"
    "$CURL_BIN" --fail --location --silent --show-error --retry 3 --connect-timeout 20 --output "$_archive" \
        "https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_RELEASE/cpython-$PYTHON_VERSION+$PYTHON_RELEASE-$_platform-install_only_stripped.tar.gz"

    _actual_sha256=$(/usr/bin/shasum -a 256 "$_archive" | /usr/bin/cut -d' ' -f1)
    if [ "$_actual_sha256" != "$_expected_sha256" ]; then
        echo "ERROR: The downloaded Python did not match its expected hash."
        exit 1
    fi

    /usr/bin/tar -xzf "$_archive" -C "$TEMP_PYTHON_DIR"
    rm -rf "$_python_home"
    mv "$TEMP_PYTHON_DIR/python" "$_python_home"
    rm -rf "$TEMP_PYTHON_DIR"
    TEMP_PYTHON_DIR=""
}

PRIVATE_PYTHON_NAME=""
if [ -n "${VIDEOMASA_PYTHON:-}" ]; then
    PYTHON="$VIDEOMASA_PYTHON"
    _version=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
    case "$_version" in
        3.11|3.12) ;;
        *)
            echo "ERROR: VIDEOMASA_PYTHON must be Python 3.11 or 3.12: $VIDEOMASA_PYTHON"
            exit 1
            ;;
    esac
else
    install_private_python
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

status "Installing components. This is the longest step..."
echo "[2/4] Installing locked dependencies (this may take a few minutes)..."
"$TEMP_PYTHON" -m pip install --disable-pip-version-check --no-cache-dir --upgrade "pip==26.1.2"
"$TEMP_PYTHON" -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --requirement "$REQUIREMENTS_FILE"

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

status "Downloading the speech model..."
echo "[3/4] Downloading the default speech model (base, ~140 MB)..."
if [ "${VIDEOMASA_SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
    "$TEMP_PYTHON" -c "import whisper; whisper.load_model('base')"
else
    echo "       Skipped by VIDEOMASA_SKIP_MODEL_DOWNLOAD."
fi
echo "       Done."
echo ""

status "Checking that everything works..."
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
/usr/bin/shasum -a 256 "$REQUIREMENTS_FILE" | /usr/bin/cut -d' ' -f1 > "$LOCK_HASH_FILE"
if [ -n "$PRIVATE_PYTHON_NAME" ]; then
    ln -sfn "$PRIVATE_PYTHON_NAME" "$VM_HOME/python/current"
fi

# A repair or app upgrade returns to the locked yt-dlp until the next update check.
rm -rf "$VM_HOME/ytdlp"

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║     Setup complete! Launching app...       ║"
echo "╚════════════════════════════════════════════╝"
echo ""

if [ "${VIDEOMASA_SKIP_RELAUNCH:-0}" != "1" ]; then
    APP_BUNDLE="$(cd "$RESOURCES_DIR/../.." && pwd)"
    /usr/bin/open "$APP_BUNDLE"
fi
