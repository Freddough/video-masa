import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TEST_ROOT = tempfile.TemporaryDirectory()
STATE_ROOT = Path(TEST_ROOT.name)
os.environ["VIDEOMASA_API_TOKEN"] = "test-launch-token"
os.environ["VIDEOMASA_PORT"] = "18765"
os.environ["VIDEOMASA_WORK_DIR"] = str(STATE_ROOT / "downloads")
os.environ["VIDEOMASA_COOKIES_DIR"] = str(STATE_ROOT / "cookies")
os.environ["VIDEOMASA_SKIP_HEALTH_CHECKS"] = "1"

import app as videomasa
from videomasa.transcription import (
    LongFormResult,
    LongFormTranscriptionFailure,
    TranscriptionTimeout,
)


BASE_URL = "http://127.0.0.1:18765"


class StabilizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = videomasa.app.test_client()
        with videomasa.jobs_lock:
            videomasa.jobs.clear()

    def bootstrap(self) -> None:
        response = self.client.get(
            "/?token=test-launch-token",
            base_url=BASE_URL,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])

    def test_api_requires_launch_token(self) -> None:
        response = self.client.get("/health", base_url=BASE_URL)
        self.assertEqual(response.status_code, 403)

        self.bootstrap()
        response = self.client.get("/health", base_url=BASE_URL)
        self.assertEqual(response.status_code, 200)

    def test_cross_site_post_is_rejected_even_with_session_cookie(self) -> None:
        self.bootstrap()
        response = self.client.post(
            "/heartbeat",
            base_url=BASE_URL,
            headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_host_header_is_rejected(self) -> None:
        response = self.client.get(
            "/?token=test-launch-token",
            base_url="http://attacker.example:18765",
        )
        self.assertEqual(response.status_code, 403)

    def test_cookie_traversal_is_rejected_and_external_file_survives(self) -> None:
        self.bootstrap()
        external = STATE_ROOT / "secret.txt"
        external.write_text("keep")
        response = self.client.post(
            "/delete-cookies",
            base_url=BASE_URL,
            json={"name": "../secret"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(external.exists())
        self.assertEqual(videomasa._cookie_args("cookie:../secret"), [])

    def test_cookie_upload_is_strictly_named_and_private(self) -> None:
        self.bootstrap()
        invalid = self.client.post(
            "/upload-cookies",
            base_url=BASE_URL,
            data={"name": "../escape", "file": (io.BytesIO(b"cookie"), "cookies.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)

        valid = self.client.post(
            "/upload-cookies",
            base_url=BASE_URL,
            data={"name": "x_session", "file": (io.BytesIO(b"cookie"), "cookies.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(valid.status_code, 200)
        cookie_path = Path(os.environ["VIDEOMASA_COOKIES_DIR"]) / "x_session.txt"
        self.assertEqual(stat.S_IMODE(cookie_path.stat().st_mode), 0o600)

    def test_status_does_not_expose_internal_file_paths(self) -> None:
        self.bootstrap()
        with videomasa.jobs_lock:
            videomasa.jobs["abc"] = {
                "status": "done",
                "message": "Complete",
                "_file_path": "/private/source.mp4",
                "_subtitle_tracks": {"base": [{"start": 0, "end": 1, "text": "Hidden"}]},
            }
        response = self.client.get("/status/abc", base_url=BASE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_file_path", response.get_json())
        self.assertNotIn("_subtitle_tracks", response.get_json())

    def test_srt_download_is_model_specific_utf8_and_media_independent(self) -> None:
        self.bootstrap()
        with videomasa.jobs_lock:
            videomasa.jobs["captioned"] = {
                "status": "done",
                "title": "Final Cut Café",
                "filename": "final.mp4",
                "model": "base",
                "transcripts": {
                    "base": {
                        "status": "done",
                        "transcript": "Café caption",
                        "timestamped": "",
                        "srt_ready": True,
                    },
                },
                "_subtitle_tracks": {
                    "base": [{"start": 0.125, "end": 2.75, "text": "Café caption"}],
                },
                "_file_path": "/source/media/already-cleaned.mp4",
            }

        response = self.client.get(
            "/download-srt/captioned?model=base",
            base_url=BASE_URL,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/x-subrip")
        self.assertIn("Final_Cut_Cafe-base.srt", response.headers["Content-Disposition"])
        self.assertEqual(
            response.data,
            b"\xef\xbb\xbf1\r\n00:00:00,125 --> 00:00:02,750\r\nCaf\xc3\xa9 caption\r\n",
        )

        unavailable = self.client.get(
            "/download-srt/captioned?model=small",
            base_url=BASE_URL,
        )
        self.assertEqual(unavailable.status_code, 409)

    def test_process_rejects_non_http_urls_and_excess_pending_jobs(self) -> None:
        self.bootstrap()
        invalid = self.client.post(
            "/process",
            base_url=BASE_URL,
            json={"url": "file:///etc/passwd", "transcribe": True},
        )
        self.assertEqual(invalid.status_code, 400)

        old_limit = videomasa.MAX_PENDING_JOBS
        videomasa.MAX_PENDING_JOBS = 1
        try:
            with videomasa.jobs_lock:
                videomasa.jobs["busy"] = {"status": "transcribing"}
            full = self.client.post(
                "/process",
                base_url=BASE_URL,
                json={"url": "https://example.com/video", "transcribe": True},
            )
            self.assertEqual(full.status_code, 429)
        finally:
            videomasa.MAX_PENDING_JOBS = old_limit

    def test_transcription_timeout_is_specific_consistent_and_retryable(self) -> None:
        source = videomasa.WORK_DIR / "timeout-podcast.wav"
        source.write_bytes(b"synthetic audio")
        videomasa.jobs["podcast"] = {
            "status": "queued",
            "message": "Queued...",
            "transcript": "",
            "timestamped": "",
            "download_ready": False,
            "download_path": "",
            "filename": "podcast.wav",
            "title": "Podcast",
            "thumbnail": "",
            "url": "",
            "do_transcribe": True,
            "do_download": False,
            "transcripts": {},
            "_subtitle_tracks": {},
            "model": "base",
            "file_status": "absent",
            "stage": "queued",
            "retryable": False,
        }
        timeout = TranscriptionTimeout(
            videomasa.TRANSCRIPTION_TIMEOUT_SECONDS + 0.4,
            videomasa.TRANSCRIPTION_TIMEOUT_SECONDS,
            ["whisper"],
        )

        with patch("app.transcribe_with_whisper", side_effect=timeout):
            videomasa.run_file_job("podcast", source, "base", True, False)

        job = videomasa.jobs["podcast"]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["failure_stage"], "transcription")
        self.assertEqual(job["failure_code"], "timeout")
        self.assertTrue(job["retryable"])
        self.assertEqual(job["transcripts"]["base"]["status"], "error")
        self.assertIn("Transcription timed out", job["message"])
        self.assertIn("source was retained", job["message"])
        self.assertTrue(source.exists())
        self.assertEqual(job["file_status"], "present")
        source.unlink(missing_ok=True)

    def test_long_form_failure_exposes_checkpoint_progress_and_resume(self) -> None:
        source = videomasa.WORK_DIR / "checkpointed-podcast.wav"
        source.write_bytes(b"synthetic long audio")
        videomasa.jobs["checkpointed"] = {
            "status": "queued",
            "message": "Queued...",
            "transcript": "",
            "timestamped": "",
            "download_ready": False,
            "download_path": "",
            "filename": source.name,
            "title": "Checkpointed Podcast",
            "thumbnail": "",
            "url": "",
            "do_transcribe": True,
            "do_download": False,
            "transcripts": {},
            "_subtitle_tracks": {},
            "model": "base",
            "file_status": "present",
            "stage": "queued",
            "retryable": False,
        }

        def interrupted_long_form(*_args, progress_callback, **_kwargs):
            progress_callback({
                "mode": "chunked",
                "phase": "transcribing",
                "completed": 2,
                "total": 4,
                "current": 3,
                "percent": 50,
                "resumed": 0,
            })
            raise LongFormTranscriptionFailure(
                "process_error",
                "A long-form transcription chunk failed.",
                completed_chunks=2,
                total_chunks=4,
                chunk_number=3,
                technical_detail="simulated failure",
            )

        with (
            patch("app.probe_media_duration", return_value=2400),
            patch("app.transcribe_long_form", side_effect=interrupted_long_form),
        ):
            videomasa.run_file_job("checkpointed", source, "base", True, False)

        job = videomasa.jobs["checkpointed"]
        self.assertEqual(job["status"], "error")
        self.assertTrue(job["retryable"])
        self.assertTrue(job["resume_available"])
        self.assertEqual(job["progress"]["completed"], 2)
        self.assertEqual(job["progress"]["total"], 4)
        self.assertIn("chunk 3 of 4", job["message"])
        self.assertIn("choose Resume", job["message"])
        self.assertTrue(source.exists())

        def resumed_long_form(*_args, progress_callback, **_kwargs):
            progress_callback({
                "mode": "chunked",
                "phase": "finalizing",
                "completed": 4,
                "total": 4,
                "current": None,
                "percent": 100,
                "resumed": 2,
            })
            return LongFormResult(
                {
                    "text": "Resumed podcast",
                    "segments": [{"start": 1200.25, "end": 1202.5, "text": " Resumed podcast "}],
                },
                42.0,
                4,
                2,
            )

        def run_synchronously(function, *args):
            function(*args)
            return True

        self.bootstrap()
        with (
            patch("app.probe_media_duration", return_value=2400),
            patch("app.transcribe_long_form", side_effect=resumed_long_form),
            patch("app._submit_job", side_effect=run_synchronously),
        ):
            response = self.client.post("/retry/checkpointed", base_url=BASE_URL, json={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"]["resumed"], 2)
        self.assertEqual(job["transcript"], "Resumed podcast")
        self.assertFalse(source.exists())

    def test_long_form_success_uses_merged_timestamps_and_finishes_progress(self) -> None:
        source = videomasa.WORK_DIR / "long-success.wav"
        source.write_bytes(b"synthetic long audio")
        videomasa.jobs["long-success"] = {
            "status": "queued",
            "message": "Queued...",
            "transcript": "",
            "timestamped": "",
            "download_ready": False,
            "download_path": "",
            "filename": source.name,
            "title": "Long Success",
            "thumbnail": "",
            "url": "",
            "do_transcribe": True,
            "do_download": False,
            "transcripts": {},
            "_subtitle_tracks": {},
            "model": "base",
            "file_status": "present",
            "stage": "queued",
            "retryable": False,
        }

        def successful_long_form(*_args, progress_callback, **_kwargs):
            progress_callback({
                "mode": "chunked",
                "phase": "finalizing",
                "completed": 3,
                "total": 3,
                "current": None,
                "percent": 100,
                "resumed": 1,
            })
            return LongFormResult(
                {
                    "text": "Beginning Ending",
                    "segments": [
                        {"start": 0.25, "end": 2.0, "text": " Beginning "},
                        {"start": 1200.5, "end": 1202.0, "text": " Ending "},
                    ],
                },
                90.0,
                3,
                1,
            )

        with (
            patch("app.probe_media_duration", return_value=1800),
            patch("app.transcribe_long_form", side_effect=successful_long_form),
        ):
            videomasa.run_file_job("long-success", source, "base", True, False)

        job = videomasa.jobs["long-success"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"]["percent"], 100)
        self.assertEqual(job["progress"]["resumed"], 1)
        self.assertEqual(job["_subtitle_tracks"]["base"][1]["start"], 1200.5)
        self.assertTrue(job["transcripts"]["base"]["srt_ready"])
        self.assertFalse(job["retryable"])
        self.assertFalse(source.exists())

    def test_invalid_whisper_output_retains_source_for_retry(self) -> None:
        source = videomasa.WORK_DIR / "invalid-output-podcast.wav"
        source.write_bytes(b"synthetic audio")
        videomasa.jobs["invalid-output"] = {
            "status": "queued",
            "message": "Queued...",
            "transcript": "",
            "timestamped": "",
            "download_ready": False,
            "download_path": "",
            "filename": source.name,
            "title": "Invalid Output Podcast",
            "thumbnail": "",
            "url": "",
            "do_transcribe": True,
            "do_download": False,
            "transcripts": {},
            "_subtitle_tracks": {},
            "model": "base",
            "file_status": "present",
            "stage": "queued",
            "retryable": False,
        }

        def malformed_transcription(source_path, _model, _output_dir, _timeout):
            Path(source_path).with_suffix(".json").write_text("{not valid json")
            return subprocess.CompletedProcess(["whisper"], 0, "", ""), 3.5

        with patch("app.transcribe_with_whisper", side_effect=malformed_transcription):
            videomasa.run_file_job("invalid-output", source, "base", True, False)

        job = videomasa.jobs["invalid-output"]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["failure_stage"], "transcription")
        self.assertEqual(job["failure_code"], "output_invalid")
        self.assertEqual(job["transcripts"]["base"]["status"], "error")
        self.assertTrue(job["retryable"])
        self.assertTrue(source.exists())
        source.unlink(missing_ok=True)

    def test_retry_uses_retained_media_and_completes_without_reupload(self) -> None:
        self.bootstrap()
        source = videomasa.WORK_DIR / "retry-podcast.wav"
        source.write_bytes(b"synthetic audio")
        videomasa.jobs["retryable"] = {
            "status": "error",
            "stage": "error",
            "message": "Transcription timed out",
            "failure_stage": "transcription",
            "failure_code": "timeout",
            "retryable": True,
            "transcript": "",
            "timestamped": "",
            "download_ready": False,
            "download_path": "",
            "filename": "retry-podcast.wav",
            "title": "Retry Podcast",
            "thumbnail": "",
            "url": "",
            "do_transcribe": True,
            "do_download": False,
            "transcripts": {"base": {"status": "error"}},
            "_subtitle_tracks": {},
            "_file_path": str(source),
            "model": "base",
            "file_status": "present",
        }

        def successful_transcription(source_path, _model, _output_dir, _timeout):
            Path(source_path).with_suffix(".json").write_text(json.dumps({
                "text": "Recovered podcast",
                "segments": [{"start": 0.25, "end": 2.5, "text": "Recovered podcast"}],
            }))
            return subprocess.CompletedProcess(["whisper"], 0, "", ""), 12.5

        def run_synchronously(function, *args):
            function(*args)
            return True

        with (
            patch("app.transcribe_with_whisper", side_effect=successful_transcription),
            patch("app._submit_job", side_effect=run_synchronously),
        ):
            response = self.client.post("/retry/retryable", base_url=BASE_URL, json={})

        self.assertEqual(response.status_code, 202)
        job = videomasa.jobs["retryable"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["transcript"], "Recovered podcast")
        self.assertTrue(job["transcripts"]["base"]["srt_ready"])
        self.assertFalse(job["retryable"])
        self.assertEqual(job["file_status"], "cleaned")
        self.assertFalse(source.exists())

    def test_retry_rejects_jobs_without_retained_transcription_media(self) -> None:
        self.bootstrap()
        videomasa.jobs["download-error"] = {
            "status": "error",
            "failure_stage": "download",
            "retryable": False,
        }
        response = self.client.post("/retry/download-error", base_url=BASE_URL, json={})
        self.assertEqual(response.status_code, 409)

    def test_inactivity_and_cleanup_do_not_interrupt_active_transcription(self) -> None:
        source = videomasa.WORK_DIR / "active-retranscription.wav"
        source.write_bytes(b"active")
        videomasa.jobs["active"] = {
            "status": "done",
            "file_status": "present",
            "download_ready": False,
            "_file_path": str(source),
            "transcripts": {"medium": {"status": "transcribing"}},
        }
        old_heartbeat = videomasa._last_heartbeat
        try:
            videomasa._last_heartbeat = 100.0
            self.assertFalse(videomasa._should_shutdown_for_inactivity(now=1000.0))
            videomasa.check_queue_and_cleanup()
            self.assertTrue(source.exists())

            videomasa.jobs["active"]["transcripts"]["medium"]["status"] = "done"
            self.assertTrue(videomasa._should_shutdown_for_inactivity(now=1000.0))
            videomasa.check_queue_and_cleanup()
            self.assertFalse(source.exists())
        finally:
            videomasa._last_heartbeat = old_heartbeat

    def test_job_renderer_uses_dom_properties_without_inline_handlers(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text()
        self.assertNotIn("cardBody.innerHTML", template)
        self.assertNotIn("onclick=", template)
        self.assertIn("label.title = job.url || job.label", template)
        self.assertIn("copyButton.addEventListener", template)
        self.assertIn("downloadSrt(job.id, activeModel)", template)
        self.assertIn("retryJob(job, button)", template)
        self.assertIn("/retry/${job.id}", template)
        self.assertIn("makeJobProgress(job)", template)
        self.assertIn("Resume transcription", template)
        self.assertIn("job.progress = data.progress || null", template)


if __name__ == "__main__":
    unittest.main()
