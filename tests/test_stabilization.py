import io
import os
import stat
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = tempfile.TemporaryDirectory()
STATE_ROOT = Path(TEST_ROOT.name)
os.environ["VIDEOMASA_API_TOKEN"] = "test-launch-token"
os.environ["VIDEOMASA_PORT"] = "18765"
os.environ["VIDEOMASA_WORK_DIR"] = str(STATE_ROOT / "downloads")
os.environ["VIDEOMASA_COOKIES_DIR"] = str(STATE_ROOT / "cookies")
os.environ["VIDEOMASA_SKIP_HEALTH_CHECKS"] = "1"

import app as videomasa


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
            }
        response = self.client.get("/status/abc", base_url=BASE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_file_path", response.get_json())

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

    def test_job_renderer_uses_dom_properties_without_inline_handlers(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text()
        self.assertNotIn("cardBody.innerHTML", template)
        self.assertNotIn("onclick=", template)
        self.assertIn("label.title = job.url || job.label", template)
        self.assertIn("copyButton.addEventListener", template)


if __name__ == "__main__":
    unittest.main()
