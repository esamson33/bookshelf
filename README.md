# Bookshelf

A local library for a folder of PDF books: browse and read them, and let a
model work out what they are actually called.

Everything happens in the browser. Start the server and open the page:

```
python3 -m bookshelf          # http://127.0.0.1:8765
python3 -m bookshelf --port 9000
python3 -m bookshelf --db /path/to/other.db --root /path/to/shelf
```

`./bookshelf_server.py` is the same thing with a shebang.

## The two views

**Library** is the reader: books grouped by category, a search across titles and
summaries, the ten most recently opened, and a PDF pane. Clicking a book records
that you opened it, which is what drives the Recent list; the `×` on a Recent row
clears that again. A book's info card can **archive** it: the file moves to
`data/__archive/<Category>/` and the book leaves the library, with nothing deleted.

**Manage** is everything that changes the shelf.

| | |
|---|---|
| **New books** | Drop PDFs on the page — or copy them into `data/__landing/` yourself — then look up their proper titles and subject categories and file them onto the shelf. |
| **Rename a folder** | Point at any folder of badly-named PDFs and give them their published titles. The files stay where they are. |
| **Duplicates** | The same book filed under two categories, and catalog rows whose file has gone. The oldest copy of each is the one kept. |
| **Archive** | Books taken off the shelf. Put one back where it came from, or delete it for good — the only place anything is deleted one book at a time. |
| **Summaries** | Fill in summaries for shelved books that never had one looked up. |

A lookup takes tens of seconds — it is a model call that searches the web — so it
runs in the background and its progress is pushed to the page as it happens.

Nothing is renamed or moved until you say so. A lookup **stages** its proposals in
the catalog, so the review survives a reload: you can change a title or a category,
drop a proposal you don't want, close the tab, and come back to it. What you edited
is what gets applied.

## Setup

Python 3.11 or newer. The only third-party package is `openai`, and only the
lookups need it — reading, browsing and de-duplicating all work without it:

```
pip install openai
cp .bookshelf.toml.example .bookshelf.toml    # then paste your key in
```

`OPENAI_API_KEY` works as a fallback. The Manage view says so plainly when no key
resolves, and disables the buttons that would need one.

## Layout

```
bookshelf/           the package
  cli.py             the launcher
  server.py          HTTP: routes, uploads, the event stream
  jobs.py            background work and its progress events
  catalog.py         every SQL statement, and the schema
  plans/             what each operation would change, and how to apply it
  scan.py            walking the filesystem, reconciling it against the catalog
  lookup.py          the OpenAI calls and their prompts
bookshelf_ui.html    the page shell
static/              app.css, app.js, manage.js, bus.js, favicons
vendor/pdfjs/        the PDF viewer
data/                the shelf itself — category folders, __landing/ and __archive/
.bookshelf.db        the catalog
```

`bookshelf/catalog.py` is the sole owner of the schema, versioned with
`PRAGMA user_version` and migrated on startup. A book is one row; `status` is
`pending` while a proposal is awaiting review and `applied` once it is on the shelf.

## Security

The server binds to `127.0.0.1` and has no login, which was fine when the only
thing it could write was a timestamp. It now deletes and renames files, so it also:

- refuses any request whose `Host` is not loopback, which is what stops DNS
  rebinding from reaching it through a name you visit;
- refuses any `POST` carrying a cross-site `Origin` or `Referer`;
- only reaches into directories under your home folder or the shelf root.

It is still a single-user tool for your own machine. Don't expose it.

## Tests

```
python3 -m unittest discover -s tests -t .
```

Standard library `unittest`; no network is touched — the lookup client is stubbed.
