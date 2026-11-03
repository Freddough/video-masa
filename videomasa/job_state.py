"""Pure helpers for job lifecycle and user-facing timing and error state."""

import re


TERMINAL_JOB_STATUSES = frozenset({"done", "error"})
DOWNLOAD_ERROR_LIMIT = 400


def has_active_jobs(job_values) -> bool:
    """Return whether a job or model-specific transcription is still active."""
    for job in job_values:
        if job.get("status") not in TERMINAL_JOB_STATUSES:
            return True
        if any(
            transcript.get("status") == "transcribing"
            for transcript in job.get("transcripts", {}).values()
        ):
            return True
    return False


def summarize_download_error(stderr) -> str:
    """Return the failure from yt-dlp stderr, not the warnings printed before it."""
    text = (stderr or "").strip()
    if not text:
        return "unknown error"
    failure = re.search(r"^ERROR:.*", text, re.MULTILINE | re.DOTALL)
    summary = failure.group(0) if failure else text[-DOWNLOAD_ERROR_LIMIT:].strip()
    return summary[:DOWNLOAD_ERROR_LIMIT]


def format_duration(seconds) -> str:
    """Format elapsed seconds compactly for status and error messages."""
    total = max(0, int(round(float(seconds))))
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"
    minutes, remaining_seconds = divmod(total, 60)
    if minutes < 60:
        if remaining_seconds:
            return f"{minutes}m {remaining_seconds}s"
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes:
        return f"{hours}h {remaining_minutes}m"
    return f"{hours} hour{'s' if hours != 1 else ''}"
