"""Runtime dependency discovery and startup health checks."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def find_ffmpeg(source_file: str | Path) -> str:
    """Find ffmpeg in the app bundle, pinned runtime, or system PATH."""
    candidate = Path(source_file).resolve().parent.parent / "Resources" / "ffmpeg"
    if candidate.exists():
        return str(candidate)

    try:
        import imageio_ffmpeg

        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    except (ImportError, RuntimeError):
        pass

    found = shutil.which("ffmpeg")
    if found:
        return found

    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(path).exists():
            return path
    return "ffmpeg"


def prepend_executable_directory(executable: str) -> None:
    """Expose a discovered executable to subprocesses without duplicating PATH."""
    executable_dir = str(Path(executable).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if executable_dir not in path_parts:
        os.environ["PATH"] = executable_dir + os.pathsep + os.environ.get("PATH", "")


def check_health(ffmpeg_bin: str, python_executable: str | None = None) -> dict:
    """Return health details for every external runtime dependency."""
    python_executable = python_executable or sys.executable
    checks = {}

    try:
        result = subprocess.run(
            [ffmpeg_bin, "-version"], capture_output=True, text=True, timeout=5
        )
        checks["ffmpeg"] = {
            "ok": result.returncode == 0,
            "path": ffmpeg_bin,
            "detail": (
                result.stdout.split("\n")[0]
                if result.returncode == 0
                else result.stderr[:200]
            ),
        }
    except FileNotFoundError:
        checks["ffmpeg"] = {
            "ok": False,
            "path": ffmpeg_bin,
            "detail": "Binary not found",
        }
    except Exception as error:
        checks["ffmpeg"] = {"ok": False, "path": ffmpeg_bin, "detail": str(error)}

    ytdlp_bin = shutil.which("yt-dlp")
    if ytdlp_bin:
        try:
            result = subprocess.run(
                [ytdlp_bin, "--version"], capture_output=True, text=True, timeout=5
            )
            checks["yt_dlp"] = {
                "ok": result.returncode == 0,
                "path": ytdlp_bin,
                "detail": result.stdout.strip() if result.returncode == 0 else result.stderr[:200],
            }
        except Exception as error:
            checks["yt_dlp"] = {"ok": False, "path": ytdlp_bin, "detail": str(error)}
    else:
        checks["yt_dlp"] = {"ok": False, "path": None, "detail": "Not found on PATH"}

    whisper_bin = shutil.which("whisper")
    if whisper_bin:
        checks["whisper"] = {
            "ok": os.access(whisper_bin, os.X_OK),
            "path": whisper_bin,
            "detail": "CLI executable found",
        }
    else:
        checks["whisper"] = {"ok": False, "path": None, "detail": "Not found on PATH"}

    try:
        result = subprocess.run(
            [python_executable, "-c", "import whisper; print(whisper.__file__)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        checks["whisper_import"] = {
            "ok": result.returncode == 0,
            "detail": "OK" if result.returncode == 0 else result.stderr.strip()[:300],
        }
    except Exception as error:
        checks["whisper_import"] = {"ok": False, "detail": str(error)}

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks["python"] = {
        "ok": sys.version_info >= (3, 10),
        "version": version,
        "architecture": platform.machine(),
        "path": python_executable,
    }
    checks["all_ok"] = all(check.get("ok", False) for check in checks.values())
    return checks
