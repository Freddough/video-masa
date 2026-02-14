"""
VIDEO TOOL — Local Video Transcriber + Downloader
localhost:5000 — paste any video link, transcribe it, download it, or both.
"""

import os
import re
import uuid
import json
import atexit
import signal
import subprocess
import threading
import mimetypes
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

WORK_DIR = Path(__file__).parent / "downloads"
WORK_DIR.mkdir(exist_ok=True)

# Job store: { job_id: { status, message, transcript, timestamped, download_ready, download_path, filename, ... } }
jobs = {}


def run_job(job_id: str, url: str, model_size: str, do_transcribe: bool, do_download: bool):
    """Background worker: download video, optionally transcribe, optionally keep file for download."""
    job = jobs[job_id]

    try:
        # Grab thumbnail URL (quick metadata-only call, no file saved)
        try:
            thumb_result = subprocess.run(
                ["yt-dlp", "--get-thumbnail", "--no-playlist", url],
                capture_output=True, text=True, timeout=15
            )
            if thumb_result.returncode == 0 and thumb_result.stdout.strip():
                job["thumbnail"] = thumb_result.stdout.strip()
        except Exception:
            pass  # thumbnail is optional, don't block the job

        job["status"] = "downloading"
        job["message"] = "Downloading video..."

        # Download with yt-dlp
        out_template = str(WORK_DIR / f"{job_id}_%(title)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-o", out_template,
            "-S", "vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            job["status"] = "error"
            job["message"] = f"Download failed: {result.stderr[:200]}"
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
            json_file = downloaded.with_suffix(".json")
            if not json_file.exists():
                # Try alternate naming
                for f in WORK_DIR.iterdir():
                    if f.name.startswith(job_id) and f.suffix == ".json":
                        json_file = f
                        break

            if json_file.exists():
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
                job["transcript"] = "Transcription completed but output not found."
                job["timestamped"] = ""
                job["transcripts"][model_size] = {"transcript": job["transcript"], "timestamped": "", "status": "done"}

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
                    ["ffmpeg", "-i", str(file_path), "-ss", "1", "-frames:v", "1",
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

            json_file = file_path.with_suffix(".json")
            if not json_file.exists():
                for f in WORK_DIR.iterdir():
                    if f.name.startswith(job_id) and f.suffix == ".json":
                        json_file = f
                        break

            if json_file.exists():
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
                job["transcript"] = "Transcription completed but output not found."
                job["timestamped"] = ""
                job["transcripts"][model_size] = {"transcript": job["transcript"], "timestamped": "", "status": "done"}

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

    if model_size not in ("tiny", "base", "small", "medium"):
        model_size = "base"

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
        "file_status": "absent",
    }

    thread = threading.Thread(target=run_job, args=(job_id, url, model_size, do_transcribe, do_download))
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

                        json_file = downloaded.with_suffix(".json")
                        if not json_file.exists():
                            for f in WORK_DIR.iterdir():
                                if f.name.startswith(job_id) and f.suffix == ".json":
                                    json_file = f
                                    break

                        if json_file.exists():
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
                            job["transcript"] = "Transcription completed but output not found."
                            job["transcripts"][merge_model] = {"transcript": job["transcript"], "timestamped": "", "status": "done"}

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

            json_file = downloaded.with_suffix(".json")
            if not json_file.exists():
                for f in WORK_DIR.iterdir():
                    if f.name.startswith(job_id) and f.suffix == ".json":
                        json_file = f
                        break

            if json_file.exists():
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
                job["transcripts"][rt_model] = {"transcript": "Output not found.", "timestamped": "", "status": "error"}

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


@app.route("/download/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("download_ready"):
        return jsonify({"error": "File not available"}), 404

    filepath = job["download_path"]
    filename = job["filename"]

    if not Path(filepath).exists():
        return jsonify({"error": "File no longer exists"}), 404

    return send_file(filepath, as_attachment=True, download_name=filename)


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


if __name__ == "__main__":
    print("\n" + "=" * 52)
    print("  VIDEO TOOL running at http://localhost:8080")
    print("=" * 52 + "\n")
    app.run(debug=False, port=8080)
