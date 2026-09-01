#!/usr/bin/env python3
"""
bookshelf.server — Local web UI for the Bookshelf.

Serves a two-pane reader backed by the same catalog DB the CLI maintains.
Standard library only — no pip installs.

    bookshelf serve                       # http://127.0.0.1:8765
    bookshelf serve --port 9000
    bookshelf serve --db /path/to/other.db --root /path/to/shelf

Features:
  • Browse books grouped by category (the catalog `directory` column).
  • Search across filename + summary.
  • "Recent" section: the 10 most recently opened books, each removable.
  • Read a book's PDF in the right-hand pane.
  • Serve the favicon set and web manifest from static/.

Opening a book stamps `accessed_at`, which drives the Recent list; removing a
book from Recent clears that stamp again.

All SQL and schema handling lives in bookshelf.catalog.Catalog, which this
module shares with the CLI.
"""
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from bookshelf.catalog import Catalog, is_helper_dir
from bookshelf.config import Config

# Explicit content types for assets Python's mimetypes may not know (ES modules,
# PDF.js cmaps/fonts/locale). Serving .mjs with a JS type is required or the
# browser refuses to execute the module.
MIME_OVERRIDES = {
    ".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css",
    ".html": "text/html; charset=utf-8", ".json": "application/json",
    ".map": "application/json", ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
    ".ftl": "text/plain; charset=utf-8", ".properties": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".gif": "image/gif",
    ".ico": "image/x-icon", ".cur": "image/x-icon",
    ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
}

# Populated by serve(); shared across handler threads (the Catalog locks
# internally, so handlers need no lock of their own).
CATALOG: Catalog = None  # type: ignore


# The UI file, vendor/ and static/ live in the app directory, not beside this
# module, so they are resolved from the config rather than from __file__.
def ui_file() -> Path:
    return CATALOG.config.app_dir / "bookshelf_ui.html"


def vendor_dir() -> Path:
    return (CATALOG.config.app_dir / "vendor").resolve()


def static_dir() -> Path:
    """The favicon set and web manifest — same app-dir rule as vendor/."""
    return (CATALOG.config.app_dir / "static").resolve()


# ── data access ──────────────────────────────────────────────────────────────

def unclassified_count() -> int:
    """Number of PDFs physically waiting in the __landing/ staging folder."""
    landing = CATALOG.config.landing_dir
    if not landing.is_dir():
        return 0
    return sum(1 for f in landing.iterdir()
               if f.is_file() and f.suffix.lower() == ".pdf")


def book_info(book_id: int) -> dict | None:
    """Everything the info modal shows, minus the cover (rendered client-side)."""
    row = CATALOG.book_row(book_id)
    if not row or row["status"] != "applied" or is_helper_dir(row["directory"]):
        return None
    path = CATALOG.book_path(book_id)
    return {
        "id":            row["id"],
        "category":      row["directory"],
        "filename":      row["filename"],
        "original_name": row["original_name"],
        "summary":       row["summary"] or "",
        "applied_at":    row["applied_at"],
        "accessed_at":   row["accessed_at"],
        "size":          path.stat().st_size if path else None,
        "missing":       path is None,
    }


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "BookshelfUI/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet-ish
        pass

    # -- helpers --
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _not_found(self):
        self._send_bytes(b"Not found", "text/plain; charset=utf-8", status=404)

    # -- routing --
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/":
            return self._serve_ui()
        if path == "/api/books":
            return self._send_json({
                "books": CATALOG.all_books(),
                "recent": CATALOG.recent_books(),
                "unclassified": unclassified_count(),
            })
        if path == "/api/recent":
            return self._send_json({"recent": CATALOG.recent_books()})
        if path.startswith("/api/book/"):
            raw = path[len("/api/book/"):]
            info = book_info(int(raw)) if raw.isdigit() else None
            if info is None:
                return self._send_json({"error": "unknown book"}, status=404)
            return self._send_json(info)
        if path.startswith("/pdf/"):
            return self._serve_pdf(path[len("/pdf/"):])
        if path.startswith("/vendor/"):
            return self._serve_static(vendor_dir(), path[len("/vendor/"):])
        if path.startswith("/static/"):
            return self._serve_static(static_dir(), path[len("/static/"):])
        # Browsers ask for /favicon.ico on their own, ignoring the <link> tags.
        if path == "/favicon.ico":
            return self._serve_static(static_dir(), "favicon.ico")
        return self._not_found()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        # Both endpoints edit `accessed_at` and answer with the fresh Recent list.
        for prefix, action in (("/api/open/", CATALOG.mark_opened),
                               ("/api/forget/", CATALOG.clear_accessed)):
            if path.startswith(prefix):
                raw = path[len(prefix):]
                if raw.isdigit() and action(int(raw)):
                    return self._send_json({"recent": CATALOG.recent_books()})
                return self._send_json({"error": "unknown book"}, status=404)
        return self._not_found()

    # -- endpoints --
    def _serve_ui(self):
        try:
            data = ui_file().read_bytes()
        except OSError:
            return self._send_bytes(
                b"bookshelf_ui.html not found next to the server.",
                "text/plain; charset=utf-8", status=500,
            )
        self._send_bytes(data, "text/html; charset=utf-8")

    def _serve_static(self, base: Path, rel_url: str):
        """Serve a file from a static root (vendor/ or static/)."""
        target = (base / unquote(rel_url)).resolve()
        # Path-traversal guard: must stay inside the given root.
        if base not in target.parents or not target.is_file():
            return self._not_found()
        ext   = target.suffix.lower()
        ctype = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(str(target))[0] \
                or "application/octet-stream"
        self._send_bytes(target.read_bytes(), ctype)

    def _serve_pdf(self, raw_id: str):
        if not raw_id.isdigit():
            return self._not_found()
        path = CATALOG.book_path(int(raw_id))
        if path is None:
            return self._not_found()

        size = path.stat().st_size
        rng = self.headers.get("Range")
        common = {
            "Accept-Ranges":       "bytes",
            "Content-Disposition": f'inline; filename="{path.name}"',
            "Cache-Control":       "no-cache",
        }

        # Single-range support so large PDFs stream in the browser viewer.
        if rng and rng.startswith("bytes="):
            try:
                start_s, end_s = rng[len("bytes="):].split("-", 1)
                start = int(start_s) if start_s else 0
                end   = int(end_s) if end_s else size - 1
                end   = min(end, size - 1)
                if start > end or start >= size:
                    raise ValueError
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
            headers = {**common, "Content-Range": f"bytes {start}-{end}/{size}"}
            self._send_bytes(chunk, "application/pdf", status=206, extra=headers)
            return

        self._send_bytes(path.read_bytes(), "application/pdf", extra=common)


def serve(config: Config, port: int = 8765) -> int:
    """Run the reader until interrupted. Returns a process exit code."""
    global CATALOG

    CATALOG = Catalog(config, threaded=True)
    CATALOG.migrate()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Bookshelf UI serving {CATALOG.applied_count()} book(s) from {config.data_dir}")
    print(f"  DB : {config.db_path}")
    print(f"  \u2192  {url}   (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        CATALOG.close()
    return 0
