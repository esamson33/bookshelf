"""The web reader, driven over a real socket on an ephemeral port.

The move into the package broke the old SCRIPT_DIR assumption — the UI file,
vendor/ and static/ live in the app directory, not beside the module — so those
paths are what these tests pin down.
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from bookshelf import server
from bookshelf.catalog import Catalog
from bookshelf.config import Config
from bookshelf.jobs import JOBS
from bookshelf.models import Proposal
from bookshelf.scan import pdf_names


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

    def post(self, path, payload=None, raw=None, headers=None):
        """POST JSON (or raw bytes) and return (status, decoded body)."""
        body = raw if raw is not None else (
            json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST",
            headers=headers or {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def get_json(self, path):
        _, body, _ = self.get(path)
        return json.loads(body)


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

    def test_forget_clears_accessed_at_and_returns_recent(self):
        book_id = self.book_id()
        self.catalog.mark_opened(book_id)
        _, body, _ = self.get("/api/recent")
        self.assertEqual(len(json.loads(body)["recent"]), 1)

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/forget/{book_id}", method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["recent"], [])
        self.assertIsNone(self.catalog.book_row(book_id)["accessed_at"])

    def test_forget_on_an_unknown_id_is_404(self):
        self.assertEqual(self.status_of("/api/forget/999999", method="POST"), 404)
        self.assertEqual(self.status_of("/api/forget/abc", method="POST"), 404)


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




class EventStreamTest(ServerTestCase):
    """The stream stays open for the life of the page, so every assertion here
    has to read a frame and then hang up rather than wait for a body."""

    def setUp(self):
        super().setUp()
        # 20s of silence would stall the suite; a short keepalive lets the
        # idle path be exercised in a fraction of a second.
        self._keepalive, server.SSE_KEEPALIVE = server.SSE_KEEPALIVE, 0.05
        self.addCleanup(setattr, server, "SSE_KEEPALIVE", self._keepalive)

    def stream(self):
        """Open /api/events and return the raw socket file to read frames from."""
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/events", timeout=5)
        self.addCleanup(response.close)
        return response

    def read_frame(self, response):
        """Read up to the blank line that terminates one SSE frame."""
        lines = []
        while True:
            line = response.fp.readline().decode("utf-8")
            if line in ("\r\n", "\n", ""):
                return "".join(lines)
            lines.append(line)

    def test_stream_announces_its_content_type(self):
        response = self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"],
                         "text/event-stream; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_stream_opens_with_a_comment_then_keeps_alive(self):
        response = self.stream()
        self.assertEqual(self.read_frame(response).strip(), ": connected")
        self.assertEqual(self.read_frame(response).strip(), ": keepalive")

    def test_a_published_event_is_framed_as_json(self):
        response = self.stream()
        self.read_frame(response)                      # ": connected"
        for _ in range(200):                           # wait to be subscribed
            if JOBS._subscribers:
                break
            time.sleep(0.01)
        JOBS.publish({"type": "books"})
        frame = ""
        while not frame.startswith("data:"):           # skip any keepalives
            frame = self.read_frame(response).strip()
        self.assertEqual(json.loads(frame[len("data:"):]), {"type": "books"})

    def test_ai_events_are_published_and_remembered(self):
        self.addCleanup(server.AI_STATE.update, dict(server.AI_STATE))
        subscription = JOBS.subscribe()
        self.addCleanup(JOBS.unsubscribe, subscription)
        server._ai_event("error", "RuntimeError: boom")
        self.assertEqual(subscription.get(timeout=1),
                         {"type": "ai", "state": "error", "error": "RuntimeError: boom"})
        self.assertEqual(server.AI_STATE, {"state": "error", "error": "RuntimeError: boom"})

    def test_lookup_client_reports_calls_to_the_page(self):
        self._saved_lookup, server.LOOKUP = server.LOOKUP, None
        self.addCleanup(setattr, server, "LOOKUP", self._saved_lookup)
        self.assertIs(server.lookup_client().on_call, server._ai_event)

    def test_a_call_in_flight_is_replayed_to_a_new_stream(self):
        self.addCleanup(server.AI_STATE.update, dict(server.AI_STATE))
        server.AI_STATE.update(state="busy", error=None)
        response = self.stream()
        self.read_frame(response)                      # ": connected"
        frame = self.read_frame(response).strip()
        self.assertEqual(json.loads(frame[len("data:"):]),
                         {"type": "ai", "state": "busy", "error": None})

    def test_a_finished_call_is_not_replayed(self):
        self.addCleanup(server.AI_STATE.update, dict(server.AI_STATE))
        server.AI_STATE.update(state="ok", error=None)
        response = self.stream()
        self.read_frame(response)                      # ": connected"
        self.assertEqual(self.read_frame(response).strip(), ": keepalive")

    def test_closing_the_stream_drops_the_subscription(self):
        """Otherwise every page reload would leak a queue and a thread."""
        def ids():
            return {id(q) for q in JOBS._subscribers}

        before = ids()
        response = self.stream()
        self.read_frame(response)
        mine = ids() - before
        self.assertEqual(len(mine), 1)

        response.close()
        for _ in range(500):        # the write fails once the peer's RST lands
            JOBS.publish({"type": "books"})
            if not mine & ids():
                break
            time.sleep(0.01)
        self.assertFalse(mine & ids(), "subscription outlived its connection")


class StubLookup:
    """Stands in for LookupClient so no test can reach the network."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def titles(self, stems, publisher_hint=""):
        self.calls.append(("titles", list(stems), publisher_hint))
        return self.rows

    def categorized_titles(self, stems, cats, samples=None):
        self.calls.append(("categorized", list(stems), list(cats)))
        return self.rows

    def summaries(self, titles, batch_size=20, progress=None):
        self.calls.append(("summaries", list(titles)))
        return {t: f"about {t}" for t in titles}


class ManageTestCase(ServerTestCase):
    """Shared setup for the endpoints that change the shelf."""

    def setUp(self):
        super().setUp()
        self.landing = self.cfg.landing_dir
        self.landing.mkdir(parents=True, exist_ok=True)
        self.lookup = StubLookup()
        self._saved_lookup, server.LOOKUP = server.LOOKUP, self.lookup
        self.addCleanup(setattr, server, "LOOKUP", self._saved_lookup)
        # JOBS is a process-wide singleton; a job left over from another test
        # would satisfy await_job() instantly.
        JOBS.reset()
        self.addCleanup(JOBS.reset)

    def drop_in(self, name, body=b"%PDF-1.4"):
        (self.landing / name).write_bytes(body)

    def await_job(self, timeout=5):
        """Block until the running job settles, then return its snapshot."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = JOBS.snapshot()
            if job and job["state"] != "running":
                return job
            time.sleep(0.01)
        self.fail("job did not finish")

    def landing_key(self):
        return self.catalog.key(self.landing)


class StatusTest(ManageTestCase):
    def test_status_counts_what_is_waiting(self):
        self.drop_in("waiting.pdf")
        self.catalog.stage(self.landing_key(),
                           [Proposal("waiting.pdf", "Real.pdf", "", "Networking")])
        status = self.get_json("/api/status")
        self.assertEqual(status["unclassified"], 1)
        self.assertEqual(status["pending_shelve"], 1)
        self.assertIsNone(status["job"])

    def test_status_reports_why_a_lookup_cannot_run(self):
        status = self.get_json("/api/status")
        self.assertFalse(status["lookup"]["available"])
        self.assertIn("No API key found", status["lookup"]["reason"])

    def test_status_never_returns_the_api_key(self):
        self.cfg.config_file.write_text('[openai]\napi_key = "sk-live-SECRET"\n')
        body = json.dumps(self.get_json("/api/status"))
        self.assertNotIn("SECRET", body)
        self.assertNotIn("sk-live", body)

    def test_categories_lists_the_shelf_directories(self):
        (self.data / "Systems").mkdir()
        (self.data / "Systems" / "b.pdf").write_bytes(b"%PDF")
        self.assertEqual(self.get_json("/api/categories")["categories"],
                         ["Networking", "Systems"])

    def test_an_emptied_category_is_hidden_but_not_deleted(self):
        (self.data / "Empty").mkdir()
        self.assertNotIn("Empty", self.get_json("/api/categories")["categories"])
        self.assertTrue((self.data / "Empty").is_dir())

    def test_landing_names_the_waiting_files(self):
        self.drop_in("b.pdf")
        self.drop_in("a.pdf")
        self.assertEqual(self.get_json("/api/landing")["files"], ["a.pdf", "b.pdf"])


class UploadTest(ManageTestCase):
    def upload(self, name, body=b"%PDF-1.4 data"):
        return self.post(f"/api/upload?name={urllib.parse.quote(name)}", raw=body)

    def test_a_pdf_lands_in_the_staging_folder(self):
        status, payload = self.upload("New Book.pdf")
        self.assertEqual(status, 200)
        self.assertEqual(payload["filename"], "New Book.pdf")
        self.assertEqual((self.landing / "New Book.pdf").read_bytes(), b"%PDF-1.4 data")
        self.assertEqual(payload["unclassified"], 1)

    def test_a_name_collision_is_suffixed_not_overwritten(self):
        self.drop_in("Book.pdf", b"original")
        _, payload = self.upload("Book.pdf", b"second")
        self.assertEqual(payload["filename"], "Book (2).pdf")
        self.assertEqual((self.landing / "Book.pdf").read_bytes(), b"original")

    def test_traversal_in_the_name_is_refused(self):
        for name in ("../escape.pdf", "sub/dir.pdf", "..\\escape.pdf", ".hidden.pdf"):
            with self.subTest(name=name):
                status, _ = self.upload(name)
                self.assertEqual(status, 400)
        self.assertEqual(pdf_names(self.landing), [])
        self.assertFalse((self.app / "escape.pdf").exists())

    def test_non_pdf_is_refused(self):
        status, payload = self.upload("notes.txt")
        self.assertEqual(status, 400)
        self.assertIn("PDF", payload["error"])

    def test_an_empty_upload_is_refused(self):
        self.assertEqual(self.upload("Book.pdf", b"")[0], 400)

    def test_an_oversize_upload_is_refused_without_being_read(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/upload?name=Big.pdf",
            data=b"%PDF", method="POST")
        request.add_header("Content-Length", str(server.MAX_UPLOAD + 1))
        try:
            urllib.request.urlopen(request)
            self.fail("expected 413")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)

    def test_an_upload_announces_itself_on_the_stream(self):
        subscription = JOBS.subscribe()
        self.addCleanup(JOBS.unsubscribe, subscription)
        self.upload("Book.pdf")
        self.assertEqual(subscription.get(timeout=2), {"type": "landing"})


class ShelveScanTest(ManageTestCase):
    def test_a_scan_looks_up_and_stages_proposals(self):
        self.drop_in("packt_raw_book.pdf")
        self.lookup.rows = [{"original": "packt_raw_book", "title": "Real Book",
                             "category": "Systems", "summary": "about it"}]

        status, payload = self.post("/api/shelve/scan", {})
        self.assertEqual(status, 202)
        self.assertEqual(payload["job"]["kind"], "shelve")

        job = self.await_job()
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["rows"][0]["proposed"], "Real Book.pdf")
        self.assertEqual(job["result"]["rows"][0]["category"], "Systems")
        # Staged, so the review survives the request that produced it.
        self.assertEqual(len(self.catalog.pending_rows(self.landing_key())), 1)

    def test_progress_lines_reach_the_job_log(self):
        self.drop_in("raw.pdf")
        self.lookup.rows = [{"original": "raw", "title": "R", "category": "C"}]
        self.post("/api/shelve/scan", {})
        job = self.await_job()
        self.assertIn("Looking up titles and categories for 1 new file(s)…", job["lines"])

    def test_a_forced_category_overrides_the_model(self):
        self.drop_in("raw.pdf")
        self.lookup.rows = [{"original": "raw", "title": "R", "category": "Ignored"}]
        self.post("/api/shelve/scan", {"category": "Chosen", "publisher": "Manning"})
        job = self.await_job()
        self.assertEqual(job["result"]["rows"][0]["category"], "Chosen")
        self.assertEqual(self.lookup.calls[0][0], "titles")
        self.assertEqual(self.lookup.calls[0][2], "Manning")

    def test_an_empty_landing_pad_finishes_without_rows(self):
        self.post("/api/shelve/scan", {})
        job = self.await_job()
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"], {"rows": []})

    def test_a_failed_lookup_surfaces_as_a_job_error(self):
        self.drop_in("raw.pdf")
        self.lookup.rows = []                       # what an unparseable reply gives
        self.post("/api/shelve/scan", {})
        job = self.await_job()
        self.assertEqual(job["state"], "error")
        self.assertIn("no usable titles", job["error"])

    def test_a_second_job_is_refused_while_one_runs(self):
        release = threading.Event()
        self.addCleanup(release.set)
        JOBS.start("summaries", lambda report: release.wait(5))
        status, payload = self.post("/api/shelve/scan", {})
        self.assertEqual(status, 409)
        self.assertIn("already running", payload["error"])


class ProposalEditTest(ManageTestCase):
    def setUp(self):
        super().setUp()
        self.drop_in("raw.pdf")
        self.catalog.stage(self.landing_key(),
                           [Proposal("raw.pdf", "R.pdf", "why", "Old")])
        self.row_id = self.catalog.pending_rows(self.landing_key())[0]["id"]

    def stored(self):
        return self.catalog.book_row(self.row_id)["filename"]

    def test_pending_lists_the_staged_batch(self):
        rows = self.get_json("/api/shelve/pending")["rows"]
        self.assertEqual(rows[0]["title"], "R")
        self.assertEqual(rows[0]["category"], "Old")
        self.assertEqual(rows[0]["id"], self.row_id)

    def test_the_title_can_be_retargeted(self):
        status, payload = self.post(f"/api/proposal/{self.row_id}", {"title": "Better"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["row"]["proposed"], "Better.pdf")
        self.assertEqual(self.stored(), "Old/Better.pdf")

    def test_the_category_can_be_retargeted(self):
        self.post(f"/api/proposal/{self.row_id}", {"category": "New"})
        self.assertEqual(self.stored(), "New/R.pdf")

    def test_both_can_change_at_once(self):
        self.post(f"/api/proposal/{self.row_id}",
                  {"title": "Better", "category": "New"})
        self.assertEqual(self.stored(), "New/Better.pdf")

    def test_the_summary_survives_an_edit(self):
        self.post(f"/api/proposal/{self.row_id}", {"title": "Better"})
        self.assertEqual(self.catalog.book_row(self.row_id)["summary"], "why")

    def test_a_slash_is_refused_because_it_would_corrupt_the_stored_form(self):
        for field in ("title", "category"):
            with self.subTest(field=field):
                status, _ = self.post(f"/api/proposal/{self.row_id}", {field: "a/b"})
                self.assertEqual(status, 400)
        self.assertEqual(self.stored(), "Old/R.pdf")

    def test_an_empty_value_is_refused(self):
        self.assertEqual(self.post(f"/api/proposal/{self.row_id}", {"title": "  "})[0], 400)
        self.assertEqual(self.post(f"/api/proposal/{self.row_id}", {"category": ""})[0], 400)

    def test_an_applied_row_cannot_be_edited_as_a_proposal(self):
        applied = self.book_id()
        self.assertEqual(self.post(f"/api/proposal/{applied}", {"title": "X"})[0], 404)

    def test_dropping_forgets_the_proposal_but_keeps_the_file(self):
        status, payload = self.post(f"/api/proposal/{self.row_id}/drop")
        self.assertEqual(status, 200)
        self.assertEqual(payload["dropped"], self.row_id)
        self.assertIsNone(self.catalog.book_row(self.row_id))
        self.assertTrue((self.landing / "raw.pdf").is_file())

    def test_dropping_an_unknown_proposal_is_404(self):
        self.assertEqual(self.post("/api/proposal/999999/drop")[0], 404)


class ShelveApplyTest(ManageTestCase):
    def test_apply_moves_the_file_and_rekeys_the_row(self):
        self.drop_in("raw.pdf")
        self.catalog.stage(self.landing_key(),
                           [Proposal("raw.pdf", "Filed.pdf", "keep me", "Systems")])

        status, payload = self.post("/api/shelve/apply")

        self.assertEqual(status, 200)
        self.assertEqual((payload["ok"], payload["errors"]), (1, 0))
        self.assertTrue((self.data / "Systems" / "Filed.pdf").is_file())
        self.assertFalse((self.landing / "raw.pdf").exists())
        shelved = [b for b in self.catalog.all_books() if b["filename"] == "Filed.pdf"]
        self.assertEqual(shelved[0]["category"], "Systems")
        self.assertEqual(shelved[0]["summary"], "keep me")

    def test_apply_picks_up_an_edit_made_after_the_scan(self):
        self.drop_in("raw.pdf")
        self.catalog.stage(self.landing_key(),
                           [Proposal("raw.pdf", "R.pdf", "", "Old")])
        row_id = self.catalog.pending_rows(self.landing_key())[0]["id"]
        self.post(f"/api/proposal/{row_id}", {"title": "Better", "category": "New"})

        self.post("/api/shelve/apply")

        self.assertTrue((self.data / "New" / "Better.pdf").is_file())

    def test_a_blocked_destination_is_reported_not_swallowed(self):
        self.drop_in("raw.pdf")
        (self.data / "Systems").mkdir()
        (self.data / "Systems" / "Real.pdf").write_bytes(b"%PDF")
        self.catalog.stage(self.landing_key(),
                           [Proposal("raw.pdf", "Real.pdf", "", "Systems")])

        _, payload = self.post("/api/shelve/apply")

        self.assertEqual((payload["ok"], payload["errors"]), (0, 1))
        self.assertIn("destination already exists", payload["lines"][0])

    def test_apply_with_nothing_staged_is_a_bad_request(self):
        self.assertEqual(self.post("/api/shelve/apply")[0], 400)

    def test_apply_announces_the_library_changed(self):
        self.drop_in("raw.pdf")
        self.catalog.stage(self.landing_key(),
                           [Proposal("raw.pdf", "Real.pdf", "", "Systems")])
        subscription = JOBS.subscribe()
        self.addCleanup(JOBS.unsubscribe, subscription)

        self.post("/api/shelve/apply")

        seen = {subscription.get(timeout=2)["type"] for _ in range(2)}
        self.assertEqual(seen, {"books", "landing"})


class DedupeTest(ManageTestCase):
    def setUp(self):
        super().setUp()
        (self.data / "Systems").mkdir(exist_ok=True)
        for category in ("Networking", "Systems"):
            (self.data / category / "dup.pdf").write_bytes(b"%PDF")

    def test_preview_lists_what_would_go(self):
        payload = self.get_json("/api/dedupe")
        group = payload["duplicates"][0]
        self.assertEqual(group["filename"], "dup.pdf")
        self.assertEqual(len(group["remove"]), 1)
        self.assertEqual(payload["file_count"], 1)

    def test_stale_rows_are_reported(self):
        self.catalog.stage("Networking", [Proposal("gone.pdf", "gone.pdf")],
                           status="applied")
        self.assertEqual([e["path"] for e in self.get_json("/api/dedupe")["stale"]],
                         ["Networking/gone.pdf"])

    def test_apply_removes_the_duplicate(self):
        status, payload = self.post("/api/dedupe/apply")
        self.assertEqual(status, 200)
        self.assertEqual(payload["ok"], 1)
        remaining = [p for p in (self.data / "Networking" / "dup.pdf",
                                 self.data / "Systems" / "dup.pdf") if p.exists()]
        self.assertEqual(len(remaining), 1)

    def test_only_the_selected_groups_are_removed(self):
        (self.data / "Networking" / "other.pdf").write_bytes(b"%PDF")
        (self.data / "Systems" / "other.pdf").write_bytes(b"%PDF")

        self.post("/api/dedupe/apply", {"filenames": ["other.pdf"]})

        self.assertTrue((self.data / "Networking" / "dup.pdf").is_file())
        self.assertTrue((self.data / "Systems" / "dup.pdf").is_file())

    def test_selecting_nothing_deletes_nothing(self):
        status, _ = self.post("/api/dedupe/apply", {"filenames": [], "stale": False})
        self.assertEqual(status, 400)
        self.assertTrue((self.data / "Systems" / "dup.pdf").is_file())

    def test_the_chosen_copy_is_the_one_kept(self):
        """Either copy can be kept — not only the earliest filed."""
        group = self.get_json("/api/dedupe")["duplicates"][0]
        other = next(c["path"] for c in group["copies"] if c["path"] != group["keep"])

        status, _ = self.post("/api/dedupe/apply",
                              {"filenames": ["dup.pdf"], "keep": {"dup.pdf": other}})

        self.assertEqual(status, 200)
        self.assertTrue((self.data / other).is_file())
        self.assertFalse((self.data / group["keep"]).exists())

    def test_keeping_a_path_outside_the_group_is_refused(self):
        status, _ = self.post("/api/dedupe/apply",
                              {"filenames": ["dup.pdf"], "keep": {"dup.pdf": "Real.pdf"}})
        self.assertEqual(status, 400)
        self.assertTrue((self.data / "Networking" / "dup.pdf").is_file())
        self.assertTrue((self.data / "Systems" / "dup.pdf").is_file())

    def test_a_category_emptied_by_dedupe_stays_on_disk(self):
        self.post("/api/dedupe/apply",
                  {"filenames": ["dup.pdf"], "keep": {"dup.pdf": "Networking/dup.pdf"}})
        self.assertTrue((self.data / "Systems").is_dir())
        self.assertNotIn("Systems", self.get_json("/api/categories")["categories"])

    def test_stale_rows_can_be_left_alone(self):
        self.catalog.stage("Networking", [Proposal("gone.pdf", "gone.pdf")],
                           status="applied")
        self.post("/api/dedupe/apply", {"filenames": ["dup.pdf"], "stale": False})
        self.assertEqual([e["path"] for e in self.get_json("/api/dedupe")["stale"]],
                         ["Networking/gone.pdf"])


class SummariesTest(ManageTestCase):
    def test_a_run_fills_in_what_the_model_returns(self):
        self.catalog._conn.execute(
            "INSERT INTO books (directory, original_name, filename, status, summary)"
            " VALUES ('Networking','Blank.pdf','Blank.pdf','applied',NULL)")
        self.catalog.commit()
        self.assertEqual(self.get_json("/api/status")["missing_summaries"], 1)

        self.post("/api/summaries/run")
        job = self.await_job()

        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["ok"], 1)
        self.assertEqual(self.get_json("/api/status")["missing_summaries"], 0)

    def test_a_run_with_nothing_missing_finishes_quietly(self):
        self.post("/api/summaries/run")
        job = self.await_job()
        self.assertEqual(job["state"], "done")
        self.assertIn("All summaries already filled.", job["lines"])


class BrowseTest(ManageTestCase):
    def test_browsing_lists_subdirectories_and_counts_pdfs(self):
        payload = self.get_json(f"/api/browse?path={urllib.parse.quote(str(self.data))}")
        self.assertEqual(payload["path"], str(self.data))
        self.assertIn("Networking", payload["dirs"])
        self.assertEqual(payload["pdf_count"], 0)

    def test_hidden_directories_are_not_offered(self):
        (self.data / ".git").mkdir()
        payload = self.get_json(f"/api/browse?path={urllib.parse.quote(str(self.data))}")
        self.assertNotIn(".git", payload["dirs"])

    def test_a_path_outside_home_is_refused(self):
        status, payload = self.get_status_json("/api/browse?path=/etc")
        self.assertEqual(status, 400)
        self.assertIn("under", payload["error"])

    def test_a_missing_directory_is_refused(self):
        status, _ = self.get_status_json(
            f"/api/browse?path={urllib.parse.quote(str(self.data / 'nope'))}")
        self.assertEqual(status, 400)

    def get_status_json(self, path):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class RenameTest(ManageTestCase):
    def setUp(self):
        super().setUp()
        self.folder = self.data / "Networking"
        (self.folder / "packt_raw.pdf").write_bytes(b"%PDF")
        self.lookup.rows = [{"original": "packt_raw", "title": "Proper Title",
                             "summary": "about it"}]

    def scan(self, **body):
        return self.post("/api/rename/scan", {"directory": str(self.folder), **body})

    def test_a_scan_stages_renames_for_review(self):
        self.assertEqual(self.scan()[0], 202)
        job = self.await_job()
        self.assertEqual(job["state"], "done")
        row = job["result"]["groups"][0]["rows"][0]
        self.assertEqual((row["original"], row["proposed"]),
                         ("packt_raw.pdf", "Proper Title.pdf"))
        self.assertIsNone(row["category"])       # renames stay where they are

    def test_pending_survives_the_scan_request(self):
        self.scan()
        self.await_job()
        payload = self.get_json(
            f"/api/rename/pending?directory={urllib.parse.quote(str(self.folder))}")
        self.assertEqual(payload["groups"][0]["rows"][0]["proposed"], "Proper Title.pdf")

    def test_a_staged_rename_can_be_retitled_then_applied(self):
        self.scan()
        self.await_job()
        row_id = self.catalog.pending_rows(self.catalog.key(self.folder))[0]["id"]
        self.post(f"/api/proposal/{row_id}", {"title": "Even Better"})

        status, payload = self.post("/api/rename/apply", {"directory": str(self.folder)})

        self.assertEqual(status, 200)
        self.assertEqual((payload["ok"], payload["errors"]), (1, 0))
        self.assertTrue((self.folder / "Even Better.pdf").is_file())
        self.assertFalse((self.folder / "packt_raw.pdf").exists())

    def test_the_landing_pad_is_refused(self):
        status, payload = self.post("/api/rename/scan",
                                    {"directory": str(self.landing)})
        self.assertEqual(status, 400)
        self.assertIn("New books", payload["error"])

    def test_a_directory_outside_home_is_refused(self):
        self.assertEqual(self.post("/api/rename/scan", {"directory": "/etc"})[0], 400)
        self.assertEqual(self.post("/api/rename/apply", {"directory": "/etc"})[0], 400)

    def test_applying_with_nothing_staged_is_refused(self):
        status, _ = self.post("/api/rename/apply", {"directory": str(self.folder)})
        self.assertEqual(status, 400)

class ArchiveTest(ManageTestCase):
    """Archiving is a move into __archive/<Category>/; delete is a second step."""

    def setUp(self):
        super().setUp()
        self.archived = self.data / "__archive" / "Networking" / "Real.pdf"

    def archive(self):
        return self.post(f"/api/archive/{self.book_id()}")

    def test_archiving_moves_the_file_and_hides_the_book(self):
        book_id = self.book_id()
        status, payload = self.archive()
        self.assertEqual(status, 200)
        self.assertTrue(self.archived.is_file())
        self.assertFalse(self.pdf.exists())
        self.assertEqual(self.get_json("/api/books")["books"], [])
        self.assertEqual(payload["archive"], [{
            "id": book_id, "category": "Networking", "filename": "Real.pdf",
            "summary": "a summary", "missing": False,
        }])
        self.assertEqual(self.get_json("/api/archive")["archive"], payload["archive"])

    def test_an_archived_book_is_no_longer_a_library_book(self):
        book_id = self.book_id()
        self.archive()
        self.assertEqual(self.status_of(f"/api/book/{book_id}"), 404)
        self.assertEqual(self.status_of(f"/api/archive/{book_id}", method="POST"), 404)
        # its PDF is still readable, e.g. from the Manage view
        self.assertEqual(self.status_of(f"/pdf/{book_id}"), 200)

    def test_archiving_drops_the_book_from_recent(self):
        self.catalog.mark_opened(self.book_id())
        self.archive()
        self.assertEqual(self.get_json("/api/recent")["recent"], [])

    def test_archiving_announces_the_library_changed(self):
        subscription = JOBS.subscribe()
        self.addCleanup(JOBS.unsubscribe, subscription)
        self.archive()
        self.assertEqual(subscription.get(timeout=2)["type"], "books")

    def test_a_name_clash_in_the_archive_is_suffixed(self):
        self.archived.parent.mkdir(parents=True)
        self.archived.write_bytes(b"%PDF older")
        _, payload = self.archive()
        self.assertEqual(payload["archive"][0]["filename"], "Real (2).pdf")
        self.assertTrue((self.archived.parent / "Real (2).pdf").is_file())
        self.assertEqual(self.archived.read_bytes(), b"%PDF older")

    def test_a_missing_file_cannot_be_archived(self):
        self.pdf.unlink()
        status, payload = self.archive()
        self.assertEqual(status, 409)
        self.assertIn("missing", payload["error"])
        self.assertEqual(len(self.get_json("/api/books")["books"]), 1)

    def test_a_catalog_clash_puts_the_file_back(self):
        self.catalog.stage("__archive/Networking", [Proposal("Real.pdf", "Other.pdf")],
                           status="applied")
        status, _ = self.archive()
        self.assertEqual(status, 409)
        self.assertTrue(self.pdf.is_file())
        self.assertFalse(self.archived.exists())

    def test_unknown_pending_and_non_numeric_ids_are_404(self):
        self.drop_in("raw.pdf")
        self.catalog.stage(self.landing_key(), [Proposal("raw.pdf", "Networking/R.pdf")])
        pending_id = self.catalog.pending_rows(self.landing_key())[0]["id"]
        for path in (f"/api/archive/{pending_id}", "/api/archive/999999",
                     "/api/archive/abc", "/api/archive/999999/restore",
                     "/api/archive/999999/delete"):
            self.assertEqual(self.status_of(path, method="POST"), 404, path)

    def test_restoring_puts_the_book_back_on_its_shelf(self):
        book_id = self.book_id()
        self.archive()
        status, payload = self.post(f"/api/archive/{book_id}/restore")
        self.assertEqual(status, 200)
        self.assertEqual(payload["archive"], [])
        self.assertTrue(self.pdf.is_file())
        self.assertFalse(self.archived.exists())
        books = self.get_json("/api/books")["books"]
        self.assertEqual([(b["id"], b["category"]) for b in books],
                         [(book_id, "Networking")])

    def test_restoring_recreates_a_category_that_was_removed(self):
        book_id = self.book_id()
        self.archive()
        (self.data / "Networking").rmdir()
        status, _ = self.post(f"/api/archive/{book_id}/restore")
        self.assertEqual(status, 200)
        self.assertTrue(self.pdf.is_file())

    def test_a_shelved_book_cannot_be_restored_or_deleted(self):
        book_id = self.book_id()
        self.assertEqual(self.status_of(f"/api/archive/{book_id}/restore", method="POST"), 404)
        self.assertEqual(self.status_of(f"/api/archive/{book_id}/delete", method="POST"), 404)
        self.assertTrue(self.pdf.is_file())

    def test_deleting_removes_the_file_and_the_row(self):
        book_id = self.book_id()
        self.archive()
        status, payload = self.post(f"/api/archive/{book_id}/delete")
        self.assertEqual(status, 200)
        self.assertEqual(payload["archive"], [])
        self.assertFalse(self.archived.exists())
        self.assertIsNone(self.catalog.book_row(book_id))

    def test_deleting_an_archived_row_whose_file_is_gone_still_clears_it(self):
        book_id = self.book_id()
        self.archive()
        self.archived.unlink()
        self.assertTrue(self.get_json("/api/archive")["archive"][0]["missing"])
        status, _ = self.post(f"/api/archive/{book_id}/delete")
        self.assertEqual(status, 200)
        self.assertIsNone(self.catalog.book_row(book_id))

    def test_archived_books_are_not_duplicates_or_stale_rows(self):
        self.archive()
        # A fresh copy of the same title on the shelf is not a duplicate of
        # the archived one, and the archived row is not "stale".
        (self.data / "Networking" / "Real.pdf").write_bytes(b"%PDF new")
        payload = self.get_json("/api/dedupe")
        self.assertEqual(payload["duplicates"], [])
        self.assertEqual(payload["stale"], [])


class GuardTest(ServerTestCase):
    """Loopback binding stopped being protection enough once the UI could
    delete and rename files: DNS rebinding defeats the bind, and any page the
    user visits can POST cross-site."""

    def request(self, path, method="GET", headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            headers=headers or {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_a_rebound_host_header_is_refused(self):
        self.assertEqual(self.request("/api/books", headers={"Host": "evil.example"}), 403)

    def test_loopback_host_names_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(self.request("/api/books", headers={"Host": host}), 200)

    def test_cross_origin_post_is_refused(self):
        self.assertEqual(
            self.request(f"/api/open/{self.book_id()}", method="POST",
                         headers={"Origin": "http://evil.example"}), 403)

    def test_cross_site_referer_post_is_refused(self):
        self.assertEqual(
            self.request(f"/api/open/{self.book_id()}", method="POST",
                         headers={"Referer": "http://evil.example/page"}), 403)

    def test_same_origin_post_is_allowed(self):
        self.assertEqual(
            self.request(f"/api/open/{self.book_id()}", method="POST",
                         headers={"Origin": f"http://127.0.0.1:{self.port}"}), 200)

    def test_post_without_an_origin_is_allowed(self):
        """curl sends neither header; the API stays scriptable from a shell."""
        self.assertEqual(self.request(f"/api/open/{self.book_id()}", method="POST"), 200)

    def test_cross_origin_get_is_allowed(self):
        """Reads are safe: without CORS headers the browser cannot read the body."""
        self.assertEqual(
            self.request("/api/books", headers={"Origin": "http://evil.example"}), 200)


class IsLoopbackTest(unittest.TestCase):
    def test_accepts_loopback_authorities(self):
        for value in ("127.0.0.1", "127.0.0.1:8765", "localhost:8765",
                      "LOCALHOST", "[::1]:8765", "::1"):
            with self.subTest(value=value):
                self.assertTrue(server.is_loopback(value))

    def test_rejects_everything_else(self):
        for value in ("", "evil.example", "evil.example:8765",
                      "127.0.0.1.evil.example", "169.254.1.1"):
            with self.subTest(value=value):
                self.assertFalse(server.is_loopback(value))


if __name__ == "__main__":
    unittest.main()
