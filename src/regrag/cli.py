"""Command-line interface for regrag."""

import argparse
from collections.abc import Sequence

from regrag import __version__, _phase

_UNIMPLEMENTED_PHASES: dict[str, str] = {
    "fetch": "P1",
    "parse": "P1",
    "index": "P2",
    "eval": "P3",
    "gate-negative": "P3",
    "serve": "P4",
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser with phase-gated subcommands."""
    parser = argparse.ArgumentParser(
        prog="regrag",
        description="Citation-grounded retrieval and evaluation over multi-year regulations.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("version", help="Print package version.")
    subparsers.add_parser("fetch", help="Fetch regulation PDFs from manifest (P1).")
    subparsers.add_parser("parse", help="Parse regulation PDFs into clause corpus (P1).")
    subparsers.add_parser("index", help="Build local lexical and dense indexes (P2).")
    subparsers.add_parser("eval", help="Run evaluation benchmark and gate (P3).")
    subparsers.add_parser("gate-negative", help="Run negative-control evaluation gate (P3).")
    subparsers.add_parser("serve", help="Run citation-grounded QA API service (P4).")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for regrag."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "version":
        print(f"regrag {__version__}")
        return 0

    phase = _UNIMPLEMENTED_PHASES[args.subcommand]
    _phase.todo(phase, args.subcommand)


__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
