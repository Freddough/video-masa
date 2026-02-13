"""
VIDEO TOOL — Local Video Transcriber + Downloader
localhost:5000 — paste any video link, transcribe it, download it, or both.
"""

import os
import re
import uuid
import json
import subprocess
import threading
import mimetypes
from pathlib import Path
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
            else:
                job["transcript"] = "Transcription completed but output not found."
                job["timestamped"] = ""

        # Clean up video file if we don't need it for download (read from dict so merges take effect)
        if not job["do_download"] and downloaded.exists():
            downloaded.unlink(missing_ok=True)

        # Clean up other whisper output files
        for ext in ['.srt', '.vtt', '.txt', '.tsv']:
            cleanup = downloaded.with_suffix(ext)
            if cleanup.exists():
                cleanup.unlink(missing_ok=True)

        job["status"] = "done"
        job["message"] = "Complete"

    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["message"] = "Process timed out."
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {str(e)}"


# ─── Routes ───────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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
        "url": url,
        "do_transcribe": do_transcribe,
        "do_download": do_download,
    }

    thread = threading.Thread(target=run_job, args=(job_id, url, model_size, do_transcribe, do_download))
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

                def run_transcription():
                    try:
                        downloaded = Path(file_path)
                        whisper_cmd = [
                            "whisper", str(downloaded),
                            "--model", model_size,
                            "--output_format", "json",
                            "--output_dir", str(WORK_DIR),
                        ]
                        wresult = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=600)
                        if wresult.returncode != 0:
                            job["status"] = "error"
                            job["message"] = f"Transcription failed: {wresult.stderr[:200]}"
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
                        else:
                            job["transcript"] = "Transcription completed but output not found."

                        for ext in ['.srt', '.vtt', '.txt', '.tsv']:
                            c = downloaded.with_suffix(ext)
                            if c.exists():
                                c.unlink(missing_ok=True)

                        job["status"] = "done"
                        job["message"] = "Complete"
                    except subprocess.TimeoutExpired:
                        job["status"] = "error"
                        job["message"] = "Transcription timed out."
                    except Exception as e:
                        job["status"] = "error"
                        job["message"] = f"Error: {str(e)}"

                t = threading.Thread(target=run_transcription)
                t.daemon = True
                t.start()
            else:
                job["status"] = "error"
                job["message"] = "File no longer exists for transcription."

    return jsonify(resp)


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


if __name__ == "__main__":
    print("\n" + "=" * 52)
    print("  VIDEO TOOL running at http://localhost:5000")
    print("=" * 52 + "\n")
    app.run(debug=False, port=5000)
