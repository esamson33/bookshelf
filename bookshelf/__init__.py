"""Bookshelf — catalog, shelve, and read a personal library of PDF books.

Everything is driven from the web UI: browsing and reading, filing new books
out of __landing/, renaming a folder's PDFs to their published titles,
de-duplicating the shelf, and backfilling summaries.

    bookshelf.cli     the launcher (`python3 -m bookshelf`)
    bookshelf.server  the HTTP layer
    bookshelf.plans   what each operation would change, and how to apply it
    bookshelf.jobs    background work and the event stream reporting it

`bookshelf_server.py` at the top level is a thin shim onto bookshelf.cli.
"""

__version__ = "0.3.0"
