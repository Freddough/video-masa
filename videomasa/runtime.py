"""Runtime dependency discovery and startup health checks."""

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


YTDLP_STALE_DAYS = 60
YTDLP_CHECK_OVERDUE_DAYS = 7


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


def locked_ytdlp_fallback(python_executable: str | None = None) -> str | None:
    """Return the environment's own yt-dlp when PATH currently prefers an updated copy."""
    locked = Path(python_executable or sys.executable).parent / "yt-dlp"
    active = shutil.which("yt-dlp")
    if not active or not locked.exists():
        return None
    return str(locked) if Path(active).resolve() != locked.resolve() else None


def ytdlp_freshness(updates_dir: str | Path | None, now: float | None = None) -> dict:
    """Report whether self-updating has stalled.

    Age alone proves nothing, because yt-dlp can go over 80 days between releases
    while fully up to date, so an old version also needs failing or overdue checks.
    """
    if not updates_dir:
        return {"stale": False}
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return {"stale": False}
    try:
        version = subprocess.run(
            [ytdlp, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        match = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version)
        released = datetime(*(int(part) for part in match.groups())).timestamp() if match else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"stale": False}
    if released is None:
        return {"stale": False}

    updates_dir = Path(updates_dir)
    current_time = time.time() if now is None else now
    age_days = int((current_time - released) // 86_400)
    try:
        last_error = (updates_dir / "last-error").read_text().strip()
    except OSError:
        last_error = ""
    try:
        check_age = current_time - (updates_dir / "last-check").stat().st_mtime
        overdue = check_age > YTDLP_CHECK_OVERDUE_DAYS * 86_400
    except OSError:
        overdue = True
    return {
        "stale": age_days > YTDLP_STALE_DAYS and (bool(last_error) or overdue),
        "version": version,
        "age_days": age_days,
        "last_error": last_error[:300],
        "updates_off": (updates_dir.parent / "no-ytdlp-update").exists(),
    }


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
