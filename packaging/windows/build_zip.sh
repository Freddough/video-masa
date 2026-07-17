#!/bin/bash
# ─── Video Masa — Windows Build Script ───
# Assembles the app folder and creates a distributable ZIP.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build/windows/VideoMasa"
DIST_DIR="$PROJECT_DIR/dist"
APP_VERSION="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")"

echo ""
echo "Building Video Masa for Windows..."
echo ""

# ─── Clean previous build ───
rm -rf "$PROJECT_DIR/build/windows"
mkdir -p "$BUILD_DIR/app/templates"
mkdir -p "$DIST_DIR"

# ─── Copy launcher and setup scripts ───
cp "$SCRIPT_DIR/launcher.bat" "$BUILD_DIR/"
cp "$SCRIPT_DIR/setup.bat" "$BUILD_DIR/"
echo "  [+] launcher.bat + setup.bat"

# ─── Copy Flask application ───
cp "$PROJECT_DIR/app.py" "$BUILD_DIR/app/"
cp -R "$PROJECT_DIR/videomasa" "$BUILD_DIR/app/"
find "$BUILD_DIR/app/videomasa" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$BUILD_DIR/app/videomasa" -type f -name '*.pyc' -delete
cp "$PROJECT_DIR/VERSION" "$BUILD_DIR/"
cp "$PROJECT_DIR/requirements.txt" "$BUILD_DIR/app/"
cp "$PROJECT_DIR/requirements.lock.txt" "$BUILD_DIR/app/"
cp "$PROJECT_DIR/templates/index.html" "$BUILD_DIR/app/templates/"
echo "  [+] Flask app + backend package"

# ─── Copy icon ───
if [ -f "$SCRIPT_DIR/icon.ico" ]; then
    cp "$SCRIPT_DIR/icon.ico" "$BUILD_DIR/"
    echo "  [+] icon.ico"
fi

# ─── Copy ffmpeg ───
if [ -f "$SCRIPT_DIR/ffmpeg.exe" ]; then
    cp "$SCRIPT_DIR/ffmpeg.exe" "$BUILD_DIR/"
    echo "  [+] ffmpeg.exe"
else
    echo "  [ ] ffmpeg.exe — not found!"
fi

echo ""

# ─── Create ZIP ───
ZIP_NAME="VideoMasa-${APP_VERSION}-Windows.zip"
rm -f "$DIST_DIR/$ZIP_NAME"
cd "$PROJECT_DIR/build/windows"
zip -r "$DIST_DIR/$ZIP_NAME" "VideoMasa" -x "*.DS_Store"

echo ""
echo "════════════════════════════════════════════"
echo "  Build complete!"
echo "  ZIP: dist/$ZIP_NAME"
echo "════════════════════════════════════════════"
echo ""
