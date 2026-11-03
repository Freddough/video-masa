#!/bin/bash
# ─── Video Masa — Uninstall ───
# Started by the app, so it waits for the app to stop before removing anything.
# Usage: uninstall.sh <server pid> <state folder> <whisper model folder>

set -u

SERVER_PID="$1"
VM_HOME="$2"
WHISPER_CACHE="$3"
RESOURCES_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_BUNDLE="$(cd "$RESOURCES_DIR/../.." && pwd)"
TRASH_BIN="${VIDEOMASA_TRASH_BIN:-/usr/bin/trash}"

# Never remove a folder this script did not expect.
case "$VM_HOME" in
    */.videomasa) ;;
    *)
        echo "Refusing to remove unexpected folder: $VM_HOME"
        exit 1
        ;;
esac

_attempt=0
while kill -0 "$SERVER_PID" 2>/dev/null && [ "$_attempt" -lt 120 ]; do
    sleep 0.25
    _attempt=$((_attempt + 1))
done
if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Video Masa did not stop, so nothing was removed."
    exit 1
fi

rm -rf "$VM_HOME"

# The model folder can be shared with other Whisper tools, so only this app's models go.
for _model in tiny base small medium; do
    rm -f "$WHISPER_CACHE/$_model.pt"
done
rmdir "$WHISPER_CACHE" 2>/dev/null || true

case "$APP_BUNDLE" in
    *.app)
        if [ -x "$TRASH_BIN" ]; then
            "$TRASH_BIN" "$APP_BUNDLE" >/dev/null 2>&1 || true
        fi
        ;;
esac
