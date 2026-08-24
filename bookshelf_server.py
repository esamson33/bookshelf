#!/usr/bin/env python3
"""
bookshelf_server.py — Local web UI for the Bookshelf.

Serves a two-pane reader backed by the same SQLite DB that rename_books.py
maintains (.rename_books.db). Standard library only — no pip installs.

    python bookshelf_server.py            # http://127.0.0.1:8765
    python bookshelf_server.py --port 9000
    python bookshelf_server.py --db /path/to/other.db --root /path/to/shelf

Features:
  • Browse books grouped by category (the DB `directory` column).
  • Search across filename + summary.
  • "Recent" section: the 10 most recently opened books.
  • Read a book's PDF in the right-hand pane.

Opening a book stamps `accessed_at`, which drives the Recent list.
"""
import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import sqlite3

# Explicit content types for assets Python's mimetypes may not know (ES modules,
# PDF.js cmaps/fonts/locale). Serving .mjs with a JS type is required or the
# browser refuses to execute the module.
MIME_OVERRIDES = {
    ".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css",
    ".html": "text/html; charset=utf-8", ".json": "application/json",
    ".map": "application/json", ".wasm": "application/wasm",
    ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
    ".ftl": "text/plain; charset=utf-8", ".properties": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".gif": "image/gif",
    ".ico": "image/x-icon", ".cur": "image/x-icon",
    ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
}

SCRIPT_DIR = Path(__file__).parent.resolve()
UI_FILE    = SCRIPT_DIR / "bookshelf_ui.html"

# Populated in main(); shared across handler threads (guarded by DB_LOCK).
CONN: sqlite3.Connection = None  # type: ignore
DB_LOCK = threading.Lock()
# Shelf root: where the book categories and __landing/ live. Defaults to data/
# (the app files — script, DB, UI, vendor — stay at SCRIPT_DIR).
SHELF_ROOT: Path = SCRIPT_DIR / "data"


def ensure_schema(conn: sqlite3.Connection):
    """Add the accessed_at column if an older DB predates it."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(renames)")]
    if "accessed_at" not in cols:
        conn.execute("ALTER TABLE renames ADD COLUMN accessed_at TEXT")
        conn.commit()


# ── data access ──────────────────────────────────────────────────────────────

def _is_helper_dir(directory: str) -> bool:
    """Helper locations (e.g. __landing) aren't real categories."""
    top = (directory or "").split("/", 1)[0]
    return top.startswith(("_", "."))


# _is_helper_dir() as a SQL predicate, so the filter runs before ORDER BY/LIMIT
# instead of after the fetch. Equivalent because the first character of the
# leading path component is just the first character of the column.
NOT_HELPER_SQL = "substr(directory, 1, 1) NOT IN ('_', '.')"


def all_books() -> list[dict]:
    with DB_LOCK:
        cur = CONN.execute(
            f"""SELECT id, directory, proposed_name, summary, accessed_at
                FROM renames WHERE status = 'applied' AND {NOT_HELPER_SQL}
                ORDER BY directory, proposed_name COLLATE NOCASE"""
        )
        return [
            {
                "id":       r["id"],
                "category": r["directory"],
                "filename": r["proposed_name"],
                "summary":  r["summary"] or "",
                "accessed_at": r["accessed_at"],
            }
            for r in cur
        ]


def recent_books(limit: int = 10) -> list[dict]:
    with DB_LOCK:
        cur = CONN.execute(
            f"""SELECT id, directory, proposed_name, accessed_at
                FROM renames
                WHERE status = 'applied' AND accessed_at IS NOT NULL
                  AND {NOT_HELPER_SQL}
                ORDER BY accessed_at DESC
                LIMIT ?""",
            (limit,),
        )
        return [
            {
                "id":       r["id"],
                "category": r["directory"],
                "filename": r["proposed_name"],
                "accessed_at": r["accessed_at"],
            }
            for r in cur
        ]


def unclassified_count() -> int:
    """Number of PDFs physically waiting in the __landing/ staging folder."""
    landing = SHELF_ROOT / "__landing"
    if not landing.is_dir():
        return 0
    return sum(1 for f in landing.iterdir()
               if f.is_file() and f.suffix.lower() == ".pdf")


def mark_opened(book_id: int) -> bool:
    with DB_LOCK:
        cur = CONN.execute(
            "UPDATE renames SET accessed_at = datetime('now', 'localtime') WHERE id = ?",
            (book_id,),
        )
        CONN.commit()
        return cur.rowcount > 0


def book_info(book_id: int) -> dict | None:
    """Everything the info modal shows, minus the cover (rendered client-side)."""
    with DB_LOCK:
        row = CONN.execute(
            """SELECT id, directory, original_name, proposed_name, summary,
                      applied_at, accessed_at
               FROM renames WHERE id = ? AND status = 'applied'""",
            (book_id,),
        ).fetchone()
    if not row or _is_helper_dir(row["directory"]):
        return None
    path = book_path(book_id)
    return {
        "id":            row["id"],
        "category":      row["directory"],
        "filename":      row["proposed_name"],
        "original_name": row["original_name"],
        "summary":       row["summary"] or "",
        "applied_at":    row["applied_at"],
        "accessed_at":   row["accessed_at"],
        "size":          path.stat().st_size if path else None,
        "missing":       path is None,
    }


def book_path(book_id: int) -> Path | None:
    with DB_LOCK:
        row = CONN.execute(
            "SELECT directory, proposed_name FROM renames WHERE id = ?", (book_id,)
        ).fetchone()
    if not row:
        return None
    candidate = (SHELF_ROOT / row["directory"] / row["proposed_name"]).resolve()
    # Guard against path traversal — must stay inside the shelf.
    if SHELF_ROOT not in candidate.parents and candidate != SHELF_ROOT:
        return None
    return candidate if candidate.is_file() else None


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
                "books": all_books(),
                "recent": recent_books(),
                "unclassified": unclassified_count(),
            })
        if path == "/api/recent":
            return self._send_json({"recent": recent_books()})
        if path.startswith("/api/book/"):
            raw = path[len("/api/book/"):]
            info = book_info(int(raw)) if raw.isdigit() else None
            if info is None:
                return self._send_json({"error": "unknown book"}, status=404)
            return self._send_json(info)
        if path.startswith("/pdf/"):
            return self._serve_pdf(path[len("/pdf/"):])
        if path.startswith("/vendor/"):
            return self._serve_vendor(path[len("/vendor/"):])
        return self._not_found()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.startswith("/api/open/"):
            raw = path[len("/api/open/"):]
            if raw.isdigit() and mark_opened(int(raw)):
                return self._send_json({"recent": recent_books()})
            return self._send_json({"error": "unknown book"}, status=404)
        return self._not_found()

    # -- endpoints --
    def _serve_ui(self):
        try:
            data = UI_FILE.read_bytes()
        except OSError:
            return self._send_bytes(
                b"bookshelf_ui.html not found next to the server.",
                "text/plain; charset=utf-8", status=500,
            )
        self._send_bytes(data, "text/html; charset=utf-8")

    def _serve_vendor(self, rel_url: str):
        """Serve static files from the vendor/ folder (the PDF.js viewer)."""
        base   = (SCRIPT_DIR / "vendor").resolve()
        target = (base / unquote(rel_url)).resolve()
        # Path-traversal guard: must stay inside vendor/.
        if base not in target.parents or not target.is_file():
            return self._not_found()
        ext   = target.suffix.lower()
        ctype = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(str(target))[0] \
                or "application/octet-stream"
        self._send_bytes(target.read_bytes(), ctype)

    def _serve_pdf(self, raw_id: str):
        if not raw_id.isdigit():
            return self._not_found()
        path = book_path(int(raw_id))
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


def main():
    global CONN, SHELF_ROOT

    ap = argparse.ArgumentParser(description="Local web UI for the Bookshelf.")
    ap.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    ap.add_argument("--db", default=str(SCRIPT_DIR / ".rename_books.db"),
                    help="Path to the SQLite DB (default: .rename_books.db beside this script)")
    ap.add_argument("--root", default=str(SCRIPT_DIR / "data"),
                    help="Shelf root used to resolve PDF files (default: the data/ folder)")
    args = ap.parse_args()

    SHELF_ROOT = Path(args.root).resolve()
    CONN = sqlite3.connect(args.db, check_same_thread=False)
    CONN.row_factory = sqlite3.Row
    ensure_schema(CONN)

    n = CONN.execute("SELECT COUNT(*) FROM renames WHERE status='applied'").fetchone()[0]
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Bookshelf UI serving {n} book(s) from {SHELF_ROOT}")
    print(f"  DB : {args.db}")
    print(f"  →  {url}   (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
