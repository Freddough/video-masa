"""Whisper execution and checkpointed long-form transcription."""

from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path


CHECKPOINT_SCHEMA_VERSION = 1
_DURATION_PATTERN = re.compile(
    r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class TranscriptionTimeout(RuntimeError):
    """Whisper exceeded the configured wall-clock limit and was terminated."""

    def __init__(self, elapsed_seconds: float, timeout_seconds: int, command):
        super().__init__("Whisper transcription timed out")
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds
        self.command = tuple(command)


class LongFormTranscriptionFailure(RuntimeError):
    """A checkpointed long-form stage failed and can be resumed."""

    def __init__(
        self,
        code,
        message,
        *,
        elapsed_seconds=0,
        completed_chunks=0,
        total_chunks=0,
        chunk_number=None,
        timeout_seconds=None,
        technical_detail="",
    ):
        super().__init__(message)
        self.code = code
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        self.completed_chunks = max(0, int(completed_chunks))
        self.total_chunks = max(0, int(total_chunks))
        self.chunk_number = chunk_number
        self.timeout_seconds = timeout_seconds
        self.technical_detail = technical_detail


@dataclass(frozen=True)
class LongFormResult:
    """Merged Whisper-compatible output plus checkpoint execution metadata."""

    whisper_data: dict
    elapsed_seconds: float
    total_chunks: int
    resumed_chunks: int


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


def probe_media_duration(source_path, ffmpeg_bin, timeout_seconds=30, runner=subprocess.run):
    """Read duration from ffmpeg's metadata output without decoding the media."""
    command = [
        str(ffmpeg_bin),
        "-nostdin",
        "-hide_banner",
        "-i",
        str(Path(source_path)),
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{result.stderr or ''}\n{result.stdout or ''}"
    match = _DURATION_PATTERN.search(output)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def checkpoint_directory(work_dir, job_id, model):
    """Return the isolated checkpoint directory for one job/model pair."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(job_id)):
        raise ValueError("Invalid checkpoint job ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(model)):
        raise ValueError("Invalid checkpoint model")
    return Path(work_dir) / ".checkpoints" / str(job_id) / str(model)


def cleanup_checkpoint(checkpoint_dir):
    """Remove an app-owned checkpoint directory after success or explicit cleanup."""
    checkpoint = Path(checkpoint_dir)
    if checkpoint.exists():
        shutil.rmtree(checkpoint, ignore_errors=True)
    job_dir = checkpoint.parent
    checkpoint_root = job_dir.parent
    for directory in (job_dir, checkpoint_root):
        try:
            directory.rmdir()
        except OSError:
            pass


def _source_identity(source):
    stat = source.stat()
    return {
        "name": source.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _write_manifest(path, manifest):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _read_json(path):
    with Path(path).open(encoding="utf-8") as input_file:
        value = json.load(input_file)
    if not isinstance(value, dict):
        raise ValueError("Whisper output must be a JSON object")
    return value


def _wav_duration(path):
    with wave.open(str(path), "rb") as audio:
        frame_rate = audio.getframerate()
        if frame_rate <= 0:
            raise ValueError(f"Invalid sample rate in {Path(path).name}")
        return audio.getnframes() / frame_rate


def _manifest_is_reusable(manifest, source_identity, model, chunk_seconds, checkpoint_dir):
    if (
        manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or manifest.get("source") != source_identity
        or manifest.get("model") != model
        or manifest.get("chunk_seconds") != chunk_seconds
    ):
        return False
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return False
    for chunk in chunks:
        chunk_path = checkpoint_dir / str(chunk.get("file", ""))
        if not chunk_path.is_file() or chunk_path.parent != checkpoint_dir:
            return False
        if chunk.get("status") == "completed":
            result_path = checkpoint_dir / str(chunk.get("result_file", ""))
            try:
                if result_path.parent != checkpoint_dir:
                    return False
                _read_json(result_path)
            except (OSError, ValueError, json.JSONDecodeError):
                chunk["status"] = "pending"
                chunk.pop("result_file", None)
    return True


def _prepare_chunks(
    source,
    model,
    checkpoint_dir,
    ffmpeg_bin,
    chunk_seconds,
    preparation_timeout,
    runner,
):
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = checkpoint_dir / "manifest.json"
    source_identity = _source_identity(source)

    if manifest_path.is_file():
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {}
        if _manifest_is_reusable(
            manifest,
            source_identity,
            model,
            chunk_seconds,
            checkpoint_dir,
        ):
            _write_manifest(manifest_path, manifest)
            return manifest

    cleanup_checkpoint(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = checkpoint_dir / "chunk-%05d.wav"
    command = [
        str(ffmpeg_bin),
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        str(output_pattern),
    ]
    started_at = time.monotonic()
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=preparation_timeout,
        )
    except subprocess.TimeoutExpired as error:
        elapsed = max(0.0, time.monotonic() - started_at)
        raise LongFormTranscriptionFailure(
            "preparation_timeout",
            "Preparing long-form audio timed out.",
            elapsed_seconds=elapsed,
            timeout_seconds=preparation_timeout,
        ) from error
    except OSError as error:
        raise LongFormTranscriptionFailure(
            "preparation_error",
            "Long-form audio preparation could not start.",
            technical_detail=str(error),
        ) from error

    if result.returncode != 0:
        detail = result.stderr or result.stdout or "unknown ffmpeg error"
        raise LongFormTranscriptionFailure(
            "preparation_error",
            "Long-form audio preparation failed.",
            elapsed_seconds=max(0.0, time.monotonic() - started_at),
            technical_detail=detail,
        )

    chunk_paths = sorted(checkpoint_dir.glob("chunk-*.wav"))
    if not chunk_paths:
        raise LongFormTranscriptionFailure(
            "preparation_output_missing",
            "Long-form audio preparation produced no chunks.",
        )

    chunks = []
    offset = 0.0
    try:
        for index, chunk_path in enumerate(chunk_paths):
            duration = _wav_duration(chunk_path)
            chunks.append(
                {
                    "index": index,
                    "file": chunk_path.name,
                    "offset_seconds": offset,
                    "duration_seconds": duration,
                    "status": "pending",
                }
            )
            offset += duration
    except (OSError, ValueError, wave.Error) as error:
        raise LongFormTranscriptionFailure(
            "preparation_output_invalid",
            "A prepared long-form audio chunk could not be read.",
            technical_detail=str(error),
        ) from error

    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "source": source_identity,
        "model": model,
        "chunk_seconds": chunk_seconds,
        "duration_seconds": offset,
        "chunks": chunks,
    }
    _write_manifest(manifest_path, manifest)
    return manifest


def _find_chunk_result(chunk_path):
    candidates = [
        chunk_path.with_suffix(".json"),
        chunk_path.parent / f"{chunk_path.name}.json",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _cleanup_chunk_sidecars(chunk_path, keep_json=None):
    candidates = [
        chunk_path.with_suffix(extension)
        for extension in (".json", ".srt", ".vtt", ".txt", ".tsv")
    ]
    candidates.append(chunk_path.parent / f"{chunk_path.name}.json")
    for candidate in candidates:
        if keep_json is None or candidate != keep_json:
            candidate.unlink(missing_ok=True)


def _merge_checkpoint_results(manifest, checkpoint_dir):
    merged_text = []
    merged_segments = []
    language = None
    for chunk in manifest["chunks"]:
        result_path = checkpoint_dir / chunk["result_file"]
        data = _read_json(result_path)
        text = str(data.get("text", "")).strip()
        if text:
            merged_text.append(text)
        language = language or data.get("language")
        offset = float(chunk["offset_seconds"])
        duration = float(chunk["duration_seconds"])
        for segment in data.get("segments", []):
            if not isinstance(segment, dict):
                continue
            shifted = dict(segment)
            relative_start = max(0.0, float(segment.get("start", 0)))
            relative_end = max(relative_start, float(segment.get("end", relative_start)))
            relative_start = min(relative_start, duration)
            relative_end = min(relative_end, duration)
            shifted["start"] = offset + relative_start
            shifted["end"] = offset + max(relative_start, relative_end)
            merged_segments.append(shifted)
    result = {
        "text": " ".join(merged_text).strip(),
        "segments": merged_segments,
    }
    if language:
        result["language"] = language
    return result


def transcribe_long_form(
    source_path,
    model,
    checkpoint_dir,
    ffmpeg_bin,
    *,
    chunk_seconds,
    preparation_timeout,
    chunk_timeout,
    progress_callback=None,
    runner=subprocess.run,
    whisper_runner=transcribe_with_whisper,
):
    """Transcribe long media in resumable chunks and merge original timestamps."""
    source = Path(source_path)
    checkpoint_dir = Path(checkpoint_dir)

    def report(phase, completed, total, current=None, resumed=0):
        if progress_callback:
            percent = 0 if total <= 0 else int(round(completed * 100 / total))
            progress_callback(
                {
                    "mode": "chunked",
                    "phase": phase,
                    "completed": completed,
                    "total": total,
                    "current": current,
                    "percent": min(100, max(0, percent)),
                    "resumed": resumed,
                }
            )

    report("preparing", 0, 0)
    manifest = _prepare_chunks(
        source,
        model,
        checkpoint_dir,
        ffmpeg_bin,
        chunk_seconds,
        preparation_timeout,
        runner,
    )
    manifest_path = checkpoint_dir / "manifest.json"
    chunks = manifest["chunks"]
    total = len(chunks)
    completed = sum(chunk.get("status") == "completed" for chunk in chunks)
    resumed = completed
    report("transcribing", completed, total, completed + 1 if completed < total else None, resumed)

    for chunk in chunks:
        if chunk.get("status") == "completed":
            continue
        chunk_number = int(chunk["index"]) + 1
        chunk_path = checkpoint_dir / chunk["file"]
        _cleanup_chunk_sidecars(chunk_path)
        report("transcribing", completed, total, chunk_number, resumed)
        try:
            process, elapsed = whisper_runner(
                chunk_path,
                model,
                checkpoint_dir,
                chunk_timeout,
            )
        except TranscriptionTimeout as error:
            total_elapsed = sum(
                float(item.get("elapsed_seconds", 0))
                for item in chunks
                if item.get("status") == "completed"
            ) + error.elapsed_seconds
            raise LongFormTranscriptionFailure(
                "timeout",
                "A long-form transcription chunk timed out.",
                elapsed_seconds=total_elapsed,
                completed_chunks=completed,
                total_chunks=total,
                chunk_number=chunk_number,
                timeout_seconds=error.timeout_seconds,
            ) from error
        except Exception as error:
            raise LongFormTranscriptionFailure(
                "exception",
                "A long-form transcription chunk could not start.",
                completed_chunks=completed,
                total_chunks=total,
                chunk_number=chunk_number,
                technical_detail=str(error),
            ) from error

        if process.returncode != 0:
            detail = process.stderr or process.stdout or "unknown Whisper error"
            raise LongFormTranscriptionFailure(
                "process_error",
                "A long-form transcription chunk failed.",
                elapsed_seconds=sum(
                    float(item.get("elapsed_seconds", 0))
                    for item in chunks
                    if item.get("status") == "completed"
                ) + elapsed,
                completed_chunks=completed,
                total_chunks=total,
                chunk_number=chunk_number,
                technical_detail=detail,
            )

        result_path = _find_chunk_result(chunk_path)
        if not result_path:
            raise LongFormTranscriptionFailure(
                "output_missing",
                "A long-form transcription chunk produced no output.",
                completed_chunks=completed,
                total_chunks=total,
                chunk_number=chunk_number,
            )
        try:
            _read_json(result_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise LongFormTranscriptionFailure(
                "output_invalid",
                "A long-form transcription chunk produced invalid output.",
                completed_chunks=completed,
                total_chunks=total,
                chunk_number=chunk_number,
                technical_detail=str(error),
            ) from error

        chunk["status"] = "completed"
        chunk["result_file"] = result_path.name
        chunk["elapsed_seconds"] = max(0.0, float(elapsed))
        completed += 1
        _cleanup_chunk_sidecars(chunk_path, keep_json=result_path)
        _write_manifest(manifest_path, manifest)
        report("checkpointed", completed, total, None, resumed)

    report("finalizing", total, total, None, resumed)
    try:
        merged = _merge_checkpoint_results(manifest, checkpoint_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise LongFormTranscriptionFailure(
            "checkpoint_invalid",
            "Saved long-form transcription results could not be merged.",
            completed_chunks=completed,
            total_chunks=total,
            technical_detail=str(error),
        ) from error
    elapsed = sum(float(chunk.get("elapsed_seconds", 0)) for chunk in chunks)
    return LongFormResult(merged, elapsed, total, resumed)
