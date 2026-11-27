"""Version-aware hybrid lexical, dense, RRF, and cross-encoder retrieval (Phase P2)."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from regrag.index.build import (
    DEFAULT_INDEX_DIR,
    DenseEncoder,
    IndexIntegrityError,
    SentenceTransformerEncoder,
    verify_index_integrity,
)

RetrievalConfig = Literal[
    "bm25_fixed",
    "bm25_clause",
    "dense_clause",
    "hybrid_rrf",
    "hybrid_rerank",
]

VALID_CONFIGS: tuple[str, ...] = (
    "bm25_fixed",
    "bm25_clause",
    "dense_clause",
    "hybrid_rrf",
    "hybrid_rerank",
)

DEFAULT_RRF_K = 60
DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
DEFAULT_RERANK_CANDIDATES = 20

# Empirically calibrated on data/index against intfloat/e5-small-v2 & ms-marco-MiniLM-L-6-v2.
DEFAULT_MIN_DENSE_SCORE = 0.82
DEFAULT_MIN_BM25_SCORE = 0.01
DEFAULT_MIN_RERANK_SCORE = -1.5

_BASE_ALLOWED_SECTION_KINDS: tuple[str, ...] = ("normative", "appendix")
_ALL_SECTION_KINDS: tuple[str, ...] = ("normative", "appendix", "amendment_preview")

_YEAR_RE = re.compile(
    r"(?<!\d)(20\d{2})(?!\d)"
    r"(?!\s*(?:mm|cm|m|kg|g|l|hz|khz|mhz|rpm|bar|barg|kpa|mpa|n|nm|kph|km/h|mph|ms|s|deg|c)\b)",
    re.IGNORECASE,
)
_FTS5_TOKEN_RE = re.compile(r"\w+(?:\.\w+)*")


class RetrievalError(RuntimeError):
    """Raised when retrieval configuration, index access, or scoring fails."""


class VersionLeakageError(RetrievalError):
    """Raised when retrieved hits violate year, doc_type, or section_kind isolation."""


@runtime_checkable
class Reranker(Protocol):
    """Protocol for query-passage cross-encoder rerankers."""

    @property
    def model_name(self) -> str:
        """Identifier of the reranking model."""

    def score_pairs(self, query: str, passages: Sequence[str]) -> Sequence[float]:
        """Return one relevance score per passage in input order."""


class CrossEncoderReranker:
    """Lazy-loading sentence-transformers CrossEncoder adapter."""

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        *,
        device: str | None = None,
        batch_size: int = 32,
        model: Any | None = None,
    ) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        if not _is_strict_int(batch_size) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._model: Any | None = model

    def _ensure_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            kwargs: dict[str, Any] = {}
            if self._device is not None:
                kwargs["device"] = self._device
            self._model = CrossEncoder(self.model_name, **kwargs)
        return self._model

    def score_pairs(self, query: str, passages: Sequence[str]) -> list[float]:
        """Score (query, passage) pairs lazily loading CrossEncoder on first call."""
        items = [str(p) for p in passages]
        if not items:
            return []

        model = self._ensure_model()
        pairs = [(str(query), p) for p in items]
        try:
            raw_scores = model.predict(
                pairs,
                batch_size=self._batch_size,
                show_progress_bar=False,
            )
            arr = np.asarray(raw_scores, dtype=np.float64).ravel()
        except Exception as exc:
            raise RetrievalError(f"Cross-encoder scoring failed: {exc}") from exc

        if arr.shape != (len(pairs),) or not np.all(np.isfinite(arr)):
            raise RetrievalError(
                f"Invalid cross-encoder output shape/values: expected ({len(pairs)},), "
                f"got {arr.shape}"
            )
        return [float(x) for x in arr]


@dataclass(frozen=True)
class RetrievalHit:
    """Single retrieved clause or fixed-window chunk with provenance and scoring breakdown."""

    chunk_id: str
    doc_id: str
    year: int
    doc_type: str
    article: str
    clause_id: str
    parent_clause_id: str | None
    breadcrumb: str
    text: str
    tables_markdown: tuple[str, ...]
    cross_refs: tuple[str, ...]
    page_start: int
    page_end: int
    section_kind: str
    has_diagram: bool
    source_sha256: str
    score: float
    rank: int
    config: str
    lexical_score: float | None = None
    dense_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True)
class RetrievalResult:
    """Version-checked retrieval response including abstention metadata."""

    query: str
    config: str
    top_k: int
    requested_year: int | None
    effective_year: int | None
    requested_doc_type: str | None
    allowed_section_kinds: tuple[str, ...]
    hits: tuple[RetrievalHit, ...]
    abstained: bool
    abstention_reason: str | None
    candidate_count: int
    latency_ms: float


@dataclass(frozen=True)
class _CachedClause:
    row_id: int
    chunk_id: str
    doc_id: str
    year: int
    doc_type: str
    article: str
    clause_id: str
    parent_clause_id: str | None
    breadcrumb: str
    text: str
    tables_markdown: tuple[str, ...]
    cross_refs: tuple[str, ...]
    search_text: str
    page_start: int
    page_end: int
    section_kind: str
    has_diagram: bool
    source_sha256: str


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def extract_query_years(query: str) -> tuple[int, ...]:
    """Extract sorted unique 4-digit 20xx regulation years, ignoring unit numbers."""
    if not isinstance(query, str) or not query:
        return ()
    return tuple(sorted({int(match.group(1)) for match in _YEAR_RE.finditer(query)}))


def sanitize_fts5_query(query: str) -> str:
    """Convert raw query text into a safe FTS5 OR expression preserving dotted clause IDs."""
    if not isinstance(query, str) or not query.strip():
        return ""
    tokens = list(
        dict.fromkeys(
            match.group(0)
            for match in _FTS5_TOKEN_RE.finditer(query)
            if any(ch.isalnum() for ch in match.group(0))
        )
    )
    return " OR ".join(f'"{tok}"' for tok in tokens)


def verify_version_isolation(
    hits: Sequence[RetrievalHit],
    *,
    effective_year: int | None,
    doc_type: str | None,
    allowed_section_kinds: Sequence[str],
) -> None:
    """Raise VersionLeakageError if any hit violates year, doc_type, or section_kind rules."""
    allowed_kinds = set(allowed_section_kinds)
    for hit in hits:
        if effective_year is not None and hit.year != effective_year:
            raise VersionLeakageError(
                f"Year leakage detected for {hit.chunk_id!r}: "
                f"expected year {effective_year}, got {hit.year}"
            )
        if doc_type is not None and hit.doc_type != doc_type:
            raise VersionLeakageError(
                f"Document type leakage detected for {hit.chunk_id!r}: "
                f"expected doc_type {doc_type!r}, got {hit.doc_type!r}"
            )
        if hit.section_kind not in allowed_kinds:
            raise VersionLeakageError(
                f"Section kind leakage detected for {hit.chunk_id!r}: "
                f"section_kind {hit.section_kind!r} not in {sorted(allowed_kinds)}"
            )


def _clause_to_hit(
    clause: _CachedClause,
    *,
    score: float,
    rank: int,
    config: str,
    lexical_score: float | None = None,
    dense_score: float | None = None,
    rrf_score: float | None = None,
    rerank_score: float | None = None,
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=clause.chunk_id,
        doc_id=clause.doc_id,
        year=clause.year,
        doc_type=clause.doc_type,
        article=clause.article,
        clause_id=clause.clause_id,
        parent_clause_id=clause.parent_clause_id,
        breadcrumb=clause.breadcrumb,
        text=clause.text,
        tables_markdown=clause.tables_markdown,
        cross_refs=clause.cross_refs,
        page_start=clause.page_start,
        page_end=clause.page_end,
        section_kind=clause.section_kind,
        has_diagram=clause.has_diagram,
        source_sha256=clause.source_sha256,
        score=score,
        rank=rank,
        config=config,
        lexical_score=lexical_score,
        dense_score=dense_score,
        rrf_score=rrf_score,
        rerank_score=rerank_score,
    )


def _build_sql_where(
    alias: str,
    *,
    filter_years: tuple[int, ...] | None,
    doc_type: str | None,
    allowed_section_kinds: tuple[str, ...],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filter_years is not None:
        clauses.append(f"{alias}.year IN ({', '.join('?' for _ in filter_years)})")
        params.extend(filter_years)
    if doc_type is not None:
        clauses.append(f"{alias}.doc_type = ?")
        params.append(doc_type)
    clauses.append(f"{alias}.section_kind IN ({', '.join('?' for _ in allowed_section_kinds)})")
    params.extend(allowed_section_kinds)
    return " AND ".join(clauses), params


class HybridRetriever:
    """Version-isolated hybrid retriever over the local SQLite FTS5 + dense index.

    Raises:
        IndexIntegrityError: If ``verify_integrity`` is True and index verification fails.
        RetrievalError: If index files cannot be read, metadata is invalid, or inputs fail.
    """

    def __init__(
        self,
        index_dir: Path | str = DEFAULT_INDEX_DIR,
        *,
        encoder: DenseEncoder | None = None,
        reranker: Reranker | None = None,
        verify_integrity: bool = True,
        min_dense_score: float = DEFAULT_MIN_DENSE_SCORE,
        min_bm25_score: float = DEFAULT_MIN_BM25_SCORE,
        min_rerank_score: float = DEFAULT_MIN_RERANK_SCORE,
        rrf_k: int = DEFAULT_RRF_K,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    ) -> None:
        if not _is_strict_int(rrf_k) or rrf_k <= 0:
            raise RetrievalError(f"rrf_k must be a positive integer, got {rrf_k!r}")
        if not _is_strict_int(rerank_candidates) or rerank_candidates <= 0:
            raise RetrievalError(
                f"rerank_candidates must be a positive integer, got {rerank_candidates!r}"
            )
        if encoder is not None and not isinstance(encoder, DenseEncoder):
            raise RetrievalError("encoder must implement the DenseEncoder protocol")
        if reranker is not None and not isinstance(reranker, Reranker):
            raise RetrievalError("reranker must implement the Reranker protocol")

        self.index_dir = Path(index_dir)
        self.sqlite_path = self.index_dir / "index.sqlite"
        self.meta_path = self.index_dir / "index_meta.json"
        self._encoder: DenseEncoder | None = encoder
        self._reranker: Reranker | None = reranker
        self._default_min_dense_score = float(min_dense_score)
        self._default_min_bm25_score = float(min_bm25_score)
        self._default_min_rerank_score = float(min_rerank_score)
        self._rrf_k = rrf_k
        self._default_rerank_candidates = rerank_candidates
        self._closed = False
        self._con: sqlite3.Connection | None = None

        self._load_metadata(verify_integrity=verify_integrity)

        try:
            sqlite_uri = f"{self.sqlite_path.resolve().as_uri()}?mode=ro"
            self._con = sqlite3.connect(sqlite_uri, uri=True, check_same_thread=False)
            self._clauses, self._clause_idx_by_id, self._embeddings = self._load_clause_cache(
                self._con, expected_dim=self.embedding_dim
            )
        except IndexIntegrityError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise RetrievalError(f"Failed loading index from {self.sqlite_path}: {exc}") from exc

    def _load_metadata(self, *, verify_integrity: bool) -> None:
        if verify_integrity:
            report = verify_index_integrity(self.index_dir)
            self.embedder_model = report.embedder_model
            self.embedding_dim = report.embedding_dim
            self.available_years = report.available_years
            self.available_doc_types = report.available_doc_types
            self.available_section_kinds = report.available_section_kinds
            return

        if not self.meta_path.is_file() or not self.sqlite_path.is_file():
            raise RetrievalError(f"Missing index files under {self.index_dir}")
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            self.embedder_model = str(meta["embedder_model"])
            self.embedding_dim = int(meta["embedding_dim"])
            self.available_years = tuple(int(y) for y in meta["available_years"])
            self.available_doc_types = tuple(str(t) for t in meta["available_doc_types"])
            self.available_section_kinds = tuple(
                str(s) for s in meta["available_section_kinds"]
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RetrievalError(f"Invalid index_meta.json ({self.meta_path}): {exc}") from exc

    @staticmethod
    def _load_clause_cache(
        con: sqlite3.Connection,
        *,
        expected_dim: int,
    ) -> tuple[tuple[_CachedClause, ...], dict[str, int], np.ndarray]:
        rows = con.execute(
            """
            SELECT row_id, chunk_id, doc_id, year, doc_type, article, clause_id,
                   parent_clause_id, breadcrumb, text, tables_markdown_json,
                   cross_refs_json, search_text, page_start, page_end,
                   section_kind, has_diagram, source_sha256, embedding_blob, embedding_dim
            FROM chunks_clause
            ORDER BY row_id ASC
            """
        ).fetchall()
        if not rows:
            raise RetrievalError("chunks_clause table is empty")

        clauses: list[_CachedClause] = []
        idx_by_id: dict[str, int] = {}
        vectors: list[np.ndarray] = []

        for idx, row in enumerate(rows):
            (
                row_id, chunk_id, doc_id, year, doc_type, article, clause_id,
                parent_clause_id, breadcrumb, text, tables_json, xrefs_json,
                search_text, page_start, page_end, section_kind, has_diagram,
                source_sha256, blob, row_dim,
            ) = row
            if int(row_dim) != expected_dim:
                raise RetrievalError(
                    f"Embedding dim mismatch for {chunk_id!r}: "
                    f"expected {expected_dim}, got {row_dim}"
                )
            vec = np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)
            if vec.shape != (expected_dim,) or not np.all(np.isfinite(vec)):
                raise RetrievalError(f"Invalid embedding vector for {chunk_id!r}")
            norm = float(np.linalg.norm(vec))
            if norm <= 1e-6:
                raise RetrievalError(f"Zero-norm embedding vector for {chunk_id!r}")
            vec /= norm

            clauses.append(
                _CachedClause(
                    row_id=int(row_id),
                    chunk_id=str(chunk_id),
                    doc_id=str(doc_id),
                    year=int(year),
                    doc_type=str(doc_type),
                    article=str(article),
                    clause_id=str(clause_id),
                    parent_clause_id=(
                        str(parent_clause_id) if parent_clause_id is not None else None
                    ),
                    breadcrumb=str(breadcrumb),
                    text=str(text),
                    tables_markdown=tuple(str(x) for x in json.loads(str(tables_json))),
                    cross_refs=tuple(str(x) for x in json.loads(str(xrefs_json))),
                    search_text=str(search_text),
                    page_start=int(page_start),
                    page_end=int(page_end),
                    section_kind=str(section_kind),
                    has_diagram=bool(has_diagram),
                    source_sha256=str(source_sha256),
                )
            )
            idx_by_id[str(chunk_id)] = idx
            vectors.append(vec)

        return tuple(clauses), idx_by_id, np.ascontiguousarray(np.vstack(vectors), dtype=np.float32)

    def close(self) -> None:
        """Close the underlying read-only SQLite connection."""
        if self._con is not None:
            with contextlib.suppress(sqlite3.Error):
                self._con.close()
            self._con = None
        self._closed = True

    def __enter__(self) -> HybridRetriever:
        if self._closed:
            raise RetrievalError("HybridRetriever is closed")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _encode_query(self, query: str) -> np.ndarray:
        if self._encoder is None:
            self._encoder = SentenceTransformerEncoder(model_name=self.embedder_model)
        try:
            arr = np.asarray(self._encoder.encode_queries([query]), dtype=np.float32)
        except Exception as exc:
            raise RetrievalError(f"Query dense encoding failed: {exc}") from exc

        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.shape != (1, self.embedding_dim) or not np.all(np.isfinite(arr)):
            raise RetrievalError(
                f"Encoded query shape/value mismatch: expected (1, {self.embedding_dim}), "
                f"got {arr.shape}"
            )
        vec = arr[0].astype(np.float32, copy=True)
        norm = float(np.linalg.norm(vec))
        if norm <= 1e-6:
            raise RetrievalError("Query encoder produced a zero-norm vector")
        return vec / norm

    def _allowed_kinds(self, include_amendment_preview: bool) -> tuple[str, ...]:
        if not include_amendment_preview:
            kinds = tuple(k for k in self.available_section_kinds if k != "amendment_preview")
            return kinds if kinds else _BASE_ALLOWED_SECTION_KINDS
        extras = tuple(k for k in self.available_section_kinds if k not in _ALL_SECTION_KINDS)
        return _ALL_SECTION_KINDS + extras

    def _resolve_years(
        self, query: str, requested_year: int | None
    ) -> tuple[int | None, tuple[int, ...] | None, str | None]:
        available = set(self.available_years)
        query_years = extract_query_years(query)

        if requested_year is not None:
            if requested_year not in available or any(y not in available for y in query_years):
                return requested_year, None, "unsupported_year"
            if any(y != requested_year for y in query_years):
                return requested_year, None, "conflicting_year_filter"
            return requested_year, (requested_year,), None

        if any(y not in available for y in query_years):
            return None, None, "unsupported_year"
        if len(query_years) == 1:
            return query_years[0], (query_years[0],), None
        if len(query_years) > 1:
            return None, query_years, None
        return None, None, None

    def _search_bm25_fixed(
        self,
        query: str,
        *,
        filter_years: tuple[int, ...] | None,
        doc_type: str | None,
        allowed_section_kinds: tuple[str, ...],
    ) -> list[RetrievalHit]:
        assert self._con is not None
        fts_query = sanitize_fts5_query(query)
        if not fts_query:
            return []

        where_sql, params = _build_sql_where(
            "m",
            filter_years=filter_years,
            doc_type=doc_type,
            allowed_section_kinds=allowed_section_kinds,
        )
        sql = f"""
            SELECT m.chunk_id, m.doc_id, m.year, m.doc_type, m.section_kind,
                   m.page_start, m.page_end, m.text, m.source_sha256,
                   -bm25(chunks_fixed_fts) AS lex_score
            FROM chunks_fixed_fts
            JOIN chunks_fixed AS m ON m.row_id = chunks_fixed_fts.rowid
            WHERE chunks_fixed_fts MATCH ? AND {where_sql}
            ORDER BY bm25(chunks_fixed_fts) ASC, m.chunk_id ASC
        """
        rows = self._con.execute(sql, [fts_query, *params]).fetchall()
        hits = [
            RetrievalHit(
                chunk_id=str(cid),
                doc_id=str(did),
                year=int(yr),
                doc_type=str(dt),
                article="",
                clause_id="",
                parent_clause_id=None,
                breadcrumb="",
                text=str(txt),
                tables_markdown=(),
                cross_refs=(),
                page_start=int(ps),
                page_end=int(pe),
                section_kind=str(sk),
                has_diagram=False,
                source_sha256=str(sha),
                score=float(raw_s),
                rank=0,
                config="bm25_fixed",
                lexical_score=float(raw_s),
            )
            for cid, did, yr, dt, sk, ps, pe, txt, sha, raw_s in rows
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return hits

    def _search_bm25_clause(
        self,
        query: str,
        *,
        filter_years: tuple[int, ...] | None,
        doc_type: str | None,
        allowed_section_kinds: tuple[str, ...],
    ) -> list[tuple[str, float]]:
        assert self._con is not None
        fts_query = sanitize_fts5_query(query)
        if not fts_query:
            return []

        where_sql, params = _build_sql_where(
            "m",
            filter_years=filter_years,
            doc_type=doc_type,
            allowed_section_kinds=allowed_section_kinds,
        )
        sql = f"""
            SELECT m.chunk_id, -bm25(chunks_clause_fts) AS lex_score
            FROM chunks_clause_fts
            JOIN chunks_clause AS m ON m.row_id = chunks_clause_fts.rowid
            WHERE chunks_clause_fts MATCH ? AND {where_sql}
            ORDER BY bm25(chunks_clause_fts) ASC, m.chunk_id ASC
        """
        rows = self._con.execute(sql, [fts_query, *params]).fetchall()
        scored = [(str(cid), float(score)) for cid, score in rows]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    def _filter_clause_indices(
        self,
        *,
        filter_years: tuple[int, ...] | None,
        doc_type: str | None,
        allowed_section_kinds: tuple[str, ...],
    ) -> list[int]:
        year_set = set(filter_years) if filter_years is not None else None
        kind_set = set(allowed_section_kinds)
        return [
            idx
            for idx, clause in enumerate(self._clauses)
            if (year_set is None or clause.year in year_set)
            and (doc_type is None or clause.doc_type == doc_type)
            and clause.section_kind in kind_set
        ]

    def _search_dense_clause(
        self, query: str, *, filtered_indices: Sequence[int]
    ) -> list[tuple[str, float]]:
        if not filtered_indices:
            return []
        q_vec = self._encode_query(query)
        cosines = self._embeddings[np.asarray(filtered_indices, dtype=np.intp)] @ q_vec
        scored = [
            (self._clauses[idx].chunk_id, float(score))
            for idx, score in zip(filtered_indices, cosines.tolist(), strict=True)
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    def _fuse_rrf(
        self,
        lex_ranked: Sequence[tuple[str, float]],
        dense_ranked: Sequence[tuple[str, float]],
    ) -> list[tuple[str, float, float | None, float | None]]:
        lex_scores = dict(lex_ranked)
        dense_scores = dict(dense_ranked)
        rrf_scores: dict[str, float] = {}
        for rank, (cid, _) in enumerate(lex_ranked, start=1):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (self._rrf_k + rank)
        for rank, (cid, _) in enumerate(dense_ranked, start=1):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (self._rrf_k + rank)
        fused = [
            (cid, score, lex_scores.get(cid), dense_scores.get(cid))
            for cid, score in rrf_scores.items()
        ]
        fused.sort(key=lambda item: (-item[1], item[0]))
        return fused

    def _rerank_candidates(
        self,
        query: str,
        fused_ranked: Sequence[tuple[str, float, float | None, float | None]],
        *,
        top_k: int,
        pool_limit: int,
        min_rerank_score: float,
    ) -> tuple[list[RetrievalHit], str | None]:
        pool = list(fused_ranked[: max(top_k, pool_limit)])
        passages = [self._clauses[self._clause_idx_by_id[cid]].search_text for cid, _, _, _ in pool]
        if self._reranker is None:
            self._reranker = CrossEncoderReranker()
        raw_scores = self._reranker.score_pairs(query, passages)
        if len(raw_scores) != len(pool):
            raise RetrievalError(
                f"Reranker returned {len(raw_scores)} scores for {len(pool)} passages"
            )
        reranked = [
            (cid, float(r_score), rrf_s, lex_s, dense_s)
            for (cid, rrf_s, lex_s, dense_s), r_score in zip(pool, raw_scores, strict=True)
        ]
        reranked.sort(key=lambda item: (-item[1], item[0]))
        if reranked[0][1] < min_rerank_score:
            return [], "low_retrieval_score"
        hits = [
            _clause_to_hit(
                self._clauses[self._clause_idx_by_id[cid]],
                score=r_score,
                rank=rank,
                config="hybrid_rerank",
                lexical_score=lex_s,
                dense_score=dense_s,
                rrf_score=rrf_s,
                rerank_score=r_score,
            )
            for rank, (cid, r_score, rrf_s, lex_s, dense_s) in enumerate(
                reranked[:top_k], start=1
            )
        ]
        return hits, None

    def _run_config(
        self,
        query: str,
        config: RetrievalConfig,
        *,
        top_k: int,
        filter_years: tuple[int, ...] | None,
        doc_type: str | None,
        allowed_kinds: tuple[str, ...],
        rerank_pool: int,
        min_dense: float,
        min_bm25: float,
        min_rerank: float,
    ) -> tuple[list[RetrievalHit], int, str | None]:
        if config == "bm25_fixed":
            fixed_hits = self._search_bm25_fixed(
                query,
                filter_years=filter_years,
                doc_type=doc_type,
                allowed_section_kinds=allowed_kinds,
            )
            if not fixed_hits:
                return [], 0, "no_matching_clauses"
            if fixed_hits[0].score < min_bm25:
                return [], len(fixed_hits), "low_retrieval_score"
            selected = [
                replace(hit, rank=rank)
                for rank, hit in enumerate(fixed_hits[:top_k], start=1)
            ]
            return selected, len(fixed_hits), None

        if config == "bm25_clause":
            lex_ranked = self._search_bm25_clause(
                query,
                filter_years=filter_years,
                doc_type=doc_type,
                allowed_section_kinds=allowed_kinds,
            )
            if not lex_ranked:
                return [], 0, "no_matching_clauses"
            if lex_ranked[0][1] < min_bm25:
                return [], len(lex_ranked), "low_retrieval_score"
            selected = [
                _clause_to_hit(
                    self._clauses[self._clause_idx_by_id[cid]],
                    score=score,
                    rank=rank,
                    config=config,
                    lexical_score=score,
                )
                for rank, (cid, score) in enumerate(lex_ranked[:top_k], start=1)
            ]
            return selected, len(lex_ranked), None

        filtered_indices = self._filter_clause_indices(
            filter_years=filter_years,
            doc_type=doc_type,
            allowed_section_kinds=allowed_kinds,
        )
        if not filtered_indices:
            return [], 0, "no_matching_clauses"

        dense_ranked = self._search_dense_clause(query, filtered_indices=filtered_indices)
        if config == "dense_clause":
            if dense_ranked[0][1] < min_dense:
                return [], len(dense_ranked), "low_retrieval_score"
            selected = [
                _clause_to_hit(
                    self._clauses[self._clause_idx_by_id[cid]],
                    score=score,
                    rank=rank,
                    config=config,
                    dense_score=score,
                )
                for rank, (cid, score) in enumerate(dense_ranked[:top_k], start=1)
            ]
            return selected, len(dense_ranked), None

        lex_ranked = self._search_bm25_clause(
            query,
            filter_years=filter_years,
            doc_type=doc_type,
            allowed_section_kinds=allowed_kinds,
        )
        fused_ranked = self._fuse_rrf(lex_ranked, dense_ranked)
        lex_ok = bool(lex_ranked) and lex_ranked[0][1] >= min_bm25
        dense_ok = bool(dense_ranked) and dense_ranked[0][1] >= min_dense
        if not lex_ok and not dense_ok:
            return [], len(fused_ranked), "low_retrieval_score"

        if config == "hybrid_rrf":
            selected = [
                _clause_to_hit(
                    self._clauses[self._clause_idx_by_id[cid]],
                    score=rrf_score,
                    rank=rank,
                    config=config,
                    lexical_score=lex_s,
                    dense_score=dense_s,
                    rrf_score=rrf_score,
                )
                for rank, (cid, rrf_score, lex_s, dense_s) in enumerate(
                    fused_ranked[:top_k], start=1
                )
            ]
            return selected, len(fused_ranked), None

        hits, rerank_reason = self._rerank_candidates(
            query,
            fused_ranked,
            top_k=top_k,
            pool_limit=rerank_pool,
            min_rerank_score=min_rerank,
        )
        return hits, len(fused_ranked), rerank_reason

    def retrieve(
        self,
        query: str,
        config: RetrievalConfig = "hybrid_rerank",
        *,
        top_k: int = 5,
        year: int | None = None,
        doc_type: str | None = None,
        include_amendment_preview: bool = False,
        rerank_candidates: int | None = None,
        min_dense_score: float | None = None,
        min_bm25_score: float | None = None,
        min_rerank_score: float | None = None,
    ) -> RetrievalResult:
        """Retrieve version-isolated chunks using the selected retrieval configuration."""
        t0 = time.perf_counter()
        if self._closed or self._con is None:
            raise RetrievalError("HybridRetriever is closed")
        if config not in VALID_CONFIGS:
            raise RetrievalError(
                f"Unsupported retrieval config {config!r}; expected one of {VALID_CONFIGS}"
            )
        if not _is_strict_int(top_k) or top_k <= 0:
            raise RetrievalError(f"top_k must be a positive integer, got {top_k!r}")
        if year is not None and not _is_strict_int(year):
            raise RetrievalError(f"year must be an integer or None, got {year!r}")
        if rerank_candidates is not None and (
            not _is_strict_int(rerank_candidates) or rerank_candidates <= 0
        ):
            raise RetrievalError(
                f"rerank_candidates must be a positive integer, got {rerank_candidates!r}"
            )

        allowed_kinds = self._allowed_kinds(include_amendment_preview)
        query_str = query if isinstance(query, str) else ""

        def _build_result(
            hits: Sequence[RetrievalHit],
            *,
            effective_yr: int | None,
            abstained: bool,
            reason: str | None,
            candidate_count: int,
        ) -> RetrievalResult:
            return RetrievalResult(
                query=query_str,
                config=config,
                top_k=top_k,
                requested_year=year,
                effective_year=effective_yr,
                requested_doc_type=doc_type,
                allowed_section_kinds=allowed_kinds,
                hits=tuple(hits),
                abstained=abstained,
                abstention_reason=reason,
                candidate_count=candidate_count,
                latency_ms=max(0.0, (time.perf_counter() - t0) * 1000.0),
            )

        if not isinstance(query, str) or not query.strip():
            return _build_result(
                (), effective_yr=year, abstained=True, reason="empty_query", candidate_count=0
            )

        effective_year, filter_years, year_reason = self._resolve_years(query, year)
        if year_reason is not None:
            return _build_result(
                (),
                effective_yr=effective_year,
                abstained=True,
                reason=year_reason,
                candidate_count=0,
            )

        hits, candidate_count, abstention_reason = self._run_config(
            query,
            config,
            top_k=top_k,
            filter_years=filter_years,
            doc_type=doc_type,
            allowed_kinds=allowed_kinds,
            rerank_pool=(
                self._default_rerank_candidates if rerank_candidates is None else rerank_candidates
            ),
            min_dense=(
                self._default_min_dense_score if min_dense_score is None else float(min_dense_score)
            ),
            min_bm25=(
                self._default_min_bm25_score if min_bm25_score is None else float(min_bm25_score)
            ),
            min_rerank=(
                self._default_min_rerank_score
                if min_rerank_score is None
                else float(min_rerank_score)
            ),
        )
        if abstention_reason is not None:
            return _build_result(
                (),
                effective_yr=effective_year,
                abstained=True,
                reason=abstention_reason,
                candidate_count=candidate_count,
            )

        verify_version_isolation(
            hits,
            effective_year=effective_year,
            doc_type=doc_type,
            allowed_section_kinds=allowed_kinds,
        )
        return _build_result(
            hits,
            effective_yr=effective_year,
            abstained=False,
            reason=None,
            candidate_count=candidate_count,
        )


__all__ = [
    "DEFAULT_MIN_BM25_SCORE",
    "DEFAULT_MIN_DENSE_SCORE",
    "DEFAULT_MIN_RERANK_SCORE",
    "DEFAULT_RERANK_CANDIDATES",
    "DEFAULT_RERANKER_MODEL",
    "DEFAULT_RRF_K",
    "VALID_CONFIGS",
    "CrossEncoderReranker",
    "HybridRetriever",
    "Reranker",
    "RetrievalConfig",
    "RetrievalError",
    "RetrievalHit",
    "RetrievalResult",
    "VersionLeakageError",
    "extract_query_years",
    "sanitize_fts5_query",
    "verify_version_isolation",
]
