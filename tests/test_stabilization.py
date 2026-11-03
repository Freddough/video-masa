import contextlib
import io
import json
import os
import stat
import subprocess
import sys
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
            videomasa.jobs["active"]["_seen"] = True
            self.assertTrue(videomasa._should_shutdown_for_inactivity(now=1000.0))
            videomasa.check_queue_and_cleanup()
            self.assertFalse(source.exists())
        finally:
            videomasa._last_heartbeat = old_heartbeat

    def run_fake_download(self, job_id, stdout, do_transcribe=False, do_download=True, queued_download=None):
        """Run run_job with yt-dlp faked; return the job and the sort orders it requested."""
        sort_orders = []

        def fake_ytdlp(cmd, **kwargs):
            if "-j" in cmd:
                formats = [{"height": height} for height in (2160, 1080, 360)]
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"formats": formats}), "")
            if "-S" in cmd:
                sort_orders.append(cmd[cmd.index("-S") + 1])
                (videomasa.WORK_DIR / f"{job_id}_Clip.mp4").write_bytes(b"video")
                return subprocess.CompletedProcess(cmd, 0, stdout, "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        job = {
            "status": "queued",
            "message": "Queued...",
            "url": "https://example.com/watch",
            "do_transcribe": do_transcribe,
            "do_download": do_download if queued_download is None else queued_download,
            "transcripts": {},
            "available_formats": {"video": [], "audio": []},
            "downloaded_quality": "Best",
            "file_status": "absent",
            "stage": "queued",
            "retryable": False,
        }
        videomasa.jobs[job_id] = job
        with (
            patch("app.subprocess.run", side_effect=fake_ytdlp),
            patch("app._transcribe_existing_file", return_value=True),
        ):
            videomasa.run_job(job_id, job["url"], "base", do_transcribe, do_download)
        self.assertEqual(job["status"], "done", job["message"])
        return job, sort_orders

    def test_kept_download_requests_highest_h264_and_labels_saved_height(self) -> None:
        job, sort_orders = self.run_fake_download("kept", "1080\n")
        self.assertEqual(sort_orders, ["vcodec:h264,res,acodec:aac"])
        self.assertEqual(job["downloaded_quality"], "1080p")
        self.assertEqual(job["available_formats"]["video"], [2160, 1080, 360])

    def test_transcribe_only_download_keeps_small_format_and_labels_saved_height(self) -> None:
        job, sort_orders = self.run_fake_download(
            "transcript-only", "360\n", do_transcribe=True, do_download=False,
        )
        self.assertEqual(sort_orders, ["vcodec:h264,acodec:aac"])
        self.assertEqual(job["downloaded_quality"], "360p")

    def test_download_added_while_queued_requests_highest_h264(self) -> None:
        _, sort_orders = self.run_fake_download(
            "merged", "1080\n", do_transcribe=True, do_download=False, queued_download=True,
        )
        self.assertEqual(sort_orders, ["vcodec:h264,res,acodec:aac"])

    def test_download_without_saved_height_does_not_claim_probed_height(self) -> None:
        job, _ = self.run_fake_download("no-height", "\n")
        self.assertEqual(job["downloaded_quality"], "Best")

    def work_file(self, name: str) -> Path:
        path = videomasa.WORK_DIR / name
        path.write_bytes(b"x")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_cleanup_refuses_a_running_job_and_keeps_its_files(self) -> None:
        self.bootstrap()
        source = self.work_file("busy_source.wav")
        running_jobs = (
            {"status": "transcribing", "transcripts": {}},
            {"status": "done", "transcripts": {"medium": {"status": "transcribing"}}},
        )
        for job in running_jobs:
            with self.subTest(job=job):
                videomasa.jobs["busy"] = {**job, "_file_path": str(source), "file_status": "present"}
                response = self.client.post("/cleanup/busy", base_url=BASE_URL)
                self.assertEqual(response.status_code, 409)
                self.assertTrue(source.exists())
                self.assertEqual(videomasa.jobs["busy"]["file_status"], "present")

    def test_cleanup_removes_every_file_for_the_job_and_hides_it_from_the_list(self) -> None:
        self.bootstrap()
        job_id = "aaaaaaaaaaaa"
        source = self.work_file(f"{job_id}_video.mp4")
        leftovers = [self.work_file(f"{job_id}_{name}") for name in ("video.mp4.part", "video.ytdl", "thumb.webp", "thumb.jpg")]
        other = self.work_file("bbbbbbbbbbbb_video.mp4")
        videomasa.jobs[job_id] = {"status": "done", "_file_path": str(source), "file_status": "present", "transcripts": {}}

        response = self.client.post(f"/cleanup/{job_id}", base_url=BASE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(source.exists())
        self.assertFalse(any(path.exists() for path in leftovers))
        self.assertTrue(other.exists())
        self.assertEqual(self.client.get("/jobs", base_url=BASE_URL).get_json()["jobs"], [])

    def test_queue_cleanup_clears_a_failed_jobs_partial_files_but_not_its_thumbnail(self) -> None:
        job_id = "cccccccccccc"
        partial = self.work_file(f"{job_id}_video.f137.mp4.part")
        thumbnail = self.work_file(f"{job_id}_thumb.jpg")
        videomasa.jobs[job_id] = {"status": "error", "file_status": "absent", "retryable": False, "transcripts": {}}

        videomasa.check_queue_and_cleanup()

        self.assertFalse(partial.exists())
        self.assertTrue(thumbnail.exists())

    def test_shutdown_cleanup_needs_a_claimed_folder_and_only_removes_job_files(self) -> None:
        job_file = self.work_file("dddddddddddd_song.flac")
        stranger = self.work_file("notes.pdf")

        with patch.object(videomasa, "_work_dir_lock", None):
            videomasa.cleanup_downloads_dir()
        self.assertTrue(job_file.exists())

        with patch.object(videomasa, "_work_dir_lock", object()):
            videomasa.cleanup_downloads_dir()
        self.assertFalse(job_file.exists())
        self.assertTrue(stranger.exists())

    def test_second_copy_on_the_same_folder_leaves_the_first_copys_files_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir) / "work"
            work_dir.mkdir()
            first_copy_file = work_dir / "eeeeeeeeeeee_unsaved.mp4"
            first_copy_file.write_bytes(b"x")
            held = videomasa.claim_directory(work_dir)
            try:
                result = subprocess.run(
                    [sys.executable, str(Path(videomasa.__file__).resolve())],
                    env={
                        **os.environ,
                        "VIDEOMASA_WORK_DIR": str(work_dir),
                        "VIDEOMASA_COOKIES_DIR": str(Path(temp_dir) / "cookies"),
                        "VIDEOMASA_PORT": "18766",
                    },
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertTrue(first_copy_file.exists())
            finally:
                held.close()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Another copy of Video Masa", result.stdout)
        self.assertNotIn("running at", result.stdout)

    def test_inactivity_shutdown_waits_for_unsaved_work_but_not_forever(self) -> None:
        old_heartbeat = videomasa._last_heartbeat
        self.addCleanup(setattr, videomasa, "_last_heartbeat", old_heartbeat)
        videomasa._last_heartbeat = 0.0
        grace = videomasa._UNSAVED_WORK_GRACE_SECONDS
        videomasa.jobs["unsaved"] = {
            "status": "done", "file_status": "present", "download_ready": True, "transcripts": {},
        }

        self.assertFalse(videomasa._should_shutdown_for_inactivity(now=videomasa._HEARTBEAT_TIMEOUT + 1.0))
        self.assertFalse(videomasa._should_shutdown_for_inactivity(now=grace - 1.0))
        self.assertTrue(videomasa._should_shutdown_for_inactivity(now=grace + 1.0))

        videomasa.jobs["unsaved"]["_saved"] = True
        self.assertTrue(videomasa._should_shutdown_for_inactivity(now=videomasa._HEARTBEAT_TIMEOUT + 1.0))

    def test_saving_a_file_and_seeing_a_transcript_count_as_handled(self) -> None:
        self.bootstrap()
        source = self.work_file("ffffffffffff_video.mp4")
        job = {
            "status": "done", "file_status": "present", "download_ready": True,
            "download_path": str(source), "_file_path": str(source), "filename": "video.mp4",
            "transcripts": {"base": {"status": "done"}},
        }
        videomasa.jobs["handled"] = job

        self.assertTrue(videomasa.has_unsaved_work([job]))
        self.assertEqual(self.client.get("/status/handled", base_url=BASE_URL).status_code, 200)
        self.assertTrue(job["_seen"])
        self.assertTrue(videomasa.has_unsaved_work([job]))
        response = self.client.get("/download/handled", base_url=BASE_URL)
        response.close()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(videomasa.has_unsaved_work([job]))

    def test_a_running_job_is_not_marked_seen(self) -> None:
        self.bootstrap()
        videomasa.jobs["running"] = {"status": "transcribing", "transcripts": {}}
        self.client.get("/status/running", base_url=BASE_URL)
        self.assertNotIn("_seen", videomasa.jobs["running"])

    def test_waking_from_sleep_gives_an_open_tab_time_to_check_in(self) -> None:
        old_heartbeat = videomasa._last_heartbeat
        self.addCleanup(setattr, videomasa, "_last_heartbeat", old_heartbeat)
        videomasa._last_heartbeat = 100.0

        videomasa._forgive_sleep(1000.0, 1030.0)
        self.assertEqual(videomasa._last_heartbeat, 100.0)

        videomasa._forgive_sleep(1000.0, 9000.0)
        self.assertEqual(videomasa._last_heartbeat, 9000.0)
        self.assertFalse(videomasa._should_shutdown_for_inactivity(now=9001.0))

    def test_download_stops_with_a_clear_message_when_disk_space_is_low(self) -> None:
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        videomasa.jobs["full-disk"] = {
            "status": "queued", "message": "", "url": "https://example.com/v", "do_transcribe": False,
            "do_download": True, "transcripts": {}, "file_status": "absent", "stage": "queued", "retryable": False,
        }
        with (
            patch("app.subprocess.run", side_effect=fake_run),
            patch("app.low_disk_space_message", return_value="Not enough disk space."),
        ):
            videomasa.run_job("full-disk", "https://example.com/v", "base", False, True)

        job = videomasa.jobs["full-disk"]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["failure_code"], "disk_space")
        self.assertEqual(job["message"], "Not enough disk space.")
        self.assertFalse(any("-S" in command for command in commands))

    def test_redownload_stops_with_a_clear_message_when_disk_space_is_low(self) -> None:
        self.bootstrap()
        videomasa.jobs["source"] = {"status": "done", "url": "https://example.com/v", "transcripts": {}}

        def run_immediately(function, *args):
            function(*args)
            return True

        with (
            patch("app._submit_job", side_effect=run_immediately),
            patch("app.subprocess.run") as run,
            patch("app.low_disk_space_message", return_value="Not enough disk space."),
        ):
            response = self.client.post("/redownload/source", base_url=BASE_URL, json={"height": 720})

        run.assert_not_called()
        new_job = videomasa.jobs[response.get_json()["new_job_id"]]
        self.assertEqual(new_job["status"], "error")
        self.assertEqual(new_job["failure_code"], "disk_space")

    def test_new_routes_require_the_launch_token(self) -> None:
        for method, path in (
            ("get", "/jobs"),
            ("get", "/storage"),
            ("get", "/ytdlp-status"),
            ("post", "/storage/remove-model"),
            ("post", "/uninstall"),
        ):
            with self.subTest(path=path):
                client = videomasa.app.test_client()
                response = getattr(client, method)(path, base_url=BASE_URL)
                self.assertEqual(response.status_code, 403)

    def test_upload_is_refused_when_disk_space_is_low(self) -> None:
        self.bootstrap()
        with patch("app.low_disk_space_message", return_value="Not enough disk space."):
            response = self.client.post(
                "/upload",
                base_url=BASE_URL,
                data={"file": (io.BytesIO(b"audio"), "clip.wav")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 507)
        self.assertEqual(response.get_json()["error"], "Not enough disk space.")

    def test_failed_download_retries_with_the_locked_ytdlp(self) -> None:
        job_id = "111111111111"
        commands = []

        def fake_run(command, **_kwargs):
            if "-S" not in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            commands.append(command[0])
            if command[0] == "yt-dlp":
                return subprocess.CompletedProcess(command, 1, "", "ERROR: broken release")
            self.work_file(f"{job_id}_Clip.mp4")
            return subprocess.CompletedProcess(command, 0, "1080\n", "")

        videomasa.jobs[job_id] = {
            "status": "queued", "message": "", "url": "https://example.com/v", "do_transcribe": False,
            "do_download": True, "transcripts": {}, "available_formats": {"video": [], "audio": []},
            "downloaded_quality": "Best", "file_status": "absent", "stage": "queued", "retryable": False,
        }
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch("app.subprocess.run", side_effect=fake_run),
            patch("app.locked_ytdlp_fallback", return_value="/locked/yt-dlp"),
        ):
            videomasa.run_job(job_id, "https://example.com/v", "base", False, True)

        self.assertEqual(commands, ["yt-dlp", "/locked/yt-dlp"])
        self.assertEqual(videomasa.jobs[job_id]["status"], "done")
        self.assertEqual(videomasa.jobs[job_id]["downloaded_quality"], "1080p")
        self.assertIn("[yt-dlp fallback]", output.getvalue())

    def test_when_both_ytdlp_copies_fail_the_updated_copys_error_is_reported(self) -> None:
        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", f"ERROR: from {command[0]}")

        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch("app.subprocess.run", side_effect=fake_run),
            patch("app.locked_ytdlp_fallback", return_value="/locked/yt-dlp"),
        ):
            result = videomasa._run_ytdlp_download(["yt-dlp", "url"])
        self.assertEqual(result.stderr, "ERROR: from yt-dlp")

    def test_no_retry_without_an_updated_copy_or_after_success(self) -> None:
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "ERROR")

        with (
            patch("app.subprocess.run", side_effect=fake_run),
            patch("app.locked_ytdlp_fallback", return_value=None),
        ):
            videomasa._run_ytdlp_download(["yt-dlp", "url"])
        self.assertEqual(len(calls), 1)

    def test_job_list_is_newest_first_and_hides_internal_fields(self) -> None:
        self.bootstrap()
        videomasa.jobs["older"] = {"status": "done", "url": "https://a.example", "_file_path": "/secret/a.mp4"}
        videomasa.jobs["newer"] = {"status": "queued", "title": "New", "do_download": True, "model": "base"}

        listed = self.client.get("/jobs", base_url=BASE_URL).get_json()["jobs"]

        self.assertEqual([job["id"] for job in listed], ["newer", "older"])
        self.assertEqual(listed[0]["title"], "New")
        self.assertNotIn("/secret", json.dumps(listed))

    def test_storage_lists_models_and_removing_one_is_restricted(self) -> None:
        self.bootstrap()
        with tempfile.TemporaryDirectory() as cache, patch.dict(os.environ, {"XDG_CACHE_HOME": cache}):
            model_dir = Path(cache) / "whisper"
            model_dir.mkdir()
            (model_dir / "base.pt").write_bytes(b"x" * 2048)
            (model_dir / "other.pt").write_bytes(b"x")
            secret = Path(cache) / "secret.pt"
            secret.write_bytes(b"x")

            report = self.client.get("/storage", base_url=BASE_URL).get_json()
            self.assertEqual([model["name"] for model in report["models"]], ["base"])
            self.assertFalse(report["uninstall_available"])

            for bad_name in ("other", "../secret", "", None):
                response = self.client.post("/storage/remove-model", base_url=BASE_URL, json={"model": bad_name})
                self.assertEqual(response.status_code, 400)
            self.assertTrue(secret.exists())

            videomasa.jobs["busy"] = {"status": "transcribing", "transcripts": {}}
            response = self.client.post("/storage/remove-model", base_url=BASE_URL, json={"model": "base"})
            self.assertEqual(response.status_code, 409)
            self.assertTrue((model_dir / "base.pt").exists())

            videomasa.jobs.clear()
            response = self.client.post("/storage/remove-model", base_url=BASE_URL, json={"model": "base"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse((model_dir / "base.pt").exists())
            self.assertTrue((model_dir / "other.pt").exists())

    def test_uninstall_is_confirmed_guarded_and_only_runs_in_the_installed_app(self) -> None:
        self.bootstrap()
        response = self.client.post("/uninstall", base_url=BASE_URL, json={"confirm": True})
        self.assertEqual(response.status_code, 404)

        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "uninstall.sh"
            script.touch()
            home = str(Path(temp_dir) / ".videomasa")
            with (
                patch.object(videomasa, "RESOURCES_DIR", temp_dir),
                patch.object(videomasa, "APP_HOME", home),
                patch("app.subprocess.Popen") as popen,
                patch("app.threading.Timer") as timer,
            ):
                self.assertTrue(self.client.get("/storage", base_url=BASE_URL).get_json()["uninstall_available"])

                response = self.client.post("/uninstall", base_url=BASE_URL, json={"confirm": "yes"})
                self.assertEqual(response.status_code, 400)

                videomasa.jobs["busy"] = {"status": "downloading", "transcripts": {}}
                response = self.client.post("/uninstall", base_url=BASE_URL, json={"confirm": True})
                self.assertEqual(response.status_code, 409)
                popen.assert_not_called()

                videomasa.jobs.clear()
                response = self.client.post("/uninstall", base_url=BASE_URL, json={"confirm": True})
                self.assertEqual(response.status_code, 200)
                arguments = popen.call_args.args[0]
                self.assertEqual(arguments[1:4], [str(script), str(os.getpid()), home])
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                timer.return_value.start.assert_called_once()

            with (
                patch.object(videomasa, "RESOURCES_DIR", temp_dir),
                patch.object(videomasa, "APP_HOME", str(Path(temp_dir) / "custom-folder")),
            ):
                self.assertFalse(self.client.get("/storage", base_url=BASE_URL).get_json()["uninstall_available"])

    def test_ytdlp_status_route_reports_nothing_when_run_from_source(self) -> None:
        self.bootstrap()
        response = self.client.get("/ytdlp-status", base_url=BASE_URL)
        self.assertEqual(response.get_json(), {"stale": False})

    def test_page_restores_jobs_and_keeps_running_jobs_out_of_clear_finished(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text()
        self.assertIn("restoreJobs();", template)
        self.assertIn("fetch('/jobs')", template)
        self.assertIn("t.status === 'transcribing'", template)
        self.assertIn("jobQueue.filter(j => !isFinished(j))", template)
        self.assertIn("setTimeout(checkYtdlpFreshness, 60000)", template)

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

    @staticmethod
    def ytdlp_failure(error: str) -> subprocess.CompletedProcess:
        stderr = (
            "WARNING: Your yt-dlp version (2026.07.04) is older than 90 days!\n"
            "         It is strongly recommended to always use the latest version.\n"
            "         You installed yt-dlp with pip or using the wheel from PyPi; Use that to update.\n"
            "         To suppress this warning, add --no-update to your command/config.\n"
            f"{error}\n"
        )
        return subprocess.CompletedProcess(["yt-dlp"], 1, "", stderr)

    def test_download_failure_reports_ytdlp_error_behind_warnings(self) -> None:
        error = "ERROR: [generic] Unsupported URL"
        cases = (
            ("https://example.com/video", f"Download failed: {error}"),
            ("https://x.com/user/status/1", f"Twitter download failed: {error}"),
        )
        for url, expected_message in cases:
            with self.subTest(url=url):
                videomasa.jobs["failed-download"] = {
                    "status": "queued",
                    "message": "Queued...",
                    "url": url,
                    "do_transcribe": False,
                    "do_download": True,
                    "transcripts": {},
                    "file_status": "absent",
                    "stage": "queued",
                    "retryable": False,
                }
                output = io.StringIO()
                with patch("app.subprocess.run", return_value=self.ytdlp_failure(error)):
                    with contextlib.redirect_stdout(output):
                        videomasa.run_job("failed-download", url, "base", False, True)

                job = videomasa.jobs["failed-download"]
                self.assertEqual(job["status"], "error")
                self.assertEqual(job["failure_stage"], "download")
                self.assertEqual(job["message"], expected_message)
                self.assertIn("[download error] job=failed-download rc=1", output.getvalue())
                self.assertIn("older than 90 days", output.getvalue())

    def test_redownload_failure_reports_ytdlp_error_behind_warnings(self) -> None:
        self.bootstrap()
        videomasa.jobs["source"] = {
            "status": "done",
            "url": "https://example.com/video",
            "title": "Clip",
            "transcripts": {},
        }

        def run_immediately(function, *args):
            function(*args)
            return True

        error = "ERROR: [generic] Unsupported URL"
        output = io.StringIO()
        with patch("app.subprocess.run", return_value=self.ytdlp_failure(error)):
            with patch("app._submit_job", side_effect=run_immediately):
                with contextlib.redirect_stdout(output):
                    response = self.client.post(
                        "/redownload/source",
                        base_url=BASE_URL,
                        json={"height": 720},
                    )

        self.assertEqual(response.status_code, 200)
        new_job_id = response.get_json()["new_job_id"]
        job = videomasa.jobs[new_job_id]
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["message"], f"Download failed: {error}")
        self.assertIn(f"[download error] job={new_job_id} rc=1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
