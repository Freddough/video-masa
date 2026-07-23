"""Isolated Whisper subprocess execution with measurable timeout failures."""

import subprocess
import time
from pathlib import Path


class TranscriptionTimeout(RuntimeError):
    """Whisper exceeded the configured wall-clock limit and was terminated."""

    def __init__(self, elapsed_seconds: float, timeout_seconds: int, command):
        super().__init__("Whisper transcription timed out")
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds
        self.command = tuple(command)


def transcribe_with_whisper(source_path, model, output_dir, timeout_seconds):
    """Run Whisper once and return its completed process plus elapsed seconds."""
    command = [
        "whisper",
        str(Path(source_path)),
        "--model", model,
        "--output_format", "json",
        "--output_dir", str(Path(output_dir)),
    ]
    started_at = time.monotonic()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        elapsed = max(0.0, time.monotonic() - started_at)
        raise TranscriptionTimeout(elapsed, timeout_seconds, command) from error
    return result, max(0.0, time.monotonic() - started_at)
