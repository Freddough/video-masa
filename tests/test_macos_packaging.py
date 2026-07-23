import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MACOS_DIR = PROJECT_ROOT / "packaging" / "macos"


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
        (self.state / "version").write_text("3.1.1\n")
        return python_path

    def test_desktop_builds_include_backend_package_and_version_manifest(self) -> None:
        mac_build = (MACOS_DIR / "build_dmg.sh").read_text()
        windows_build = (PROJECT_ROOT / "packaging" / "windows" / "build_zip.sh").read_text()

        self.assertIn('cp -R "$PROJECT_DIR/videomasa"', mac_build)
        self.assertIn('cp -R "$PROJECT_DIR/videomasa"', windows_build)
        self.assertIn('cp "$PROJECT_DIR/VERSION" "$BUILD_DIR/"', windows_build)
        self.assertIn('VideoMasa-${APP_VERSION}-Windows.zip', windows_build)

    def test_launcher_detects_broken_python_symlink_and_safely_opens_setup(self) -> None:
        broken_python = self.state / "venv" / "bin" / "python"
        broken_python.parent.mkdir(parents=True)
        broken_python.symlink_to("/missing/homebrew/python3.13")
        (self.state / "version").write_text("3.1.1\n")

        osascript_log = self.root / "osascript-args.txt"
        fake_osascript = self.root / "fake-osascript"
        write_executable(
            fake_osascript,
            f"""\
            #!/bin/bash
            printf '%s\\n' "$@" > {str(osascript_log)!r}
            """,
        )

        result = self.run_script(
            "launcher.sh",
            VIDEOMASA_OSASCRIPT_BIN=str(fake_osascript),
            VIDEOMASA_OPEN_BIN="/usr/bin/true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        args = osascript_log.read_text().splitlines()
        self.assertEqual(args[0], "-")
        self.assertEqual(args[1], str(self.resources / "setup.sh"))
        self.assertTrue(broken_python.is_symlink(), "launcher must not delete the old environment")

    def test_launcher_uses_exact_venv_interpreter_and_cleans_pid_after_exit(self) -> None:
        port = free_port()
        invocation_log = self.root / "python-invocation.txt"
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
                            self.wfile.write(b'{"all_ok":true,"app_version":"3.1.1"}')
                        else:
                            self.send_response(404)
                            self.end_headers()

                    def log_message(self, *_args):
                        pass

                http.server.HTTPServer(
                    ("127.0.0.1", int(os.environ["VIDEOMASA_PORT"])), Handler
                ).serve_forever()
                """
            )
        )
        self.make_healthy_venv(
            f"""\
            #!/bin/bash
            if [ "${{1:-}}" = "-c" ]; then
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

    def make_fake_base_python(self, fail_pip: bool) -> Path:
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
        self.assertEqual((self.state / "version").read_text(), "3.1.1\n")
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
        self.assertEqual((self.state / "version").read_text(), "3.1.1\n")


if __name__ == "__main__":
    unittest.main()
