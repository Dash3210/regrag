"""Hybrid lexical (SQLite FTS5) and dense embedding index builder (Phase P2)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from regrag.corpus.fetch import (
    DEFAULT_MANIFEST_PATH,
    CorpusManifest,
    ManifestError,
    compute_sha256,
    load_manifest,
)
from regrag.corpus.parse import (
    DEFAULT_PROCESSED_DIR,
    ClauseRecord,
    CleanedPage,
    _atomic_write_text,
)

DEFAULT_INDEX_DIR = Path("data/index")
DEFAULT_EMBEDDER_MODEL = "intfloat/e5-small-v2"
DEFAULT_FIXED_CHUNK_WORDS = 300
DEFAULT_FIXED_OVERLAP_WORDS = 60
DEFAULT_BATCH_SIZE = 64
INDEX_SCHEMA_VERSION = 1

_NORM_MIN = 1e-6
_UNIT_NORM_TOL = 1e-3


class IndexBuildError(RuntimeError):
    """Raised when index inputs, embeddings, or SQLite construction fail validation."""


class IndexIntegrityError(RuntimeError):
    """Raised when an index artifact fails structural, FTS5, embedding, or digest checks."""


@runtime_checkable
class DenseEncoder(Protocol):
    """Protocol for dense text encoders supporting asymmetric passage and query encoding."""

    @property
    def model_name(self) -> str:
        """Identifier of the dense embedding model."""

    @property
    def dimension(self) -> int:
        """Output embedding dimensionality."""

    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        """Encode passage texts into an L2-normalized float32 2D array."""

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        """Encode query texts into an L2-normalized float32 2D array."""


@dataclass(frozen=True)
class FixedChunkRecord:
    """Fixed-size sliding-window baseline chunk derived from cleaned regulation pages."""

    chunk_id: str
    doc_id: str
    year: int
    doc_type: str
    section_kind: str
    page_start: int
    page_end: int
    text: str
    source_sha256: str


@dataclass(frozen=True)
class IndexReport:
    """Verified build and provenance summary for the local hybrid retrieval index."""

    schema_version: int
    corpus_name: str
    manifest_sha256: str
    clauses_sha256: str
    pages_sha256: str
    index_sqlite_sha256: str
    content_sha256: str
    embedder_model: str
    embedding_dim: int
    clause_count: int
    fixed_chunk_count: int
    fixed_chunk_words: int
    fixed_overlap_words: int
    available_years: tuple[int, ...]
    available_doc_types: tuple[str, ...]
    available_section_kinds: tuple[str, ...]
    index_dir: Path
    sqlite_path: Path
    meta_path: Path


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_and_normalize_embeddings(
    raw: Any,
    *,
    expected_rows: int,
    expected_dim: int,
) -> np.ndarray:
    """Validate 2D finite non-zero embeddings and return L2-normalized float32 array."""
    if expected_dim <= 0:
        raise IndexBuildError(f"Embedding dimension must be positive, got {expected_dim}")
    try:
        arr = np.asarray(raw, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise IndexBuildError(f"Embeddings could not be converted to float32 array: {exc}") from exc

    if expected_rows == 0:
        if arr.shape != (0, expected_dim):
            raise IndexBuildError(
                f"Expected empty embeddings shape (0, {expected_dim}), got {arr.shape}"
            )
        return np.ascontiguousarray(arr, dtype=np.float32)

    if arr.ndim != 2:
        raise IndexBuildError(f"Expected 2D embeddings array, got {arr.ndim}D shape {arr.shape}")
    if arr.shape != (expected_rows, expected_dim):
        raise IndexBuildError(
            f"Embeddings shape mismatch: expected ({expected_rows}, {expected_dim}), "
            f"got {arr.shape}"
        )
    if not np.all(np.isfinite(arr)):
        raise IndexBuildError("Embeddings contain NaN or infinite values")

    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    if np.any(norms <= _NORM_MIN):
        raise IndexBuildError("Embeddings contain zero-norm vector(s)")
    normalized = (arr / norms).astype(np.float32, copy=False)
    return np.ascontiguousarray(normalized, dtype=np.float32)


class SentenceTransformerEncoder:
    """Lazy-loading SentenceTransformer encoder with automatic E5 query/passage prefixes."""

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDER_MODEL,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        device: str | None = None,
        model: Any | None = None,
    ) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        if not _is_strict_int(batch_size) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.model_name = model_name
        self.batch_size = batch_size
        self._device = device
        self._model: Any | None = model
        self._dimension: int | None = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            kwargs: dict[str, Any] = {}
            if self._device is not None:
                kwargs["device"] = self._device
            self._model = SentenceTransformer(self.model_name, **kwargs)
        return self._model

    @property
    def dimension(self) -> int:
        """Return embedding dimensionality, loading the underlying model lazily if needed."""
        if self._dimension is None:
            model = self._ensure_model()
            dim = model.get_sentence_embedding_dimension()
            if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
                raise IndexBuildError(
                    f"Model {self.model_name!r} returned invalid embedding dimension: {dim!r}"
                )
            self._dimension = dim
        return self._dimension

    def _encode_with_prefix(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        dim = self.dimension
        items = [str(t) for t in texts]
        if not items:
            return np.empty((0, dim), dtype=np.float32)

        if "e5" in self.model_name.lower():
            items = [f"{prefix}{t}" for t in items]

        model = self._ensure_model()
        batches: list[np.ndarray] = []
        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            encoded = model.encode(
                batch,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            arr = np.asarray(encoded, dtype=np.float32)
            if arr.ndim == 1 and len(batch) == 1:
                arr = arr.reshape(1, -1)
            batches.append(arr)

        stacked = np.vstack(batches).astype(np.float32, copy=False)
        return _validate_and_normalize_embeddings(
            stacked, expected_rows=len(items), expected_dim=dim
        )

    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        """Encode passage strings into L2-normalized float32 embeddings."""
        return self._encode_with_prefix(texts, "passage: ")

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        """Encode query strings into L2-normalized float32 embeddings."""
        return self._encode_with_prefix(texts, "query: ")


def build_fixed_chunks(
    pages: Sequence[CleanedPage],
    *,
    chunk_words: int = DEFAULT_FIXED_CHUNK_WORDS,
    overlap_words: int = DEFAULT_FIXED_OVERLAP_WORDS,
) -> list[FixedChunkRecord]:
    """Build deterministic sliding-window baseline chunks grouped per (doc_id, section_kind)."""
    if not _is_strict_int(chunk_words) or chunk_words <= 0:
        raise IndexBuildError(f"chunk_words must be a positive integer, got {chunk_words!r}")
    if not _is_strict_int(overlap_words) or overlap_words < 0 or overlap_words >= chunk_words:
        raise IndexBuildError(
            f"overlap_words must satisfy 0 <= overlap_words < chunk_words, "
            f"got overlap_words={overlap_words!r}, chunk_words={chunk_words!r}"
        )

    step = chunk_words - overlap_words
    grouped: dict[tuple[str, str], list[CleanedPage]] = {}
    for page in pages:
        key = (page.doc_id, page.section_kind)
        grouped.setdefault(key, []).append(page)

    records: list[FixedChunkRecord] = []
    for (doc_id, section_kind), group_pages in grouped.items():
        ordered_pages = sorted(group_pages, key=lambda p: p.page_num)
        first = ordered_pages[0]
        words: list[str] = []
        word_pages: list[int] = []
        for page in ordered_pages:
            if (
                page.year != first.year
                or page.doc_type != first.doc_type
                or page.source_sha256 != first.source_sha256
            ):
                raise IndexBuildError(
                    f"Inconsistent metadata across pages for ({doc_id}, {section_kind})"
                )
            parts = [page.text.strip()] if page.text.strip() else []
            parts.extend(tbl.strip() for tbl in page.tables_markdown if tbl.strip())
            combined = "\n\n".join(parts)
            if not combined:
                continue
            page_tokens = combined.split()
            words.extend(page_tokens)
            word_pages.extend([page.page_num] * len(page_tokens))

        total_words = len(words)
        if total_words == 0:
            continue

        seq = 1
        start = 0
        while start < total_words:
            end = min(start + chunk_words, total_words)
            window_pages = word_pages[start:end]
            records.append(
                FixedChunkRecord(
                    chunk_id=f"{doc_id}::{section_kind}::fixed::{seq:04d}",
                    doc_id=doc_id,
                    year=first.year,
                    doc_type=first.doc_type,
                    section_kind=section_kind,
                    page_start=min(window_pages),
                    page_end=max(window_pages),
                    text=" ".join(words[start:end]),
                    source_sha256=first.source_sha256,
                )
            )
            if end == total_words:
                break
            seq += 1
            start += step

    return records


def _require_str(row: Mapping[str, Any], field: str, context: str) -> str:
    val = row.get(field)
    if not isinstance(val, str):
        raise IndexBuildError(f"{context}: field '{field}' must be a string")
    return val


def _require_int(row: Mapping[str, Any], field: str, context: str) -> int:
    val = row.get(field)
    if not isinstance(val, int) or isinstance(val, bool):
        raise IndexBuildError(f"{context}: field '{field}' must be an integer")
    return val


def _require_str_tuple(row: Mapping[str, Any], field: str, context: str) -> tuple[str, ...]:
    val = row.get(field)
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise IndexBuildError(f"{context}: field '{field}' must be a list of strings")
    return tuple(val)


def _load_jsonl_records(path: Path, kind: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise IndexBuildError(f"Required input file not found: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IndexBuildError(f"Failed reading {kind} file {path}: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise IndexBuildError(f"Invalid JSON in {path}:{line_no}: {exc}") from exc
        if not isinstance(item, dict):
            raise IndexBuildError(f"Expected JSON object in {path}:{line_no}")
        rows.append(item)
    return rows


def _load_clauses(path: Path, manifest: CorpusManifest) -> list[ClauseRecord]:
    raw_rows = _load_jsonl_records(path, "clauses")
    if not raw_rows:
        raise IndexBuildError(f"Clauses file is empty: {path}")

    docs_by_id = {doc.doc_id: doc for doc in manifest.documents}
    seen_chunk_ids: set[str] = set()
    clauses: list[ClauseRecord] = []
    for idx, row in enumerate(raw_rows, start=1):
        ctx = f"{path}:{idx}"
        chunk_id = _require_str(row, "chunk_id", ctx)
        if not chunk_id or chunk_id in seen_chunk_ids:
            raise IndexBuildError(f"{ctx}: empty or duplicate chunk_id {chunk_id!r}")
        seen_chunk_ids.add(chunk_id)

        doc_id = _require_str(row, "doc_id", ctx)
        if doc_id not in docs_by_id:
            raise IndexBuildError(f"{ctx}: unknown doc_id {doc_id!r} not in manifest")

        parent_raw = row.get("parent_clause_id")
        if parent_raw is not None and not isinstance(parent_raw, str):
            raise IndexBuildError(f"{ctx}: field 'parent_clause_id' must be string or null")

        has_diagram = row.get("has_diagram")
        if not isinstance(has_diagram, bool):
            raise IndexBuildError(f"{ctx}: field 'has_diagram' must be boolean")

        clauses.append(
            ClauseRecord(
                chunk_id=chunk_id,
                doc_id=doc_id,
                year=_require_int(row, "year", ctx),
                doc_type=_require_str(row, "doc_type", ctx),
                article=_require_str(row, "article", ctx),
                clause_id=_require_str(row, "clause_id", ctx),
                parent_clause_id=parent_raw,
                breadcrumb=_require_str(row, "breadcrumb", ctx),
                text=_require_str(row, "text", ctx),
                tables_markdown=_require_str_tuple(row, "tables_markdown", ctx),
                cross_refs=_require_str_tuple(row, "cross_refs", ctx),
                page_start=_require_int(row, "page_start", ctx),
                page_end=_require_int(row, "page_end", ctx),
                section_kind=_require_str(row, "section_kind", ctx),
                has_diagram=has_diagram,
                source_sha256=_require_str(row, "source_sha256", ctx),
            )
        )
    return clauses


def _load_pages(path: Path, manifest: CorpusManifest) -> list[CleanedPage]:
    raw_rows = _load_jsonl_records(path, "pages")
    if not raw_rows:
        raise IndexBuildError(f"Pages file is empty: {path}")

    doc_order = {doc.doc_id: idx for idx, doc in enumerate(manifest.documents)}
    pages: list[CleanedPage] = []
    for idx, row in enumerate(raw_rows, start=1):
        ctx = f"{path}:{idx}"
        doc_id = _require_str(row, "doc_id", ctx)
        if doc_id not in doc_order:
            raise IndexBuildError(f"{ctx}: unknown doc_id {doc_id!r} not in manifest")

        has_diagram = row.get("has_diagram")
        if not isinstance(has_diagram, bool):
            raise IndexBuildError(f"{ctx}: field 'has_diagram' must be boolean")

        pages.append(
            CleanedPage(
                doc_id=doc_id,
                year=_require_int(row, "year", ctx),
                doc_type=_require_str(row, "doc_type", ctx),
                page_num=_require_int(row, "page_num", ctx),
                section_kind=_require_str(row, "section_kind", ctx),
                text=_require_str(row, "text", ctx),
                tables_markdown=_require_str_tuple(row, "tables_markdown", ctx),
                has_diagram=has_diagram,
                source_sha256=_require_str(row, "source_sha256", ctx),
            )
        )
    pages.sort(key=lambda p: (doc_order[p.doc_id], p.page_num))
    return pages


def _verify_parse_summary_if_present(
    summary_path: Path,
    *,
    manifest_sha256: str,
    clauses_sha256: str,
    pages_sha256: str,
    clause_count: int,
    page_count: int,
) -> None:
    if not summary_path.exists():
        return
    if not summary_path.is_file():
        raise IndexBuildError(f"parse_summary.json path is not a file: {summary_path}")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexBuildError(f"Invalid parse_summary.json ({summary_path}): {exc}") from exc
    if not isinstance(summary, dict):
        raise IndexBuildError(f"parse_summary.json must be a JSON object ({summary_path})")

    digest_checks = (
        ("clauses_sha256", clauses_sha256),
        ("pages_sha256", pages_sha256),
        ("manifest_sha256", manifest_sha256),
    )
    for key, actual in digest_checks:
        if key in summary and summary[key] != actual:
            raise IndexBuildError(
                f"parse_summary.json {key} mismatch: expected {summary[key]!r}, got {actual!r}"
            )
    if "total_clauses" in summary and summary["total_clauses"] != clause_count:
        raise IndexBuildError(
            f"parse_summary.json total_clauses mismatch: "
            f"expected {summary['total_clauses']!r}, got {clause_count!r}"
        )
    if "total_pages" in summary and summary["total_pages"] != page_count:
        raise IndexBuildError(
            f"parse_summary.json total_pages mismatch: "
            f"expected {summary['total_pages']!r}, got {page_count!r}"
        )


def _build_clause_search_text(clause: ClauseRecord) -> str:
    parts = [clause.breadcrumb.strip(), clause.text.strip()]
    base = "\n".join(part for part in parts if part)
    if clause.tables_markdown:
        tables_block = "\n".join(tbl.strip() for tbl in clause.tables_markdown if tbl.strip())
        if tables_block:
            return f"{base}\n{tables_block}" if base else tables_block
    return base


def _hash_index_contents(
    *,
    meta_fields: Mapping[str, Any],
    clause_rows: Sequence[tuple[Any, ...]],
    fixed_rows: Sequence[tuple[Any, ...]],
) -> str:
    """Compute a deterministic SHA-256 over canonical index metadata, rows, and embeddings."""
    hasher = hashlib.sha256()
    header_json = json.dumps(
        dict(meta_fields),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    hasher.update(header_json)
    hasher.update(b"\n")

    for row in clause_rows:
        # row[:-2] are scalar/text fields, row[-2] is embedding_blob, row[-1] is embedding_dim
        scalar_payload = json.dumps(
            [*row[:-2], row[-1]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        hasher.update(scalar_payload)
        hasher.update(b"|")
        hasher.update(bytes(row[-2]))
        hasher.update(b"\n")

    for row in fixed_rows:
        fixed_payload = json.dumps(
            list(row),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        hasher.update(fixed_payload)
        hasher.update(b"\n")

    return hasher.hexdigest()


def _create_sqlite_schema(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("PRAGMA page_size = 4096")
    con.execute(
        """
        CREATE TABLE index_info (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE chunks_clause (
            row_id INTEGER PRIMARY KEY,
            chunk_id TEXT NOT NULL UNIQUE,
            doc_id TEXT NOT NULL,
            year INTEGER NOT NULL,
            doc_type TEXT NOT NULL,
            article TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            parent_clause_id TEXT,
            breadcrumb TEXT NOT NULL,
            text TEXT NOT NULL,
            tables_markdown_json TEXT NOT NULL,
            cross_refs_json TEXT NOT NULL,
            search_text TEXT NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            section_kind TEXT NOT NULL,
            has_diagram INTEGER NOT NULL,
            source_sha256 TEXT NOT NULL,
            embedding_blob BLOB NOT NULL,
            embedding_dim INTEGER NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE VIRTUAL TABLE chunks_clause_fts USING fts5(
            chunk_id UNINDEXED,
            clause_id,
            breadcrumb,
            search_text,
            content='chunks_clause',
            content_rowid='row_id',
            tokenize='porter unicode61'
        )
        """
    )
    con.execute(
        """
        CREATE TABLE chunks_fixed (
            row_id INTEGER PRIMARY KEY,
            chunk_id TEXT NOT NULL UNIQUE,
            doc_id TEXT NOT NULL,
            year INTEGER NOT NULL,
            doc_type TEXT NOT NULL,
            section_kind TEXT NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            text TEXT NOT NULL,
            source_sha256 TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE VIRTUAL TABLE chunks_fixed_fts USING fts5(
            chunk_id UNINDEXED,
            text,
            content='chunks_fixed',
            content_rowid='row_id',
            tokenize='porter unicode61'
        )
        """
    )
    con.execute(
        "CREATE INDEX idx_clause_filters ON chunks_clause(year, doc_type, section_kind)"
    )
    con.execute(
        "CREATE INDEX idx_fixed_filters ON chunks_fixed(year, doc_type, section_kind)"
    )


def build_index(
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    index_dir: Path = DEFAULT_INDEX_DIR,
    *,
    encoder: DenseEncoder | None = None,
    embedder_model: str = DEFAULT_EMBEDDER_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    fixed_chunk_words: int = DEFAULT_FIXED_CHUNK_WORDS,
    fixed_overlap_words: int = DEFAULT_FIXED_OVERLAP_WORDS,
) -> IndexReport:
    """Build atomic SQLite FTS5 + dense embedding index and metadata sidecar."""
    mpath = Path(manifest_path)
    pdir = Path(processed_dir)
    out_dir = Path(index_dir)

    try:
        manifest = load_manifest(mpath)
        manifest_sha256 = compute_sha256(mpath)
    except (ManifestError, OSError) as exc:
        raise IndexBuildError(f"Invalid manifest ({mpath}): {exc}") from exc

    clauses_path = pdir / "clauses.jsonl"
    pages_path = pdir / "pages.jsonl"
    summary_path = pdir / "parse_summary.json"

    clauses = _load_clauses(clauses_path, manifest)
    pages = _load_pages(pages_path, manifest)
    clauses_sha256 = compute_sha256(clauses_path)
    pages_sha256 = compute_sha256(pages_path)

    _verify_parse_summary_if_present(
        summary_path,
        manifest_sha256=manifest_sha256,
        clauses_sha256=clauses_sha256,
        pages_sha256=pages_sha256,
        clause_count=len(clauses),
        page_count=len(pages),
    )

    fixed_chunks = build_fixed_chunks(
        pages,
        chunk_words=fixed_chunk_words,
        overlap_words=fixed_overlap_words,
    )
    if not fixed_chunks:
        raise IndexBuildError("Fixed-chunk baseline produced zero chunks from pages.jsonl")

    active_encoder: DenseEncoder
    if encoder is None:
        active_encoder = SentenceTransformerEncoder(
            model_name=embedder_model,
            batch_size=batch_size,
        )
    else:
        if not isinstance(encoder, DenseEncoder):
            raise IndexBuildError("encoder must implement the DenseEncoder protocol")
        active_encoder = encoder

    search_texts = [_build_clause_search_text(clause) for clause in clauses]
    try:
        raw_embeddings = active_encoder.encode_passages(search_texts)
        embedding_dim = int(active_encoder.dimension)
        actual_model = str(active_encoder.model_name)
    except IndexBuildError:
        raise
    except Exception as exc:
        raise IndexBuildError(f"Dense encoding failed: {exc}") from exc

    embeddings = _validate_and_normalize_embeddings(
        raw_embeddings,
        expected_rows=len(clauses),
        expected_dim=embedding_dim,
    )

    clause_rows: list[tuple[Any, ...]] = []
    for row_id, (clause, search_text, vec) in enumerate(
        zip(clauses, search_texts, embeddings, strict=True),
        start=1,
    ):
        blob = np.ascontiguousarray(vec, dtype="<f4").tobytes()
        clause_rows.append(
            (
                row_id,
                clause.chunk_id,
                clause.doc_id,
                clause.year,
                clause.doc_type,
                clause.article,
                clause.clause_id,
                clause.parent_clause_id,
                clause.breadcrumb,
                clause.text,
                json.dumps(list(clause.tables_markdown), ensure_ascii=False),
                json.dumps(list(clause.cross_refs), ensure_ascii=False),
                search_text,
                clause.page_start,
                clause.page_end,
                clause.section_kind,
                1 if clause.has_diagram else 0,
                clause.source_sha256,
                blob,
                embedding_dim,
            )
        )

    fixed_rows: list[tuple[Any, ...]] = [
        (
            row_id,
            fc.chunk_id,
            fc.doc_id,
            fc.year,
            fc.doc_type,
            fc.section_kind,
            fc.page_start,
            fc.page_end,
            fc.text,
            fc.source_sha256,
        )
        for row_id, fc in enumerate(fixed_chunks, start=1)
    ]

    available_years = tuple(sorted({c.year for c in clauses} | {f.year for f in fixed_chunks}))
    available_doc_types = tuple(
        sorted({c.doc_type for c in clauses} | {f.doc_type for f in fixed_chunks})
    )
    available_section_kinds = tuple(
        sorted({c.section_kind for c in clauses} | {f.section_kind for f in fixed_chunks})
    )

    core_meta: dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "corpus_name": manifest.corpus_name,
        "manifest_sha256": manifest_sha256,
        "clauses_sha256": clauses_sha256,
        "pages_sha256": pages_sha256,
        "embedder_model": actual_model,
        "embedding_dim": embedding_dim,
        "clause_count": len(clause_rows),
        "fixed_chunk_count": len(fixed_rows),
        "fixed_chunk_words": fixed_chunk_words,
        "fixed_overlap_words": fixed_overlap_words,
        "available_years": list(available_years),
        "available_doc_types": list(available_doc_types),
        "available_section_kinds": list(available_section_kinds),
    }
    content_sha256 = _hash_index_contents(
        meta_fields=core_meta,
        clause_rows=clause_rows,
        fixed_rows=fixed_rows,
    )

    info_rows: list[tuple[str, str]] = [
        (
            key,
            json.dumps(val, ensure_ascii=False, separators=(",", ":"))
            if isinstance(val, list)
            else str(val),
        )
        for key, val in sorted({**core_meta, "content_sha256": content_sha256}.items())
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = out_dir / "index.sqlite"
    part_path = out_dir / "index.sqlite.part"
    meta_path = out_dir / "index_meta.json"

    part_path.unlink(missing_ok=True)
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(part_path)
        _create_sqlite_schema(con)
        con.executemany("INSERT INTO index_info(key, value) VALUES (?, ?)", info_rows)
        con.executemany(
            """
            INSERT INTO chunks_clause (
                row_id, chunk_id, doc_id, year, doc_type, article, clause_id,
                parent_clause_id, breadcrumb, text, tables_markdown_json,
                cross_refs_json, search_text, page_start, page_end,
                section_kind, has_diagram, source_sha256, embedding_blob, embedding_dim
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            clause_rows,
        )
        con.execute("INSERT INTO chunks_clause_fts(chunks_clause_fts) VALUES('rebuild')")
        con.executemany(
            """
            INSERT INTO chunks_fixed (
                row_id, chunk_id, doc_id, year, doc_type, section_kind,
                page_start, page_end, text, source_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fixed_rows,
        )
        con.execute("INSERT INTO chunks_fixed_fts(chunks_fixed_fts) VALUES('rebuild')")
        con.commit()
        con.close()
        con = None
        os.replace(part_path, sqlite_path)
    except BaseException as exc:
        if con is not None:
            with contextlib.suppress(sqlite3.Error):
                con.close()
        part_path.unlink(missing_ok=True)
        if isinstance(exc, IndexBuildError):
            raise
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise IndexBuildError(f"Failed building SQLite index ({sqlite_path}): {exc}") from exc
        raise

    index_sqlite_sha256 = compute_sha256(sqlite_path)
    meta_payload = {
        **core_meta,
        "content_sha256": content_sha256,
        "index_sqlite_sha256": index_sqlite_sha256,
    }
    _atomic_write_text(
        meta_path,
        json.dumps(meta_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    return IndexReport(
        schema_version=INDEX_SCHEMA_VERSION,
        corpus_name=manifest.corpus_name,
        manifest_sha256=manifest_sha256,
        clauses_sha256=clauses_sha256,
        pages_sha256=pages_sha256,
        index_sqlite_sha256=index_sqlite_sha256,
        content_sha256=content_sha256,
        embedder_model=actual_model,
        embedding_dim=embedding_dim,
        clause_count=len(clause_rows),
        fixed_chunk_count=len(fixed_rows),
        fixed_chunk_words=fixed_chunk_words,
        fixed_overlap_words=fixed_overlap_words,
        available_years=available_years,
        available_doc_types=available_doc_types,
        available_section_kinds=available_section_kinds,
        index_dir=out_dir,
        sqlite_path=sqlite_path,
        meta_path=meta_path,
    )


def _validate_meta_payload(meta: Any, meta_path: Path) -> dict[str, Any]:
    if not isinstance(meta, dict):
        raise IndexIntegrityError(f"index_meta.json must be a JSON object: {meta_path}")

    int_fields = (
        "schema_version",
        "embedding_dim",
        "clause_count",
        "fixed_chunk_count",
        "fixed_chunk_words",
        "fixed_overlap_words",
    )
    str_fields = (
        "corpus_name",
        "manifest_sha256",
        "clauses_sha256",
        "pages_sha256",
        "index_sqlite_sha256",
        "content_sha256",
        "embedder_model",
    )
    list_fields = ("available_years", "available_doc_types", "available_section_kinds")

    for field in (*int_fields, *str_fields, *list_fields):
        if field not in meta:
            raise IndexIntegrityError(f"index_meta.json missing required field '{field}'")

    for field in int_fields:
        if not _is_strict_int(meta[field]):
            raise IndexIntegrityError(f"index_meta.json field '{field}' must be an integer")
    for field in str_fields:
        if not isinstance(meta[field], str) or not meta[field]:
            raise IndexIntegrityError(f"index_meta.json field '{field}' must be a non-empty string")

    if not isinstance(meta["available_years"], list) or not all(
        _is_strict_int(y) for y in meta["available_years"]
    ):
        raise IndexIntegrityError("index_meta.json field 'available_years' must be list[int]")
    for field in ("available_doc_types", "available_section_kinds"):
        if not isinstance(meta[field], list) or not all(isinstance(x, str) for x in meta[field]):
            raise IndexIntegrityError(f"index_meta.json field '{field}' must be list[str]")

    if meta["schema_version"] != INDEX_SCHEMA_VERSION:
        raise IndexIntegrityError(
            f"Unsupported index schema_version {meta['schema_version']!r} "
            f"(expected {INDEX_SCHEMA_VERSION})"
        )
    if meta["embedding_dim"] <= 0 or meta["clause_count"] <= 0 or meta["fixed_chunk_count"] <= 0:
        raise IndexIntegrityError("Index counts and embedding_dim must be positive")

    return meta


def verify_index_integrity(
    index_dir: Path = DEFAULT_INDEX_DIR,
    *,
    verify_sha256: bool = True,
) -> IndexReport:
    """Verify SQLite index, FTS5 tables, embedding blobs, and metadata digests."""
    idx_dir = Path(index_dir)
    sqlite_path = idx_dir / "index.sqlite"
    meta_path = idx_dir / "index_meta.json"

    if not meta_path.is_file():
        raise IndexIntegrityError(f"Missing index metadata file: {meta_path}")
    if not sqlite_path.is_file():
        raise IndexIntegrityError(f"Missing SQLite index file: {sqlite_path}")

    try:
        raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexIntegrityError(f"Failed reading index_meta.json ({meta_path}): {exc}") from exc

    meta = _validate_meta_payload(raw_meta, meta_path)
    expected_sqlite_sha = str(meta["index_sqlite_sha256"])
    if verify_sha256:
        actual_sqlite_sha = compute_sha256(sqlite_path)
        if actual_sqlite_sha != expected_sqlite_sha:
            raise IndexIntegrityError(
                f"index.sqlite SHA-256 mismatch: expected {expected_sqlite_sha}, "
                f"got {actual_sqlite_sha}"
            )
    else:
        actual_sqlite_sha = expected_sqlite_sha

    required_tables = {
        "index_info",
        "chunks_clause",
        "chunks_clause_fts",
        "chunks_clause_fts_docsize",
        "chunks_fixed",
        "chunks_fixed_fts",
        "chunks_fixed_fts_docsize",
    }

    try:
        with contextlib.closing(sqlite3.connect(sqlite_path)) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            missing_tables = sorted(required_tables - tables)
            if missing_tables:
                raise IndexIntegrityError(
                    f"Missing required SQLite tables: {', '.join(missing_tables)}"
                )

            info = {
                str(k): str(v)
                for k, v in con.execute("SELECT key, value FROM index_info").fetchall()
            }
            for key, expected_val in meta.items():
                if key == "index_sqlite_sha256":
                    continue
                if key not in info:
                    raise IndexIntegrityError(f"index_info missing key '{key}'")
                expected_str = (
                    json.dumps(expected_val, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(expected_val, list)
                    else str(expected_val)
                )
                if info[key] != expected_str:
                    raise IndexIntegrityError(
                        f"index_info mismatch for '{key}': expected {expected_str!r}, "
                        f"got {info[key]!r}"
                    )

            clause_count = int(meta["clause_count"])
            fixed_count = int(meta["fixed_chunk_count"])
            expected_dim = int(meta["embedding_dim"])

            for table_name, expected_rows in (
                ("chunks_clause", clause_count),
                ("chunks_clause_fts", clause_count),
                ("chunks_clause_fts_docsize", clause_count),
                ("chunks_fixed", fixed_count),
                ("chunks_fixed_fts", fixed_count),
                ("chunks_fixed_fts_docsize", fixed_count),
            ):
                actual_rows = int(con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
                if actual_rows != expected_rows:
                    raise IndexIntegrityError(
                        f"Row count mismatch in {table_name}: expected {expected_rows}, "
                        f"got {actual_rows}"
                    )

            con.execute(
                "INSERT INTO chunks_clause_fts(chunks_clause_fts) VALUES('integrity-check')"
            )
            con.execute(
                "INSERT INTO chunks_fixed_fts(chunks_fixed_fts) VALUES('integrity-check')"
            )

            clause_rows = con.execute(
                """
                SELECT row_id, chunk_id, doc_id, year, doc_type, article, clause_id,
                       parent_clause_id, breadcrumb, text, tables_markdown_json,
                       cross_refs_json, search_text, page_start, page_end,
                       section_kind, has_diagram, source_sha256, embedding_blob, embedding_dim
                FROM chunks_clause
                ORDER BY row_id ASC
                """
            ).fetchall()

            expected_bytes = expected_dim * 4
            for idx, row in enumerate(clause_rows, start=1):
                if row[0] != idx:
                    raise IndexIntegrityError(
                        f"Non-contiguous row_id in chunks_clause: expected {idx}, got {row[0]}"
                    )
                blob, row_dim = row[-2], row[-1]
                if row_dim != expected_dim:
                    raise IndexIntegrityError(
                        f"Embedding dim mismatch for {row[1]}: expected {expected_dim}, "
                        f"got {row_dim}"
                    )
                if not isinstance(blob, (bytes, bytearray)) or len(blob) != expected_bytes:
                    actual_len = len(blob) if isinstance(blob, (bytes, bytearray)) else -1
                    raise IndexIntegrityError(
                        f"Invalid embedding_blob byte length for {row[1]}: "
                        f"expected {expected_bytes}, got {actual_len}"
                    )
                vec = np.frombuffer(blob, dtype="<f4")
                if not np.all(np.isfinite(vec)):
                    raise IndexIntegrityError(f"Non-finite embedding value in {row[1]}")
                norm = float(np.linalg.norm(vec))
                if not np.isfinite(norm) or abs(norm - 1.0) > _UNIT_NORM_TOL:
                    raise IndexIntegrityError(
                        f"Non-unit embedding norm in {row[1]}: norm={norm}"
                    )

            fixed_rows = con.execute(
                """
                SELECT row_id, chunk_id, doc_id, year, doc_type, section_kind,
                       page_start, page_end, text, source_sha256
                FROM chunks_fixed
                ORDER BY row_id ASC
                """
            ).fetchall()
            for idx, row in enumerate(fixed_rows, start=1):
                if row[0] != idx:
                    raise IndexIntegrityError(
                        f"Non-contiguous row_id in chunks_fixed: expected {idx}, got {row[0]}"
                    )
    except IndexIntegrityError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise IndexIntegrityError(f"SQLite integrity check failed ({sqlite_path}): {exc}") from exc

    core_meta = {
        key: meta[key]
        for key in (
            "schema_version",
            "corpus_name",
            "manifest_sha256",
            "clauses_sha256",
            "pages_sha256",
            "embedder_model",
            "embedding_dim",
            "clause_count",
            "fixed_chunk_count",
            "fixed_chunk_words",
            "fixed_overlap_words",
            "available_years",
            "available_doc_types",
            "available_section_kinds",
        )
    }
    recomputed_content_sha = _hash_index_contents(
        meta_fields=core_meta,
        clause_rows=clause_rows,
        fixed_rows=fixed_rows,
    )
    if recomputed_content_sha != meta["content_sha256"]:
        raise IndexIntegrityError(
            f"Index content_sha256 mismatch: expected {meta['content_sha256']}, "
            f"got {recomputed_content_sha}"
        )

    return IndexReport(
        schema_version=int(meta["schema_version"]),
        corpus_name=str(meta["corpus_name"]),
        manifest_sha256=str(meta["manifest_sha256"]),
        clauses_sha256=str(meta["clauses_sha256"]),
        pages_sha256=str(meta["pages_sha256"]),
        index_sqlite_sha256=actual_sqlite_sha,
        content_sha256=str(meta["content_sha256"]),
        embedder_model=str(meta["embedder_model"]),
        embedding_dim=int(meta["embedding_dim"]),
        clause_count=int(meta["clause_count"]),
        fixed_chunk_count=int(meta["fixed_chunk_count"]),
        fixed_chunk_words=int(meta["fixed_chunk_words"]),
        fixed_overlap_words=int(meta["fixed_overlap_words"]),
        available_years=tuple(int(y) for y in meta["available_years"]),
        available_doc_types=tuple(str(t) for t in meta["available_doc_types"]),
        available_section_kinds=tuple(str(s) for s in meta["available_section_kinds"]),
        index_dir=idx_dir,
        sqlite_path=sqlite_path,
        meta_path=meta_path,
    )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EMBEDDER_MODEL",
    "DEFAULT_FIXED_CHUNK_WORDS",
    "DEFAULT_FIXED_OVERLAP_WORDS",
    "DEFAULT_INDEX_DIR",
    "INDEX_SCHEMA_VERSION",
    "DenseEncoder",
    "FixedChunkRecord",
    "IndexBuildError",
    "IndexIntegrityError",
    "IndexReport",
    "SentenceTransformerEncoder",
    "build_fixed_chunks",
    "build_index",
    "verify_index_integrity",
]
