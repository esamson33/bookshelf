#!/usr/bin/env python3
"""
bookshelf_server.py — compatibility shim for the local web reader.

The reader now lives in the package and is reachable as a subcommand:

    bookshelf serve [--port 9000] [--db PATH] [--root PATH]

This script forwards to it so existing invocations keep working.
"""

import sys

from bookshelf.cli import main

if __name__ == "__main__":
    sys.exit(main(["serve", *sys.argv[1:]]))
