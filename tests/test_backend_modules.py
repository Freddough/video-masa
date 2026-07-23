import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from videomasa.config import int_from_env, read_app_version
from videomasa.runtime import check_health
from videomasa.job_state import format_duration, has_active_jobs
from videomasa.security import (
    constant_time_token_match,
    cookie_path,
    request_host_is_local,
    request_origin_is_local,
    validated_url,
)
from videomasa.subtitles import build_srt, format_srt_timestamp, parse_whisper_result
from videomasa.transcription import (
    LongFormTranscriptionFailure,
    TranscriptionTimeout,
    checkpoint_directory,
    probe_media_duration,
    transcribe_long_form,
    transcribe_with_whisper,
)


class ConfigTests(unittest.TestCase):
    def test_version_prefers_file_beside_application(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "app.py"
            source.write_text("")
            (root / "VERSION").write_text("9.8.7\n")
            self.assertEqual(read_app_version(source, "fallback"), "9.8.7")

    def test_integer_environment_setting_preserves_existing_contract(self) -> None:
        with patch.dict(os.environ, {"VIDEOMASA_TEST_LIMIT": "12"}):
            self.assertEqual(int_from_env("VIDEOMASA_TEST_LIMIT", 3), 12)


class SecurityTests(unittest.TestCase):
    def test_token_comparison_requires_exact_nonempty_value(self) -> None:
        self.assertTrue(constant_time_token_match("secret", "secret"))
        self.assertFalse(constant_time_token_match("", "secret"))
        self.assertFalse(constant_time_token_match("other", "secret"))

    def test_loopback_host_and_origin_validation(self) -> None:
        self.assertTrue(request_host_is_local("127.0.0.1:8080", 8080))
        self.assertTrue(request_host_is_local("localhost:8080", 8080))
        self.assertFalse(request_host_is_local("attacker.example:8080", 8080))
        self.assertFalse(request_host_is_local("localhost:9000", 8080))
        self.assertTrue(request_origin_is_local(None, 8080))
        self.assertTrue(request_origin_is_local("http://127.0.0.1:8080", 8080))
        self.assertFalse(request_origin_is_local("https://attacker.example", 8080))

    def test_url_validation_accepts_http_and_rejects_unsafe_schemes(self) -> None:
        self.assertEqual(validated_url(" https://example.com/video ", 100),
                         ("https://example.com/video", ""))
        self.assertEqual(validated_url("file:///etc/passwd", 100)[0], None)
        self.assertIn("limit", validated_url("https://example.com/" + "x" * 100, 20)[1])

    def test_cookie_path_is_strict_and_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(cookie_path("youtube.txt", root), (root / "youtube.txt").resolve())
            self.assertIsNone(cookie_path("../secret", root))
            self.assertIsNone(cookie_path("bad/name", root))


class RuntimeTests(unittest.TestCase):
    def test_healthy_runtime_report_is_structured_and_architecture_aware(self) -> None:
        def fake_run(command, **_kwargs):
            if command[0] == "/ffmpeg":
                return subprocess.CompletedProcess(command, 0, "ffmpeg version test\n", "")
            if command[0] == "/yt-dlp":
                return subprocess.CompletedProcess(command, 0, "2026.07.04\n", "")
            return subprocess.CompletedProcess(command, 0, "/runtime/whisper/__init__.py\n", "")

        with (
            patch("videomasa.runtime.subprocess.run", side_effect=fake_run),
            patch("videomasa.runtime.shutil.which", side_effect=lambda name: f"/{name}"),
            patch("videomasa.runtime.os.access", return_value=True),
            patch("videomasa.runtime.platform.machine", return_value="arm64"),
        ):
            health = check_health("/ffmpeg", "/python")

        self.assertTrue(health["all_ok"])
        self.assertEqual(health["python"]["architecture"], "arm64")
        self.assertEqual(health["whisper_import"]["detail"], "OK")


class SubtitleTests(unittest.TestCase):
    def test_srt_timestamps_preserve_milliseconds_and_roll_over(self) -> None:
        self.assertEqual(format_srt_timestamp(1.2346), "00:00:01,235")
        self.assertEqual(format_srt_timestamp(59.9996), "00:01:00,000")
        self.assertEqual(format_srt_timestamp(3661.789), "01:01:01,789")

    def test_whisper_segments_generate_standard_subrip_blocks(self) -> None:
        transcript, timestamped, segments = parse_whisper_result({
            "text": "Hello world. Second line.",
            "segments": [
                {"start": 1.2346, "end": 3.5, "text": " Hello world. "},
                {"start": 3.5, "end": 5.025, "text": "Second\r\nline."},
                {"start": "bad", "end": 8, "text": "skip me"},
                {"start": 8, "end": 9, "text": "   "},
            ],
        })

        self.assertEqual(transcript, "Hello world. Second line.")
        self.assertIn("[00:01 → 00:03]  Hello world.", timestamped)
        self.assertEqual(len(segments), 2)
        self.assertEqual(
            build_srt(segments),
            "1\r\n00:00:01,235 --> 00:00:03,500\r\nHello world.\r\n\r\n"
            "2\r\n00:00:03,500 --> 00:00:05,025\r\nSecond\r\nline.\r\n",
        )


class JobStateTests(unittest.TestCase):
    def test_active_jobs_include_model_only_retranscriptions(self) -> None:
        self.assertFalse(has_active_jobs([{"status": "done", "transcripts": {}}]))
        self.assertTrue(has_active_jobs([{"status": "queued", "transcripts": {}}]))
        self.assertTrue(has_active_jobs([{
            "status": "done",
            "transcripts": {"medium": {"status": "transcribing"}},
        }]))

    def test_duration_formatting_is_readable_for_timeout_messages(self) -> None:
        self.assertEqual(format_duration(42), "42 seconds")
        self.assertEqual(format_duration(600), "10 minutes")
        self.assertEqual(format_duration(14_400), "4 hours")
        self.assertEqual(format_duration(3_661), "1h 1m")


class TranscriptionExecutionTests(unittest.TestCase):
    def test_ffmpeg_duration_probe_parses_hours_minutes_and_seconds(self) -> None:
        def fake_runner(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "Duration: 01:02:03.45, start: 0.000000, bitrate: 128 kb/s",
            )

        self.assertEqual(
            probe_media_duration("podcast.mp4", "/ffmpeg", runner=fake_runner),
            3723.45,
        )

    def test_timeout_reports_configured_limit_and_measured_elapsed_time(self) -> None:
        timeout = subprocess.TimeoutExpired(["whisper"], 25)
        with (
            patch("videomasa.transcription.subprocess.run", side_effect=timeout),
            patch("videomasa.transcription.time.monotonic", side_effect=[100.0, 125.5]),
        ):
            with self.assertRaises(TranscriptionTimeout) as caught:
                transcribe_with_whisper("podcast.mp4", "base", "/tmp", 25)

        self.assertEqual(caught.exception.timeout_seconds, 25)
        self.assertEqual(caught.exception.elapsed_seconds, 25.5)
        self.assertEqual(caught.exception.command[0], "whisper")

    def test_long_form_retry_skips_checkpointed_chunks_and_merges_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "podcast.mp4"
            source.write_bytes(b"source")
            checkpoint = checkpoint_directory(root, "abc123", "base")

            def write_wav(path, seconds=1):
                with wave.open(str(path), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16_000)
                    audio.writeframes(b"\0\0" * 16_000 * seconds)

            def fake_ffmpeg(command, **_kwargs):
                pattern = Path(command[-1])
                pattern.parent.mkdir(parents=True, exist_ok=True)
                for index in range(3):
                    write_wav(pattern.parent / f"chunk-{index:05d}.wav")
                return subprocess.CompletedProcess(command, 0, "", "")

            first_attempt_calls = []

            def interrupted_whisper(chunk, _model, _output_dir, _timeout):
                index = int(Path(chunk).stem.rsplit("-", 1)[1])
                first_attempt_calls.append(index)
                if index == 1:
                    return subprocess.CompletedProcess(["whisper"], 1, "", "GPU stopped"), 2.0
                Path(chunk).with_suffix(".json").write_text(json.dumps({
                    "text": f"chunk {index}",
                    "segments": [{"start": 0.2, "end": 0.8, "text": f" chunk {index} "}],
                }))
                return subprocess.CompletedProcess(["whisper"], 0, "", ""), 1.5

            with self.assertRaises(LongFormTranscriptionFailure) as caught:
                transcribe_long_form(
                    source,
                    "base",
                    checkpoint,
                    "/ffmpeg",
                    chunk_seconds=600,
                    preparation_timeout=30,
                    chunk_timeout=120,
                    runner=fake_ffmpeg,
                    whisper_runner=interrupted_whisper,
                )

            self.assertEqual(first_attempt_calls, [0, 1])
            self.assertEqual(caught.exception.code, "process_error")
            self.assertEqual(caught.exception.completed_chunks, 1)
            self.assertEqual(caught.exception.chunk_number, 2)

            resumed_calls = []
            progress = []

            def resumed_whisper(chunk, _model, _output_dir, _timeout):
                index = int(Path(chunk).stem.rsplit("-", 1)[1])
                resumed_calls.append(index)
                Path(chunk).with_suffix(".json").write_text(json.dumps({
                    "text": f"chunk {index}",
                    "segments": [{"start": 0.2, "end": 0.8, "text": f" chunk {index} "}],
                }))
                return subprocess.CompletedProcess(["whisper"], 0, "", ""), 2.0

            outcome = transcribe_long_form(
                source,
                "base",
                checkpoint,
                "/ffmpeg",
                chunk_seconds=600,
                preparation_timeout=30,
                chunk_timeout=120,
                runner=fake_ffmpeg,
                whisper_runner=resumed_whisper,
                progress_callback=progress.append,
            )

            self.assertEqual(resumed_calls, [1, 2])
            self.assertEqual(outcome.resumed_chunks, 1)
            self.assertEqual(outcome.total_chunks, 3)
            self.assertEqual(outcome.whisper_data["text"], "chunk 0 chunk 1 chunk 2")
            self.assertEqual(
                [segment["start"] for segment in outcome.whisper_data["segments"]],
                [0.2, 1.2, 2.2],
            )
            self.assertEqual(progress[-1]["phase"], "finalizing")
            self.assertEqual(progress[-1]["percent"], 100)


if __name__ == "__main__":
    unittest.main()
