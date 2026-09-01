"""The web reader, driven over a real socket on an ephemeral port.

The move into the package broke the old SCRIPT_DIR assumption — the UI file,
vendor/ and static/ live in the app directory, not beside the module — so those
paths are what these tests pin down.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from bookshelf import server
from bookshelf.catalog import Catalog
from bookshelf.config import Config
from bookshelf.models import Proposal


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name).resolve()
        self.data = self.app / "data"
        (self.data / "Networking").mkdir(parents=True)
        (self.app / "vendor" / "pdfjs").mkdir(parents=True)
        (self.app / "static").mkdir()

        (self.app / "bookshelf_ui.html").write_text("<h1>UI</h1>")
        (self.app / "vendor" / "pdfjs" / "pdf.mjs").write_text("export const x = 1;")
        (self.app / "static" / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
        (self.app / "static" / "favicon-32.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.app / "static" / "site.webmanifest").write_text('{"name": "Bookshelf"}')
        (self.app / "secret.txt").write_text("do not serve me")
        self.pdf = self.data / "Networking" / "Real.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 " + b"x" * 500)

        self.cfg = Config(app_dir=self.app, data_dir=self.data,
                          db_path=self.app / "cat.db", config_file=self.app / "c.toml")
        self.catalog = Catalog(self.cfg, threaded=True)
        self.catalog.migrate()
        self.catalog.stage("Networking", [Proposal("Real.pdf", "Real.pdf", "a summary")],
                           status="applied")

        self._saved, server.CATALOG = server.CATALOG, self.catalog
        self.addCleanup(self._restore)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        # serve_forever polls every 0.5s by default, which shutdown() waits
        # out — that alone would cost half a second per test.
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _restore(self):
        server.CATALOG = self._saved
        self.catalog.close()

    def get(self, path, headers=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         headers=headers or {})
        with urllib.request.urlopen(request) as response:
            return response.status, response.read(), dict(response.headers)

    def status_of(self, path, method="GET"):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code

    def book_id(self):
        return self.catalog.all_books()[0]["id"]


class PathResolutionTest(ServerTestCase):
    """These are what the move into the package could have silently broken."""

    def test_ui_file_comes_from_the_app_dir_not_the_package(self):
        self.assertEqual(server.ui_file(), self.app / "bookshelf_ui.html")

    def test_vendor_dir_comes_from_the_app_dir(self):
        self.assertEqual(server.vendor_dir(), self.app / "vendor")

    def test_root_serves_the_ui_file(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<h1>UI</h1>")
        self.assertIn("text/html", headers["Content-Type"])

    def test_vendor_module_is_served_as_javascript(self):
        """Serving .mjs as anything else makes the browser refuse the module."""
        status, _, headers = self.get("/vendor/pdfjs/pdf.mjs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript")

    def test_vendor_traversal_is_blocked(self):
        self.assertEqual(self.status_of("/vendor/../secret.txt"), 404)

    def test_static_dir_comes_from_the_app_dir(self):
        self.assertEqual(server.static_dir(), self.app / "static")

    def test_static_traversal_is_blocked(self):
        self.assertEqual(self.status_of("/static/../secret.txt"), 404)


class FaviconTest(ServerTestCase):
    def test_bare_favicon_ico_is_served(self):
        """Browsers request /favicon.ico themselves, ignoring the <link> tags."""
        status, body, headers = self.get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(body, (self.app / "static" / "favicon.ico").read_bytes())
        self.assertEqual(headers["Content-Type"], "image/x-icon")

    def test_png_icon_is_served_with_the_png_type(self):
        status, _, headers = self.get("/static/favicon-32.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")

    def test_manifest_is_served_with_the_manifest_type(self):
        _, _, headers = self.get("/static/site.webmanifest")
        self.assertEqual(headers["Content-Type"], "application/manifest+json")

    def test_missing_static_file_is_404(self):
        self.assertEqual(self.status_of("/static/nope.png"), 404)


class ApiTest(ServerTestCase):
    def test_books_lists_shelved_books(self):
        _, body, _ = self.get("/api/books")
        payload = json.loads(body)
        self.assertEqual(len(payload["books"]), 1)
        self.assertEqual(payload["books"][0]["category"], "Networking")
        self.assertEqual(payload["books"][0]["summary"], "a summary")

    def test_unclassified_counts_pdfs_waiting_in_landing(self):
        _, body, _ = self.get("/api/books")
        self.assertEqual(json.loads(body)["unclassified"], 0)

        self.cfg.landing_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.landing_dir / "waiting.pdf").write_bytes(b"%PDF")
        _, body, _ = self.get("/api/books")
        self.assertEqual(json.loads(body)["unclassified"], 1)

    def test_book_detail_reports_size_and_presence(self):
        _, body, _ = self.get(f"/api/book/{self.book_id()}")
        payload = json.loads(body)
        self.assertEqual(payload["filename"], "Real.pdf")
        self.assertEqual(payload["size"], self.pdf.stat().st_size)
        self.assertFalse(payload["missing"])

    def test_book_detail_flags_a_missing_file(self):
        self.pdf.unlink()
        _, body, _ = self.get(f"/api/book/{self.book_id()}")
        payload = json.loads(body)
        self.assertTrue(payload["missing"])
        self.assertIsNone(payload["size"])

    def test_unknown_and_non_numeric_ids_are_404(self):
        self.assertEqual(self.status_of("/api/book/999999"), 404)
        self.assertEqual(self.status_of("/api/book/abc"), 404)

    def test_unknown_route_is_404(self):
        self.assertEqual(self.status_of("/nope"), 404)

    def test_open_stamps_accessed_at_and_returns_recent(self):
        _, body, _ = self.get("/api/recent")
        self.assertEqual(json.loads(body)["recent"], [])

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/open/{self.book_id()}", method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(len(payload["recent"]), 1)

    def test_open_on_an_unknown_id_is_404(self):
        self.assertEqual(self.status_of("/api/open/999999", method="POST"), 404)


class PdfTest(ServerTestCase):
    def test_full_pdf_is_served(self):
        status, body, headers = self.get(f"/pdf/{self.book_id()}")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.pdf.read_bytes())
        self.assertEqual(headers["Accept-Ranges"], "bytes")

    def test_range_request_returns_partial_content(self):
        status, body, headers = self.get(f"/pdf/{self.book_id()}",
                                         headers={"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 100)
        size = self.pdf.stat().st_size
        self.assertEqual(headers["Content-Range"], f"bytes 0-99/{size}")

    def test_open_ended_range_runs_to_the_end(self):
        size = self.pdf.stat().st_size
        status, body, _ = self.get(f"/pdf/{self.book_id()}",
                                   headers={"Range": "bytes=10-"})
        self.assertEqual(status, 206)
        self.assertEqual(len(body), size - 10)

    def test_unsatisfiable_range_is_416(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/pdf/{self.book_id()}",
            headers={"Range": "bytes=99999999-"})
        try:
            urllib.request.urlopen(request)
            self.fail("expected 416")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 416)

    def test_missing_file_is_404(self):
        self.pdf.unlink()
        self.assertEqual(self.status_of(f"/pdf/{self.book_id()}"), 404)


if __name__ == "__main__":
    unittest.main()
