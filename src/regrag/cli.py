"""Command-line interface for regrag."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from regrag import __version__, _phase
from regrag.corpus.fetch import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_RAW_DIR,
    ChecksumMismatchError,
    FetchError,
    ManifestError,
    fetch_corpus,
)
from regrag.corpus.parse import (
    DEFAULT_PROCESSED_DIR,
    ParseError,
    parse_corpus,
)

_UNIMPLEMENTED_PHASES: dict[str, str] = {
    "index": "P2",
    "eval": "P3",
    "gate-negative": "P3",
    "serve": "P4",
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser with wired P1 and phase-gated subcommands."""
    parser = argparse.ArgumentParser(
        prog="regrag",
        description="Citation-grounded retrieval and evaluation over multi-year regulations.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("version", help="Print package version.")

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch and verify regulation PDFs declared in the corpus manifest.",
    )
    fetch_parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Path to the corpus manifest JSON file.",
    )
    fetch_parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Directory where raw regulation PDFs are stored.",
    )
    fetch_parser.add_argument(
        "--doc-id",
        action="append",
        dest="doc_ids",
        default=None,
        help="Restrict fetching to specific doc_id (repeatable).",
    )
    fetch_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even when a verified local PDF already exists.",
    )

    parse_parser = subparsers.add_parser(
        "parse",
        help="Parse regulation PDFs into deterministic clause and page JSONL artifacts.",
    )
    parse_parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Path to the corpus manifest JSON file.",
    )
    parse_parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help="Directory containing downloaded regulation PDFs.",
    )
    parse_parser.add_argument(
        "--processed-dir",
        type=Path,
        default=DEFAULT_PROCESSED_DIR,
        help="Output directory for clauses.jsonl, pages.jsonl, and parse_summary.json.",
    )
    parse_parser.add_argument(
        "--doc-id",
        action="append",
        dest="doc_ids",
        default=None,
        help="Restrict parsing to specific doc_id (repeatable).",
    )

    subparsers.add_parser("index", help="Build local lexical and dense indexes (P2).")
    subparsers.add_parser("eval", help="Run evaluation benchmark and gate (P3).")
    subparsers.add_parser("gate-negative", help="Run negative-control evaluation gate (P3).")
    subparsers.add_parser("serve", help="Run citation-grounded QA API service (P4).")

    return parser


def _run_fetch(args: argparse.Namespace) -> int:
    try:
        results = fetch_corpus(
            manifest_path=args.manifest,
            raw_dir=args.raw_dir,
            doc_ids=args.doc_ids,
            force=args.force,
        )
    except (ManifestError, ChecksumMismatchError, FetchError) as exc:
        print(f"regrag fetch: {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(
            f"{result.doc_id}: {result.status} "
            f"({result.size_bytes} bytes, attempts={result.attempts}, "
            f"sha256={result.sha256[:12]}) -> {result.path}"
        )
    return 0


def _run_parse(args: argparse.Namespace) -> int:
    try:
        report = parse_corpus(
            manifest_path=args.manifest,
            raw_dir=args.raw_dir,
            processed_dir=args.processed_dir,
            doc_ids=args.doc_ids,
        )
    except (ManifestError, ParseError) as exc:
        print(f"regrag parse: {exc}", file=sys.stderr)
        return 1

    print(
        f"Parsed {report.documents_parsed} document(s): "
        f"{report.total_pages} pages, {report.total_clauses} clauses -> "
        f"{report.clauses_path} "
        f"(clauses_sha256={report.clauses_sha256[:12]}, "
        f"pages_sha256={report.pages_sha256[:12]})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for regrag."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "version":
        print(f"regrag {__version__}")
        return 0
    if args.subcommand == "fetch":
        return _run_fetch(args)
    if args.subcommand == "parse":
        return _run_parse(args)

    phase = _UNIMPLEMENTED_PHASES[args.subcommand]
    _phase.todo(phase, args.subcommand)


__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
