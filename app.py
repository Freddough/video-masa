"""
VIDEO TOOL — Local Video Transcriber + Downloader
localhost:5000 — paste any video link, transcribe it, download it, or both.
"""

import os
import re
import sys
import uuid
import json
import atexit
import signal
import subprocess
import threading
import mimetypes
import webbrowser
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)


def _find_ffmpeg():
    """Find ffmpeg: bundled in .app Resources, or system PATH."""
    # Check relative to this script (for .app bundle: ../Resources/ffmpeg)
    candidate = Path(__file__).resolve().parent.parent / "Resources" / "ffmpeg"
    if candidate.exists():
        return str(candidate)
    # Fall back to system PATH
    return "ffmpeg"


FFMPEG_BIN = _find_ffmpeg()

WORK_DIR = Path(os.environ.get("VIDEOMASA_WORK_DIR", Path(__file__).parent / "downloads"))
WORK_DIR.mkdir(exist_ok=True)

ALLOWED_COOKIES_BROWSERS = ("none", "chrome", "firefox", "safari", "edge", "brave", "opera", "vivaldi", "chromium")

# Persistent cookie storage — packaged app sets VIDEOMASA_COOKIES_DIR to ~/.videomasa/cookies/
# so cookies survive app upgrades. In dev mode, falls back to ./cookies/.
COOKIES_DIR = Path(os.environ.get("VIDEOMASA_COOKIES_DIR", Path(__file__).resolve().parent / "cookies"))
COOKIES_DIR.mkdir(exist_ok=True)

# Job store: { job_id: { status, message, transcript, timestamped, download_ready, download_path, filename, ... } }
jobs = {}

# Heartbeat tracking — browser pings every 30s, server shuts down if no ping for 5 min
import time
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


def _is_twitter_url(url):
    """Check if a URL is from Twitter/X."""
    return bool(re.match(r'https?://(www\.)?(twitter\.com|x\.com)/', url))


def _cookie_args(cookies_browser="none"):
    """Return yt-dlp cookie flags based on user selection."""
    if cookies_browser.startswith("cookie:"):
        name = cookies_browser[7:]
        cookie_path = COOKIES_DIR / f"{name}.txt"
        if cookie_path.exists():
            return ["--cookies", str(cookie_path)]
        return []
    if cookies_browser not in ("none",):
        return ["--cookies-from-browser", cookies_browser]
    return []


def _probe_formats(url, cookies_browser="none"):
    """Probe available formats for a URL using yt-dlp -j.
    Returns dict: {"video": [2160, 1080, ...], "audio": [130, 49, ...]}"""
    try:
        cmd = ["yt-dlp", "-j", "--no-playlist"] + _cookie_args(cookies_browser) + [url]
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
                         "-o", str(WORK_DIR / f"{job_id}_thumb"), url]
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

        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

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
            job["status"] = "transcribing"
            job["message"] = "Transcribing audio..."
            job["transcripts"][model_size] = {"transcript": "", "timestamped": "", "status": "transcribing"}

            whisper_cmd = [
                "whisper",
                str(downloaded),
                "--model", model_size,
                "--output_format", "json",
                "--output_dir", str(WORK_DIR),
            ]
            wresult = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=600)

            if wresult.returncode != 0:
                job["status"] = "error"
                job["message"] = f"Transcription failed: {wresult.stderr[:200]}"
                job["transcripts"][model_size] = {"transcript": "", "timestamped": "", "status": "error"}
                return

            # Parse whisper JSON output
            json_file = _find_whisper_json(downloaded, job_id)

            if json_file:
                with open(json_file) as jf:
                    data = json.load(jf)
                job["transcript"] = data.get("text", "").strip()
                segments = data.get("segments", [])
                timestamped_lines = []
                for seg in segments:
                    start = seg.get("start", 0)
                    end = seg.get("end", 0)
                    text = seg.get("text", "").strip()
                    sm, ss = divmod(int(start), 60)
                    em, es = divmod(int(end), 60)
                    timestamped_lines.append(f"[{sm:02d}:{ss:02d} → {em:02d}:{es:02d}]  {text}")
                job["timestamped"] = "\n".join(timestamped_lines)
                # Clean up json
                json_file.unlink(missing_ok=True)
                job["transcripts"][model_size] = {"transcript": job["transcript"], "timestamped": job["timestamped"], "status": "done"}
            else:
                stderr_hint = (wresult.stderr or wresult.stdout or "")[:300]
                job["transcript"] = f"Transcription output not found. Whisper output: {stderr_hint}" if stderr_hint else "Transcription output not found."
                job["timestamped"] = ""
                job["transcripts"][model_size] = {"transcript": job["transcript"], "timestamped": "", "status": "error"}

        # Clean up other whisper output files
        for ext in ['.srt', '.vtt', '.txt', '.tsv']:
            cleanup = downloaded.with_suffix(ext)
            if cleanup.exists():
                cleanup.unlink(missing_ok=True)

        job["status"] = "done"
        job["message"] = "Complete"
        check_queue_and_cleanup()

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["message"] = "Process timed out."
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
            job["status"] = "transcribing"
            job["message"] = "Transcribing audio..."
            job["transcripts"][model_size] = {"transcript": "", "timestamped": "", "status": "transcribing"}

            whisper_cmd = [
                "whisper",
                str(file_path),
                "--model", model_size,
                "--output_format", "json",
                "--output_dir", str(WORK_DIR),
            ]
            wresult = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=600)

            if wresult.returncode != 0:
                job["status"] = "error"
                job["message"] = f"Transcription failed: {wresult.stderr[:200]}"
                job["transcripts"][model_size] = {"transcript": "", "timestamped": "", "status": "error"}
                return

            json_file = _find_whisper_json(file_path, job_id)

            if json_file:
                with open(json_file) as jf:
                    data = json.load(jf)
                job["transcript"] = data.get("text", "").strip()
                segments = data.get("segments", [])
                timestamped_lines = []
                for seg in segments:
                    start = seg.get("start", 0)
                    end = seg.get("end", 0)
                    text = seg.get("text", "").strip()
                    sm, ss = divmod(int(start), 60)
                    em, es = divmod(int(end), 60)
                    timestamped_lines.append(f"[{sm:02d}:{ss:02d} → {em:02d}:{es:02d}]  {text}")
                job["timestamped"] = "\n".join(timestamped_lines)
                json_file.unlink(missing_ok=True)
                job["transcripts"][model_size] = {"transcript": job["transcript"], "timestamped": job["timestamped"], "status": "done"}
            else:
                stderr_hint = (wresult.stderr or wresult.stdout or "")[:300]
                job["transcript"] = f"Transcription output not found. Whisper output: {stderr_hint}" if stderr_hint else "Transcription output not found."
                job["timestamped"] = ""
                job["transcripts"][model_size] = {"transcript": job["transcript"], "timestamped": "", "status": "error"}

        for ext in ['.srt', '.vtt', '.txt', '.tsv']:
            cleanup = file_path.with_suffix(ext)
            if cleanup.exists():
                cleanup.unlink(missing_ok=True)

        job["status"] = "done"
        job["message"] = "Complete"
        check_queue_and_cleanup()

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["message"] = "Process timed out."
        check_queue_and_cleanup()
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {str(e)}"
        check_queue_and_cleanup()


# ─── Routes ───────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/thumb/<job_id>")
def thumb(job_id):
    thumb_path = WORK_DIR / f"{job_id}_thumb.jpg"
    if not thumb_path.exists():
        return jsonify({"error": "Thumbnail not found"}), 404
    return send_file(str(thumb_path), mimetype="image/jpeg")


@app.route("/process", methods=["POST"])
def process():
    data = request.json
    url = data.get("url", "").strip()
    model_size = data.get("model", "base")
    do_transcribe = data.get("transcribe", True)
    do_download = data.get("download", False)
    cookies_browser = data.get("cookies_browser", "none")

    if model_size not in ("tiny", "base", "small", "medium"):
        model_size = "base"
    if not cookies_browser.startswith("cookie:") and cookies_browser not in ALLOWED_COOKIES_BROWSERS:
        cookies_browser = "none"

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not do_transcribe and not do_download:
        return jsonify({"error": "Select at least one action (Transcribe or Download)"}), 400

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
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
        "model": model_size,
        "available_formats": {"video": [], "audio": []},
        "downloaded_quality": "Best",
        "file_status": "absent",
    }

    thread = threading.Thread(target=run_job, args=(job_id, url, model_size, do_transcribe, do_download, cookies_browser))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.webm', '.mkv', '.mp3', '.wav', '.m4a', '.ogg', '.flac', '.avi', '.m4v'}


@app.route("/upload", methods=["POST"])
def upload():
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

    jobs[job_id] = {
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
        "model": model_size,
        "file_status": "absent",
    }

    thread = threading.Thread(target=run_file_job, args=(job_id, saved_path, model_size, do_transcribe, do_download))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/merge/<job_id>", methods=["POST"])
def merge_job(job_id):
    """Merge new capabilities (transcribe/download) into an existing in-progress job."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    data = request.json
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
                job["status"] = "transcribing"
                job["message"] = "Transcribing audio..."
                if "transcripts" not in job:
                    job["transcripts"] = {}
                job["transcripts"][model_size] = {"transcript": "", "timestamped": "", "status": "transcribing"}

                merge_model = model_size  # capture for closure

                def run_transcription():
                    try:
                        downloaded = Path(file_path)
                        whisper_cmd = [
                            "whisper", str(downloaded),
                            "--model", merge_model,
                            "--output_format", "json",
                            "--output_dir", str(WORK_DIR),
                        ]
                        wresult = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=600)
                        if wresult.returncode != 0:
                            job["status"] = "error"
                            job["message"] = f"Transcription failed: {wresult.stderr[:200]}"
                            job["transcripts"][merge_model] = {"transcript": "", "timestamped": "", "status": "error"}
                            return

                        json_file = _find_whisper_json(downloaded, job_id)

                        if json_file:
                            with open(json_file) as jf:
                                jdata = json.load(jf)
                            job["transcript"] = jdata.get("text", "").strip()
                            segments = jdata.get("segments", [])
                            lines = []
                            for seg in segments:
                                s, e = seg.get("start", 0), seg.get("end", 0)
                                sm, ss = divmod(int(s), 60)
                                em, es = divmod(int(e), 60)
                                lines.append(f"[{sm:02d}:{ss:02d} → {em:02d}:{es:02d}]  {seg.get('text', '').strip()}")
                            job["timestamped"] = "\n".join(lines)
                            json_file.unlink(missing_ok=True)
                            job["transcripts"][merge_model] = {"transcript": job["transcript"], "timestamped": job["timestamped"], "status": "done"}
                        else:
                            stderr_hint = (wresult.stderr or wresult.stdout or "")[:300]
                            job["transcript"] = f"Transcription output not found. Whisper output: {stderr_hint}" if stderr_hint else "Transcription output not found."
                            job["transcripts"][merge_model] = {"transcript": job["transcript"], "timestamped": "", "status": "error"}

                        for ext in ['.srt', '.vtt', '.txt', '.tsv']:
                            c = downloaded.with_suffix(ext)
                            if c.exists():
                                c.unlink(missing_ok=True)

                        job["status"] = "done"
                        job["message"] = "Complete"
                        check_queue_and_cleanup()
                    except subprocess.TimeoutExpired:
                        job["status"] = "error"
                        job["message"] = "Transcription timed out."
                        job["transcripts"][merge_model] = {"transcript": "", "timestamped": "", "status": "error"}
                        check_queue_and_cleanup()
                    except Exception as e:
                        job["status"] = "error"
                        job["message"] = f"Error: {str(e)}"
                        job["transcripts"][merge_model] = {"transcript": "", "timestamped": "", "status": "error"}
                        check_queue_and_cleanup()

                t = threading.Thread(target=run_transcription)
                t.daemon = True
                t.start()
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

    data = request.json
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

    # Mark as transcribing
    if "transcripts" not in job:
        job["transcripts"] = {}
    job["transcripts"][model] = {"transcript": "", "timestamped": "", "status": "transcribing"}

    rt_model = model  # capture for closure

    def do_retranscribe():
        try:
            downloaded = Path(file_path)
            whisper_cmd = [
                "whisper", str(downloaded),
                "--model", rt_model,
                "--output_format", "json",
                "--output_dir", str(WORK_DIR),
            ]
            result = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=600)

            if result.returncode != 0:
                job["transcripts"][rt_model] = {"transcript": "", "timestamped": "", "status": "error"}
                return

            json_file = _find_whisper_json(downloaded, job_id)

            if json_file:
                with open(json_file) as jf:
                    jdata = json.load(jf)
                transcript = jdata.get("text", "").strip()
                segments = jdata.get("segments", [])
                lines = []
                for seg in segments:
                    s, e = seg.get("start", 0), seg.get("end", 0)
                    sm, ss = divmod(int(s), 60)
                    em, es = divmod(int(e), 60)
                    lines.append(f"[{sm:02d}:{ss:02d} → {em:02d}:{es:02d}]  {seg.get('text', '').strip()}")
                timestamped = "\n".join(lines)
                json_file.unlink(missing_ok=True)
                job["transcripts"][rt_model] = {"transcript": transcript, "timestamped": timestamped, "status": "done"}
            else:
                stderr_hint = (result.stderr or result.stdout or "")[:300]
                job["transcripts"][rt_model] = {"transcript": f"Output not found. Whisper: {stderr_hint}" if stderr_hint else "Output not found.", "timestamped": "", "status": "error"}

            for ext in ['.srt', '.vtt', '.txt', '.tsv']:
                c = downloaded.with_suffix(ext)
                if c.exists():
                    c.unlink(missing_ok=True)

        except subprocess.TimeoutExpired:
            job["transcripts"][rt_model] = {"transcript": "", "timestamped": "", "status": "error"}
        except Exception as e:
            job["transcripts"][rt_model] = {"transcript": "", "timestamped": "", "status": "error"}

    t = threading.Thread(target=do_retranscribe)
    t.daemon = True
    t.start()

    return jsonify({"ok": True, "model": model})


@app.route("/saved-cookies")
def saved_cookies():
    """List saved cookie files."""
    names = sorted(f.stem for f in COOKIES_DIR.iterdir() if f.suffix == ".txt")
    return jsonify({"cookies": names})


@app.route("/upload-cookies", methods=["POST"])
def upload_cookies():
    """Accept a Netscape-format cookies.txt file with a user-chosen name."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    raw_name = request.form.get("name", "").strip()
    if not raw_name:
        return jsonify({"error": "No name provided"}), 400
    safe_name = secure_filename(raw_name)
    if not safe_name:
        return jsonify({"error": "Invalid name — use letters, numbers, dashes, or underscores"}), 400
    # Strip .txt if user included it
    if safe_name.lower().endswith(".txt"):
        safe_name = safe_name[:-4]
    f.save(str(COOKIES_DIR / f"{safe_name}.txt"))
    return jsonify({"ok": True, "name": safe_name})


@app.route("/delete-cookies", methods=["POST"])
def delete_cookies():
    """Remove a saved cookie file by name."""
    data = request.json or {}
    name = data.get("name", "")
    if not name:
        return jsonify({"error": "No name provided"}), 400
    cookie_path = COOKIES_DIR / f"{name}.txt"
    if cookie_path.exists():
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

    data = request.json
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

    jobs[new_job_id] = {
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
        "model": "",
        "available_formats": source_job.get("available_formats", {"video": [], "audio": []}),
        "downloaded_quality": quality_label,
        "file_status": "absent",
    }

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

            cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

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
            new_job["status"] = "error"
            new_job["message"] = "Download timed out."
            check_queue_and_cleanup()
        except Exception as e:
            new_job["status"] = "error"
            new_job["message"] = f"Error: {str(e)}"
            check_queue_and_cleanup()

    t = threading.Thread(target=do_redownload, daemon=True)
    t.start()

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
    all_terminal = all(j.get("status") in ("done", "error") for j in jobs.values())
    if not all_terminal:
        return
    for job in jobs.values():
        if job.get("file_status") != "present":
            continue
        # Don't delete files the user hasn't downloaded yet
        if job.get("download_ready"):
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


def _heartbeat_watchdog():
    """Background thread: check heartbeat, shut down if browser tab is gone."""
    while True:
        time.sleep(30)
        if time.time() - _last_heartbeat > _HEARTBEAT_TIMEOUT:
            print("\nNo browser heartbeat for 5 minutes — shutting down.")
            cleanup_downloads_dir()
            os.kill(os.getpid(), signal.SIGTERM)
            break


if __name__ == "__main__":
    # Clean up any leftover files from a previous un-clean shutdown
    cleanup_downloads_dir()
    port = int(os.environ.get("VIDEOMASA_PORT", 8080))
    if os.environ.get("VIDEOMASA_OPEN_BROWSER", "").lower() in ("1", "true", "yes"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    # Start heartbeat watchdog — auto-shuts down if browser tab is closed
    watchdog = threading.Thread(target=_heartbeat_watchdog, daemon=True)
    watchdog.start()
    print("\n" + "=" * 52)
    print(f"  VIDEO TOOL running at http://localhost:{port}")
    print("=" * 52 + "\n")
    app.run(debug=False, port=port)
