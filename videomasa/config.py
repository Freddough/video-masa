"""Application configuration helpers with no Flask dependency."""

import os
from pathlib import Path


def read_app_version(source_file: str | Path, fallback: str) -> str:
    """Read VERSION beside the source tree or its parent bundle directory."""
    source_dir = Path(source_file).resolve().parent
    for candidate in (source_dir / "VERSION", source_dir.parent / "VERSION"):
        if candidate.is_file():
            return candidate.read_text().strip()
    return fallback


def int_from_env(name: str, default: int) -> int:
    """Read an integer setting using the existing environment contract."""
    return int(os.environ.get(name, default))
