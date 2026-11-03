import hashlib
import io
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MACOS_DIR = PROJECT_ROOT / "packaging" / "macos"
APP_VERSION = (PROJECT_ROOT / "VERSION").read_text().strip()


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MacOSPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.resources = self.root / "Video Masa's Test.app" / "Contents" / "Resources"
        self.resources.mkdir(parents=True)
        shutil.copy2(MACOS_DIR / "launcher.sh", self.resources / "launcher.sh")
        shutil.copy2(MACOS_DIR / "setup.sh", self.resources / "setup.sh")
        shutil.copy2(PROJECT_ROOT / "VERSION", self.resources / "VERSION")
        (self.resources / "app").mkdir()
        (self.resources / "app" / "app.py").write_text("print('placeholder')\n")
        shutil.copy2(PROJECT_ROOT / "requirements.txt", self.resources / "app" / "requirements.txt")
        shutil.copy2(PROJECT_ROOT / "requirements.lock.txt", self.resources / "app" / "requirements.lock.txt")
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_script(self, name: str, **extra_env: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(
            {
                "VIDEOMASA_HOME": str(self.state),
                "VIDEOMASA_SKIP_RELAUNCH": "1",
                # Never let a test show a real dialog or open a real Terminal window.
                "VIDEOMASA_OSASCRIPT_BIN": "/usr/bin/true",
                **extra_env,
            }
        )
        return subprocess.run(
            ["/bin/bash", str(self.resources / name)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def make_healthy_venv(self, python_body: str) -> Path:
        python_path = self.state / "venv" / "bin" / "python"
        write_executable(python_path, python_body)
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "version").write_text(f"{APP_VERSION}\n")
        return python_path

    def write_health_server(self) -> Path:
        server_helper = self.root / "health_server.py"
        server_helper.write_text(
            textwrap.dedent(
                """\
                import http.server
                import os

                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        if self.path == "/health":
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(b'{"all_ok":true,"app_version":"@VERSION@"}')
                        else:
                            self.send_response(404)
                            self.end_headers()

                    def log_message(self, *_args):
                        pass

                http.server.HTTPServer(
                    ("127.0.0.1", int(os.environ["VIDEOMASA_PORT"])), Handler
                ).serve_forever()
                """
            ).replace("@VERSION@", APP_VERSION)
        )
        return server_helper

    def test_desktop_builds_include_backend_package_and_version_manifest(self) -> None:
        mac_build = (MACOS_DIR / "build_dmg.sh").read_text()
        windows_build = (PROJECT_ROOT / "packaging" / "windows" / "build_zip.sh").read_text()

        self.assertIn('cp -R "$PROJECT_DIR/videomasa"', mac_build)
        self.assertIn('cp -R "$PROJECT_DIR/videomasa"', windows_build)
        self.assertIn('cp "$PROJECT_DIR/VERSION" "$BUILD_DIR/"', windows_build)
        self.assertIn('VideoMasa-${APP_VERSION}-Windows.zip', windows_build)

    def test_launcher_detects_broken_python_symlink_and_runs_setup(self) -> None:
        broken_python = self.state / "venv" / "bin" / "python"
        broken_python.parent.mkdir(parents=True)
        broken_python.symlink_to("/missing/homebrew/python3.13")
        (self.state / "version").write_text(f"{APP_VERSION}\n")

        self.assertTrue(self.launcher_opens_setup())
        self.assertTrue(broken_python.is_symlink(), "launcher must not delete the old environment")

    def test_launcher_uses_exact_venv_interpreter_and_cleans_pid_after_exit(self) -> None:
        port = free_port()
        invocation_log = self.root / "python-invocation.txt"
        server_helper = self.write_health_server()
        self.make_healthy_venv(
            f"""\
            #!/bin/bash
            if [ "${{1:-}}" = "-c" ] || [ "${{1:-}}" = "-m" ]; then
                exit 0
            fi
            printf '%s\\n' "$*" > {str(invocation_log)!r}
            exec {sys.executable!r} {str(server_helper)!r}
            """
        )

        open_log = self.root / "open.txt"
        fake_open = self.root / "fake-open"
        write_executable(
            fake_open,
            f"""\
            #!/bin/bash
            printf '%s\\n' "$1" > {str(open_log)!r}
            kill "$(cat {str(self.state / 'server.pid')!r})"
            """,
        )

        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_PORT=str(port),
            VIDEOMASA_OPEN_BIN=str(fake_open),
            VIDEOMASA_READY_ATTEMPTS="40",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocation_log.read_text().strip(), str(self.resources / "app" / "app.py"))
        opened_url = open_log.read_text().strip()
        self.assertTrue(opened_url.startswith(f"http://127.0.0.1:{port}/?token="))
        self.assertGreater(len(opened_url.rsplit("=", 1)[1]), 32)
        self.assertFalse((self.state / "server.pid").exists())
        self.assertFalse((self.state / "server.token").exists())

    def test_launcher_surfaces_server_error_and_writes_diagnostics(self) -> None:
        self.make_healthy_venv(
            """\
            #!/bin/bash
            if [ "${1:-}" = "-c" ]; then
                exit 0
            fi
            echo "synthetic startup failure" >&2
            exit 42
            """
        )
        fake_osascript = self.root / "fake-osascript"
        write_executable(
            fake_osascript,
            """\
            #!/bin/bash
            cat >/dev/null
            echo "Copy Diagnostics"
            """,
        )
        copied = self.root / "clipboard.txt"
        fake_pbcopy = self.root / "fake-pbcopy"
        write_executable(fake_pbcopy, f"#!/bin/bash\ncat > {str(copied)!r}\n")

        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_PORT=str(free_port()),
            VIDEOMASA_OSASCRIPT_BIN=str(fake_osascript),
            VIDEOMASA_PBCOPY_BIN=str(fake_pbcopy),
            VIDEOMASA_READY_ATTEMPTS="4",
        )

        self.assertEqual(result.returncode, 1)
        diagnostics = (self.state / "diagnostics.txt").read_text()
        self.assertIn("synthetic startup failure", diagnostics)
        self.assertEqual(copied.read_text(), diagnostics)
        self.assertFalse((self.state / "server.pid").exists())

    def test_launcher_and_setup_reexecute_native_arm64_when_translated(self) -> None:
        fake_sysctl = self.root / "fake-sysctl"
        write_executable(fake_sysctl, "#!/bin/bash\necho 1\n")

        for script_name in ("launcher.sh", "setup.sh"):
            with self.subTest(script=script_name):
                arch_log = self.root / f"{script_name}.arch-args"
                fake_arch = self.root / f"{script_name}.fake-arch"
                write_executable(
                    fake_arch,
                    f"#!/bin/bash\nprintf '%s\\n' \"$@\" > {str(arch_log)!r}\n",
                )

                result = self.run_script(
                    script_name,
                    VIDEOMASA_SYSCTL_BIN=str(fake_sysctl),
                    VIDEOMASA_ARCH_BIN=str(fake_arch),
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    arch_log.read_text().splitlines(),
                    ["-arm64", "/bin/bash", str(self.resources / script_name)],
                )

    def make_fake_base_python(self, fail_pip: bool, version: str = "3.12") -> Path:
        fake_python = self.root / "fake-python"
        generated_python = self.root / "generated-venv-python"
        write_executable(
            generated_python,
            f"""\
            #!/bin/bash
            if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
                exit {19 if fail_pip else 0}
            fi
            if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "yt_dlp" ]; then
                echo "2026.7.4"
                exit 0
            fi
            if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "py_compile" ]; then
                exec {sys.executable!r} "$@"
            fi
            if [ "${{1:-}}" = "-" ]; then
                cat >/dev/null
                echo "  ✓ imports"
                exit 0
            fi
            exit 0
            """,
        )
        write_executable(
            fake_python,
            f"""\
            #!/bin/bash
            if [ "${{1:-}}" = "-c" ]; then
                echo {version}
                exit 0
            fi
            if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
                mkdir -p "$3/bin"
                cp {str(generated_python)!r} "$3/bin/python"
                chmod +x "$3/bin/python"
                exit 0
            fi
            exit 1
            """,
        )
        return fake_python

    def test_setup_failure_leaves_existing_environment_and_version_untouched(self) -> None:
        old_marker = self.state / "venv" / "old-marker"
        old_marker.parent.mkdir(parents=True)
        old_marker.write_text("keep me")
        (self.state / "version").write_text("3.0\n")

        result = self.run_script(
            "setup.sh",
            VIDEOMASA_PYTHON=str(self.make_fake_base_python(fail_pip=True)),
            VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
            VIDEOMASA_FFMPEG="/usr/bin/true",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(old_marker.exists())
        self.assertEqual((self.state / "version").read_text(), "3.0\n")
        self.assertFalse(any(self.state.glob("venv.runtime-*")))
        self.assertFalse((self.state / "setup.lock").exists())

    def test_setup_only_accepts_python_versions_its_dependencies_support(self) -> None:
        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")
        cases = (("3.10", False), ("3.11", True), ("3.12", True), ("3.13", False), ("3.15", False))
        for version, supported in cases:
            with self.subTest(version=version):
                result = self.run_script(
                    "setup.sh",
                    VIDEOMASA_PYTHON=str(self.make_fake_base_python(fail_pip=False, version=version)),
                    VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
                    VIDEOMASA_FFMPEG=str(fake_bin / "ffmpeg"),
                    PATH=f"{fake_bin}:{os.environ['PATH']}",
                )
                self.assertEqual(result.returncode == 0, supported, result.stdout + result.stderr)
                if not supported:
                    self.assertIn("VIDEOMASA_PYTHON must be", result.stdout)

    def test_first_launch_needs_no_terminal_password_or_homebrew(self) -> None:
        setup = (MACOS_DIR / "setup.sh").read_text()
        launcher = (MACOS_DIR / "launcher.sh").read_text()
        for needed_by_nobody in ("brew install", "sudo ", "python.org", 'tell application "Terminal"', "do script"):
            self.assertNotIn(needed_by_nobody, setup)
            self.assertNotIn(needed_by_nobody, launcher)

    def test_setup_atomically_replaces_and_preserves_broken_environment(self) -> None:
        broken_python = self.state / "venv" / "bin" / "python"
        broken_python.parent.mkdir(parents=True)
        broken_python.symlink_to("/missing/python")
        (self.state / "version").write_text("3.0\n")

        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")
        result = self.run_script(
            "setup.sh",
            VIDEOMASA_PYTHON=str(self.make_fake_base_python(fail_pip=False)),
            VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
            VIDEOMASA_FFMPEG=str(fake_bin / "ffmpeg"),
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / "venv").is_symlink())
        self.assertTrue((self.state / "venv" / "bin" / "python").exists())
        self.assertTrue((self.state / "venv" / "bin" / "ffmpeg").is_symlink())
        self.assertEqual((self.state / "version").read_text(), f"{APP_VERSION}\n")
        backups = list(self.state.glob("venv.broken-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "bin" / "python").is_symlink())
        self.assertEqual(len(list(self.state.glob("venv.runtime-*"))), 1)
        self.assertFalse((self.state / "setup.lock").exists())

    def test_setup_verifies_source_without_writing_to_protected_app_bundle(self) -> None:
        app_dir = self.resources / "app"
        app_path = app_dir / "app.py"
        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")

        app_path.chmod(0o444)
        app_dir.chmod(0o555)
        try:
            result = self.run_script(
                "setup.sh",
                VIDEOMASA_PYTHON=str(self.make_fake_base_python(fail_pip=False)),
                VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
                VIDEOMASA_FFMPEG=str(fake_bin / "ffmpeg"),
                PATH=f"{fake_bin}:{os.environ['PATH']}",
            )
        finally:
            app_dir.chmod(0o755)
            app_path.chmod(0o644)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((app_dir / "__pycache__").exists())
        self.assertEqual((self.state / "version").read_text(), f"{APP_VERSION}\n")

    def launch_recording_ytdlp_update(self) -> tuple:
        """Launch a fake server that stops once the browser opens; return the two logs."""
        server_helper = self.write_health_server()
        update_log = self.root / "update-invocation.txt"
        path_log = self.root / "server-path.txt"
        self.make_healthy_venv(
            f"""\
            #!/bin/bash
            if [ "${{1:-}}" = "-c" ]; then
                exit 0
            fi
            if [ "${{1:-}}" = "-m" ]; then
                printf '%s\\n' "$PWD" "$@" > {str(update_log)!r}.tmp
                mv {str(update_log)!r}.tmp {str(update_log)!r}
                exit 0
            fi
            printf '%s' "$PATH" > {str(path_log)!r}
            printf '%s\\n' "$VIDEOMASA_HOME" "$VIDEOMASA_RESOURCES_DIR" "$VIDEOMASA_YTDLP_UPDATES_DIR" > {str(self.root / "server-env.txt")!r}
            exec {sys.executable!r} {str(server_helper)!r}
            """
        )
        fake_open = self.root / "fake-open"
        write_executable(
            fake_open,
            f"""\
            #!/bin/bash
            kill "$(cat {str(self.state / 'server.pid')!r})"
            """,
        )
        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_PORT=str(free_port()),
            VIDEOMASA_OPEN_BIN=str(fake_open),
            VIDEOMASA_READY_ATTEMPTS="40",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.time() + 3
        while not update_log.exists() and time.time() < deadline:
            time.sleep(0.05)
        return update_log, path_log

    def test_launcher_starts_ytdlp_update_and_prefers_updated_copy_on_path(self) -> None:
        update_log, path_log = self.launch_recording_ytdlp_update()

        self.assertEqual(
            update_log.read_text().splitlines(),
            [
                str(self.resources / "app"),
                "-m",
                "videomasa.ytdlp_update",
                str(self.state / "ytdlp"),
            ],
        )
        path_parts = path_log.read_text().split(":")
        self.assertLess(
            path_parts.index(str(self.state / "ytdlp" / "bin")),
            path_parts.index(str(self.state / "venv" / "bin")),
        )
        self.assertEqual(
            (self.root / "server-env.txt").read_text().splitlines(),
            [str(self.state), str(self.resources), str(self.state / "ytdlp")],
        )

    def test_launcher_skips_ytdlp_update_when_opted_out(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "no-ytdlp-update").touch()

        update_log, _ = self.launch_recording_ytdlp_update()

        self.assertFalse(update_log.exists())

    def test_launcher_keeps_working_updated_ytdlp_and_drops_broken_one(self) -> None:
        link = self.state / "ytdlp" / "bin" / "yt-dlp"
        link.parent.mkdir(parents=True)
        working = self.root / "working-yt-dlp"
        write_executable(working, "#!/bin/bash\necho 2026.11.02\n")
        link.symlink_to(working)

        stamp = self.state / "ytdlp" / "last-check"
        stamp.touch()
        self.launch_recording_ytdlp_update()
        self.assertTrue(link.is_symlink())
        self.assertTrue(stamp.exists())

        link.unlink()
        link.symlink_to("/missing/python/yt-dlp")
        self.launch_recording_ytdlp_update()
        self.assertFalse(link.is_symlink())
        self.assertFalse(stamp.exists())

    def test_setup_returns_to_locked_ytdlp_only_after_success(self) -> None:
        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")
        updated = self.state / "ytdlp" / "bin" / "yt-dlp"
        updated.parent.mkdir(parents=True)
        updated.write_text("updated copy")

        for fail_pip, should_remain in ((True, True), (False, False)):
            with self.subTest(fail_pip=fail_pip):
                self.run_script(
                    "setup.sh",
                    VIDEOMASA_PYTHON=str(self.make_fake_base_python(fail_pip=fail_pip)),
                    VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
                    VIDEOMASA_FFMPEG=str(fake_bin / "ffmpeg"),
                    PATH=f"{fake_bin}:{os.environ['PATH']}",
                )
                self.assertEqual(updated.exists(), should_remain)

    def locked_packages_hash(self) -> str:
        lock_file = self.resources / "app" / "requirements.lock.txt"
        return hashlib.sha256(lock_file.read_bytes()).hexdigest()

    def launcher_opens_setup(self) -> bool:
        setup = self.resources / "setup.sh"
        original = setup.read_text()
        marker = self.root / "setup-ran.txt"
        marker.unlink(missing_ok=True)
        write_executable(setup, f"#!/bin/bash\ntouch {str(marker)!r}\nexit 1\n")
        fake_open = self.root / "fake-open"
        write_executable(fake_open, f"#!/bin/bash\nkill \"$(cat {str(self.state / 'server.pid')!r})\"\n")
        try:
            self.run_script(
                "launcher.sh",
                VIDEOMASA_PORT=str(free_port()),
                VIDEOMASA_OPEN_BIN=str(fake_open),
                VIDEOMASA_READY_ATTEMPTS="40",
            )
        finally:
            setup.write_text(original)
        return marker.exists()

    def make_runnable_venv(self) -> None:
        server_helper = self.write_health_server()
        self.make_healthy_venv(
            f"""\
            #!/bin/bash
            if [ "${{1:-}}" = "-c" ] || [ "${{1:-}}" = "-m" ]; then
                exit 0
            fi
            exec {sys.executable!r} {str(server_helper)!r}
            """
        )

    def test_app_upgrade_only_rebuilds_when_locked_packages_change(self) -> None:
        self.make_runnable_venv()
        hash_file = self.state / "lock-hash"
        hash_file.write_text(f"{self.locked_packages_hash()}\n")

        (self.state / "version").write_text("1.0.0\n")
        self.assertFalse(self.launcher_opens_setup(), "same locked packages must not rebuild")

        with open(self.resources / "app" / "requirements.lock.txt", "a") as lock_file:
            lock_file.write("# changed\n")
        self.assertTrue(self.launcher_opens_setup(), "changed locked packages must rebuild")

    def test_older_install_keeps_version_check_until_it_records_a_hash(self) -> None:
        self.make_runnable_venv()
        hash_file = self.state / "lock-hash"

        (self.state / "version").write_text("1.0.0\n")
        self.assertTrue(self.launcher_opens_setup())
        self.assertFalse(hash_file.exists())

        (self.state / "version").write_text(f"{APP_VERSION}\n")
        self.assertFalse(self.launcher_opens_setup())
        self.assertEqual(hash_file.read_text().strip(), self.locked_packages_hash())

    def test_setup_records_the_lock_hash_and_skips_pips_cache(self) -> None:
        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")
        result = self.run_script(
            "setup.sh",
            VIDEOMASA_PYTHON=str(self.make_fake_base_python(fail_pip=False)),
            VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
            VIDEOMASA_FFMPEG=str(fake_bin / "ffmpeg"),
            PATH=f"{fake_bin}:{os.environ['PATH']}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "lock-hash").read_text().strip(), self.locked_packages_hash())

        pip_lines = [
            line for line in (MACOS_DIR / "setup.sh").read_text().splitlines()
            if "-m pip install" in line
        ]
        self.assertEqual(len(pip_lines), 2)
        for line in pip_lines:
            self.assertIn("--no-cache-dir", line)

    def wait_for_old_environments_to_go(self) -> list:
        deadline = time.time() + 5
        while time.time() < deadline:
            leftovers = sorted(path.name for path in self.state.glob("venv.*"))
            if len(leftovers) <= 1:
                return leftovers
            time.sleep(0.05)
        return sorted(path.name for path in self.state.glob("venv.*"))

    def test_launcher_removes_old_environments_once_the_app_is_ready(self) -> None:
        self.make_runnable_venv()
        real_venv = self.state / "venv"
        current = self.state / "venv.runtime-current"
        real_venv.rename(current)
        real_venv.symlink_to(current.name)
        for name in ("venv.runtime-old", "venv.broken-old"):
            (self.state / name / "bin").mkdir(parents=True)
        (self.state / "venv.previous-old").symlink_to("venv.runtime-old")
        unrelated = self.state / "venvs-notes.txt"
        unrelated.write_text("keep")
        pythons = self.state / "python"
        for name in ("3.12.15-old", "3.12.16-new"):
            (pythons / name).mkdir(parents=True)
        (pythons / "current").symlink_to("3.12.16-new")

        self.assertFalse(self.launcher_opens_setup())

        self.assertEqual(self.wait_for_old_environments_to_go(), ["venv.runtime-current"])
        self.assertTrue((self.state / "venv" / "bin" / "python").exists())
        self.assertTrue(unrelated.exists())
        deadline = time.time() + 5
        while (pythons / "3.12.15-old").exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(sorted(path.name for path in pythons.iterdir()), ["3.12.16-new", "current"])

    def test_old_environments_stay_while_a_repair_is_running(self) -> None:
        self.make_runnable_venv()
        old = self.state / "venv.runtime-being-built"
        old.mkdir()
        (self.state / "setup.lock").mkdir()

        self.assertFalse(self.launcher_opens_setup())
        time.sleep(0.5)

        self.assertTrue(old.exists())

    def run_uninstall(self, home: Path, pid: str = "999999") -> subprocess.CompletedProcess:
        script = self.resources / "uninstall.sh"
        shutil.copy2(MACOS_DIR / "uninstall.sh", script)
        self.trash_log = self.root / "trash.txt"
        fake_trash = self.root / "fake-trash"
        write_executable(fake_trash, f"#!/bin/bash\nprintf '%s\\n' \"$1\" > {str(self.trash_log)!r}\n")
        return subprocess.run(
            ["/bin/bash", str(script), pid, str(home), str(self.root / "whisper")],
            env={**os.environ, "VIDEOMASA_TRASH_BIN": str(fake_trash)},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_uninstall_removes_app_data_this_apps_models_and_the_app(self) -> None:
        home = self.root / ".videomasa"
        (home / "cookies").mkdir(parents=True)
        (home / "cookies" / "session.txt").write_text("secret")
        models = self.root / "whisper"
        models.mkdir()
        (models / "base.pt").write_text("model")
        (models / "large-v3.pt").write_text("another tool's model")

        result = self.run_uninstall(home)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(home.exists())
        self.assertEqual([path.name for path in models.iterdir()], ["large-v3.pt"])
        self.assertEqual(
            os.path.realpath(self.trash_log.read_text().strip()),
            os.path.realpath(self.root / "Video Masa's Test.app"),
        )

    def test_uninstall_removes_an_empty_model_folder(self) -> None:
        home = self.root / ".videomasa"
        home.mkdir()
        models = self.root / "whisper"
        models.mkdir()
        (models / "tiny.pt").write_text("model")

        self.run_uninstall(home)

        self.assertFalse(models.exists())

    def test_uninstall_refuses_an_unexpected_folder(self) -> None:
        other = self.root / "documents"
        other.mkdir()
        (other / "thesis.txt").write_text("keep")

        result = self.run_uninstall(other)

        self.assertEqual(result.returncode, 1)
        self.assertTrue((other / "thesis.txt").exists())
        self.assertFalse(self.trash_log.exists())

    def test_uninstall_leaves_everything_if_the_app_will_not_stop(self) -> None:
        home = self.root / ".videomasa"
        home.mkdir()
        script_body = (MACOS_DIR / "uninstall.sh").read_text().replace('-lt 120', '-lt 2')
        script = self.resources / "uninstall.sh"
        script.write_text(script_body)
        result = subprocess.run(
            ["/bin/bash", str(script), str(os.getpid()), str(home), str(self.root / "whisper")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue(home.exists())

    def fake_setup(self, body: str) -> None:
        write_executable(self.resources / "setup.sh", "#!/bin/bash\n" + body)

    def launcher_output_lines(self, result: subprocess.CompletedProcess) -> list:
        return [line for line in result.stdout.splitlines() if line.startswith("@@")]

    def test_launcher_runs_setup_in_place_and_reports_progress(self) -> None:
        self.make_runnable_venv()
        (self.state / "version").write_text("1.0.0\n")
        self.fake_setup(
            """\
            echo "detail for the log"
            echo "@@STATUS Installing" >&"$VIDEOMASA_STATUS_FD"
            [ "$VIDEOMASA_SKIP_RELAUNCH" = "1" ] || echo "@@STATUS relaunch not skipped" >&3
            exit 0
            """.replace("            ", "")
        )
        fake_open = self.root / "fake-open"
        write_executable(fake_open, f"#!/bin/bash\nkill \"$(cat {str(self.state / 'server.pid')!r})\"\n")
        osascript_log = self.root / "osascript.txt"
        fake_osascript = self.root / "fake-osascript"
        write_executable(fake_osascript, f"#!/bin/bash\ntouch {str(osascript_log)!r}\n")
        port = free_port()

        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_PORT=str(port),
            VIDEOMASA_OPEN_BIN=str(fake_open),
            VIDEOMASA_OSASCRIPT_BIN=str(fake_osascript),
            VIDEOMASA_READY_ATTEMPTS="40",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.launcher_output_lines(result)
        self.assertEqual(lines[:2], ["@@STATUS Getting started...", "@@STATUS Installing"])
        self.assertTrue(lines[2].startswith(f"@@URL http://127.0.0.1:{port}/?token="))
        self.assertEqual(lines[3], "@@READY")
        log = (self.state / "setup.log").read_text()
        self.assertIn("detail for the log", log)
        self.assertNotIn("@@STATUS", log)
        self.assertFalse(osascript_log.exists(), "first launch must not script other apps")

    def test_failed_setup_tells_the_window_and_starts_nothing(self) -> None:
        self.fake_setup("echo 'pip exploded'\nexit 7\n")

        result = self.run_script("launcher.sh", VIDEOMASA_PORT=str(free_port()))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            self.launcher_output_lines(result)[-1],
            "@@FAILED Please check your internet connection and try again.",
        )
        self.assertIn("pip exploded", (self.state / "setup.log").read_text())
        self.assertFalse((self.state / "server.pid").exists())

    def start_launcher(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["/bin/bash", str(self.resources / "launcher.sh")],
            env={
                **os.environ,
                "VIDEOMASA_HOME": str(self.state),
                "VIDEOMASA_PORT": str(free_port()),
                "VIDEOMASA_OPEN_BIN": "/usr/bin/true",
                "VIDEOMASA_OSASCRIPT_BIN": "/usr/bin/true",
                "VIDEOMASA_READY_ATTEMPTS": "40",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def test_quitting_stops_the_server_and_cleans_up(self) -> None:
        self.make_runnable_venv()
        launcher = self.start_launcher()
        self.addCleanup(launcher.kill)
        for line in launcher.stdout:
            if line.startswith("@@READY"):
                break
        server_pid = int((self.state / "server.pid").read_text())

        launcher.send_signal(signal.SIGTERM)
        launcher.communicate(timeout=20)

        with self.assertRaises(ProcessLookupError):
            os.kill(server_pid, 0)
        self.assertFalse((self.state / "server.pid").exists())
        self.assertFalse((self.state / "server.token").exists())

    def test_force_quitting_the_app_stops_the_server(self) -> None:
        self.make_runnable_venv()
        env = {
            **os.environ,
            "VIDEOMASA_HOME": str(self.state),
            "VIDEOMASA_PORT": str(free_port()),
            "VIDEOMASA_OPEN_BIN": "/usr/bin/true",
            "VIDEOMASA_OSASCRIPT_BIN": "/usr/bin/true",
        }
        launcher_path = str(self.resources / "launcher.sh")
        # The parent exits straight away, so the launcher is left like after a force quit.
        subprocess.run(
            ["/bin/bash", "-c", '/bin/bash "$1" > /dev/null 2>&1 < /dev/null &', "parent", launcher_path],
            env=env,
            check=True,
        )
        pid_file = self.state / "server.pid"
        deadline = time.time() + 20
        server_pid = None
        while time.time() < deadline:
            if pid_file.exists() and pid_file.read_text().strip():
                server_pid = int(pid_file.read_text())
                break
            time.sleep(0.05)
        self.assertIsNotNone(server_pid)
        self.addCleanup(lambda: subprocess.run(["kill", str(server_pid)], capture_output=True))

        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                os.kill(server_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            self.fail("server kept running after the app was force quit")

    def test_quitting_during_setup_stops_it_without_reporting_a_failure(self) -> None:
        setup_pid_file = self.root / "setup-pid.txt"
        self.fake_setup(
            f"echo $$ > {str(setup_pid_file)!r}\n"
            'echo "@@STATUS Working" >&"$VIDEOMASA_STATUS_FD"\n'
            "sleep 60 &\nwait $!\n"
        )
        launcher = self.start_launcher()
        self.addCleanup(launcher.kill)
        for line in launcher.stdout:
            if line.startswith("@@STATUS Working"):
                break

        launcher.send_signal(signal.SIGTERM)
        remaining, _ = launcher.communicate(timeout=20)

        self.assertNotIn("@@FAILED", remaining)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(setup_pid_file.read_text()), 0)

    def test_repair_runs_setup_again_without_leaving_the_app(self) -> None:
        self.make_healthy_venv("#!/bin/bash\n[ \"${1:-}\" = \"-c\" ] && exit 0\nexit 42\n")
        (self.state / "lock-hash").write_text(f"{self.locked_packages_hash()}\n")
        fake_osascript = self.root / "fake-osascript"
        write_executable(fake_osascript, "#!/bin/bash\ncat >/dev/null\necho 'Repair Installation'\n")
        marker = self.root / "repair-ran.txt"
        self.fake_setup(f"touch {str(marker)!r}\nexit 1\n")

        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_PORT=str(free_port()),
            VIDEOMASA_OSASCRIPT_BIN=str(fake_osascript),
            VIDEOMASA_READY_ATTEMPTS="4",
        )

        self.assertTrue(marker.exists())
        self.assertFalse((self.state / "version").exists())
        self.assertFalse((self.state / "lock-hash").exists())
        self.assertEqual(self.launcher_output_lines(result)[-1], "@@FAILED Please check your internet connection and try again.")

    def make_python_archive(self) -> tuple:
        """Return a tarball shaped like the real download, and its hash."""
        fake_python = self.make_fake_base_python(fail_pip=False)
        archive = self.root / "python.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            data = fake_python.read_bytes()
            info = tarfile.TarInfo("python/bin/python3")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def fake_curl(self, source: Path = None, exit_code: int = 0) -> tuple:
        """Return a curl that serves a file and records each call."""
        calls = self.root / "curl-calls.txt"
        curl = self.root / "fake-curl"
        copy = f'cp {str(source)!r} "$output"' if source else 'printf junk > "$output"'
        write_executable(
            curl,
            "#!/bin/bash\n"
            f"echo \"${{@: -1}}\" >> {str(calls)!r}\n"
            "while [ $# -gt 0 ]; do [ \"$1\" = \"--output\" ] && output=\"$2\"; shift; done\n"
            f"[ {exit_code} -eq 0 ] || exit {exit_code}\n"
            # The launcher's health check also uses curl, with nothing to save.
            "[ -n \"$output\" ] || exit 7\n"
            f"{copy}\n",
        )
        return curl, calls

    def setup_env(self, curl: Path, **extra: str) -> dict:
        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")
        return {
            "VIDEOMASA_CURL_BIN": str(curl),
            "VIDEOMASA_SKIP_MODEL_DOWNLOAD": "1",
            "VIDEOMASA_FFMPEG": str(fake_bin / "ffmpeg"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            **extra,
        }

    def test_setup_installs_its_own_python_once_and_records_it(self) -> None:
        archive, sha256 = self.make_python_archive()
        curl, calls = self.fake_curl(archive)
        env = self.setup_env(curl, VIDEOMASA_PYTHON_ARCHIVE_SHA256=sha256)

        result = self.run_script("setup.sh", **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        urls = calls.read_text().splitlines()
        self.assertEqual(len(urls), 1)
        self.assertIn("astral-sh/python-build-standalone/releases/download/", urls[0])
        self.assertIn("apple-darwin-install_only_stripped.tar.gz", urls[0])
        current = self.state / "python" / "current"
        self.assertTrue(current.is_symlink())
        self.assertTrue((current / "bin" / "python3").exists())
        self.assertEqual([path.name for path in (self.state / "python").glob(".download.*")], [])

        result = self.run_script("setup.sh", **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls.read_text().splitlines()), 1, "Python should not download twice")

    def test_setup_rejects_a_python_download_with_the_wrong_hash(self) -> None:
        curl, _ = self.fake_curl()

        result = self.run_script("setup.sh", **self.setup_env(curl))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not match its expected hash", result.stdout)
        self.assertEqual(sorted(path.name for path in (self.state / "python").iterdir()), [])
        self.assertFalse((self.state / "venv").exists())

    def test_setup_fails_cleanly_when_python_cannot_be_downloaded(self) -> None:
        curl, _ = self.fake_curl(exit_code=22)

        result = self.run_script("setup.sh", **self.setup_env(curl))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(path.name for path in (self.state / "python").iterdir()), [])
        self.assertFalse((self.state / "setup.lock").exists())

    def test_setup_pins_a_hash_for_each_mac_processor(self) -> None:
        setup = (MACOS_DIR / "setup.sh").read_text()
        self.assertRegex(setup, r'PYTHON_SHA256_ARM64="[0-9a-f]{64}"')
        self.assertRegex(setup, r'PYTHON_SHA256_X86_64="[0-9a-f]{64}"')

    def test_real_setup_reports_each_step_to_the_window(self) -> None:
        archive, sha256 = self.make_python_archive()
        curl, _ = self.fake_curl(archive)

        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_PORT=str(free_port()),
            VIDEOMASA_READY_ATTEMPTS="2",
            **self.setup_env(curl, VIDEOMASA_PYTHON_ARCHIVE_SHA256=sha256),
        )

        statuses = [line for line in self.launcher_output_lines(result) if line.startswith("@@STATUS")]
        self.assertEqual(
            statuses,
            [
                "@@STATUS Getting started...",
                "@@STATUS Downloading Python...",
                "@@STATUS Installing components. This is the longest step...",
                "@@STATUS Downloading the speech model...",
                "@@STATUS Checking that everything works...",
            ],
        )

    def test_app_shell_handles_the_dock_and_running_from_the_disk_image(self) -> None:
        shell = (MACOS_DIR / "VideoMasaApp.swift").read_text()
        self.assertIn("applicationShouldHandleReopen", shell)
        self.assertIn("applicationShouldTerminate", shell)
        self.assertIn('path.hasPrefix("/Volumes/")', shell)
        self.assertIn('path.contains("/AppTranslocation/")', shell)

    def test_build_makes_a_universal_shell_and_a_stable_download_name(self) -> None:
        build = (MACOS_DIR / "build_dmg.sh").read_text()
        self.assertIn("-target arm64-apple-macos", build)
        self.assertIn("-target x86_64-apple-macos", build)
        self.assertIn('"$DIST_DIR/VideoMasa.dmg"', build)
        self.assertIn('--options runtime --timestamp --sign "$SIGN_ID" "$APP_BUNDLE/Contents/MacOS/VideoMasa"', build)

    def test_build_packages_the_uninstall_script(self) -> None:
        build = (MACOS_DIR / "build_dmg.sh").read_text()
        self.assertIn('cp "$SCRIPT_DIR/uninstall.sh"', build)

    def test_storage_stays_flat_across_years_of_app_upgrades(self) -> None:
        """Ten rebuilds with 1 MB environments must leave about one environment on disk."""
        budget_bytes = 1_500_000
        fake_bin = self.root / "bin"
        write_executable(fake_bin / "ffmpeg", "#!/bin/bash\necho 'ffmpeg version test'\n")
        server_helper = self.write_health_server()
        fake_python = self.make_fake_base_python(fail_pip=False)
        lock_file = self.resources / "app" / "requirements.lock.txt"

        for upgrade in range(10):
            with open(lock_file, "a") as lock:
                lock.write(f"# upgrade {upgrade}\n")
            result = self.run_script(
                "setup.sh",
                VIDEOMASA_PYTHON=str(fake_python),
                VIDEOMASA_SKIP_MODEL_DOWNLOAD="1",
                VIDEOMASA_FFMPEG=str(fake_bin / "ffmpeg"),
                PATH=f"{fake_bin}:{os.environ['PATH']}",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            environment = self.state / "venv"
            (environment / "packages.bin").write_bytes(b"x" * 1_000_000)
            write_executable(
                environment / "bin" / "python",
                f"""\
                #!/bin/bash
                if [ "${{1:-}}" = "-c" ] || [ "${{1:-}}" = "-m" ]; then
                    exit 0
                fi
                exec {sys.executable!r} {str(server_helper)!r}
                """,
            )
            self.assertFalse(self.launcher_opens_setup(), f"upgrade {upgrade} should already be built")
            self.wait_for_old_environments_to_go()

        used = sum(
            path.stat().st_size for path in self.state.rglob("*") if path.is_file() and not path.is_symlink()
        )
        self.assertLess(used, budget_bytes)
        self.assertEqual(len(list(self.state.glob("venv.runtime-*"))), 1)
        self.assertEqual(list(self.state.glob("venv.previous-*")), [])


if __name__ == "__main__":
    unittest.main()
