import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from videomasa.job_state import has_unsaved_work
from videomasa.runtime import locked_ytdlp_fallback, ytdlp_freshness
from videomasa.storage import (
    claim_directory,
    directory_size,
    format_bytes,
    low_disk_space_message,
    remove_all_job_files,
    remove_job_files,
    storage_report,
)


JOB_ID = "abc123abc123"


class StorageHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)

    def touch(self, name: str, size: int = 1) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def names(self) -> list:
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())

    def test_format_bytes_uses_readable_units(self) -> None:
        self.assertEqual(format_bytes(2048), "2 KB")
        self.assertEqual(format_bytes(5 * 1024 * 1024), "5 MB")
        self.assertEqual(format_bytes(int(1.5 * 1024 ** 3)), "1.5 GB")

    def test_directory_size_adds_nested_files(self) -> None:
        self.touch("a.bin", 10)
        self.touch("nested/b.bin", 5)
        self.assertEqual(directory_size(self.root), 15)

    def test_removing_a_job_covers_every_file_type_and_only_that_job(self) -> None:
        for suffix in ("video.mp4", "video.f137.mp4.part", "video.f137.mp4.ytdl", "thumb.webp", "thumb.jpg", "song.flac"):
            self.touch(f"{JOB_ID}_{suffix}")
        self.touch("def456def456_other.mp4")

        remove_job_files(self.root, JOB_ID, keep_thumbnail=True)
        self.assertEqual(self.names(), [f"{JOB_ID}_thumb.jpg", "def456def456_other.mp4"])

        remove_job_files(self.root, JOB_ID)
        self.assertEqual(self.names(), ["def456def456_other.mp4"])

    def test_removing_all_jobs_leaves_files_that_are_not_named_for_a_job(self) -> None:
        self.touch(f"{JOB_ID}_video.mp4.part")
        self.touch(f"{JOB_ID}_song.ogg")
        for keeper in ("notes.pdf", "holiday.mp4", "short_name.mp4", "subfolder/abc123abc123_keep.mp4"):
            self.touch(keeper)

        remove_all_job_files(self.root)

        self.assertEqual(
            self.names(),
            ["holiday.mp4", "notes.pdf", "short_name.mp4", "subfolder/abc123abc123_keep.mp4"],
        )

    def test_only_one_process_can_claim_a_folder_until_it_lets_go(self) -> None:
        first = claim_directory(self.root)
        self.assertIsNotNone(first)
        self.assertIsNone(claim_directory(self.root))
        first.close()
        second = claim_directory(self.root)
        self.assertIsNotNone(second)
        second.close()

    def test_low_disk_message_only_appears_below_the_minimum(self) -> None:
        usage = type("Usage", (), {"free": 1024 ** 3})
        with patch("videomasa.storage.shutil.disk_usage", return_value=usage):
            self.assertEqual(low_disk_space_message(self.root, 1024 ** 3), "")
            message = low_disk_space_message(self.root, 2 * 1024 ** 3)
        self.assertIn("1.0 GB free", message)
        self.assertIn("at least 2.0 GB", message)

    def test_report_lists_locations_and_only_downloaded_models(self) -> None:
        work = self.root / "work"
        environment = self.root / "env"
        updates = self.root / "updates"
        self.touch("work/file", 1024)
        self.touch("env/file", 2048)
        self.touch("updates/file", 1024)
        self.touch("cache/whisper/base.pt", 4096)
        self.touch("cache/whisper/unrelated.pt", 4096)

        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            report = storage_report(work, environment, updates)
            without_updates = storage_report(work, environment)

        self.assertEqual([item["label"] for item in report["items"]], ["App environment", "Temporary downloads", "yt-dlp updates"])
        self.assertEqual([model["name"] for model in report["models"]], ["base"])
        self.assertEqual(report["total"], "8 KB")
        self.assertEqual(len(without_updates["items"]), 2)


class UnsavedWorkTests(unittest.TestCase):
    def test_files_transcripts_and_resumable_jobs_count_until_handled(self) -> None:
        cases = (
            ("unsaved file", {"status": "done", "file_status": "present", "download_ready": True}, True),
            ("saved file", {"status": "done", "file_status": "present", "download_ready": True, "_saved": True}, False),
            ("resumable", {"status": "error", "file_status": "present", "retryable": True}, True),
            ("resumable, file gone", {"status": "error", "file_status": "cleaned", "retryable": True}, False),
            ("unseen transcript", {"status": "done", "transcripts": {"base": {"status": "done"}}}, True),
            ("seen transcript", {"status": "done", "transcripts": {"base": {"status": "done"}}, "_seen": True}, False),
            ("failed job", {"status": "error", "file_status": "absent"}, False),
            ("cleared", {"status": "done", "file_status": "present", "download_ready": True, "_cleared": True}, False),
        )
        for name, job, expected in cases:
            with self.subTest(name):
                self.assertEqual(has_unsaved_work([job]), expected)


class YtDlpFreshnessTests(unittest.TestCase):
    NOW = datetime(2026, 11, 3).timestamp()

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.home = Path(temp_dir.name)
        self.updates = self.home / "ytdlp"
        self.updates.mkdir()

    def freshness(self, version: str = "2026.07.04") -> dict:
        completed = type("Completed", (), {"stdout": f"{version}\n"})
        with (
            patch("videomasa.runtime.shutil.which", return_value="/fake/yt-dlp"),
            patch("videomasa.runtime.subprocess.run", return_value=completed),
        ):
            return ytdlp_freshness(self.updates, now=self.NOW)

    def checked(self, days_ago: float) -> None:
        stamp = self.updates / "last-check"
        stamp.touch()
        when = self.NOW - days_ago * 86_400
        os.utime(stamp, (when, when))

    def test_old_version_with_overdue_checks_is_stale(self) -> None:
        self.checked(30)
        status = self.freshness()
        self.assertTrue(status["stale"])
        self.assertEqual(status["age_days"], 122)

    def test_old_version_is_not_stale_while_checks_say_it_is_the_newest(self) -> None:
        self.checked(1)
        self.assertFalse(self.freshness()["stale"])

    def test_old_version_with_a_failing_update_is_stale_and_says_why(self) -> None:
        self.checked(1)
        (self.updates / "last-error").write_text("no matching distribution\n")
        status = self.freshness()
        self.assertTrue(status["stale"])
        self.assertEqual(status["last_error"], "no matching distribution")

    def test_recent_version_is_never_stale(self) -> None:
        (self.updates / "last-error").write_text("offline\n")
        self.assertFalse(self.freshness("2026.10.26")["stale"])

    def test_opt_out_is_reported(self) -> None:
        (self.home / "no-ytdlp-update").touch()
        self.assertTrue(self.freshness()["updates_off"])

    def test_running_from_source_or_with_unreadable_versions_reports_nothing(self) -> None:
        self.assertEqual(ytdlp_freshness(None), {"stale": False})
        self.assertEqual(self.freshness("not-a-version"), {"stale": False})


class LockedYtDlpFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.python = self.root / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.locked = self.python.parent / "yt-dlp"
        self.locked.touch()

    def test_only_offered_when_an_updated_copy_is_active(self) -> None:
        updated = self.root / "updated" / "yt-dlp"
        updated.parent.mkdir()
        updated.touch()
        with patch("videomasa.runtime.shutil.which", return_value=str(updated)):
            self.assertEqual(locked_ytdlp_fallback(str(self.python)), str(self.locked))
        with patch("videomasa.runtime.shutil.which", return_value=str(self.locked)):
            self.assertIsNone(locked_ytdlp_fallback(str(self.python)))
        with patch("videomasa.runtime.shutil.which", return_value=None):
            self.assertIsNone(locked_ytdlp_fallback(str(self.python)))

    def test_not_offered_without_a_locked_copy(self) -> None:
        self.locked.unlink()
        with patch("videomasa.runtime.shutil.which", return_value="/somewhere/yt-dlp"):
            self.assertIsNone(locked_ytdlp_fallback(str(self.python)))


if __name__ == "__main__":
    unittest.main()
