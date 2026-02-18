@echo off
REM ─── Video Masa — First-Time Setup (Windows) ───
REM Installs Python 3 if needed, creates a venv, and installs all dependencies.

echo.
echo ========================================
echo   Video Masa — First-Time Setup
echo ========================================
echo.

set SCRIPT_DIR=%~dp0
set VM_HOME=%USERPROFILE%\.videomasa
set VENV_DIR=%VM_HOME%\venv

REM ─── Check for Python 3 ───
where python >nul 2>&1
if not errorlevel 1 goto :python_found

REM ─── Python not found — try to install it ───
echo Python 3 not found. Installing it now...
echo.

REM Try winget first (available on Windows 10 1709+ and Windows 11)
where winget >nul 2>&1
if not errorlevel 1 (
    echo Installing Python via winget...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    if not errorlevel 1 goto :refresh_path
)

REM Winget not available — download from python.org
echo Downloading Python from python.org...
set PYTHON_URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
set PYTHON_INSTALLER=%TEMP%\python-installer.exe
curl -L -o "%PYTHON_INSTALLER%" "%PYTHON_URL%"
if errorlevel 1 (
    echo.
    echo ERROR: Failed to download Python installer.
    echo Please install Python manually from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Installing Python (this may take a minute)...
"%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
del "%PYTHON_INSTALLER%"

:refresh_path
REM Refresh PATH so we can find the newly installed python
set PATH=%LOCALAPPDATA%\Programs\Python\Python312\;%LOCALAPPDATA%\Programs\Python\Python312\Scripts\;%PATH%

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python installation completed but python was not found in PATH.
    echo Please close this window, restart your computer, and try again.
    echo.
    pause
    exit /b 1
)

:python_found
python --version
echo.

REM ─── Create virtual environment ───
echo [1/3] Creating Python environment...
python -m venv "%VENV_DIR%"
call "%VENV_DIR%\Scripts\activate.bat"
echo        Done.
echo.

REM ─── Install dependencies ───
echo [2/3] Installing dependencies (this may take a few minutes)...
pip install --upgrade pip --quiet
pip install flask openai-whisper yt-dlp
echo        Done.
echo.

REM ─── Pre-download the default Whisper model ───
echo [3/3] Downloading default speech model (base, ~140 MB)...
python -c "import whisper; whisper.load_model('base')"
echo        Done.
echo.

echo ========================================
echo   Setup complete! Launching app...
echo ========================================
echo.

REM ─── Launch the app normally ───
call "%SCRIPT_DIR%launcher.bat"
