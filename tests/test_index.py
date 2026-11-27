"""Unit, determinism, and tamper-detection tests for Phase P2 index builder."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import types
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from regrag.corpus.parse import ClauseRecord, CleanedPage
from regrag.index import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EMBEDDER_MODEL,
    DEFAULT_FIXED_CHUNK_WORDS,
    DEFAULT_FIXED_OVERLAP_WORDS,
    DEFAULT_INDEX_DIR,
    INDEX_SCHEMA_VERSION,
    DenseEncoder,
    FixedChunkRecord,
    IndexBuildError,
    IndexIntegrityError,
    IndexReport,
    SentenceTransformerEncoder,
    build_fixed_chunks,
    build_index,
    verify_index_integrity,
)
from regrag.index import build as index_build_mod


class _StubEncoder:
    """Deterministic offline DenseEncoder implementation for tests."""

    def __init__(self, model_name: str = "stub/deterministic-4d", dimension: int = 4) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self.passages_seen: list[str] = []
        self.queries_seen: list[str] = []

    def _embed_one(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = np.array(
            [int.from_bytes(digest[i * 4 : (i + 1) * 4], "little") for i in range(self.dimension)],
            dtype=np.float32,
        )
        raw = (raw % 997) + 1.0
        norm = float(np.linalg.norm(raw))
        return (raw / norm).astype(np.float32)

    def encode_passages(self, texts: list[str] | tuple[str, ...] | Any) -> np.ndarray:
        items = list(texts)
        self.passages_seen.extend(items)
        if not items:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.vstack([self._embed_one(t) for t in items]).astype(np.float32)

    def encode_queries(self, texts: list[str] | tuple[str, ...] | Any) -> np.ndarray:
        items = list(texts)
        self.queries_seen.extend(items)
        if not items:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.vstack([self._embed_one(t) for t in items]).astype(np.float32)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_sample_corpus(
    base_dir: Path,
    *,
    include_summary: bool = True,
) -> tuple[Path, Path]:
    """Create a minimal deterministic processed corpus and manifest inside tmp_path."""
    corpus_dir = base_dir / "corpus"
    processed_dir = base_dir / "processed"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    sha_2024 = "a" * 64
    sha_2025 = "b" * 64
    manifest_payload = {
        "manifest_version": 1,
        "corpus_name": "fia-f1-test",
        "documents": [
            {
                "doc_id": "fia_2024_technical",
                "year": 2024,
                "doc_type": "technical",
                "issue": "Issue 8",
                "published_date": "2024-10-17",
                "title": "2024 Formula 1 Technical Regulations",
                "filename": "fia_2024_technical.pdf",
                "url": "https://example.com/2024.pdf",
                "sha256": sha_2024,
                "expected_bytes": 1000,
                "expected_pages": 3,
            },
            {
                "doc_id": "fia_2025_sporting",
                "year": 2025,
                "doc_type": "sporting",
                "issue": "Issue 5",
                "published_date": "2025-04-30",
                "title": "2025 Formula One Sporting Regulations",
                "filename": "fia_2025_sporting.pdf",
                "url": "https://example.com/2025.pdf",
                "sha256": sha_2025,
                "expected_bytes": 1000,
                "expected_pages": 2,
            },
        ],
    }
    manifest_path = corpus_dir / "manifest.json"
    manifest_bytes = (json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)

    clauses = [
        ClauseRecord(
            chunk_id="fia_2024_technical::normative::3.1.1",
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            article="Article 3",
            clause_id="3.1.1",
            parent_clause_id="3.1",
            breadcrumb="Article 3 > 3.1 Definitions > 3.1.1",
            text="Cooling ducts must remain strictly within the legality box.",
            tables_markdown=("| Param | Limit |\n| --- | --- |\n| Mass | 798 kg |",),
            cross_refs=("Article 4",),
            page_start=1,
            page_end=2,
            section_kind="normative",
            has_diagram=True,
            source_sha256=sha_2024,
        ),
        ClauseRecord(
            chunk_id="fia_2024_technical::amendment_preview::App.6",
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            article="Appendix 6",
            clause_id="App.6",
            parent_clause_id=None,
            breadcrumb="Appendix 6 > Approved Changes",
            text="Approved changes for 2026 floor geometry.",
            tables_markdown=(),
            cross_refs=(),
            page_start=3,
            page_end=3,
            section_kind="amendment_preview",
            has_diagram=False,
            source_sha256=sha_2024,
        ),
        ClauseRecord(
            chunk_id="fia_2025_sporting::appendix::App.3",
            doc_id="fia_2025_sporting",
            year=2025,
            doc_type="sporting",
            article="Appendix 3",
            clause_id="App.3",
            parent_clause_id=None,
            breadcrumb="Appendix 3 > Podium Ceremony",
            text="Podium procedures apply after every Grand Prix.",
            tables_markdown=(),
            cross_refs=("Sporting Regulations",),
            page_start=1,
            page_end=2,
            section_kind="appendix",
            has_diagram=False,
            source_sha256=sha_2025,
        ),
    ]

    pages = [
        CleanedPage(
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            page_num=1,
            section_kind="normative",
            text=" ".join(f"w{i}" for i in range(1, 15)) + " cooling system",
            tables_markdown=("| Param | Limit |\n| --- | --- |\n| Mass | 798 kg |",),
            has_diagram=True,
            source_sha256=sha_2024,
        ),
        CleanedPage(
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            page_num=2,
            section_kind="normative",
            text=" ".join(f"p2w{i}" for i in range(1, 15)),
            tables_markdown=(),
            has_diagram=False,
            source_sha256=sha_2024,
        ),
        CleanedPage(
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            page_num=3,
            section_kind="amendment_preview",
            text="Approved changes for 2026 floor geometry.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=sha_2024,
        ),
        CleanedPage(
            doc_id="fia_2025_sporting",
            year=2025,
            doc_type="sporting",
            page_num=1,
            section_kind="appendix",
            text="Podium procedures apply after every Grand Prix.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=sha_2025,
        ),
    ]

    clauses_bytes = (
        "\n".join(json.dumps(asdict(c), sort_keys=True) for c in clauses) + "\n"
    ).encode("utf-8")
    pages_bytes = (
        "\n".join(json.dumps(asdict(p), sort_keys=True) for p in pages) + "\n"
    ).encode("utf-8")

    (processed_dir / "clauses.jsonl").write_bytes(clauses_bytes)
    (processed_dir / "pages.jsonl").write_bytes(pages_bytes)

    if include_summary:
        summary = {
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "corpus_name": "fia-f1-test",
            "documents_parsed": 2,
            "total_pages": len(pages),
            "total_clauses": len(clauses),
            "clauses_sha256": _sha256_bytes(clauses_bytes),
            "pages_sha256": _sha256_bytes(pages_bytes),
            "documents": [],
        }
        (processed_dir / "parse_summary.json").write_bytes(
            (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )

    return processed_dir, manifest_path


def test_constants_exceptions_and_lazy_import() -> None:
    assert Path("data/index") == DEFAULT_INDEX_DIR
    assert DEFAULT_EMBEDDER_MODEL == "intfloat/e5-small-v2"
    assert DEFAULT_FIXED_CHUNK_WORDS == 300
    assert DEFAULT_FIXED_OVERLAP_WORDS == 60
    assert DEFAULT_BATCH_SIZE == 64
    assert INDEX_SCHEMA_VERSION == 1
    assert issubclass(IndexBuildError, RuntimeError)
    assert issubclass(IndexIntegrityError, RuntimeError)
    assert is_dataclass(FixedChunkRecord)
    assert is_dataclass(IndexReport)
    assert "sentence_transformers" not in sys.modules
    assert set(index_build_mod.__all__) >= {
        "DEFAULT_INDEX_DIR",
        "DEFAULT_EMBEDDER_MODEL",
        "DEFAULT_FIXED_CHUNK_WORDS",
        "DEFAULT_FIXED_OVERLAP_WORDS",
        "DEFAULT_BATCH_SIZE",
        "INDEX_SCHEMA_VERSION",
        "IndexBuildError",
        "IndexIntegrityError",
        "DenseEncoder",
        "SentenceTransformerEncoder",
        "FixedChunkRecord",
        "IndexReport",
        "build_fixed_chunks",
        "build_index",
        "verify_index_integrity",
    }


def test_sentence_transformer_encoder_e5_prefixes_and_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str], dict[str, Any]]] = []

    class _FakeST:
        def __init__(self, model_name_or_path: str, device: str | None = None) -> None:
            self.model_name_or_path = model_name_or_path
            self.device = device

        def get_sentence_embedding_dimension(self) -> int:
            return 3

        def encode(
            self,
            sentences: list[str],
            *,
            batch_size: int = 32,
            normalize_embeddings: bool = True,
            show_progress_bar: bool = False,
            convert_to_numpy: bool = True,
        ) -> np.ndarray:
            calls.append(
                (
                    self.model_name_or_path,
                    list(sentences),
                    {
                        "batch_size": batch_size,
                        "normalize_embeddings": normalize_embeddings,
                        "show_progress_bar": show_progress_bar,
                        "convert_to_numpy": convert_to_numpy,
                    },
                )
            )
            # Return deliberately unnormalized float64 to confirm wrapper normalizes to float32
            return np.array([[3.0, 4.0, 0.0] for _ in sentences], dtype=np.float64)

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeST  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    e5 = SentenceTransformerEncoder(model_name="intfloat/e5-small-v2", batch_size=2)
    assert isinstance(e5, DenseEncoder)
    assert e5.model_name == "intfloat/e5-small-v2"
    assert e5.dimension == 3

    empty = e5.encode_passages([])
    assert empty.shape == (0, 3)
    assert empty.dtype == np.float32

    passages = e5.encode_passages(["alpha", "beta", "gamma"])
    assert passages.shape == (3, 3)
    assert passages.dtype == np.float32
    norms = np.linalg.norm(passages, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-6)
    assert [batch[1] for batch in calls] == [
        ["passage: alpha", "passage: beta"],
        ["passage: gamma"],
    ]

    calls.clear()
    queries = e5.encode_queries(["what is min mass?"])
    assert queries.shape == (1, 3)
    assert calls[0][1] == ["query: what is min mass?"]

    calls.clear()
    non_e5 = SentenceTransformerEncoder(model_name="BAAI/bge-small-en-v1.5", batch_size=2)
    non_e5.encode_passages(["raw passage"])
    non_e5.encode_queries(["raw query"])
    assert calls[0][1] == ["raw passage"]
    assert calls[1][1] == ["raw query"]

    with pytest.raises(ValueError, match="batch_size"):
        SentenceTransformerEncoder(batch_size=0)
    with pytest.raises(ValueError, match="model_name"):
        SentenceTransformerEncoder(model_name="   ")

    class _BadDimST(_FakeST):
        def get_sentence_embedding_dimension(self) -> int:
            return 0

    fake_module.SentenceTransformer = _BadDimST  # type: ignore[attr-defined]
    bad_enc = SentenceTransformerEncoder(model_name="intfloat/e5-small-v2")
    with pytest.raises(IndexBuildError, match="dimension"):
        _ = bad_enc.dimension


def test_build_fixed_chunks_sliding_window_and_amendment_isolation() -> None:
    pages = [
        CleanedPage(
            doc_id="doc1",
            year=2024,
            doc_type="technical",
            page_num=1,
            section_kind="normative",
            text=" ".join(f"p1_{i}" for i in range(6)),
            tables_markdown=("| A | B |",),
            has_diagram=False,
            source_sha256="a" * 64,
        ),
        CleanedPage(
            doc_id="doc1",
            year=2024,
            doc_type="technical",
            page_num=2,
            section_kind="normative",
            text=" ".join(f"p2_{i}" for i in range(6)),
            tables_markdown=(),
            has_diagram=False,
            source_sha256="a" * 64,
        ),
        CleanedPage(
            doc_id="doc1",
            year=2024,
            doc_type="technical",
            page_num=3,
            section_kind="amendment_preview",
            text="future change one two three four",
            tables_markdown=(),
            has_diagram=False,
            source_sha256="a" * 64,
        ),
    ]

    # Page 1 contributes 6 words + 5 tokens from "| A | B |" = 11 words; Page 2 adds 6 => 17 words.
    chunks = build_fixed_chunks(pages, chunk_words=10, overlap_words=3)
    normative = [c for c in chunks if c.section_kind == "normative"]
    amendment = [c for c in chunks if c.section_kind == "amendment_preview"]

    assert [c.chunk_id for c in normative] == [
        "doc1::normative::fixed::0001",
        "doc1::normative::fixed::0002",
    ]
    assert normative[0].page_start == 1
    assert normative[0].page_end == 1
    assert normative[1].page_start == 1
    assert normative[1].page_end == 2
    # 3-word overlap between window 1 tail and window 2 head
    w1 = normative[0].text.split()
    w2 = normative[1].text.split()
    assert w1[-3:] == w2[:3]
    assert len(w1) == 10
    assert len(w2) == 10

    assert len(amendment) == 1
    assert amendment[0].chunk_id == "doc1::amendment_preview::fixed::0001"
    assert amendment[0].page_start == 3
    assert amendment[0].page_end == 3
    assert "future" in amendment[0].text

    with pytest.raises(IndexBuildError):
        build_fixed_chunks(pages, chunk_words=0, overlap_words=0)
    with pytest.raises(IndexBuildError):
        build_fixed_chunks(pages, chunk_words=10, overlap_words=10)


def test_build_index_schema_fts5_determinism_and_no_summary(tmp_path: Path) -> None:
    processed_dir, manifest_path = _make_sample_corpus(tmp_path, include_summary=True)
    idx1 = tmp_path / "idx1"
    idx2 = tmp_path / "idx2"

    report1 = build_index(
        processed_dir=processed_dir,
        manifest_path=manifest_path,
        index_dir=idx1,
        encoder=_StubEncoder(),
        fixed_chunk_words=12,
        fixed_overlap_words=4,
    )
    report2 = build_index(
        processed_dir=processed_dir,
        manifest_path=manifest_path,
        index_dir=idx2,
        encoder=_StubEncoder(),
        fixed_chunk_words=12,
        fixed_overlap_words=4,
    )

    assert report1.schema_version == INDEX_SCHEMA_VERSION
    assert report1.corpus_name == "fia-f1-test"
    assert report1.clause_count == 3
    assert report1.embedding_dim == 4
    assert report1.available_years == (2024, 2025)
    assert report1.available_doc_types == ("sporting", "technical")
    assert report1.available_section_kinds == ("amendment_preview", "appendix", "normative")

    # Determinism across repeated runs
    assert report1.content_sha256 == report2.content_sha256
    assert report1.index_sqlite_sha256 == report2.index_sqlite_sha256
    assert (idx1 / "index.sqlite").read_bytes() == (idx2 / "index.sqlite").read_bytes()
    meta_bytes1 = (idx1 / "index_meta.json").read_bytes()
    meta_bytes2 = (idx2 / "index_meta.json").read_bytes()
    assert meta_bytes1 == meta_bytes2
    assert b"\r" not in meta_bytes1

    # Verify SQLite internals, FTS5 porter stemming, and embedding blobs
    con = sqlite3.connect(idx1 / "index.sqlite")
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert con.execute("PRAGMA page_size").fetchone()[0] == 4096

        idx_names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert {"idx_clause_filters", "idx_fixed_filters"} <= idx_names

        fts_hits = con.execute(
            "SELECT chunk_id FROM chunks_clause_fts WHERE chunks_clause_fts MATCH 'cool'"
        ).fetchall()
        assert fts_hits == [("fia_2024_technical::normative::3.1.1",)]

        fixed_hits = con.execute(
            "SELECT chunk_id FROM chunks_fixed_fts WHERE chunks_fixed_fts MATCH 'podium'"
        ).fetchall()
        assert fixed_hits == [("fia_2025_sporting::appendix::fixed::0001",)]

        row = con.execute(
            "SELECT search_text, embedding_blob, embedding_dim, has_diagram "
            "FROM chunks_clause WHERE chunk_id = ?",
            ("fia_2024_technical::normative::3.1.1",),
        ).fetchone()
        assert row is not None
        search_text, blob, dim, has_diagram = row
        assert "Article 3 > 3.1 Definitions > 3.1.1" in search_text
        assert "798 kg" in search_text
        assert dim == 4
        assert has_diagram == 1
        assert isinstance(blob, bytes) and len(blob) == 16
        vec = np.frombuffer(blob, dtype="<f4")
        assert np.isclose(float(np.linalg.norm(vec)), 1.0, atol=1e-5)
    finally:
        con.close()

    verified = verify_index_integrity(idx1)
    assert verified == report1

    # Also works cleanly when parse_summary.json is absent
    (processed_dir / "parse_summary.json").unlink()
    idx3 = tmp_path / "idx3"
    report3 = build_index(
        processed_dir=processed_dir,
        manifest_path=manifest_path,
        index_dir=idx3,
        encoder=_StubEncoder(),
        fixed_chunk_words=12,
        fixed_overlap_words=4,
    )
    assert report3.index_sqlite_sha256 == report1.index_sqlite_sha256


def test_build_index_cleans_up_part_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_dir, manifest_path = _make_sample_corpus(tmp_path)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    stale_part = index_dir / "index.sqlite.part"
    stale_part.write_bytes(b"stale leftover")

    def _boom_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated disk error")

    monkeypatch.setattr(os, "replace", _boom_replace)

    with pytest.raises(IndexBuildError, match="simulated disk error"):
        build_index(
            processed_dir=processed_dir,
            manifest_path=manifest_path,
            index_dir=index_dir,
            encoder=_StubEncoder(),
        )

    assert not stale_part.exists()
    assert not (index_dir / "index.sqlite").exists()


def test_build_index_input_and_embedding_validation_errors(tmp_path: Path) -> None:
    processed_dir, manifest_path = _make_sample_corpus(tmp_path)

    # Bad encoder shapes / NaNs / zero-norm / dim mismatch
    class _BadEncoder(_StubEncoder):
        def __init__(self, mode: str) -> None:
            super().__init__()
            self.mode = mode

        def encode_passages(self, texts: list[str] | tuple[str, ...] | Any) -> np.ndarray:
            n = len(list(texts))
            if self.mode == "1d":
                return np.ones((n,), dtype=np.float32)
            if self.mode == "wrong_rows":
                return np.ones((max(0, n - 1), self.dimension), dtype=np.float32)
            if self.mode == "wrong_dim":
                return np.ones((n, self.dimension + 1), dtype=np.float32)
            if self.mode == "nan":
                arr = np.ones((n, self.dimension), dtype=np.float32)
                arr[0, 0] = np.nan
                return arr
            return np.zeros((n, self.dimension), dtype=np.float32)

    for mode in ("1d", "wrong_rows", "wrong_dim", "nan", "zero"):
        with pytest.raises(IndexBuildError):
            build_index(
                processed_dir=processed_dir,
                manifest_path=manifest_path,
                index_dir=tmp_path / f"bad_{mode}",
                encoder=_BadEncoder(mode),
            )

    # Corrupt parse_summary digest
    summary_path = processed_dir / "parse_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["clauses_sha256"] = "0" * 64
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(IndexBuildError, match="clauses_sha256"):
        build_index(
            processed_dir=processed_dir,
            manifest_path=manifest_path,
            index_dir=tmp_path / "bad_summary",
            encoder=_StubEncoder(),
        )

    # Empty clauses
    summary_path.unlink()
    (processed_dir / "clauses.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(IndexBuildError, match="empty"):
        build_index(
            processed_dir=processed_dir,
            manifest_path=manifest_path,
            index_dir=tmp_path / "empty_clauses",
            encoder=_StubEncoder(),
        )

    # Corrupt JSONL
    (processed_dir / "clauses.jsonl").write_text("{not-json\n", encoding="utf-8")
    with pytest.raises(IndexBuildError):
        build_index(
            processed_dir=processed_dir,
            manifest_path=manifest_path,
            index_dir=tmp_path / "bad_json",
            encoder=_StubEncoder(),
        )

    # Missing manifest
    with pytest.raises(IndexBuildError):
        build_index(
            processed_dir=processed_dir,
            manifest_path=tmp_path / "missing_manifest.json",
            index_dir=tmp_path / "missing_manifest",
            encoder=_StubEncoder(),
        )


def test_build_index_default_encoder_lazy_load_and_invalid_encoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_dir, manifest_path = _make_sample_corpus(tmp_path)

    with pytest.raises(IndexBuildError, match="DenseEncoder"):
        build_index(
            processed_dir=processed_dir,
            manifest_path=manifest_path,
            index_dir=tmp_path / "bad_enc",
            encoder="not-an-encoder",  # type: ignore[arg-type]
        )

    class _FakeE5:
        def __init__(self, model_name_or_path: str, device: str | None = None) -> None:
            self.model_name_or_path = model_name_or_path

        def get_sentence_embedding_dimension(self) -> int:
            return 4

        def encode(
            self,
            sentences: list[str],
            *,
            batch_size: int = 64,
            normalize_embeddings: bool = True,
            show_progress_bar: bool = False,
            convert_to_numpy: bool = True,
        ) -> np.ndarray:
            return np.ones((len(sentences), 4), dtype=np.float32) * 0.5

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _FakeE5  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    report = build_index(
        processed_dir=processed_dir,
        manifest_path=manifest_path,
        index_dir=tmp_path / "default_enc",
        fixed_chunk_words=12,
        fixed_overlap_words=4,
    )
    assert report.embedder_model == DEFAULT_EMBEDDER_MODEL
    assert report.embedding_dim == 4
    assert verify_index_integrity(tmp_path / "default_enc") == report


def _refresh_meta_sqlite_sha(index_dir: Path) -> None:
    """Update index_meta.json's index_sqlite_sha256 after direct SQLite tampering."""
    sqlite_bytes = (index_dir / "index.sqlite").read_bytes()
    meta_path = index_dir / "index_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["index_sqlite_sha256"] = _sha256_bytes(sqlite_bytes)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "missing_meta",
        "missing_sqlite",
        "corrupt_meta_json",
        "missing_meta_field",
        "wrong_schema_version",
        "meta_sha_mismatch",
        "corrupt_sqlite_bytes",
        "missing_sqlite_table",
        "bad_blob_len",
        "zero_norm_blob",
        "nan_blob",
        "delete_clause_row",
        "delete_fixed_row",
        "delete_fts_docsize_row",
        "tamper_clause_text",
        "tamper_index_info",
    ],
)
def test_verify_index_integrity_rejects_tampering(tmp_path: Path, tamper_kind: str) -> None:
    processed_dir, manifest_path = _make_sample_corpus(tmp_path)
    index_dir = tmp_path / f"tamper_{tamper_kind}"
    build_index(
        processed_dir=processed_dir,
        manifest_path=manifest_path,
        index_dir=index_dir,
        encoder=_StubEncoder(),
        fixed_chunk_words=12,
        fixed_overlap_words=4,
    )

    sqlite_path = index_dir / "index.sqlite"
    meta_path = index_dir / "index_meta.json"

    if tamper_kind == "missing_meta":
        meta_path.unlink()
    elif tamper_kind == "missing_sqlite":
        sqlite_path.unlink()
    elif tamper_kind == "corrupt_meta_json":
        meta_path.write_text("{bad-json", encoding="utf-8")
    elif tamper_kind == "missing_meta_field":
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["content_sha256"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif tamper_kind == "wrong_schema_version":
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["schema_version"] = 999
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif tamper_kind == "meta_sha_mismatch":
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["index_sqlite_sha256"] = "f" * 64
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    elif tamper_kind == "corrupt_sqlite_bytes":
        raw = bytearray(sqlite_path.read_bytes())
        raw[-128:] = b"\xff" * 128
        sqlite_path.write_bytes(bytes(raw))
    else:
        con = sqlite3.connect(sqlite_path)
        try:
            if tamper_kind == "missing_sqlite_table":
                con.execute("DROP TABLE chunks_fixed")
            elif tamper_kind == "bad_blob_len":
                con.execute(
                    "UPDATE chunks_clause SET embedding_blob = ? WHERE row_id = 1",
                    (b"short",),
                )
            elif tamper_kind == "zero_norm_blob":
                zero_blob = np.zeros(4, dtype="<f4").tobytes()
                con.execute(
                    "UPDATE chunks_clause SET embedding_blob = ? WHERE row_id = 1", (zero_blob,)
                )
            elif tamper_kind == "nan_blob":
                nan_blob = np.array([np.nan, 1.0, 0.0, 0.0], dtype="<f4").tobytes()
                con.execute(
                    "UPDATE chunks_clause SET embedding_blob = ? WHERE row_id = 1", (nan_blob,)
                )
            elif tamper_kind == "delete_clause_row":
                con.execute("DELETE FROM chunks_clause WHERE row_id = 1")
            elif tamper_kind == "delete_fixed_row":
                con.execute("DELETE FROM chunks_fixed WHERE row_id = 1")
            elif tamper_kind == "delete_fts_docsize_row":
                con.execute("DELETE FROM chunks_clause_fts_docsize WHERE id = 1")
            elif tamper_kind == "tamper_clause_text":
                con.execute("UPDATE chunks_clause SET text = 'tampered' WHERE row_id = 1")
            elif tamper_kind == "tamper_index_info":
                con.execute("UPDATE index_info SET value = 'evil' WHERE key = 'corpus_name'")
            con.commit()
        finally:
            con.close()
        _refresh_meta_sqlite_sha(index_dir)

    with pytest.raises(IndexIntegrityError):
        verify_index_integrity(index_dir, verify_sha256=(tamper_kind != "tamper_clause_text"))
