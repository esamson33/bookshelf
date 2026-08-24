#!/usr/bin/env python3
"""
rename_books.py — compatibility shim for the old flag-based interface.

The tool is now `bookshelf`, with a subcommand per mode:

    bookshelf rename <directory> [--publisher "Manning"] [--recursive]
    bookshelf shelve [--category "Machine Learning and AI"] [--fresh]
    bookshelf dedupe
    bookshelf summaries

This script translates the historical flags onto those subcommands so old
invocations, aliases, and muscle memory keep working:

    rename_books.py <dir>              ->  bookshelf rename <dir>
    rename_books.py --new              ->  bookshelf shelve
    rename_books.py --dedupe           ->  bookshelf dedupe
    rename_books.py --backfill-summaries -> bookshelf summaries

Everything of substance lives in the bookshelf package.
"""

import sys

from bookshelf.cli import main

# Old flag -> new subcommand, in the order the original parser checked them.
MODE_FLAGS = [
    ("--backfill-summaries", "summaries"),
    ("--dedupe", "dedupe"),
    ("--new", "shelve"),
]

# Flags the old parser accepted that the new subcommands still take verbatim.
PASSTHROUGH_WITH_VALUE = {
    "--publisher", "-p", "--category", "-c", "--db", "--root", "--config",
}
PASSTHROUGH_FLAGS = {"--recursive", "-r", "--fresh", "--yes", "-y"}


def translate(argv: list[str]) -> list[str]:
    """Rewrite historical argv into the subcommand form."""
    if not argv or argv[0] in ("-h", "--help"):
        return ["--help"]

    command = None
    rest: list[str] = []
    positional: list[str] = []

    index = 0
    while index < len(argv):
        arg = argv[index]
        matched = next((sub for flag, sub in MODE_FLAGS if arg == flag), None)
        if matched:
            command = matched
        elif arg in PASSTHROUGH_FLAGS:
            rest.append(arg)
        elif arg in PASSTHROUGH_WITH_VALUE:
            rest.append(arg)
            if index + 1 < len(argv):
                index += 1
                rest.append(argv[index])
        elif arg.startswith("-"):
            rest.append(arg)          # unknown: let argparse report it
        else:
            positional.append(arg)
        index += 1

    if command is None:
        command = "rename"
        rest = positional + rest
    elif positional:
        # A directory alongside --new/--dedupe was ignored before; keep it so.
        pass

    # --category / --fresh only ever applied to --new; the rename subcommand
    # rejects them, which is the same outcome the old parser.error() gave.
    return [command, *rest]


if __name__ == "__main__":
    sys.exit(main(translate(sys.argv[1:])))
