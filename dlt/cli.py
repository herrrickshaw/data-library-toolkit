#!/usr/bin/env python3
"""
data-library-toolkit CLI.

  dlt discover [--fresh]              scan configured rclone remotes, add new documents
  dlt zotero-discover                 pull items from the local Zotero library as documents
  dlt zotero-link                     backfill zotero_key/tags for documents matching a real Zotero item
  dlt convert [--max-docs N]          convert one capped batch of pending documents to .md
  dlt convert --loop                  convert continuously until nothing is pending
  dlt status                          print progress summary
  dlt analyze [--out PATH]            write a markdown insights report
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict

from . import config
from .catalog import connect


def build_parser() -> argparse.ArgumentParser:
    """Argument parser with one sub-command per pipeline step."""
    ap = argparse.ArgumentParser(prog="dlt", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="scan rclone remotes for new documents")
    p_discover.add_argument("--fresh", action="store_true", help="ignore any existing discovery checkpoint")
    p_discover.add_argument("--dry-run", action="store_true")

    sub.add_parser("zotero-discover", help="pull items from the local Zotero library")
    sub.add_parser("zotero-link", help="backfill zotero_key/tags for matching documents")

    p_convert = sub.add_parser("convert", help="convert pending documents to markdown")
    p_convert.add_argument("--max-docs", type=int, default=None)
    p_convert.add_argument("--loop", action="store_true", help="keep converting batches until nothing is pending")

    sub.add_parser("status", help="print progress summary")

    p_analyze = sub.add_parser("analyze", help="write a markdown insights report")
    p_analyze.add_argument("--out", type=str, default=None)
    return ap


def _cmd_discover(args) -> None:
    from . import discover
    con = connect()
    found, _archives, _skipped = discover.scan(fresh=args.fresh)
    discover.insert_new(con, found, dry_run=args.dry_run)
    con.close()


def _cmd_zotero_discover(args) -> None:
    from . import zotero_index
    con = connect()
    zotero_index.discover_from_zotero(con)
    con.close()


def _cmd_zotero_link(args) -> None:
    from . import zotero_index
    con = connect()
    zotero_index.enrich_existing(con)
    con.close()


def _cmd_convert(args) -> None:
    from . import convert
    if args.loop:
        while True:
            result = convert.run(max_docs=args.max_docs)
            if not result or result.get("remaining", 0) <= 0:
                break
    else:
        convert.run(max_docs=args.max_docs)


def _cmd_status(args) -> None:
    from . import convert
    convert.run(status_only=True)


def _cmd_analyze(args) -> None:
    from . import analyze
    # Not read_only: connect() runs CREATE ... IF NOT EXISTS, which DuckDB rejects on a read-only attach.
    con = connect()
    analyze.generate(con, out_path=Path(args.out) if args.out else None)
    con.close()


# Sub-command -> handler. Heavy modules are imported lazily inside each handler.
COMMANDS: Dict[str, Callable] = {
    "discover": _cmd_discover,
    "zotero-discover": _cmd_zotero_discover,
    "zotero-link": _cmd_zotero_link,
    "convert": _cmd_convert,
    "status": _cmd_status,
    "analyze": _cmd_analyze,
}


def main() -> None:
    args = build_parser().parse_args()
    config.ensure_dirs()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
