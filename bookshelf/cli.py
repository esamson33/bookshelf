"""Command-line entry point.

Subcommands replace the original flag soup, where --category and --fresh only
meant anything alongside --new and the positional directory was required
unless one of three flags was set — constraints argparse now enforces
structurally instead of via hand-written parser.error() calls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .catalog import Catalog
from .config import Config, ConfigError
from .driver import run_plan
from .lookup import LookupClient
from .plans import Context, PlanError
from .plans import dedupe as dedupe_plan
from .plans import rename as rename_plan
from .plans import shelve as shelve_plan
from .plans import summaries as summaries_plan


def build_parser() -> argparse.ArgumentParser:
    # Defined twice: once at the top level with a real default, once on a
    # parent for the subparsers with SUPPRESS so an absent option there does
    # not clobber a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    for flag, meta, help_text in (
        ("--db", "PATH", "Path to the catalog DB (default: .bookshelf.db beside the app)"),
        ("--root", "PATH", "Shelf root holding the category directories (default: data/)"),
        ("--config", "PATH", "TOML config holding the API key (default: .bookshelf.toml)"),
    ):
        common.add_argument(flag, metavar=meta, default=argparse.SUPPRESS, help=help_text)

    parser = argparse.ArgumentParser(
        prog="bookshelf",
        description="Catalog, shelve, and tidy a library of PDF books.",
    )
    parser.add_argument("--db", metavar="PATH", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--root", metavar="PATH", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--config", metavar="PATH", default=None, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    yes = ("--yes", "-y")
    yes_help = "Skip the confirmation prompt"

    p = sub.add_parser("rename", parents=[common], help="Rename PDFs in a directory to their proper titles")
    p.add_argument("directory", help="Directory containing PDF files")
    p.add_argument("--publisher", "-p", default="", help="Publisher hint, e.g. 'Manning', 'Packt'")
    p.add_argument("--recursive", "-r", action="store_true", help="Also process subdirectories")
    p.add_argument(*yes, action="store_true", help=yes_help)

    p = sub.add_parser("shelve", parents=[common], help="File PDFs from __landing/ into subject categories")
    p.add_argument("--category", "-c", default="",
                   help="File every landing book into this category instead of letting the model choose")
    p.add_argument("--fresh", action="store_true",
                   help="Discard previously proposed moves and look everything up again")
    p.add_argument("--publisher", "-p", default="", help="Publisher hint")
    p.add_argument(*yes, action="store_true", help=yes_help)

    p = sub.add_parser("dedupe", parents=[common], help="Remove duplicate PDFs and stale catalog rows")
    p.add_argument(*yes, action="store_true", help=yes_help)

    p = sub.add_parser("summaries", parents=[common], help="Fetch summaries for books missing one")
    p.add_argument(*yes, action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("serve", parents=[common], help="Run the local web reader")
    p.add_argument("--port", type=int, default=8765, help="Port (default 8765)")

    return parser


def build_plan(args, ctx: Context, parser: argparse.ArgumentParser):
    if args.command == "summaries":
        return summaries_plan.build(ctx)
    if args.command == "dedupe":
        return dedupe_plan.build(ctx)
    if args.command == "shelve":
        return shelve_plan.build(ctx, args.category, args.fresh, args.publisher)

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        raise PlanError(f"'{root}' is not a directory.")
    if root == ctx.config.landing_dir:
        parser.error("Use 'shelve' to process the __landing/ staging directory.")
    return rename_plan.build(ctx, root, args.recursive, args.publisher)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = Config.from_args(args)

    if args.command == "serve":
        # The reader owns its own Catalog (threaded, one per process).
        from .server import serve

        return serve(config, args.port)

    catalog = Catalog(config)
    catalog.migrate()
    ctx = Context(config=config, catalog=catalog, lookup=LookupClient(config))

    try:
        plan = build_plan(args, ctx, parser)
        return run_plan(plan, catalog, assume_yes=getattr(args, "yes", False))
    except (PlanError, ConfigError) as e:
        print(f"Error: {e}")
        return 1
    finally:
        catalog.close()


if __name__ == "__main__":
    sys.exit(main())
