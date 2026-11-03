"""Keep a self-updating copy of yt-dlp beside the locked desktop environment.

Sites change faster than app releases, so a pinned yt-dlp goes stale. Updates
install into their own folder and never touch the locked environment. The
launcher puts <updates_dir>/bin ahead of that environment on PATH, so removing
the link there falls back to the locked yt-dlp.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path


CHECK_INTERVAL_SECONDS = 24 * 60 * 60
COOLDOWN_SECONDS = 2 * 24 * 60 * 60
INSTALL_TIMEOUT_SECONDS = 600
KEEP_RELEASES = 2
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"


def parse_version(text):
    """Return a comparable date version, so 2026.07.04 equals 2026.7.4, or None."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    return tuple(int(part) for part in text.split("."))


def _run(command, timeout) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-400:]
        raise RuntimeError(f"{Path(command[0]).name} exited {result.returncode}: {detail}")
    return result.stdout


def _fetch_pypi_data() -> dict:
    return json.loads(
        _run(["/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "30", PYPI_URL], 40)
    )


def _supports_python(requires_python, python_version) -> bool:
    match = re.match(r">=\s*(\d+)\.(\d+)", requires_python or "")
    return not match or python_version >= (int(match.group(1)), int(match.group(2)))


def pick_release(pypi_data, now=None, python_version=None):
    """Return the newest stable release that has been out long enough and runs on this Python.

    The wait gives a bad or compromised release time to be noticed and pulled.
    """
    now = time.time() if now is None else now
    python_version = sys.version_info[:2] if python_version is None else python_version
    best = None
    for version, files in pypi_data.get("releases", {}).items():
        parsed = parse_version(version)
        wheel = next((item for item in files if item.get("packagetype") == "bdist_wheel"), None)
        if parsed is None or wheel is None or wheel.get("yanked"):
            continue
        if not _supports_python(wheel.get("requires_python"), python_version):
            continue
        uploaded = wheel.get("upload_time_iso_8601", "").replace("Z", "+00:00")
        try:
            if now - datetime.fromisoformat(uploaded).timestamp() < COOLDOWN_SECONDS:
                continue
        except ValueError:
            continue
        if best is None or parsed > parse_version(best):
            best = version
    return best


def _current_version(updates_dir: Path):
    link = updates_dir / "bin" / "yt-dlp"
    if link.exists():
        try:
            return parse_version(_run([str(link), "--version"], 60))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
    return parse_version(metadata.version("yt-dlp"))


def _check_is_due(stamp: Path) -> bool:
    try:
        elapsed = time.time() - stamp.stat().st_mtime
    except OSError:
        return True
    return not 0 <= elapsed < CHECK_INTERVAL_SECONDS


def _install_release(updates_dir: Path, version: str) -> Path:
    """Install at the final path because a venv's scripts cannot be moved afterwards."""
    release = updates_dir / "releases" / version
    ytdlp = release / "bin" / "yt-dlp"
    shutil.rmtree(release, ignore_errors=True)
    release.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run([sys.executable, "-m", "venv", "--without-pip", str(release)], 120)
        _run(
            [
                sys.executable, "-m", "pip", "--python", str(release / "bin" / "python"),
                "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                "--only-binary", ":all:", f"yt-dlp[default]=={version}",
            ],
            INSTALL_TIMEOUT_SECONDS,
        )
        installed = _run([str(ytdlp), "--version"], 60).strip()
        if parse_version(installed) != parse_version(version):
            raise RuntimeError(f"expected yt-dlp {version}, found {installed!r}")
    except Exception:
        shutil.rmtree(release, ignore_errors=True)
        raise
    return ytdlp


def _activate_release(updates_dir: Path, ytdlp: Path) -> None:
    link = updates_dir / "bin" / "yt-dlp"
    link.parent.mkdir(parents=True, exist_ok=True)
    temp_link = link.with_name(f".yt-dlp.{os.getpid()}")
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(ytdlp)
    os.replace(temp_link, link)


def _prune_releases(updates_dir: Path) -> None:
    """Keep the previous release too, since a running download may still be using it."""
    releases = [
        path for path in (updates_dir / "releases").iterdir()
        if path.is_dir() and parse_version(path.name)
    ]
    releases.sort(key=lambda path: parse_version(path.name), reverse=True)
    for old_release in releases[KEEP_RELEASES:]:
        shutil.rmtree(old_release, ignore_errors=True)


def update_ytdlp(updates_dir: Path) -> str:
    """Install a newer yt-dlp if a check is due. Failures raise and leave no stamp to retry."""
    updates_dir.mkdir(parents=True, exist_ok=True)
    with open(updates_dir / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "Another yt-dlp update is already running."

        stamp = updates_dir / "last-check"
        if not _check_is_due(stamp):
            return "Checked for a yt-dlp update recently."

        latest = pick_release(_fetch_pypi_data())
        if latest is None:
            stamp.touch()
            return "No yt-dlp release on PyPI is ready to install yet."

        current_version = _current_version(updates_dir)
        if current_version is not None and current_version >= parse_version(latest):
            stamp.touch()
            return f"yt-dlp is up to date (newest ready release is {latest})."

        _activate_release(updates_dir, _install_release(updates_dir, latest))
        _prune_releases(updates_dir)
        stamp.touch()
        return f"Updated yt-dlp to {latest}."


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    updates_dir = Path(argv[0])
    last_error = updates_dir / "last-error"
    try:
        message = update_ytdlp(updates_dir)
        last_error.unlink(missing_ok=True)
    except Exception as error:
        message = f"Keeping the current yt-dlp: {error}"
        last_error.write_text(f"{error}\n")
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
