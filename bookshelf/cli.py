"""Command-line entry point.

There is one command now. Every mode the CLI used to carry — rename, shelve,
dedupe, summaries — lives in the web UI, which can show a proposal, let it be
edited, and stream a lookup's progress; a terminal could only print a table and
block on input(). What is left here is the launcher and the path overrides.
"""

from __future__ import annotations

import argparse
import sys

from .config import Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bookshelf",
        description="Serve the web UI for a library of PDF books.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    for flag, meta, help_text in (
        ("--db", "PATH", "Path to the catalog DB (default: .bookshelf.db beside the app)"),
        ("--root", "PATH", "Shelf root holding the category directories (default: data/)"),
        ("--config", "PATH", "TOML config holding the API key (default: .bookshelf.toml)"),
    ):
        parser.add_argument(flag, metavar=meta, default=None, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from .server import serve

    return serve(Config.from_args(args), args.port)


if __name__ == "__main__":
    sys.exit(main())
