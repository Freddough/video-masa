#!/bin/bash
# ─── Video Masa — First-Time Setup ───
# Installs Python 3 if needed, creates a venv, and installs all dependencies.

set -e

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║     Video Masa — First-Time Setup          ║"
echo "╚════════════════════════════════════════════╝"
echo ""

RESOURCES_DIR="$(cd "$(dirname "$0")" && pwd)"
VM_HOME="$HOME/.videomasa"
VENV_DIR="$VM_HOME/venv"

# ─── Find or install Python 3 ───
find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" &>/dev/null; then
            version=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
            major=$(echo "$version" | cut -d. -f1)
            if [ "$major" = "3" ]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(find_python || true)

if [ -z "$PYTHON" ]; then
    echo "Python 3 not found. Installing it now..."
    echo ""

    if command -v brew &>/dev/null; then
        # Homebrew is available — use it
        echo "Installing Python via Homebrew..."
        brew install python
    else
        # Download the official Python.org installer
        PYTHON_VERSION="3.12.8"
        PKG_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-macos11.pkg"
        PKG_PATH="/tmp/python-${PYTHON_VERSION}.pkg"

        echo "Downloading Python ${PYTHON_VERSION} from python.org..."
        curl -L --progress-bar -o "$PKG_PATH" "$PKG_URL"

        echo ""
        echo "Installing Python ${PYTHON_VERSION}..."
        echo "(You may be prompted for your password)"
        echo ""
        sudo installer -pkg "$PKG_PATH" -target /
        rm -f "$PKG_PATH"
    fi

    echo ""

    # Re-detect after install
    # The python.org installer puts python3 in /usr/local/bin (Intel) or /Library/Frameworks/...
    export PATH="/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"
    PYTHON=$(find_python || true)

    if [ -z "$PYTHON" ]; then
        echo "ERROR: Python installation completed but python3 was not found."
        echo "Please restart your Terminal and try opening Video Masa again."
        echo ""
        read -rp "Press Enter to exit..."
        exit 1
    fi
fi

echo "Found $($PYTHON --version)"
echo ""

# ─── Create virtual environment ───
echo "[1/3] Creating Python environment..."
"$PYTHON" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
echo "       Done."
echo ""

# ─── Install dependencies ───
echo "[2/3] Installing dependencies (this may take a few minutes)..."
echo "       Installing pip packages: flask, openai-whisper, yt-dlp"
pip install --upgrade pip --quiet
pip install flask openai-whisper yt-dlp 2>&1 | while IFS= read -r line; do
    # Show a simplified progress indicator
    if echo "$line" | grep -q "^Collecting\|^Downloading\|^Installing\|^Successfully"; then
        echo "       $line"
    fi
done
echo "       Done."
echo ""

# ─── Pre-download the default Whisper model ───
echo "[3/3] Downloading default speech model (base, ~140 MB)..."
python -c "import whisper; whisper.load_model('base')" 2>&1 | tail -1
echo "       Done."
echo ""

echo "╔════════════════════════════════════════════╗"
echo "║     Setup complete! Launching app...       ║"
echo "╚════════════════════════════════════════════╝"
echo ""

# ─── Launch the app normally ───
bash "$RESOURCES_DIR/launcher.sh"

echo "Video Masa is running at http://localhost:8080"
echo "You can close this Terminal window."
echo ""
