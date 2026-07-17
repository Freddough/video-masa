"""Subtitle parsing and serialization helpers."""

from math import isfinite


def sanitize_segments(raw_segments):
    """Return caption-safe Whisper segments while preserving their order."""
    clean = []
    for segment in raw_segments or []:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start", 0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        if not isfinite(start) or not isfinite(end):
            continue

        text = str(segment.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue

        start = max(0.0, start)
        end = max(start, end)
        clean.append({"start": start, "end": end, "text": text})
    return clean


def parse_whisper_result(data):
    """Extract the plain transcript, display timestamps, and subtitle segments."""
    transcript = str(data.get("text") or "").strip()
    segments = sanitize_segments(data.get("segments", []))
    timestamped_lines = []
    for segment in segments:
        start_minutes, start_seconds = divmod(int(segment["start"]), 60)
        end_minutes, end_seconds = divmod(int(segment["end"]), 60)
        timestamped_lines.append(
            f"[{start_minutes:02d}:{start_seconds:02d} → "
            f"{end_minutes:02d}:{end_seconds:02d}]  {segment['text']}"
        )
    return transcript, "\n".join(timestamped_lines), segments


def format_srt_timestamp(seconds):
    """Format non-negative seconds as an SRT timestamp rounded to milliseconds."""
    try:
        numeric = float(seconds)
    except (TypeError, ValueError):
        numeric = 0.0
    if not isfinite(numeric):
        numeric = 0.0

    total_milliseconds = int(max(0.0, numeric) * 1000 + 0.5)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def build_srt(segments):
    """Serialize Whisper segments as UTF-8-ready SubRip content with CRLF lines."""
    blocks = []
    for index, segment in enumerate(sanitize_segments(segments), start=1):
        text = segment["text"].replace("\n", "\r\n")
        blocks.append(
            f"{index}\r\n"
            f"{format_srt_timestamp(segment['start'])} --> "
            f"{format_srt_timestamp(segment['end'])}\r\n"
            f"{text}"
        )
    return "\r\n\r\n".join(blocks) + ("\r\n" if blocks else "")
