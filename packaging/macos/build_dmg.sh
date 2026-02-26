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

# ─── Prepare DMG staging ───
DMG_NAME="VideoMasa-2.3.dmg"
DMG_STAGING="$BUILD_DIR/dmg_staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING/.background"

cp -R "$APP_BUNDLE" "$DMG_STAGING/"

# Create Finder alias to /Applications (regular file, supports custom icons)
osascript -e "
tell application \"Finder\"
    make new alias file at (POSIX file \"$DMG_STAGING\" as alias) to (POSIX file \"/Applications\" as alias)
    set name of result to \"Applications\"
end tell
" 2>/dev/null && echo "  [+] Applications alias" || {
    # Fallback to symlink if Finder alias creation fails
    ln -s /Applications "$DMG_STAGING/Applications"
    echo "  [+] Applications symlink (fallback)"
}

# Apply custom icon to Applications alias
if [ -f "$SCRIPT_DIR/applications_icon.png" ]; then
    ICON_TMP="$BUILD_DIR/app_icon_tmp"
    mkdir -p "$ICON_TMP/icon.iconset"

    for SIZE in 16 32 128 256 512; do
        sips -z $SIZE $SIZE "$SCRIPT_DIR/applications_icon.png" \
            --out "$ICON_TMP/icon.iconset/icon_${SIZE}x${SIZE}.png" > /dev/null 2>&1
    done

    iconutil -c icns "$ICON_TMP/icon.iconset" -o "$ICON_TMP/app_folder.icns" 2>/dev/null

    osascript -e "
        use framework \"AppKit\"
        set iconImage to current application's NSImage's alloc()'s initWithContentsOfFile:\"$ICON_TMP/app_folder.icns\"
        set iconOK to current application's NSWorkspace's sharedWorkspace()'s setIcon:iconImage forFile:\"$DMG_STAGING/Applications\" options:0
        return iconOK as boolean
    " 2>/dev/null | grep -q "true" && echo "  [+] Custom Applications icon" || echo "  [ ] Custom Applications icon skipped"

    rm -rf "$ICON_TMP"
fi

# Generate background image
echo "Generating DMG background..."
python3 "$SCRIPT_DIR/create_dmg_background.py" "$DMG_STAGING/.background/background.png"

echo ""

# ─── Create DMG ───
echo "Creating DMG..."
rm -f "$DIST_DIR/$DMG_NAME"
TEMP_DMG="$BUILD_DIR/temp_rw.dmg"
rm -f "$TEMP_DMG"

# Detach any existing volumes with the same name
hdiutil detach "/Volumes/$APP_NAME" 2>/dev/null || true

# Calculate size: app size + 20MB headroom
APP_SIZE=$(du -sm "$DMG_STAGING" | awk '{print $1}')
DMG_SIZE=$(( APP_SIZE + 20 ))

# Create read-write DMG
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDRW \
    -size "${DMG_SIZE}m" \
    "$TEMP_DMG" 2>/dev/null

# Mount and apply visual layout
hdiutil attach -readwrite -noverify -noautoopen "$TEMP_DMG" > /dev/null
MOUNT_DIR="/Volumes/$APP_NAME"

echo "  Mounted at: $MOUNT_DIR"

# Let Finder index the volume
sleep 2

osascript <<APPLESCRIPT
tell application "Finder"
    tell disk "$APP_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set bounds of container window to {100, 100, 700, 500}
        set theViewOptions to icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 80
        set background picture of theViewOptions to file ".background:background.png"
        set position of item "${APP_NAME}.app" of container window to {150, 180}
        set position of item "Applications" of container window to {450, 180}
        close
        open
        update without registering applications
        delay 3
        close
    end tell
end tell
APPLESCRIPT

sync
sleep 2

# Unmount
hdiutil detach "$MOUNT_DIR" -force 2>/dev/null

# Convert to compressed read-only
hdiutil convert "$TEMP_DMG" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DIST_DIR/$DMG_NAME" 2>/dev/null

rm -f "$TEMP_DMG"
rm -rf "$DMG_STAGING"

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
