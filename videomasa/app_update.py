"""Tell people when a newer desktop release exists. Nothing installs by itself."""

import json
import re
import subprocess


RELEASE_API_URL = "https://api.github.com/repos/Freddough/video-masa/releases/latest"
DOWNLOAD_URL = "https://github.com/Freddough/video-masa/releases/latest/download/VideoMasa.dmg"


def _version_numbers(text):
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def check_for_newer_release(current_version: str) -> dict:
    """Report a newer release. Being offline or rate limited just means no notice."""
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "--fail", "--silent", "--location", "--max-time", "10", RELEASE_API_URL],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {"available": False}
        tag = json.loads(result.stdout).get("tag_name")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"available": False}

    latest = _version_numbers(tag)
    current = _version_numbers(current_version)
    if latest is None or current is None or latest <= current:
        return {"available": False}
    return {"available": True, "version": tag.lstrip("v"), "url": DOWNLOAD_URL}
