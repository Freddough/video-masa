#!/bin/bash
# ─── Video Masa — Windows Build Script ───
# Assembles the app folder and creates a distributable ZIP.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build/windows/VideoMasa"
DIST_DIR="$PROJECT_DIR/dist"

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
cp "$PROJECT_DIR/requirements.txt" "$BUILD_DIR/app/"
cp "$PROJECT_DIR/templates/index.html" "$BUILD_DIR/app/templates/"
echo "  [+] Flask app"

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
ZIP_NAME="VideoMasa-1.0-Windows.zip"
rm -f "$DIST_DIR/$ZIP_NAME"
cd "$PROJECT_DIR/build/windows"
zip -r "$DIST_DIR/$ZIP_NAME" "VideoMasa" -x "*.DS_Store"

echo ""
echo "════════════════════════════════════════════"
echo "  Build complete!"
echo "  ZIP: dist/$ZIP_NAME"
echo "════════════════════════════════════════════"
echo ""
