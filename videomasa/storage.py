"""Disk usage, job-file cleanup, and single-copy ownership of the work folder."""

import os
import re
import shutil
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


JOB_FILE_PATTERN = re.compile(r"[0-9a-f]{12}_")
WHISPER_MODELS = ("tiny", "base", "small", "medium")


def format_bytes(count) -> str:
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    if count < 1024 ** 3:
        return f"{count / 1024 ** 2:.0f} MB"
    return f"{count / 1024 ** 3:.1f} GB"


def directory_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def remove_job_files(work_dir, job_id, keep_thumbnail=False) -> None:
    """Remove everything a job left behind, including partial downloads, whatever its type."""
    prefix = f"{job_id}_"
    thumbnail = f"{job_id}_thumb.jpg"
    for path in Path(work_dir).iterdir():
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        if keep_thumbnail and path.name == thumbnail:
            continue
        path.unlink(missing_ok=True)


def remove_all_job_files(work_dir) -> None:
    """Remove files named for a job and nothing else, so a shared folder stays safe."""
    for path in Path(work_dir).iterdir():
        if path.is_file() and JOB_FILE_PATTERN.match(path.name):
            path.unlink(missing_ok=True)


def low_disk_space_message(path, minimum_bytes) -> str:
    free = shutil.disk_usage(path).free
    if free >= minimum_bytes:
        return ""
    return (
        f"Not enough disk space: {format_bytes(free)} free, and Video Masa needs at least "
        f"{format_bytes(minimum_bytes)}. Free up some space and try again."
    )


def claim_directory(path):
    """Return a lock file to keep open for the process's life, or None if another copy holds it."""
    lock = open(Path(path) / ".instance.lock", "a+")
    try:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return None
    return lock


def whisper_cache_dir() -> Path:
    """Match where Whisper itself stores downloaded models."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "whisper"


def whisper_model_path(model) -> Path:
    return whisper_cache_dir() / f"{model}.pt"


def storage_report(work_dir, environment_dir, updates_dir=None) -> dict:
    """Describe what the app occupies on disk, for the Storage panel."""
    locations = [("App environment", environment_dir), ("Temporary downloads", work_dir)]
    if updates_dir:
        locations.append(("yt-dlp updates", updates_dir))

    items = []
    for label, path in locations:
        size = directory_size(path)
        items.append({"label": label, "bytes": size, "size": format_bytes(size)})

    models = []
    for model in WHISPER_MODELS:
        path = whisper_model_path(model)
        if path.is_file():
            size = path.stat().st_size
            models.append({"name": model, "bytes": size, "size": format_bytes(size)})

    usage = shutil.disk_usage(work_dir)
    total = sum(item["bytes"] for item in items) + sum(model["bytes"] for model in models)
    return {
        "items": items,
        "models": models,
        "total": format_bytes(total),
        "free": format_bytes(usage.free),
    }
