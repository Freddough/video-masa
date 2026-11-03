import contextlib
import fcntl
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from videomasa import ytdlp_update


class YtDlpUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.updates = Path(temp_dir.name) / "ytdlp"
        self.commands = []
        self.locked = "2026.7.4"
        self.latest = "2026.11.2"
        self.yanked = False
        self.pypi_fails = False
        self.pip_fails = False
        self.reported_version = None

    @property
    def link(self) -> Path:
        return self.updates / "bin" / "yt-dlp"

    @property
    def stamp(self) -> Path:
        return self.updates / "last-check"

    def fake_run(self, command, **_kwargs) -> subprocess.CompletedProcess:
        self.commands.append(command)
        if command[0] == "/usr/bin/curl":
            if self.pypi_fails:
                return subprocess.CompletedProcess(command, 22, "", "offline")
            body = json.dumps({"info": {"version": self.latest, "yanked": self.yanked}})
            return subprocess.CompletedProcess(command, 0, body, "")
        if "venv" in command:
            Path(command[-1], "bin").mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        if "pip" in command:
            if self.pip_fails:
                return subprocess.CompletedProcess(command, 1, "", "no matching distribution")
            release = Path(command[command.index("--python") + 1]).parent.parent
            (release / "bin" / "yt-dlp").write_text("#!/bin/sh\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:] == ["--version"]:
            release_name = Path(command[0]).resolve().parent.parent.name
            # yt-dlp pads its own version, unlike the normalized PyPI one.
            padded = ".".join(f"{int(part):02d}" for part in release_name.split("."))
            return subprocess.CompletedProcess(command, 0, f"{self.reported_version or padded}\n", "")
        raise AssertionError(f"Unexpected command: {command}")

    def run_update(self) -> str:
        with (
            patch.object(ytdlp_update.subprocess, "run", side_effect=self.fake_run),
            patch.object(ytdlp_update.metadata, "version", side_effect=lambda _name: self.locked),
        ):
            return ytdlp_update.update_ytdlp(self.updates)

    def run_main(self) -> str:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(ytdlp_update.subprocess, "run", side_effect=self.fake_run),
            patch.object(ytdlp_update.metadata, "version", side_effect=lambda _name: self.locked),
        ):
            self.assertEqual(ytdlp_update.main([str(self.updates)]), 0)
        return output.getvalue()

    def pip_commands(self) -> list:
        return [command for command in self.commands if "pip" in command]

    def age_stamp(self) -> None:
        old = time.time() - ytdlp_update.CHECK_INTERVAL_SECONDS - 60
        os.utime(self.stamp, (old, old))

    def active_release(self) -> str:
        return self.link.resolve().parent.parent.name

    def test_versions_compare_by_date_and_reject_prereleases(self) -> None:
        self.assertEqual(ytdlp_update.parse_version("2026.07.04"), ytdlp_update.parse_version("2026.7.4"))
        self.assertGreater(ytdlp_update.parse_version("2026.10.26"), ytdlp_update.parse_version("2026.9.30"))
        self.assertGreater(ytdlp_update.parse_version("2026.10.26.1"), ytdlp_update.parse_version("2026.10.26"))
        for unusable in ("2026.11.2.dev0", "", None):
            self.assertIsNone(ytdlp_update.parse_version(unusable))

    def test_newer_release_installs_separately_and_becomes_active(self) -> None:
        self.assertEqual(self.run_update(), "Updated yt-dlp to 2026.11.2.")

        self.assertEqual(self.active_release(), "2026.11.2")
        self.assertTrue(self.stamp.exists())
        pip_install = self.pip_commands()[0]
        self.assertIn("yt-dlp[default]==2026.11.2", pip_install)
        self.assertIn(":all:", pip_install)
        self.assertEqual(
            pip_install[pip_install.index("--python") + 1],
            str(self.updates / "releases" / "2026.11.2" / "bin" / "python"),
        )

    def test_check_runs_at_most_once_a_day(self) -> None:
        self.run_update()
        self.commands.clear()

        self.assertEqual(self.run_update(), "Checked for a yt-dlp update recently.")
        self.assertEqual(self.commands, [])

        self.age_stamp()
        self.run_update()
        self.assertEqual(self.commands[0][0], "/usr/bin/curl")

    def test_a_stamp_from_the_future_does_not_block_checks(self) -> None:
        self.run_update()
        future = time.time() + 3600
        os.utime(self.stamp, (future, future))
        self.commands.clear()

        self.run_update()
        self.assertEqual(self.commands[0][0], "/usr/bin/curl")

    def test_no_install_when_locked_version_is_current_or_newer(self) -> None:
        for locked in ("2026.11.2", "2026.12.1"):
            with self.subTest(locked=locked):
                self.locked = locked
                self.commands.clear()
                self.stamp.unlink(missing_ok=True)

                self.assertIn("up to date", self.run_update())
                self.assertEqual(self.pip_commands(), [])
                self.assertFalse(self.link.exists())
                self.assertTrue(self.stamp.exists())

    def test_no_reinstall_when_updated_copy_is_current(self) -> None:
        self.run_update()
        self.age_stamp()
        self.commands.clear()

        self.assertIn("up to date", self.run_update())
        self.assertEqual(self.pip_commands(), [])

    def test_unusable_or_yanked_release_is_skipped(self) -> None:
        for latest, yanked in (("2026.11.2.dev0", False), ("2026.11.2", True)):
            with self.subTest(latest=latest, yanked=yanked):
                self.latest, self.yanked = latest, yanked
                self.commands.clear()
                self.stamp.unlink(missing_ok=True)

                self.assertIn("unusable", self.run_update())
                self.assertEqual(self.pip_commands(), [])

    def test_failed_install_keeps_current_copy_and_retries_next_launch(self) -> None:
        self.run_update()
        self.age_stamp()
        aged = self.stamp.stat().st_mtime
        self.latest = "2026.12.1"
        self.pip_fails = True

        output = self.run_main()

        self.assertIn("Keeping the current yt-dlp", output)
        self.assertIn("no matching distribution", output)
        self.assertEqual(self.active_release(), "2026.11.2")
        self.assertFalse((self.updates / "releases" / "2026.12.1").exists())
        self.assertEqual(self.stamp.stat().st_mtime, aged)

    def test_wrong_version_after_install_is_not_activated(self) -> None:
        self.reported_version = "2020.01.01"

        output = self.run_main()

        self.assertIn("expected yt-dlp 2026.11.2", output)
        self.assertFalse(self.link.exists())
        self.assertFalse((self.updates / "releases" / "2026.11.2").exists())
        self.assertFalse(self.stamp.exists())

    def test_network_failure_changes_nothing_and_retries_next_launch(self) -> None:
        self.pypi_fails = True

        output = self.run_main()

        self.assertIn("Keeping the current yt-dlp", output)
        self.assertEqual(self.pip_commands(), [])
        self.assertFalse(self.stamp.exists())

    def test_broken_updated_copy_is_replaced_using_locked_version(self) -> None:
        self.run_update()
        self.age_stamp()
        real_run = self.fake_run

        def broken_copy(command, **kwargs):
            if command[0] == str(self.link):
                raise OSError("bad interpreter")
            return real_run(command, **kwargs)

        self.latest = "2026.12.1"
        with (
            patch.object(ytdlp_update.subprocess, "run", side_effect=broken_copy),
            patch.object(ytdlp_update.metadata, "version", side_effect=lambda _name: self.locked),
        ):
            self.assertEqual(ytdlp_update.update_ytdlp(self.updates), "Updated yt-dlp to 2026.12.1.")
        self.assertEqual(self.active_release(), "2026.12.1")

    def test_only_active_and_previous_releases_are_kept(self) -> None:
        for latest in ("2026.11.2", "2026.12.1", "2027.1.5"):
            self.latest = latest
            self.stamp.unlink(missing_ok=True)
            self.run_update()

        self.assertEqual(
            sorted(path.name for path in (self.updates / "releases").iterdir()),
            ["2026.12.1", "2027.1.5"],
        )
        self.assertEqual(self.active_release(), "2027.1.5")

    def test_second_update_is_skipped_while_one_is_running(self) -> None:
        self.updates.mkdir(parents=True)
        with open(self.updates / "update.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn("already running", self.run_update())
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
