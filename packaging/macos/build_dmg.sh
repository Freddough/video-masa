#!/bin/bash
# ─── Video Masa — macOS Build Script ───
# Assembles the .app bundle and creates a distributable DMG.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build/macos"
DIST_DIR="$PROJECT_DIR/dist"
APP_NAME="Video Masa"
APP_BUNDLE="$BUILD_DIR/${APP_NAME}.app"

echo ""
echo "Building ${APP_NAME}.app..."
echo ""

# ─── Clean previous build ───
rm -rf "$BUILD_DIR"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources/app/templates"
mkdir -p "$DIST_DIR"

# ─── Info.plist ───
cp "$SCRIPT_DIR/Info.plist" "$APP_BUNDLE/Contents/"
echo "  [+] Info.plist"

# ─── App icon ───
if [ -f "$SCRIPT_DIR/icon.icns" ]; then
    cp "$SCRIPT_DIR/icon.icns" "$APP_BUNDLE/Contents/Resources/"
    echo "  [+] icon.icns"
else
    echo "  [ ] icon.icns — not found (app will use default icon)"
    echo "      Place an icon.icns file in packaging/macos/ to add a custom icon"
fi

# ─── Stub executable ───
cat > "$APP_BUNDLE/Contents/MacOS/VideoMasa" << 'STUB'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$DIR/Resources/launcher.sh"
STUB
chmod +x "$APP_BUNDLE/Contents/MacOS/VideoMasa"
echo "  [+] Stub executable"

# ─── Launcher and setup scripts ───
cp "$SCRIPT_DIR/launcher.sh" "$APP_BUNDLE/Contents/Resources/"
cp "$SCRIPT_DIR/setup.sh" "$APP_BUNDLE/Contents/Resources/"
chmod +x "$APP_BUNDLE/Contents/Resources/launcher.sh"
chmod +x "$APP_BUNDLE/Contents/Resources/setup.sh"
echo "  [+] launcher.sh + setup.sh"

# ─── Flask application ───
cp "$PROJECT_DIR/app.py" "$APP_BUNDLE/Contents/Resources/app/"
cp "$PROJECT_DIR/requirements.txt" "$APP_BUNDLE/Contents/Resources/app/"
cp "$PROJECT_DIR/templates/index.html" "$APP_BUNDLE/Contents/Resources/app/templates/"
echo "  [+] Flask app (app.py, templates, requirements.txt)"

# ─── ffmpeg binary ───
if [ -f "$SCRIPT_DIR/ffmpeg" ]; then
    cp "$SCRIPT_DIR/ffmpeg" "$APP_BUNDLE/Contents/Resources/"
    chmod +x "$APP_BUNDLE/Contents/Resources/ffmpeg"
    echo "  [+] ffmpeg binary"
else
    echo "  [ ] ffmpeg binary — not found!"
    echo "      Download a static macOS build from: https://evermeet.cx/ffmpeg/"
    echo "      Place the 'ffmpeg' binary in packaging/macos/"
    echo ""
    echo "      The app will still work if ffmpeg is installed on the user's system"
    echo "      (e.g. via 'brew install ffmpeg'), but bundling it is recommended."
fi

echo ""

# ─── Ad-hoc code sign ───
echo "Code signing (ad-hoc)..."
codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null && echo "  [+] Signed" || echo "  [ ] Signing skipped (codesign not available)"

echo ""

# ─── Create DMG ───
DMG_NAME="VideoMasa-1.0.dmg"
echo "Creating DMG..."
# Remove old DMG if present
rm -f "$DIST_DIR/$DMG_NAME"
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$BUILD_DIR" \
    -ov \
    -format UDZO \
    "$DIST_DIR/$DMG_NAME" 2>/dev/null

echo ""
echo "════════════════════════════════════════════"
echo "  Build complete!"
echo "  DMG: dist/$DMG_NAME"
echo "  App: build/macos/${APP_NAME}.app"
echo "════════════════════════════════════════════"
echo ""
echo "To test without the DMG, run:"
echo "  open \"$APP_BUNDLE\""
echo ""
