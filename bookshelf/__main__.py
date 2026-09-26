"""`python3 -m bookshelf` — the same entry point as bookshelf_server.py."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
