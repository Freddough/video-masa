@echo off
REM ─── Video Masa Launcher (Windows) ───
REM Handles first-run setup detection, server startup, and browser opening.

set VM_HOME=%USERPROFILE%\.videomasa
set VENV_DIR=%VM_HOME%\venv
set WORK_DIR=%VM_HOME%\downloads
set PID_FILE=%VM_HOME%\server.pid
set PORT=8080
set SCRIPT_DIR=%~dp0

REM ─── Create home directory ───
if not exist "%VM_HOME%" mkdir "%VM_HOME%"
if not exist "%WORK_DIR%" mkdir "%WORK_DIR%"

REM ─── First-run: no venv yet → run setup ───
if not exist "%VENV_DIR%" (
    call "%SCRIPT_DIR%setup.bat"
    exit /b
)

REM ─── Normal launch: start server + open browser ───
set PATH=%SCRIPT_DIR%;%PATH%
set VIDEOMASA_WORK_DIR=%WORK_DIR%
set VIDEOMASA_OPEN_BROWSER=1
set VIDEOMASA_PORT=%PORT%

call "%VENV_DIR%\Scripts\activate.bat"
cd /d "%SCRIPT_DIR%app"
start /b python app.py
timeout /t 2 /nobreak >nul
start http://localhost:%PORT%
