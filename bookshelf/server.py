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
  • Archive a book out of the library into __archive/; the Manage view lists
    what is archived and can put a book back or delete it for good.
  • Serve the favicon set and web manifest from static/.

Opening a book stamps `accessed_at`, which drives the Recent list; removing a
book from Recent clears that stamp again.

All SQL and schema handling lives in bookshelf.catalog.Catalog, which this
module shares with the CLI.
"""
import json
import mimetypes
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from bookshelf.catalog import ARCHIVE_PREFIX, Catalog, is_helper_dir
from bookshelf.config import Config, ConfigError
from bookshelf.jobs import JOBS, Busy
from bookshelf.lookup import LookupClient
from bookshelf.models import Proposal
from bookshelf.plans import Context
from bookshelf.plans import dedupe as dedupe_plan
from bookshelf.plans import rename as rename_plan
from bookshelf.plans import shelve as shelve_plan
from bookshelf.plans import summaries as summaries_plan
from bookshelf.scan import categories, pdf_names

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

#: Seconds an idle event stream waits before writing a comment line. Proxies
#: and browsers drop a silent connection; a comment costs nothing and is
#: ignored by EventSource. Module-level so tests can shorten it.
SSE_KEEPALIVE = 20.0

#: Body-size ceilings. A book is a book, not a disk image, and a JSON body
#: here never holds more than a title and a category.
MAX_UPLOAD = 512 * 1024 * 1024
MAX_JSON = 1 << 20

# Populated by serve(); shared across handler threads (the Catalog locks
# internally, so handlers need no lock of their own).
CATALOG: Catalog = None  # type: ignore

#: Built on first use so the OpenAI client — and the API key it needs — is
#: only ever touched by a request that actually looks something up. Tests
#: replace it with a stub.
LOOKUP: LookupClient = None  # type: ignore


# The UI file, vendor/ and static/ live in the app directory, not beside this
# module, so they are resolved from the config rather than from __file__.
def ui_file() -> Path:
    return CATALOG.config.app_dir / "bookshelf_ui.html"


def vendor_dir() -> Path:
    return (CATALOG.config.app_dir / "vendor").resolve()


def static_dir() -> Path:
    """The favicon set and web manifest — same app-dir rule as vendor/."""
    return (CATALOG.config.app_dir / "static").resolve()


# ── request guards ───────────────────────────────────────────────────────────
#
# The server binds to loopback, which used to be protection enough: the only
# write it accepted was a timestamp. Now that the UI deletes and renames files,
# loopback alone is not enough — any page the user visits can POST to
# http://127.0.0.1:8765/, and DNS rebinding defeats the bind by pointing an
# attacker-controlled name at 127.0.0.1. Checking Host closes the rebinding
# path (the browser sends the attacker's name, not ours); checking Origin
# closes plain cross-site POSTs.

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(authority: str) -> bool:
    """True when a Host/Origin authority names this machine."""
    host = (authority or "").strip().lower()
    if host.startswith("["):                 # [::1]:8765
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:               # 127.0.0.1:8765
        host = host.split(":", 1)[0]
    return host in LOOPBACK_HOSTS


# ── data access ──────────────────────────────────────────────────────────────

#: The latest OpenAI call state, so a page opened mid-call can show it busy.
AI_STATE: dict = {"state": "idle", "error": None}


def _ai_event(state: str, error: str | None) -> None:
    """LookupClient's on_call hook: push each call's state to the page."""
    AI_STATE.update(state=state, error=error)
    JOBS.publish({"type": "ai", "state": state, "error": error})


def lookup_client() -> LookupClient:
    global LOOKUP
    if LOOKUP is None:
        LOOKUP = LookupClient(CATALOG.config, on_call=_ai_event)
    return LOOKUP


def context(out=print) -> Context:
    """A plan Context bound to the running server's catalog and config."""
    return Context(config=CATALOG.config, catalog=CATALOG,
                   lookup=lookup_client(), out=out)


def lookup_status() -> dict:
    """Whether a lookup could run — reported so the UI can say why not.

    Never returns the key itself, only whether one resolved.
    """
    try:
        CATALOG.config.api_key()
    except ConfigError as e:
        return {"available": False, "reason": str(e)}
    try:
        import openai  # noqa: F401
    except ImportError:
        return {"available": False,
                "reason": "The 'openai' package is not installed. Run: pip install openai"}
    return {"available": True, "reason": ""}


def unique_name(directory: Path, name: str) -> Path:
    """A path in `directory` that does not exist yet, suffixed if need be."""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 1000):
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"Too many files named like {name}.")


class BadDirectory(Exception):
    """A directory the browser named that we will not act on."""


def browse_roots() -> list[Path]:
    """The directories a caller may reach into.

    The user's home covers the ordinary case (a Downloads folder full of
    unnamed PDFs). The shelf root is included because it can be pointed
    somewhere else entirely with --root, and locking the shelf itself out of
    the one feature meant for arbitrary folders would be perverse.
    """
    roots = [CATALOG.config.data_dir]
    try:
        roots.append(Path.home().resolve())
    except (OSError, RuntimeError):
        pass                                # no home on this account; shelf only
    return roots


def contained_by(path: Path) -> Path | None:
    """The allowed root holding `path`, or None if none does."""
    for root in browse_roots():
        if path == root or root in path.parents:
            return root
    return None


def safe_directory(raw: str) -> Path:
    """Resolve a caller-supplied directory, or refuse it.

    The page can name any path, so this is the one place that decides which
    ones are reachable: inside an allowed root, an existing directory, and
    never the landing pad — filing books out of __landing/ is what New books
    does, and renaming them in place would strand them there.
    """
    if not (raw or "").strip():
        raise BadDirectory("No directory given.")
    try:
        path = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        raise BadDirectory("That path cannot be resolved.") from None

    if contained_by(path) is None:
        allowed = " or ".join(str(r) for r in browse_roots())
        raise BadDirectory(f"Only directories under {allowed} can be scanned.")
    if not path.is_dir():
        raise BadDirectory(f"'{path}' is not a directory.")
    if path == CATALOG.config.landing_dir:
        raise BadDirectory("Use New books to file the landing pad.")
    return path


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


# ── archive ──────────────────────────────────────────────────────────────────
#
# Archiving is a move, not a status: the file goes to __archive/<Category>/
# and the row's directory key follows it. That keeps every existing filter
# honest (the library, Recent, search and Duplicates all skip helper dirs),
# and putting a book back is the same move the other way.

class ArchiveError(Exception):
    """A move that could not be made; the message is for the page."""


def shelved_row(book_id: int):
    """The row for a book on the live shelf, or None."""
    row = CATALOG.book_row(book_id)
    if not row or row["status"] != "applied" or is_helper_dir(row["directory"]):
        return None
    return row


def archived_row(book_id: int):
    """The row for a book in the archive, or None."""
    row = CATALOG.book_row(book_id)
    if not row or row["status"] != "applied" \
            or not row["directory"].startswith(ARCHIVE_PREFIX):
        return None
    return row


def move_book(row, dst_dir: Path) -> None:
    """Move a book's file into `dst_dir` and re-point its catalog row.

    The file goes first, so a failed move leaves nothing to undo; if the
    catalog then refuses the new location the file comes straight back.
    """
    src = CATALOG.book_path(row["id"])
    if src is None:
        raise ArchiveError("That book's file is missing from the shelf.")
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        target = unique_name(dst_dir, row["filename"])
        src.rename(target)
    except OSError as e:
        raise ArchiveError(f"Could not move the file: {e}") from None
    try:
        CATALOG.move_book(row["id"], CATALOG.key(dst_dir), target.name)
    except ValueError as e:
        target.rename(src)
        raise ArchiveError(str(e)) from None


def archive_listing() -> list[dict]:
    """What the Manage view shows, flagging any file gone from disk."""
    return [{**book, "missing": CATALOG.book_path(book["id"]) is None}
            for book in CATALOG.archived_books()]


# ── routing table ────────────────────────────────────────────────────────────
#
# (method, pattern, handler name). Patterns are fullmatch'd against the path
# with the query string stripped, so a non-numeric id simply fails to match and
# falls through to 404 — the behaviour the hand-rolled prefix chain had.

ROUTES = [
    ("GET",  r"/",                 "_route_ui"),
    ("GET",  r"/api/books",        "_route_books"),
    ("GET",  r"/api/events",       "_route_events"),
    ("GET",  r"/api/status",       "_route_status"),
    ("GET",  r"/api/categories",   "_route_categories"),
    ("GET",  r"/api/landing",      "_route_landing"),
    ("GET",  r"/api/shelve/pending", "_route_shelve_pending"),
    ("GET",  r"/api/dedupe",       "_route_dedupe"),
    ("GET",  r"/api/browse",       "_route_browse"),
    ("GET",  r"/api/rename/pending", "_route_rename_pending"),
    ("GET",  r"/api/recent",       "_route_recent"),
    ("GET",  r"/api/archive",      "_route_archive"),
    ("GET",  r"/api/book/(\d+)",   "_route_book"),
    ("GET",  r"/pdf/(\d+)",        "_route_pdf"),
    ("GET",  r"/vendor/(.*)",      "_route_vendor"),
    ("GET",  r"/static/(.*)",      "_route_static"),
    ("GET",  r"/favicon\.ico",     "_route_favicon"),
    ("POST", r"/api/open/(\d+)",   "_route_open"),
    ("POST", r"/api/forget/(\d+)", "_route_forget"),
    ("POST", r"/api/archive/(\d+)",         "_route_archive_book"),
    ("POST", r"/api/archive/(\d+)/restore", "_route_archive_restore"),
    ("POST", r"/api/archive/(\d+)/delete",  "_route_archive_delete"),
    ("POST", r"/api/upload",       "_route_upload"),
    ("POST", r"/api/shelve/scan",  "_route_shelve_scan"),
    ("POST", r"/api/shelve/apply", "_route_shelve_apply"),
    ("POST", r"/api/proposal/(\d+)",      "_route_proposal"),
    ("POST", r"/api/proposal/(\d+)/drop", "_route_proposal_drop"),
    ("POST", r"/api/dedupe/apply", "_route_dedupe_apply"),
    ("POST", r"/api/summaries/run", "_route_summaries_run"),
    ("POST", r"/api/rename/scan",  "_route_rename_scan"),
    ("POST", r"/api/rename/apply", "_route_rename_apply"),
]

COMPILED = [(method, re.compile(pattern), name) for method, pattern, name in ROUTES]


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
        if self.command != "HEAD":
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

    def _json_body(self) -> dict:
        """The request's JSON object, or {} for anything unparseable."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_JSON:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _bad_request(self, why: str):
        self._send_json({"error": why}, status=400)

    def _not_found(self):
        self._send_bytes(b"Not found", "text/plain; charset=utf-8", status=404)

    def _forbidden(self, why: str):
        self._send_bytes(why.encode("utf-8"), "text/plain; charset=utf-8", status=403)

    # -- guards --
    def _allowed(self, method: str) -> bool:
        """Reject rebinding and cross-site requests before anything runs."""
        if not is_loopback(self.headers.get("Host", "")):
            self._forbidden("Bad Host header.")
            return False
        if method != "GET":
            # A same-origin fetch sends no Origin on GET but always does on
            # POST; curl and the like send neither, which stays allowed so the
            # API remains scriptable from the shell.
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            if origin and not is_loopback(urlsplit(origin).netloc):
                self._forbidden("Cross-origin request refused.")
                return False
        return True

    # -- dispatch --
    def _dispatch(self, method: str):
        if not self._allowed(method):
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        for route_method, pattern, name in COMPILED:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if match:
                return getattr(self, name)(*match.groups())
        return self._not_found()

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- routes --
    def _route_ui(self):
        try:
            data = ui_file().read_bytes()
        except OSError:
            return self._send_bytes(
                b"bookshelf_ui.html not found next to the server.",
                "text/plain; charset=utf-8", status=500,
            )
        self._send_bytes(data, "text/html; charset=utf-8")

    def _route_books(self):
        self._send_json({
            "books": CATALOG.all_books(),
            "recent": CATALOG.recent_books(),
            "unclassified": unclassified_count(),
        })

    def _route_recent(self):
        self._send_json({"recent": CATALOG.recent_books()})

    def _route_events(self):
        """Server-sent events: job progress, and signals that state changed.

        Held open for the life of the page. ThreadingHTTPServer gives one
        thread per connection, which is the right trade for a single-user
        localhost app. The response is deliberately left at HTTP/1.0 with no
        Content-Length — EventSource reads until close, and switching the
        handler to 1.1 would require a correct length on every other path,
        including _route_pdf's 416 branch.
        """
        if self.command == "HEAD":
            return self._send_bytes(b"", "text/event-stream; charset=utf-8")

        subscription = JOBS.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self._sse(": connected")
            # A reload mid-job should pick the job back up, not show nothing.
            running = JOBS.snapshot()
            if running:
                self._sse_event({"type": "job", **running})
            if AI_STATE["state"] == "busy":
                self._sse_event({"type": "ai", **AI_STATE})
            while True:
                try:
                    event = subscription.get(timeout=SSE_KEEPALIVE)
                except queue.Empty:
                    self._sse(": keepalive")
                    continue
                self._sse_event(event)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass                            # the page navigated away or closed
        finally:
            JOBS.unsubscribe(subscription)

    def _sse(self, raw: str) -> None:
        self.wfile.write(f"{raw}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _sse_event(self, event: dict) -> None:
        self._sse(f"data: {json.dumps(event)}")

    def _route_book(self, raw_id: str):
        info = book_info(int(raw_id))
        if info is None:
            return self._send_json({"error": "unknown book"}, status=404)
        self._send_json(info)

    def _route_vendor(self, rel_url: str):
        self._serve_static(vendor_dir(), rel_url)

    def _route_static(self, rel_url: str):
        self._serve_static(static_dir(), rel_url)

    def _route_favicon(self):
        # Browsers ask for /favicon.ico on their own, ignoring the <link> tags.
        self._serve_static(static_dir(), "favicon.ico")

    def _route_open(self, raw_id: str):
        self._touch(CATALOG.mark_opened, raw_id)

    def _route_forget(self, raw_id: str):
        self._touch(CATALOG.clear_accessed, raw_id)

    def _touch(self, action, raw_id: str):
        """Both accessed_at endpoints answer with the fresh Recent list."""
        if action(int(raw_id)):
            return self._send_json({"recent": CATALOG.recent_books()})
        self._send_json({"error": "unknown book"}, status=404)

    # -- archive --
    def _route_archive(self):
        self._send_json({"archive": archive_listing()})

    def _route_archive_book(self, raw_id: str):
        """Take a book off the shelf. Nothing is deleted."""
        row = shelved_row(int(raw_id))
        if row is None:
            return self._send_json({"error": "unknown book"}, status=404)
        # __archive/<Category>/ — the category rides along in the path so a
        # restore knows where to put the book back.
        self._archive_move(row, CATALOG.config.archive_dir / row["directory"])

    def _route_archive_restore(self, raw_id: str):
        row = archived_row(int(raw_id))
        if row is None:
            return self._send_json({"error": "unknown book"}, status=404)
        category = row["directory"][len(ARCHIVE_PREFIX):]
        self._archive_move(row, CATALOG.config.data_dir / category)

    def _archive_move(self, row, dst_dir: Path):
        """Both directions answer with the fresh archive list."""
        try:
            move_book(row, dst_dir)
        except ArchiveError as e:
            return self._send_json({"error": str(e)}, status=409)
        JOBS.publish({"type": "books"})
        self._send_json({"archive": archive_listing()})

    def _route_archive_delete(self, raw_id: str):
        """Delete an archived book for good: the file, then its row.

        Only reachable for a book already in the archive, so a delete is
        always a second, separate decision from taking it off the shelf.
        """
        row = archived_row(int(raw_id))
        if row is None:
            return self._send_json({"error": "unknown book"}, status=404)
        path = CATALOG.book_path(row["id"])
        try:
            if path is not None:
                path.unlink()
        except OSError as e:
            return self._send_json({"error": f"Could not delete the file: {e}"},
                                   status=500)
        CATALOG.delete_ids([row["id"]])
        JOBS.publish({"type": "books"})
        self._send_json({"archive": archive_listing()})

    # -- state --
    def _route_status(self):
        """Everything the Manage view needs to decide what it can offer."""
        landing_key = CATALOG.key(CATALOG.config.landing_dir)
        pending = CATALOG.pending_by_directory()
        self._send_json({
            "unclassified":      unclassified_count(),
            "pending_shelve":    sum(e["count"] for e in pending
                                     if e["directory"] == landing_key),
            "pending":           [e for e in pending if e["directory"] != landing_key],
            "missing_summaries": len(CATALOG.missing_summaries()),
            "job":               JOBS.snapshot(),
            "lookup":            lookup_status(),
            "landing_dir":       str(CATALOG.config.landing_dir),
            "shelf_dir":         str(CATALOG.config.data_dir),
        })

    def _route_categories(self):
        self._send_json({"categories": categories(CATALOG.config.data_dir)})

    def _route_landing(self):
        """The PDFs physically waiting to be filed, named rather than counted."""
        self._send_json({"files": pdf_names(CATALOG.config.landing_dir)})

    # -- upload --
    def _route_upload(self):
        """One PDF per request, body raw rather than multipart.

        A multipart parser would be a page of code in BaseHTTPRequestHandler
        and buys nothing here: the browser can POST a File object directly.
        """
        name = parse_qs(urlsplit(self.path).query).get("name", [""])[0].strip()
        if not name or Path(name).name != name or "\\" in name or name.startswith("."):
            return self._bad_request("Bad file name.")
        if not name.lower().endswith(".pdf"):
            return self._bad_request("Only PDF files can be added to the shelf.")

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._bad_request("Empty upload.")
        if length > MAX_UPLOAD:
            return self._send_json(
                {"error": f"That file is larger than the {MAX_UPLOAD >> 20} MB limit."},
                status=413)

        landing = CATALOG.config.landing_dir
        landing.mkdir(parents=True, exist_ok=True)
        target = unique_name(landing, name)
        try:
            # Streamed: a 500 MB book must not be held in memory to be saved.
            with open(target, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 16, remaining))
                    if not chunk:
                        raise OSError("Upload ended early.")
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            target.unlink(missing_ok=True)
            return self._send_json({"error": f"Could not save the file: {e}"}, status=500)

        JOBS.publish({"type": "landing"})
        self._send_json({"filename": target.name,
                         "unclassified": unclassified_count()})

    # -- shelve --
    def _route_shelve_scan(self):
        body = self._json_body()
        category  = str(body.get("category") or "").strip()
        publisher = str(body.get("publisher") or "").strip()
        fresh     = bool(body.get("fresh"))

        def work(report):
            plan = shelve_plan.build(context(report), category, fresh, publisher)
            plan.out = report
            if plan.is_empty():
                report(plan.empty_message)
                return {"rows": []}
            # Staging is what makes the review survive the request: from here
            # on the proposals live in the catalog, not in this object.
            plan.stage(CATALOG)
            report(f"Staged {len(plan.proposals)} proposal(s) for review.")
            return shelve_plan.from_pending(context()).payload()

        self._start_job("shelve", work)

    def _route_shelve_pending(self):
        self._send_json(shelve_plan.from_pending(context()).payload())

    def _route_shelve_apply(self):
        plan = shelve_plan.from_pending(context())
        if plan.is_empty():
            return self._bad_request(plan.empty_message)
        lines: list[str] = []
        plan.out = lines.append
        outcome = plan.apply(CATALOG)
        JOBS.publish({"type": "books"})
        JOBS.publish({"type": "landing"})
        self._send_json({**outcome.as_dict(), "lines": lines})

    # -- renaming a folder in place --
    def _route_browse(self):
        """List a directory so the page can offer a picker.

        Names only — no file contents leave the shelf through here.
        """
        raw = parse_qs(urlsplit(self.path).query).get("path", [""])[0]
        try:
            directory = safe_directory(raw or str(browse_roots()[-1]))
        except BadDirectory as e:
            return self._bad_request(str(e))
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            return self._bad_request(f"Could not read that directory: {e}")

        # Climbing stops at whichever root let us in, so ".." can't walk out.
        root = contained_by(directory)
        return self._send_json({
            "path": str(directory),
            "parent": None if directory == root else str(directory.parent),
            "roots": [str(r) for r in browse_roots()],
            "dirs": [e.name for e in entries
                     if e.is_dir() and not e.name.startswith(".")],
            "pdf_count": sum(1 for e in entries
                             if e.is_file() and e.suffix.lower() == ".pdf"),
        })

    def _route_rename_scan(self):
        body = self._json_body()
        try:
            root = safe_directory(str(body.get("directory") or ""))
        except BadDirectory as e:
            return self._bad_request(str(e))
        recursive = bool(body.get("recursive"))
        publisher = str(body.get("publisher") or "").strip()

        def work(report):
            plan = rename_plan.build(context(report), root, recursive, publisher)
            plan.out = report
            if plan.is_empty():
                report(plan.empty_message)
                return {"root": str(root), "groups": []}
            plan.stage(CATALOG)
            report(f"Staged {sum(len(p) for _, p in plan.groups)} rename(s) for review.")
            return rename_plan.from_pending(context(), root).payload()

        self._start_job("rename", work)

    def _route_rename_pending(self):
        raw = parse_qs(urlsplit(self.path).query).get("directory", [""])[0]
        try:
            root = safe_directory(raw)
        except BadDirectory as e:
            return self._bad_request(str(e))
        self._send_json(rename_plan.from_pending(context(), root).payload())

    def _route_rename_apply(self):
        body = self._json_body()
        try:
            root = safe_directory(str(body.get("directory") or ""))
        except BadDirectory as e:
            return self._bad_request(str(e))
        plan = rename_plan.from_pending(context(), root)
        if plan.is_empty():
            return self._bad_request("Nothing staged for that directory.")
        lines: list[str] = []
        plan.out = lines.append
        outcome = plan.apply(CATALOG)
        JOBS.publish({"type": "books"})
        self._send_json({**outcome.as_dict(), "lines": lines})

    # -- duplicates --
    def _route_dedupe(self):
        """Computed on the spot: it is a directory walk, not an API call."""
        self._send_json(dedupe_plan.build(context()).payload())

    def _route_dedupe_apply(self):
        body = self._json_body()
        # An absent "filenames" means every group; an empty list means none,
        # which is a no-op rather than a silent delete-everything.
        filenames = body.get("filenames")
        keep = body.get("keep") or {}
        if not isinstance(keep, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in keep.items()):
            return self._bad_request("'keep' must map filenames to paths.")
        try:
            plan = dedupe_plan.build(context()).select(
                filenames, include_stale=bool(body.get("stale", True)), keep=keep)
        except ValueError as e:
            return self._bad_request(str(e))
        if plan.is_empty():
            return self._bad_request("Nothing selected to remove.")
        lines: list[str] = []
        plan.out = lines.append
        outcome = plan.apply(CATALOG)
        JOBS.publish({"type": "books"})
        self._send_json({**outcome.as_dict(), "lines": lines})

    # -- summaries --
    def _route_summaries_run(self):
        def work(report):
            plan = summaries_plan.build(context(report))
            plan.out = report
            if plan.is_empty():
                report(plan.empty_message)
                return {"ok": 0, "errors": 0, "summary": plan.empty_message}
            outcome = plan.apply(CATALOG)
            JOBS.publish({"type": "books"})
            return outcome.as_dict()

        self._start_job("summaries", work)

    # -- editing a staged proposal --
    def _pending_row(self, raw_id: str):
        row = CATALOG.book_row(int(raw_id))
        return row if row and row["status"] == "pending" else None

    def _route_proposal(self, raw_id: str):
        """Retarget one staged proposal's title, category, or both.

        This is what replaced the CLI's `edit()`: the proposal is a row, so an
        edit is an UPDATE rather than a method on an object that would have to
        be kept alive between two requests.
        """
        row = self._pending_row(raw_id)
        if row is None:
            return self._send_json({"error": "unknown proposal"}, status=404)

        body = self._json_body()
        proposal = Proposal.from_db(row["original_name"], row["filename"],
                                    row["summary"] or "", row_id=row["id"],
                                    candidates=row["candidates"])

        if "title" in body:
            title = str(body["title"] or "").strip()
            if not title:
                return self._bad_request("A title cannot be empty.")
            if "/" in title:
                return self._bad_request("A title cannot contain '/'.")
            proposal = proposal.with_title(title)

        if "category" in body:
            category = str(body["category"] or "").strip()
            if not category:
                return self._bad_request("A category cannot be empty.")
            if "/" in category:
                return self._bad_request("A category cannot contain '/'.")
            proposal = proposal.with_category(category)

        CATALOG.set_pending_value(row["id"], proposal.db_value)
        self._send_json({"row": proposal.as_row()})

    def _route_proposal_drop(self, raw_id: str):
        """Forget a proposal. The file stays put; only the suggestion goes."""
        row = self._pending_row(raw_id)
        if row is None:
            return self._send_json({"error": "unknown proposal"}, status=404)
        CATALOG.delete_ids([row["id"]])
        self._send_json({"dropped": row["id"]})

    # -- jobs --
    def _start_job(self, kind: str, work):
        try:
            job = JOBS.start(kind, work)
        except Busy as e:
            return self._send_json({"error": str(e)}, status=409)
        self._send_json({"job": job.snapshot()}, status=202)

    # -- file serving --
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

    def _route_pdf(self, raw_id: str):
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
    print(f"  →  {url}   (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        CATALOG.close()
    return 0
