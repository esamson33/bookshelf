#!/usr/bin/env python3
"""
bookshelf_server.py — launch the Bookshelf web UI.

    ./bookshelf_server.py [--port 9000] [--db PATH] [--root PATH]

Equivalent to `python3 -m bookshelf`.
"""

import sys

from bookshelf.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
