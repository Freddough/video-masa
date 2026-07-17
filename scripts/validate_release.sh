#!/bin/bash
# Repeatable source and optional macOS artifact release gates.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_VERSION="$(tr -d '[:space:]' < VERSION)"
PYTHON_CACHE_DIR="$(mktemp -d /tmp/videomasa-validation-cache.XXXXXX)"
export PYTHONPYCACHEPREFIX="$PYTHON_CACHE_DIR"
trap 'rm -rf "$PYTHON_CACHE_DIR"' EXIT

echo "Validating Video Masa $APP_VERSION source..."
"$PYTHON_BIN" -m unittest discover -s tests -v
"$PYTHON_BIN" -m py_compile app.py videomasa/*.py tests/*.py
bash -n packaging/macos/build_dmg.sh packaging/macos/launcher.sh packaging/macos/setup.sh
awk '/<script>/{capture=1; next} /<\/script>/{capture=0} capture' templates/index.html | node --check -
plutil -lint packaging/macos/Info.plist

PLIST_VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' packaging/macos/Info.plist)
[ "$PLIST_VERSION" = "$APP_VERSION" ] || {
    echo "Version mismatch: VERSION=$APP_VERSION Info.plist=$PLIST_VERSION"
    exit 1
}
/usr/bin/grep -q "read_app_version(__file__, \"$APP_VERSION\")" app.py
/usr/bin/grep -q "APP_VERSION=\"$APP_VERSION\"" packaging/macos/launcher.sh
/usr/bin/grep -q "APP_VERSION=\"$APP_VERSION\"" packaging/macos/setup.sh
git diff --check
rm -rf "$PYTHON_CACHE_DIR"
trap - EXIT

if [ "$#" -gt 0 ]; then
    DMG_PATH="$1"
    [ -f "$DMG_PATH" ] || { echo "DMG not found: $DMG_PATH"; exit 1; }
    echo "Validating artifact $DMG_PATH..."
    hdiutil verify "$DMG_PATH"
    codesign --verify --verbose=2 "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG_PATH"

    MOUNT_DIR="$(mktemp -d /tmp/videomasa-release-gate.XXXXXX)"
    cleanup_mount() {
        hdiutil detach "$MOUNT_DIR" -quiet 2>/dev/null || true
        rmdir "$MOUNT_DIR" 2>/dev/null || true
    }
    trap cleanup_mount EXIT
    hdiutil attach "$DMG_PATH" -readonly -nobrowse -mountpoint "$MOUNT_DIR" -quiet
    codesign --verify --deep --strict --verbose=2 "$MOUNT_DIR/Video Masa.app"
    spctl --assess --type execute --verbose=2 "$MOUNT_DIR/Video Masa.app"
    BUNDLE_VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
        "$MOUNT_DIR/Video Masa.app/Contents/Info.plist")
    [ "$BUNDLE_VERSION" = "$APP_VERSION" ] || {
        echo "Artifact version mismatch: expected $APP_VERSION, found $BUNDLE_VERSION"
        exit 1
    }
    cleanup_mount
    trap - EXIT
fi

echo "Release validation passed."
