"""Corpus ingestion, SHA-256 provenance fetching, and clause-aware PDF parsing (Phase P1)."""

from regrag.corpus.fetch import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_RAW_DIR,
    ChecksumMismatchError,
    CorpusManifest,
    FetchError,
    FetchResult,
    ManifestDocument,
    ManifestError,
    compute_sha256,
    fetch_corpus,
    fetch_document,
    load_manifest,
)
from regrag.corpus.parse import (
    DEFAULT_PROCESSED_DIR,
    ClauseRecord,
    CleanedPage,
    ParseError,
    ParseReport,
    parse_corpus,
    parse_document,
)

__all__ = [
    "ChecksumMismatchError",
    "CleanedPage",
    "ClauseRecord",
    "CorpusManifest",
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_PROCESSED_DIR",
    "DEFAULT_RAW_DIR",
    "FetchError",
    "FetchResult",
    "ManifestDocument",
    "ManifestError",
    "ParseError",
    "ParseReport",
    "compute_sha256",
    "fetch_corpus",
    "fetch_document",
    "load_manifest",
    "parse_corpus",
    "parse_document",
]
