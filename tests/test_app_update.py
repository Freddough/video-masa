import json
import subprocess
import unittest
from unittest.mock import patch

from videomasa.app_update import DOWNLOAD_URL, check_for_newer_release


class NewReleaseCheckTests(unittest.TestCase):
    def check(self, tag, current="3.3.0", returncode=0):
        body = json.dumps({"tag_name": tag})
        completed = subprocess.CompletedProcess(["curl"], returncode, body, "")
        with patch("videomasa.app_update.subprocess.run", return_value=completed):
            return check_for_newer_release(current)

    def test_newer_release_points_at_the_stable_download(self) -> None:
        self.assertEqual(
            self.check("v3.10.0"),
            {"available": True, "version": "3.10.0", "url": DOWNLOAD_URL},
        )
        self.assertTrue(DOWNLOAD_URL.endswith("/releases/latest/download/VideoMasa.dmg"))

    def test_same_or_older_release_is_not_offered(self) -> None:
        for tag in ("v3.3.0", "v3.2.0", "3.3"):
            with self.subTest(tag=tag):
                self.assertEqual(self.check(tag), {"available": False})

    def test_unreadable_tags_are_ignored(self) -> None:
        for tag in ("nightly", "", None, "v4.0.0-beta"):
            with self.subTest(tag=tag):
                self.assertEqual(self.check(tag), {"available": False})

    def test_failures_mean_no_notice(self) -> None:
        self.assertEqual(self.check("v9.0.0", returncode=22), {"available": False})
        with patch("videomasa.app_update.subprocess.run", side_effect=OSError("no curl")):
            self.assertEqual(check_for_newer_release("3.3.0"), {"available": False})
        bad = subprocess.CompletedProcess(["curl"], 0, "<html>", "")
        with patch("videomasa.app_update.subprocess.run", return_value=bad):
            self.assertEqual(check_for_newer_release("3.3.0"), {"available": False})
        timed_out = subprocess.TimeoutExpired(["curl"], 15)
        with patch("videomasa.app_update.subprocess.run", side_effect=timed_out):
            self.assertEqual(check_for_newer_release("3.3.0"), {"available": False})


if __name__ == "__main__":
    unittest.main()
