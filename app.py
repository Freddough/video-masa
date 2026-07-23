"""
VIDEO TOOL — Local Video Transcriber + Downloader
localhost:5000 — paste any video link, transcribe it, download it, or both.
"""

import os
import re
import uuid
import json
import secrets
import logging
import atexit
import shutil
import signal
import subprocess
import threading
import mimetypes
import webbrowser
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from videomasa.config import int_from_env, read_app_version
from videomasa.runtime import check_health, find_ffmpeg, prepend_executable_directory
from videomasa.job_state import format_duration, has_active_jobs
from videomasa.security import (
    constant_time_token_match,
    cookie_path,
    request_host_is_local,
    request_origin_is_local,
    validated_url,
)
from videomasa.subtitles import build_srt, parse_whisper_result
from videomasa.transcription import TranscriptionTimeout, transcribe_with_whisper

os.umask(0o077)
app = Flask(__name__)


def _read_app_version():
    return read_app_version(__file__, "3.1.1")


APP_VERSION = _read_app_version()
APP_PORT = int_from_env("VIDEOMASA_PORT", 8080)
CONFIGURED_API_TOKEN = os.environ.get("VIDEOMASA_API_TOKEN")
API_TOKEN = CONFIGURED_API_TOKEN or secrets.token_urlsafe(32)
SESSION_COOKIE_NAME = "videomasa_session"
MAX_UPLOAD_BYTES = int_from_env("VIDEOMASA_MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024)
MAX_COOKIE_BYTES = int_from_env("VIDEOMASA_MAX_COOKIE_BYTES", 10 * 1024 * 1024)
MAX_URL_LENGTH = int_from_env("VIDEOMASA_MAX_URL_LENGTH", 4096)
MAX_WORKERS = int_from_env("VIDEOMASA_MAX_WORKERS", 2)
MAX_PENDING_JOBS = int_from_env("VIDEOMASA_MAX_PENDING_JOBS", 8)
MAX_RETAINED_JOBS = int_from_env("VIDEOMASA_MAX_RETAINED_JOBS", 100)
DOWNLOAD_TIMEOUT_SECONDS = max(60, int_from_env("VIDEOMASA_DOWNLOAD_TIMEOUT_SECONDS", 1800))
TRANSCRIPTION_TIMEOUT_SECONDS = max(60, int_from_env("VIDEOMASA_TRANSCRIPTION_TIMEOUT_SECONDS", 14_400))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


class _RedactLaunchToken(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                re.sub(r"([?&]token=)[^&\s\"]+", r"\1[REDACTED]", value)
                if isinstance(value, str) else value
                for value in record.args
            )
        return True


logging.getLogger("werkzeug").addFilter(_RedactLaunchToken())


FFMPEG_BIN = find_ffmpeg(__file__)
prepend_executable_directory(FFMPEG_BIN)

# ─── Startup health checks ────────────────────────────────────
# Run once at import time; results cached in _health for the /health endpoint.

if os.environ.get("VIDEOMASA_SKIP_HEALTH_CHECKS") == "1":
    _health = {"all_ok": True}
else:
    _health = check_health(FFMPEG_BIN)

# Print startup health summary
_failed = [k for k, v in _health.items() if k != "all_ok" and not v.get("ok")]
if _failed:
    print(f"\n⚠  Health check: {len(_failed)} issue(s) detected: {', '.join(_failed)}")
    for k in _failed:
        print(f"   • {k}: {_health[k].get('detail', 'unknown')}")
    print()
else:
    print("\n✓  Health check: all dependencies OK\n")


WORK_DIR = Path(os.environ.get("VIDEOMASA_WORK_DIR", Path(__file__).parent / "downloads"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.chmod(0o700)

ALLOWED_COOKIES_BROWSERS = ("none", "chrome", "firefox", "safari", "edge", "brave", "opera", "vivaldi", "chromium")

# Persistent cookie storage — packaged app sets VIDEOMASA_COOKIES_DIR to ~/.videomasa/cookies/
# so cookies survive app upgrades. In dev mode, falls back to ./cookies/.
COOKIES_DIR = Path(os.environ.get("VIDEOMASA_COOKIES_DIR", Path(__file__).resolve().parent / "cookies"))
COOKIES_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.chmod(0o700)
for _cookie_file in COOKIES_DIR.glob("*.txt"):
    if _cookie_file.is_file() and not _cookie_file.is_symlink():
        _cookie_file.chmod(0o600)

# Job store: { job_id: { status, message, transcript, timestamped, download_ready, download_path, filename, ... } }
jobs = {}
jobs_lock = threading.RLock()
job_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="videomasa")
job_task_slots = threading.BoundedSemaphore(MAX_PENDING_JOBS)


def _shutdown_executor():
    job_executor.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_executor)


def _constant_time_token_match(candidate):
    return constant_time_token_match(candidate, API_TOKEN)


def _request_host_is_local():
    return request_host_is_local(request.host, APP_PORT)


def _request_origin_is_local():
    return request_origin_is_local(request.headers.get("Origin"), APP_PORT)


@app.before_request
def protect_local_api():
    """Require the per-launch secret and reject cross-site/host-confused requests."""
    if not _request_host_is_local():
        return jsonify({"error": "Invalid Host header"}), 403

    launch_token = request.args.get("token") if request.endpoint == "index" else None
    if request.method == "GET" and request.endpoint == "index" and _constant_time_token_match(launch_token):
        return None

    supplied_token = request.headers.get("X-Video-Masa-Token") or request.cookies.get(SESSION_COOKIE_NAME)
    if not _constant_time_token_match(supplied_token):
        return jsonify({"error": "Unauthorized local request"}), 403

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or not _request_origin_is_local():
            return jsonify({"error": "Cross-site request rejected"}), 403


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"}), 413


def _prune_terminal_jobs_locked():
    if len(jobs) < MAX_RETAINED_JOBS:
        return
    for job_id, job in list(jobs.items()):
        if (
            job.get("status") in {"done", "error"}
            and not has_active_jobs([job])
            and not job.get("retryable")
        ):
            jobs.pop(job_id, None)
        if len(jobs) < MAX_RETAINED_JOBS:
            break


def _add_job(job_id, job):
    with jobs_lock:
        _prune_terminal_jobs_locked()
        pending = sum(item.get("status") not in {"done", "error"} for item in jobs.values())
        if pending >= MAX_PENDING_JOBS:
            return False, f"Too many jobs are queued or running (limit: {MAX_PENDING_JOBS})"
        if len(jobs) >= MAX_RETAINED_JOBS:
            return False, f"Too many retained jobs (limit: {MAX_RETAINED_JOBS}); clear finished jobs and retry"
        jobs[job_id] = job
    return True, ""


def _submit_job(function, *args):
    if not job_task_slots.acquire(blocking=False):
        return False

    def run_and_release():
        try:
            function(*args)
        finally:
            job_task_slots.release()

    try:
        job_executor.submit(run_and_release)
    except RuntimeError:
        job_task_slots.release()
        return False
    return True

# Heartbeat tracking — browser pings every 30s, server shuts down if no ping for 5 min
_last_heartbeat = time.time()
_HEARTBEAT_TIMEOUT = 300  # seconds (5 minutes)


def _find_whisper_json(source_path, job_id):
    """Find whisper JSON output file, handling different naming conventions.
    Some whisper versions create 'input.json', others 'input.mp4.json'."""
    source = Path(source_path)
    # 1. Standard: same stem with .json suffix (e.g. video.mp4 → video.json)
    candidate = source.with_suffix(".json")
    if candidate.exists():
        return candidate
    # 2. Appended: full filename + .json (e.g. video.mp4 → video.mp4.json)
    candidate = source.parent / (source.name + ".json")
    if candidate.exists():
        return candidate
    # 3. Fallback: search WORK_DIR for any .json starting with job_id
    for f in WORK_DIR.iterdir():
        if f.suffix == ".json" and f.name.startswith(job_id):
            return f
    # 4. Broader fallback: any .json containing the source stem
    stem = source.stem
    for f in WORK_DIR.iterdir():
        if f.suffix == ".json" and stem in f.name:
            return f
    return None


def _store_completed_transcript(job, model, whisper_data, make_primary=True):
    """Store one model's public transcript and private subtitle timing track."""
    transcript, timestamped, segments = parse_whisper_result(whisper_data)
    job.setdefault("_subtitle_tracks", {})[model] = segments
    job["transcripts"][model] = {
        "transcript": transcript,
        "timestamped": timestamped,
        "status": "done",
        "srt_ready": bool(segments),
    }
    if make_primary:
        job["transcript"] = transcript
        job["timestamped"] = timestamped
    return transcript, timestamped


def _clear_job_failure(job):
    for key in ("failure_stage", "failure_code", "elapsed_seconds", "timeout_seconds", "stage_started_at"):
        job.pop(key, None)
    job["retryable"] = False


def _begin_transcription(job, model, affect_job_status=True):
    job.setdefault("transcripts", {})[model] = {
        "transcript": "",
        "timestamped": "",
        "status": "transcribing",
    }
    job.setdefault("_subtitle_tracks", {}).pop(model, None)
    if affect_job_status:
        _clear_job_failure(job)
        job["status"] = "transcribing"
        job["stage"] = "transcription"
        job["stage_started_at"] = int(time.time())
        job["timeout_seconds"] = TRANSCRIPTION_TIMEOUT_SECONDS
        job["message"] = (
            "Transcribing audio... Long recordings may take time "
            f"(limit: {format_duration(TRANSCRIPTION_TIMEOUT_SECONDS)})."
        )


def _record_transcription_failure(
    job,
    model,
    message,
    code,
    elapsed_seconds,
    affect_job_status=True,
):
    elapsed = max(0, int(round(elapsed_seconds)))
    job.setdefault("transcripts", {})[model] = {
        "transcript": "",
        "timestamped": "",
        "status": "error",
        "error_code": code,
        "message": message,
        "elapsed_seconds": elapsed,
    }
    if affect_job_status:
        job["status"] = "error"
        job["stage"] = "error"
        job["message"] = message
        job["failure_stage"] = "transcription"
        job["failure_code"] = code
        job["elapsed_seconds"] = elapsed
        job["timeout_seconds"] = TRANSCRIPTION_TIMEOUT_SECONDS
        job["retryable"] = bool(job.get("_file_path") and Path(job["_file_path"]).exists())


def _cleanup_whisper_outputs(source_path):
    source = Path(source_path)
    candidates = [source.with_suffix(ext) for ext in (".json", ".srt", ".vtt", ".txt", ".tsv")]
    candidates.append(source.parent / f"{source.name}.json")
    for candidate in candidates:
        if candidate != source:
            candidate.unlink(missing_ok=True)


def _transcribe_existing_file(job_id, source_path, model, make_primary=True, affect_job_status=True):
    """Transcribe retained media with consistent timeout, logging, and state."""
    job = jobs[job_id]
    source = Path(source_path)
    _cleanup_whisper_outputs(source)
    _begin_transcription(job, model, affect_job_status=affect_job_status)

    try:
        result, elapsed = transcribe_with_whisper(
            source,
            model,
            WORK_DIR,
            TRANSCRIPTION_TIMEOUT_SECONDS,
        )
    except TranscriptionTimeout as error:
        elapsed_text = format_duration(error.elapsed_seconds)
        limit_text = format_duration(error.timeout_seconds)
        message = (
            f"Transcription timed out after {elapsed_text} (limit: {limit_text}). "
            "The source was retained — choose Retry to try again."
        )
        _record_transcription_failure(
            job,
            model,
            message,
            "timeout",
            error.elapsed_seconds,
            affect_job_status=affect_job_status,
        )
        _cleanup_whisper_outputs(source)
        print(
            f"[transcription timeout] job={job_id} model={model} "
            f"elapsed={error.elapsed_seconds:.1f}s limit={error.timeout_seconds}s",
            flush=True,
        )
        return False
    except Exception as error:
        message = f"Transcription could not start: {str(error)}"
        _record_transcription_failure(
            job,
            model,
            message,
            "exception",
            0,
            affect_job_status=affect_job_status,
        )
        _cleanup_whisper_outputs(source)
        print(f"[transcription exception] job={job_id} model={model}: {error}", flush=True)
        return False

    if result.returncode != 0:
        full_error = result.stderr or result.stdout or "unknown error"
        message = f"Transcription failed after {format_duration(elapsed)}: {full_error[:400]}"
        _record_transcription_failure(
            job,
            model,
            message,
            "process_error",
            elapsed,
            affect_job_status=affect_job_status,
        )
        _cleanup_whisper_outputs(source)
        print(f"[whisper error] job={job_id} rc={result.returncode}\n{full_error}", flush=True)
        return False

    json_file = _find_whisper_json(source, job_id)
    if not json_file:
        hint = (result.stderr or result.stdout or "")[:300]
        message = f"Transcription output not found. Whisper output: {hint}" if hint else "Transcription output not found."
        _record_transcription_failure(
            job,
            model,
            message,
            "output_missing",
            elapsed,
            affect_job_status=affect_job_status,
        )
        _cleanup_whisper_outputs(source)
        return False

    try:
        with open(json_file) as input_file:
            whisper_data = json.load(input_file)
    except (OSError, ValueError) as error:
        message = f"Transcription output could not be read: {str(error)}"
        _record_transcription_failure(
            job,
            model,
            message,
            "output_invalid",
            elapsed,
            affect_job_status=affect_job_status,
        )
        _cleanup_whisper_outputs(source)
        return False
    _store_completed_transcript(job, model, whisper_data, make_primary=make_primary)
    _cleanup_whisper_outputs(source)
    if affect_job_status:
        _clear_job_failure(job)
        job["stage"] = "finalizing"
        job["elapsed_seconds"] = max(0, int(round(elapsed)))
    return True


def _is_twitter_url(url):
    """Check if a URL is from Twitter/X."""
    return bool(re.match(r'https?://(www\.)?(twitter\.com|x\.com)/', url))


def _cookie_path(name):
    return cookie_path(name, COOKIES_DIR)


def _validated_url(value):
    return validated_url(value, MAX_URL_LENGTH)


def _cookie_args(cookies_browser="none"):
    """Return yt-dlp cookie flags based on user selection."""
    if cookies_browser.startswith("cookie:"):
        name = cookies_browser[7:]
        cookie_path = _cookie_path(name)
        if cookie_path and cookie_path.is_file() and not cookie_path.is_symlink():
            return ["--cookies", str(cookie_path)]
        return []
    if cookies_browser not in ("none",):
        return ["--cookies-from-browser", cookies_browser]
    return []


def _probe_formats(url, cookies_browser="none"):
    """Probe available formats for a URL using yt-dlp -j.
    Returns dict: {"video": [2160, 1080, ...], "audio": [130, 49, ...]}"""
    try:
        cmd = ["yt-dlp", "-j", "--no-playlist"] + _cookie_args(cookies_browser) + ["--", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"video": [], "audio": []}
        info = json.loads(result.stdout)
        formats = info.get("formats", [])
        heights = set()
        bitrates = set()
        for fmt in formats:
            h = fmt.get("height")
            if h and isinstance(h, int) and h > 0:
                heights.add(h)
            # Audio-only formats: vcodec is "none" and has acodec
            vcodec = fmt.get("vcodec", "")
            acodec = fmt.get("acodec", "")
            abr = fmt.get("abr")
            if vcodec == "none" and acodec and acodec != "none" and abr:
                bitrates.add(round(abr))
        return {
            "video": sorted(heights, reverse=True),
            "audio": sorted(bitrates, reverse=True),
        }
    except Exception:
        return {"video": [], "audio": []}


def run_job(job_id: str, url: str, model_size: str, do_transcribe: bool, do_download: bool,
            cookies_browser: str = "none"):
    """Background worker: download video, optionally transcribe, optionally keep file for download."""
    job = jobs[job_id]

    try:
        # Download thumbnail locally (remote CDN URLs expire/get blocked)
        try:
            thumb_cmd = ["yt-dlp", "--no-playlist", "--write-thumbnail",
                         "--skip-download", "--convert-thumbnails", "jpg"] + _cookie_args(cookies_browser) + [
                         "-o", str(WORK_DIR / f"{job_id}_thumb"), "--", url]
            thumb_path = WORK_DIR / f"{job_id}_thumb.jpg"
            subprocess.run(thumb_cmd, capture_output=True, text=True, timeout=15)
            if thumb_path.exists():
                job["thumbnail"] = f"/thumb/{job_id}"
        except Exception:
            pass  # thumbnail is optional, don't block the job

        # Probe available formats in background thread
        probe_result = [None]
        def do_probe():
            probe_result[0] = _probe_formats(url, cookies_browser)
        probe_thread = threading.Thread(target=do_probe, daemon=True)
        probe_thread.start()

        job["status"] = "downloading"
        job["stage"] = "download"
        job["stage_started_at"] = int(time.time())
        job["timeout_seconds"] = DOWNLOAD_TIMEOUT_SECONDS
        job["message"] = "Downloading video..."

        # Download with yt-dlp — always best quality
        out_template = str(WORK_DIR / f"{job_id}_%(title)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-o", out_template,
            "-S", "vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4",
        ]

        cmd.extend(_cookie_args(cookies_browser))

        if _is_twitter_url(url):
            cmd.extend(["--extractor-retries", "5"])

        cmd.extend(["--", url])
        download_started = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            elapsed = max(0, int(round(time.monotonic() - download_started)))
            job.update({
                "status": "error",
                "stage": "error",
                "message": (
                    f"Download timed out after {format_duration(elapsed)} "
                    f"(limit: {format_duration(DOWNLOAD_TIMEOUT_SECONDS)})."
                ),
                "failure_stage": "download",
                "failure_code": "timeout",
                "elapsed_seconds": elapsed,
                "timeout_seconds": DOWNLOAD_TIMEOUT_SECONDS,
                "retryable": False,
            })
            print(
                f"[download timeout] job={job_id} elapsed={elapsed}s "
                f"limit={DOWNLOAD_TIMEOUT_SECONDS}s",
                flush=True,
            )
            check_queue_and_cleanup()
            return

        # Collect probe results (wait up to 5s if still running)
        probe_thread.join(timeout=5)
        if probe_result[0]:
            job["available_formats"] = probe_result[0]
            # Best quality = highest probed video height
            video_heights = probe_result[0].get("video", [])
            if video_heights:
                job["downloaded_quality"] = f"{video_heights[0]}p"

        if result.returncode != 0:
            job["status"] = "error"
            job["stage"] = "error"
            job["failure_stage"] = "download"
            job["failure_code"] = "process_error"
            job["retryable"] = False
            stderr = result.stderr
            if _is_twitter_url(url):
                if "No video could be found" in stderr or "Requested format is not available" in stderr:
                    job["message"] = "Twitter: Video requires login. Set 'Browser cookies' to your browser and retry."
                elif "Failed to parse JSON" in stderr or "guest token" in stderr.lower():
                    job["message"] = "Twitter: API error. Try setting browser cookies, or update yt-dlp."
                else:
                    job["message"] = f"Twitter download failed: {stderr[:200]}"
            else:
                job["message"] = f"Download failed: {stderr[:200]}"
            check_queue_and_cleanup()
            return

        # Find the downloaded file
        downloaded = None
        for f in WORK_DIR.iterdir():
            if f.name.startswith(job_id) and f.suffix in ('.mp4', '.mkv', '.webm', '.mov', '.m4a', '.mp3', '.wav'):
                downloaded = f
                break

        if not downloaded:
            job["status"] = "error"
            job["message"] = "Download completed but file not found."
            return

        # Always store file path and title (title = filename minus job_id prefix and extension)
        job["_file_path"] = str(downloaded)
        job["file_status"] = "present"
        prefix = f"{job_id}_"
        display_name = downloaded.name
        if display_name.startswith(prefix):
            display_name = display_name[len(prefix):]
        title = downloaded.stem
        if title.startswith(prefix):
            title = title[len(prefix):]
        job["title"] = title
        job["filename"] = display_name

        # If download requested, mark file as ready (read from job dict so merges take effect)
        if job["do_download"]:
            job["download_ready"] = True
            job["download_path"] = str(downloaded)

        # If transcribe requested, run whisper (read from dict so merges take effect)
        if job["do_transcribe"]:
            if not _transcribe_existing_file(job_id, downloaded, model_size):
                check_queue_and_cleanup()
                return

        _clear_job_failure(job)
        job["status"] = "done"
        job["stage"] = "done"
        job["message"] = "Complete"
        check_queue_and_cleanup()

    except subprocess.TimeoutExpired:
        failed_stage = job.get("stage", "process")
        job["status"] = "error"
        job["stage"] = "error"
        job["failure_stage"] = failed_stage
        job["failure_code"] = "timeout"
        job["message"] = "An unexpected processing stage timed out. See the server log for details."
        check_queue_and_cleanup()
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {str(e)}"
        check_queue_and_cleanup()


def run_file_job(job_id: str, file_path: Path, model_size: str, do_transcribe: bool, do_download: bool):
    """Background worker for locally uploaded files (no yt-dlp needed)."""
    job = jobs[job_id]
    try:
        job["status"] = "processing"
        job["message"] = "Processing file..."

        job["_file_path"] = str(file_path)
        job["file_status"] = "present"

        display_name = file_path.name
        prefix = f"{job_id}_"
        if display_name.startswith(prefix):
            display_name = display_name[len(prefix):]
        title = file_path.stem
        if title.startswith(prefix):
            title = title[len(prefix):]
        job["title"] = title
        job["filename"] = display_name

        # Generate thumbnail for video files using ffmpeg
        thumb_path = WORK_DIR / f"{job_id}_thumb.jpg"
        try:
            mime = mimetypes.guess_type(str(file_path))[0] or ""
            if mime.startswith("video/"):
                subprocess.run(
                    [FFMPEG_BIN, "-i", str(file_path), "-ss", "1", "-frames:v", "1",
                     "-vf", "scale=320:-1", "-q:v", "5", str(thumb_path)],
                    capture_output=True, timeout=15
                )
                if thumb_path.exists():
                    job["thumbnail"] = f"/thumb/{job_id}"
        except Exception:
            pass  # thumbnail is optional

        if do_download:
            job["download_ready"] = True
            job["download_path"] = str(file_path)

        if do_transcribe:
            if not _transcribe_existing_file(job_id, file_path, model_size):
                check_queue_and_cleanup()
                return

        _clear_job_failure(job)
        job["status"] = "done"
        job["stage"] = "done"
        job["message"] = "Complete"
        check_queue_and_cleanup()

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["stage"] = "error"
        job["failure_code"] = "timeout"
        job["message"] = "An unexpected processing stage timed out. See the server log for details."
        check_queue_and_cleanup()
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {str(e)}"
        check_queue_and_cleanup()


# ─── Routes ───────────────────────────────────────────────────

@app.route("/")
def index():
    launch_token = request.args.get("token")
    if launch_token:
        if not _constant_time_token_match(launch_token):
            return jsonify({"error": "Invalid launch token"}), 403
        response = redirect(url_for("index"))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            API_TOKEN,
            httponly=True,
            samesite="Strict",
            secure=False,
        )
        return response
    return render_template("index.html")


@app.route("/health")
def health():
    """Return dependency health status. Frontend checks this on load."""
    return jsonify({**_health, "app_version": APP_VERSION})


@app.route("/thumb/<job_id>")
def thumb(job_id):
    thumb_path = WORK_DIR / f"{job_id}_thumb.jpg"
    if not thumb_path.exists():
        return jsonify({"error": "Thumbnail not found"}), 404
    return send_file(str(thumb_path), mimetype="image/jpeg")


@app.route("/process", methods=["POST"])
def process():
    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415
    data = request.get_json(silent=True) or {}
    url, url_error = _validated_url(data.get("url", ""))
    if url_error:
        return jsonify({"error": url_error}), 400
    model_size = data.get("model", "base")
    do_transcribe = data.get("transcribe", True)
    do_download = data.get("download", False)
    cookies_browser = data.get("cookies_browser", "none")

    if model_size not in ("tiny", "base", "small", "medium"):
        model_size = "base"
    if not cookies_browser.startswith("cookie:") and cookies_browser not in ALLOWED_COOKIES_BROWSERS:
        cookies_browser = "none"

    if not do_transcribe and not do_download:
        return jsonify({"error": "Select at least one action (Transcribe or Download)"}), 400

    job_id = uuid.uuid4().hex[:12]
    job = {
        "status": "queued",
        "message": "Queued...",
        "transcript": "",
        "timestamped": "",
        "download_ready": False,
        "download_path": "",
        "filename": "",
        "title": "",
        "thumbnail": "",
        "url": url,
        "do_transcribe": do_transcribe,
        "do_download": do_download,
        "transcripts": {},
        "_subtitle_tracks": {},
        "model": model_size,
        "available_formats": {"video": [], "audio": []},
        "downloaded_quality": "Best",
        "file_status": "absent",
        "stage": "queued",
        "retryable": False,
    }

    added, error = _add_job(job_id, job)
    if not added:
        return jsonify({"error": error}), 429
    if not _submit_job(run_job, job_id, url, model_size, do_transcribe, do_download, cookies_browser):
        with jobs_lock:
            jobs.pop(job_id, None)
        return jsonify({"error": "Job queue is full or shutting down"}), 503

    return jsonify({"job_id": job_id})


ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.webm', '.mkv', '.mp3', '.wav', '.m4a', '.ogg', '.flac', '.avi', '.m4v'}


@app.route("/upload", methods=["POST"])
def upload():
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify({"error": f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"}), 413
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    model_size = request.form.get("model", "base")
    do_transcribe = request.form.get("transcribe", "true").lower() in ("true", "1", "yes")
    do_download = request.form.get("download", "false").lower() in ("true", "1", "yes")

    if model_size not in ("tiny", "base", "small", "medium"):
        model_size = "base"

    if not do_transcribe and not do_download:
        return jsonify({"error": "Select at least one action (Transcribe or Download)"}), 400

    job_id = uuid.uuid4().hex[:12]
    safe_name = secure_filename(file.filename)
    saved_path = WORK_DIR / f"{job_id}_{safe_name}"
    file.save(str(saved_path))

    job = {
        "status": "queued",
        "message": "Queued...",
        "transcript": "",
        "timestamped": "",
        "download_ready": False,
        "download_path": "",
        "filename": safe_name,
        "title": Path(safe_name).stem,
        "thumbnail": "",
        "url": "",
        "do_transcribe": do_transcribe,
        "do_download": do_download,
        "transcripts": {},
        "_subtitle_tracks": {},
        "model": model_size,
        "file_status": "absent",
        "stage": "queued",
        "retryable": False,
    }

    added, error = _add_job(job_id, job)
    if not added:
        saved_path.unlink(missing_ok=True)
        return jsonify({"error": error}), 429
    if not _submit_job(run_file_job, job_id, saved_path, model_size, do_transcribe, do_download):
        with jobs_lock:
            jobs.pop(job_id, None)
        saved_path.unlink(missing_ok=True)
        return jsonify({"error": "Job queue is full or shutting down"}), 503

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        public_job = {key: value for key, value in job.items() if not key.startswith("_")} if job else None
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(public_job)


@app.route("/merge/<job_id>", methods=["POST"])
def merge_job(job_id):
    """Merge new capabilities (transcribe/download) into an existing in-progress job."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415
    data = request.get_json(silent=True) or {}
    add_download = data.get("download", False)
    add_transcribe = data.get("transcribe", False)
    model_size = data.get("model", "base")
    if model_size not in ("tiny", "base", "small", "medium"):
        model_size = "base"

    resp = {"ok": True}

    # Add download capability
    if add_download and not job["do_download"]:
        job["do_download"] = True
        file_path = job.get("_file_path", "")
        if file_path and Path(file_path).exists():
            job["download_ready"] = True
            job["download_path"] = file_path
            resp["download_ready"] = True
            resp["filename"] = job.get("filename", "")

    # Add transcribe capability
    if add_transcribe and not job["do_transcribe"]:
        job["do_transcribe"] = True
        # If job already finished (download-only), spawn a new transcription thread
        if job["status"] == "done":
            file_path = job.get("_file_path", "")
            if file_path and Path(file_path).exists():
                merge_model = model_size  # capture for closure
                job["status"] = "queued"
                job["message"] = "Transcription queued..."

                def run_transcription():
                    try:
                        if not _transcribe_existing_file(job_id, file_path, merge_model):
                            check_queue_and_cleanup()
                            return
                        _clear_job_failure(job)
                        job["status"] = "done"
                        job["stage"] = "done"
                        job["message"] = "Complete"
                        check_queue_and_cleanup()
                    except Exception as e:
                        job["status"] = "error"
                        job["stage"] = "error"
                        job["message"] = f"Error: {str(e)}"
                        job["transcripts"][merge_model] = {"transcript": "", "timestamped": "", "status": "error"}
                        check_queue_and_cleanup()

                if not _submit_job(run_transcription):
                    job["status"] = "error"
                    job["message"] = "Job queue is full. Try again after another job finishes."
                    return jsonify({"error": job["message"]}), 429
            else:
                job["status"] = "error"
                job["message"] = "File no longer exists for transcription."

    return jsonify(resp)


@app.route("/retranscribe/<job_id>", methods=["POST"])
def retranscribe(job_id):
    """Re-transcribe an existing job with a different whisper model."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415
    data = request.get_json(silent=True) or {}
    model = data.get("model", "base")
    if model not in ("tiny", "base", "small", "medium"):
        model = "base"

    # Check if already transcribing this model
    transcripts = job.get("transcripts", {})
    if model in transcripts and transcripts[model].get("status") == "transcribing":
        return jsonify({"error": "Already transcribing with this model"}), 400

    file_path = job.get("_file_path", "")
    if not file_path or not Path(file_path).exists():
        return jsonify({"error": "Source file was cleaned up. Resubmit the URL to transcribe with a different model."}), 410

    # Mark as transcribing before the worker starts so the UI can switch tabs immediately.
    _begin_transcription(job, model, affect_job_status=False)

    rt_model = model  # capture for closure

    def do_retranscribe():
        try:
            _transcribe_existing_file(
                job_id,
                file_path,
                rt_model,
                make_primary=False,
                affect_job_status=False,
            )
        except Exception as e:
            _record_transcription_failure(
                job,
                rt_model,
                f"Error: {str(e)}",
                "exception",
                0,
                affect_job_status=False,
            )

    if not _submit_job(do_retranscribe):
        job["transcripts"].pop(model, None)
        return jsonify({"error": "Job queue is full. Try again after another job finishes."}), 429

    return jsonify({"ok": True, "model": model})


def _retry_transcription_job(job_id, file_path, model):
    job = jobs[job_id]
    try:
        if not _transcribe_existing_file(job_id, file_path, model):
            check_queue_and_cleanup()
            return
        _clear_job_failure(job)
        job["status"] = "done"
        job["stage"] = "done"
        job["message"] = "Complete"
        check_queue_and_cleanup()
    except Exception as error:
        _record_transcription_failure(
            job,
            model,
            f"Retry failed: {str(error)}",
            "exception",
            0,
        )
        check_queue_and_cleanup()


@app.route("/retry/<job_id>", methods=["POST"])
def retry_job(job_id):
    """Retry a recoverable transcription without re-uploading the media."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job.get("status") != "error" or not job.get("retryable"):
            return jsonify({"error": "This job is not retryable"}), 409
        if job.get("failure_stage") != "transcription":
            return jsonify({"error": "Only retained transcription failures can be retried"}), 409

        file_path = job.get("_file_path", "")
        if not file_path or not Path(file_path).exists():
            job["retryable"] = False
            job["file_status"] = "cleaned"
            return jsonify({"error": "The retained source is no longer available"}), 410

        model = job.get("model", "base")
        if model not in ("tiny", "base", "small", "medium"):
            model = "base"
        job["status"] = "queued"
        job["stage"] = "queued"
        job["message"] = "Retry queued..."
        job["retryable"] = False

    if not _submit_job(_retry_transcription_job, job_id, file_path, model):
        with jobs_lock:
            job["status"] = "error"
            job["stage"] = "error"
            job["message"] = "Job queue is full. Try Retry again after another job finishes."
            job["retryable"] = True
        return jsonify({"error": job["message"]}), 429

    return jsonify({"ok": True, "status": "queued", "model": model}), 202


@app.route("/saved-cookies")
def saved_cookies():
    """List saved cookie files."""
    names = sorted(
        f.stem
        for f in COOKIES_DIR.iterdir()
        if f.suffix == ".txt"
        and f.is_file()
        and not f.is_symlink()
        and _cookie_path(f.stem) == f.resolve()
    )
    return jsonify({"cookies": names})


@app.route("/upload-cookies", methods=["POST"])
def upload_cookies():
    """Accept a Netscape-format cookies.txt file with a user-chosen name."""
    if request.content_length and request.content_length > MAX_COOKIE_BYTES:
        return jsonify({"error": f"Cookie file exceeds the {MAX_COOKIE_BYTES // (1024 * 1024)} MB limit"}), 413
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    raw_name = request.form.get("name", "").strip()
    if not raw_name:
        return jsonify({"error": "No name provided"}), 400
    if raw_name.lower().endswith(".txt"):
        raw_name = raw_name[:-4]
    cookie_path = _cookie_path(raw_name)
    if cookie_path is None:
        return jsonify({"error": "Invalid name — use letters, numbers, dashes, or underscores"}), 400
    f.save(str(cookie_path))
    cookie_path.chmod(0o600)
    return jsonify({"ok": True, "name": cookie_path.stem})


@app.route("/delete-cookies", methods=["POST"])
def delete_cookies():
    """Remove a saved cookie file by name."""
    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not name:
        return jsonify({"error": "No name provided"}), 400
    cookie_path = _cookie_path(name)
    if cookie_path is None:
        return jsonify({"error": "Invalid cookie name"}), 400
    if cookie_path.is_file() and not cookie_path.is_symlink():
        cookie_path.unlink()
    return jsonify({"ok": True})


@app.route("/download/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    filepath = job.get("download_path") or job.get("_file_path", "")
    filename = job.get("filename", "")

    if not filepath or not Path(filepath).exists():
        return jsonify({"error": "File no longer exists on disk"}), 404

    return send_file(filepath, as_attachment=True, download_name=filename or Path(filepath).name)


@app.route("/download-srt/<job_id>")
def download_srt(job_id):
    """Download one completed Whisper model as a CapCut-compatible SRT file."""
    requested_model = request.args.get("model", "").strip().lower()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        model = requested_model or job.get("model", "base")
        if model not in ("tiny", "base", "small", "medium"):
            return jsonify({"error": "Invalid transcription model"}), 400

        entry = job.get("transcripts", {}).get(model, {})
        segments = list(job.get("_subtitle_tracks", {}).get(model, []))
        title = job.get("title") or Path(job.get("filename") or "video-masa").stem

    if entry.get("status") != "done" or not entry.get("srt_ready") or not segments:
        return jsonify({"error": "SRT captions are not ready for this model"}), 409

    srt_content = build_srt(segments)
    if not srt_content:
        return jsonify({"error": "No timed caption segments are available"}), 409

    safe_title = secure_filename(title) or "video-masa"
    payload = BytesIO(b"\xef\xbb\xbf" + srt_content.encode("utf-8"))
    return send_file(
        payload,
        mimetype="application/x-subrip",
        as_attachment=True,
        download_name=f"{safe_title}-{model}.srt",
    )


@app.route("/download-mp3/<job_id>")
def download_mp3(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Try download_path first, fall back to _file_path (always set when file is downloaded)
    file_path_str = job.get("download_path") or job.get("_file_path", "")
    if not file_path_str:
        return jsonify({"error": "No file path available"}), 404

    filepath = Path(file_path_str)
    if not filepath.exists():
        return jsonify({"error": "File no longer exists on disk"}), 404

    mp3_path = filepath.with_suffix(".mp3")
    if not mp3_path.exists():
        try:
            result = subprocess.run(
                [FFMPEG_BIN, "-i", str(filepath), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3_path)],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({"error": f"MP3 conversion failed: {result.stderr[:200] if result.stderr else 'unknown error'}"}), 500
        except FileNotFoundError:
            return jsonify({"error": "ffmpeg not found — required for MP3 conversion"}), 500

    mp3_filename = Path(job.get("filename", filepath.stem)).with_suffix(".mp3").name
    return send_file(str(mp3_path), as_attachment=True, download_name=mp3_filename)


@app.route("/redownload/<job_id>", methods=["POST"])
def redownload(job_id):
    """Re-download at a specific quality, creating a NEW job in the queue."""
    import shutil

    source_job = jobs.get(job_id)
    if not source_job:
        return jsonify({"error": "Job not found"}), 404

    url = source_job.get("url", "")
    if not url:
        return jsonify({"error": "No URL — file uploads cannot be re-downloaded"}), 400

    if not request.is_json:
        return jsonify({"error": "Expected application/json"}), 415
    data = request.get_json(silent=True) or {}
    height = data.get("height")          # int like 720, or None
    audio_only = data.get("audio_only", False)
    audio_bitrate = data.get("audio_bitrate")  # int like 130, or None

    if not height and not audio_only and not audio_bitrate:
        return jsonify({"error": "Specify height, audio_bitrate, or audio_only"}), 400

    cookies_browser = data.get("cookies_browser", "none")
    if not cookies_browser.startswith("cookie:") and cookies_browser not in ALLOWED_COOKIES_BROWSERS:
        cookies_browser = "none"

    # Determine quality label
    if audio_only or audio_bitrate:
        quality_label = f"{audio_bitrate}kbps" if audio_bitrate else "Audio"
    else:
        quality_label = f"{height}p"

    # Create new job
    new_job_id = uuid.uuid4().hex[:12]

    # Copy thumbnail so it persists independently
    source_thumb = WORK_DIR / f"{job_id}_thumb.jpg"
    new_thumb = WORK_DIR / f"{new_job_id}_thumb.jpg"
    thumb_url = ""
    if source_thumb.exists():
        shutil.copy2(str(source_thumb), str(new_thumb))
        thumb_url = f"/thumb/{new_job_id}"

    new_job_data = {
        "status": "queued",
        "message": f"Downloading at {quality_label}...",
        "transcript": "",
        "timestamped": "",
        "download_ready": False,
        "download_path": "",
        "filename": "",
        "title": source_job.get("title", ""),
        "thumbnail": thumb_url,
        "url": url,
        "do_transcribe": False,
        "do_download": True,
        "transcripts": {},
        "_subtitle_tracks": {},
        "model": "",
        "available_formats": source_job.get("available_formats", {"video": [], "audio": []}),
        "downloaded_quality": quality_label,
        "file_status": "absent",
        "stage": "queued",
        "retryable": False,
    }

    added, error = _add_job(new_job_id, new_job_data)
    if not added:
        new_thumb.unlink(missing_ok=True)
        return jsonify({"error": error}), 429
    new_job = jobs[new_job_id]

    def do_redownload():
        try:
            new_job["status"] = "downloading"
            new_job["message"] = f"Downloading at {quality_label}..."

            out_template = str(WORK_DIR / f"{new_job_id}_%(title)s.%(ext)s")
            cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

            if audio_only or audio_bitrate:
                if audio_bitrate:
                    cmd.extend(["-S", f"abr:{audio_bitrate},acodec:aac", "-x", "--audio-format", "m4a"])
                else:
                    cmd.extend(["-S", "acodec:aac", "-x", "--audio-format", "m4a"])
            else:
                cmd.extend(["-S", f"res:{height},vcodec:h264,acodec:aac", "--merge-output-format", "mp4"])

            cmd.extend(_cookie_args(cookies_browser))
            if _is_twitter_url(url):
                cmd.extend(["--extractor-retries", "5"])

            cmd.extend(["--", url])
            download_started = time.monotonic()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )

            if result.returncode != 0:
                new_job["status"] = "error"
                new_job["message"] = f"Download failed: {result.stderr[:200]}"
                check_queue_and_cleanup()
                return

            # Find downloaded file
            downloaded = None
            for f in WORK_DIR.iterdir():
                if f.name.startswith(new_job_id) and not f.name.endswith("_thumb.jpg") and f.suffix in ('.mp4', '.mkv', '.webm', '.mov', '.m4a', '.mp3', '.wav'):
                    downloaded = f
                    break

            if not downloaded:
                new_job["status"] = "error"
                new_job["message"] = "Download completed but file not found."
                check_queue_and_cleanup()
                return

            new_job["_file_path"] = str(downloaded)
            new_job["file_status"] = "present"
            prefix = f"{new_job_id}_"
            display_name = downloaded.name
            if display_name.startswith(prefix):
                display_name = display_name[len(prefix):]
            new_job["filename"] = display_name
            new_job["download_ready"] = True
            new_job["download_path"] = str(downloaded)
            new_job["status"] = "done"
            new_job["message"] = "Complete"
            check_queue_and_cleanup()

        except subprocess.TimeoutExpired:
            elapsed = max(0, int(round(time.monotonic() - download_started)))
            new_job["status"] = "error"
            new_job["stage"] = "error"
            new_job["failure_stage"] = "download"
            new_job["failure_code"] = "timeout"
            new_job["elapsed_seconds"] = elapsed
            new_job["timeout_seconds"] = DOWNLOAD_TIMEOUT_SECONDS
            new_job["message"] = (
                f"Download timed out after {format_duration(elapsed)} "
                f"(limit: {format_duration(DOWNLOAD_TIMEOUT_SECONDS)})."
            )
            check_queue_and_cleanup()
        except Exception as e:
            new_job["status"] = "error"
            new_job["message"] = f"Error: {str(e)}"
            check_queue_and_cleanup()

    if not _submit_job(do_redownload):
        with jobs_lock:
            jobs.pop(new_job_id, None)
        new_thumb.unlink(missing_ok=True)
        return jsonify({"error": "Job queue is full or shutting down"}), 503

    return jsonify({"ok": True, "new_job_id": new_job_id})


@app.route("/cleanup/<job_id>", methods=["POST"])
def cleanup_file(job_id):
    """Delete the server-side video file for a completed job."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    file_path = job.get("_file_path", "")
    if file_path:
        p = Path(file_path)
        if p.exists():
            p.unlink(missing_ok=True)
        # Clean up converted MP3 too
        mp3_p = p.with_suffix(".mp3")
        if mp3_p.exists():
            mp3_p.unlink(missing_ok=True)

    # Clean up thumbnail too
    thumb_path = WORK_DIR / f"{job_id}_thumb.jpg"
    thumb_path.unlink(missing_ok=True)

    job["file_status"] = "cleaned"
    job["download_ready"] = False
    return jsonify({"ok": True})


# ─── Cleanup helpers ─────────────────────────────────────────

def check_queue_and_cleanup():
    """If all jobs are in a terminal state, delete remaining video files.
    Skips files that are still marked download_ready (user hasn't saved yet)."""
    if not jobs:
        return
    if has_active_jobs(jobs.values()):
        return
    for job in jobs.values():
        if job.get("file_status") != "present":
            continue
        # Don't delete files the user hasn't downloaded yet
        if job.get("download_ready"):
            continue
        # Recoverable failures keep their media until Retry, Clear, or shutdown.
        if job.get("retryable"):
            continue
        file_path = job.get("_file_path", "")
        if file_path:
            p = Path(file_path)
            if p.exists():
                p.unlink(missing_ok=True)
        job["file_status"] = "cleaned"


def cleanup_downloads_dir():
    """Remove all video files from the downloads directory (used on shutdown)."""
    try:
        for f in WORK_DIR.iterdir():
            if f.is_file() and f.suffix in ('.mp4', '.mkv', '.webm', '.mov', '.m4a', '.mp3', '.wav', '.json', '.srt', '.vtt', '.txt', '.tsv', '.jpg'):
                f.unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(cleanup_downloads_dir)


def _signal_handler(signum, frame):
    cleanup_downloads_dir()
    raise SystemExit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Graceful shutdown endpoint for the launcher/menu bar to stop the server."""
    cleanup_downloads_dir()
    os.kill(os.getpid(), signal.SIGTERM)
    return jsonify({"ok": True})


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    """Browser pings this every 30s. If no ping for 5 min, server auto-shuts down."""
    global _last_heartbeat
    _last_heartbeat = time.time()
    return jsonify({"ok": True})


def _should_shutdown_for_inactivity(now=None):
    current_time = time.time() if now is None else now
    if current_time - _last_heartbeat <= _HEARTBEAT_TIMEOUT:
        return False
    with jobs_lock:
        return not has_active_jobs(jobs.values())


def _heartbeat_watchdog():
    """Background thread: check heartbeat, shut down if browser tab is gone."""
    while True:
        time.sleep(30)
        if _should_shutdown_for_inactivity():
            print("\nNo browser heartbeat for 5 minutes — shutting down.")
            cleanup_downloads_dir()
            os.kill(os.getpid(), signal.SIGTERM)
            break


if __name__ == "__main__":
    # Clean up any leftover files from a previous un-clean shutdown
    cleanup_downloads_dir()
    port = APP_PORT
    launch_url = f"http://127.0.0.1:{port}/?token={API_TOKEN}"
    if os.environ.get("VIDEOMASA_OPEN_BROWSER", "").lower() in ("1", "true", "yes"):
        threading.Timer(1.5, lambda: webbrowser.open(launch_url)).start()
    # Start heartbeat watchdog — auto-shuts down if browser tab is closed
    watchdog = threading.Thread(target=_heartbeat_watchdog, daemon=True)
    watchdog.start()
    print("\n" + "=" * 52)
    display_url = f"http://127.0.0.1:{port}" if CONFIGURED_API_TOKEN else launch_url
    print(f"  VIDEO TOOL running at {display_url}")
    print("=" * 52 + "\n")
    app.run(debug=False, host="127.0.0.1", port=port)
