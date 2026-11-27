"""Unit, version-isolation, abstention, oracle, and tamper tests for Phase P2 retrieval."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import types
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from regrag import retrieve as retrieve_mod
from regrag.corpus.parse import ClauseRecord, CleanedPage
from regrag.index import IndexIntegrityError, build_index
from regrag.retrieve import (
    DEFAULT_MIN_BM25_SCORE,
    DEFAULT_MIN_DENSE_SCORE,
    DEFAULT_MIN_RERANK_SCORE,
    DEFAULT_RERANK_CANDIDATES,
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RRF_K,
    VALID_CONFIGS,
    CrossEncoderReranker,
    HybridRetriever,
    Reranker,
    RetrievalConfig,
    RetrievalError,
    RetrievalHit,
    RetrievalResult,
    VersionLeakageError,
    extract_query_years,
    sanitize_fts5_query,
    verify_version_isolation,
)


class _TopicEncoder:
    """Deterministic 6D topic encoder for offline retrieval and cosine oracle tests."""

    def __init__(self) -> None:
        self.model_name = "stub/topic-6d"
        self.dimension = 6
        self.queries_seen: list[str] = []

    def _encode_text(self, text: str) -> np.ndarray:
        lower = text.lower()
        vec = np.zeros(self.dimension, dtype=np.float32)
        if any(tok in lower for tok in ("mass", "weight", "798kg", "798 kg", "800kg", "800 kg")):
            vec[0] += 1.0
        if any(tok in lower for tok in ("wing", "flexibility", "deflect", "floor", "3.15.17")):
            vec[1] += 1.0
        if any(tok in lower for tok in ("podium", "ceremony", "parc ferme", "protocol")):
            vec[2] += 1.0
        if any(tok in lower for tok in ("pit", "speed", "80 km/h", "60 km/h", "34.7")):
            vec[3] += 1.0
        if any(tok in lower for tok in ("cooling", "duct", "power unit", "intake")):
            vec[4] += 1.0
        if float(np.linalg.norm(vec)) == 0.0:
            # Orthogonal noise dimension so unrelated queries score 0.0 against domain clauses.
            vec[5] = 1.0
        norm = float(np.linalg.norm(vec))
        return (vec / norm).astype(np.float32)

    def encode_passages(self, texts: Sequence[str]) -> np.ndarray:
        items = list(texts)
        if not items:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.vstack([self._encode_text(t) for t in items]).astype(np.float32)

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        items = list(texts)
        self.queries_seen.extend(items)
        if not items:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.vstack([self._encode_text(t) for t in items]).astype(np.float32)


class _MappingReranker:
    """Deterministic offline Reranker implementation scoring passages by substring rules."""

    def __init__(
        self,
        rules: Sequence[tuple[str, float]],
        *,
        default_score: float = -1.0,
        model_name: str = "stub/cross-encoder",
    ) -> None:
        self.model_name = model_name
        self._rules = list(rules)
        self._default_score = default_score
        self.calls: list[tuple[str, list[str]]] = []

    def score_pairs(self, query: str, passages: Sequence[str]) -> list[float]:
        passage_list = [str(p) for p in passages]
        self.calls.append((query, passage_list))
        scores: list[float] = []
        for passage in passage_list:
            matched = self._default_score
            for needle, val in self._rules:
                if needle in passage:
                    matched = val
                    break
            scores.append(float(matched))
        return scores


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_test_corpus_index(base_dir: Path) -> tuple[Path, _TopicEncoder]:
    """Create a multi-year Technical+Sporting corpus with amendment_preview and build index."""
    corpus_dir = base_dir / "corpus"
    processed_dir = base_dir / "processed"
    index_dir = base_dir / "index"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    shas = {
        "fia_2024_technical": "1" * 64,
        "fia_2025_technical": "2" * 64,
        "fia_2024_sporting": "3" * 64,
        "fia_2025_sporting": "4" * 64,
    }
    manifest_payload = {
        "manifest_version": 1,
        "corpus_name": "fia-f1-retrieve-test",
        "documents": [
            {
                "doc_id": "fia_2024_technical",
                "year": 2024,
                "doc_type": "technical",
                "issue": "Issue 8",
                "published_date": "2024-10-17",
                "title": "2024 Technical Regulations",
                "filename": "fia_2024_technical.pdf",
                "url": "https://example.com/2024_tech.pdf",
                "sha256": shas["fia_2024_technical"],
                "expected_bytes": 1000,
                "expected_pages": 3,
            },
            {
                "doc_id": "fia_2025_technical",
                "year": 2025,
                "doc_type": "technical",
                "issue": "Issue 3",
                "published_date": "2025-04-07",
                "title": "2025 Technical Regulations",
                "filename": "fia_2025_technical.pdf",
                "url": "https://example.com/2025_tech.pdf",
                "sha256": shas["fia_2025_technical"],
                "expected_bytes": 1000,
                "expected_pages": 2,
            },
            {
                "doc_id": "fia_2024_sporting",
                "year": 2024,
                "doc_type": "sporting",
                "issue": "Issue 7",
                "published_date": "2024-07-31",
                "title": "2024 Sporting Regulations",
                "filename": "fia_2024_sporting.pdf",
                "url": "https://example.com/2024_sport.pdf",
                "sha256": shas["fia_2024_sporting"],
                "expected_bytes": 1000,
                "expected_pages": 2,
            },
            {
                "doc_id": "fia_2025_sporting",
                "year": 2025,
                "doc_type": "sporting",
                "issue": "Issue 5",
                "published_date": "2025-04-30",
                "title": "2025 Sporting Regulations",
                "filename": "fia_2025_sporting.pdf",
                "url": "https://example.com/2025_sport.pdf",
                "sha256": shas["fia_2025_sporting"],
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
            chunk_id="fia_2024_technical::normative::3.15.17",
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            article="Article 3",
            clause_id="3.15.17",
            parent_clause_id="3.15",
            breadcrumb="Article 3 > 3.15 Aerodynamic Flexibility > 3.15.17",
            text="Upper rear wing flexibility under load must not deflect more than 5mm.",
            tables_markdown=(),
            cross_refs=("Article 3.15.1",),
            page_start=1,
            page_end=1,
            section_kind="normative",
            has_diagram=False,
            source_sha256=shas["fia_2024_technical"],
        ),
        ClauseRecord(
            chunk_id="fia_2024_technical::normative::4.1",
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            article="Article 4",
            clause_id="4.1",
            parent_clause_id=None,
            breadcrumb="Article 4 > 4.1 Minimum mass",
            text="The minimum mass of the car, without fuel, must not be less than 798kg.",
            tables_markdown=("| Parameter | Limit |\n| --- | --- |\n| Minimum mass | 798 kg |",),
            cross_refs=("Sporting Regulations",),
            page_start=2,
            page_end=2,
            section_kind="normative",
            has_diagram=False,
            source_sha256=shas["fia_2024_technical"],
        ),
        ClauseRecord(
            chunk_id="fia_2024_technical::normative::5.1.1",
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            article="Article 5",
            clause_id="5.1.1",
            parent_clause_id="5.1",
            breadcrumb="Article 5 > 5.1 Power Unit > 5.1.1",
            text="Cooling ducts and power unit intake dimensions are strictly regulated.",
            tables_markdown=(),
            cross_refs=(),
            page_start=2,
            page_end=2,
            section_kind="normative",
            has_diagram=True,
            source_sha256=shas["fia_2024_technical"],
        ),
        ClauseRecord(
            chunk_id="fia_2024_technical::amendment_preview::App.6",
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            article="Appendix 6",
            clause_id="App.6",
            parent_clause_id=None,
            breadcrumb="Appendix 6 > Approved Changes for Future Seasons",
            text=(
                "Approved draft amendment: minimum mass increases to 800kg and "
                "maximum floor width is 2000mm."
            ),
            tables_markdown=(),
            cross_refs=(),
            page_start=3,
            page_end=3,
            section_kind="amendment_preview",
            has_diagram=False,
            source_sha256=shas["fia_2024_technical"],
        ),
        ClauseRecord(
            chunk_id="fia_2025_technical::normative::3.15.17",
            doc_id="fia_2025_technical",
            year=2025,
            doc_type="technical",
            article="Article 3",
            clause_id="3.15.17",
            parent_clause_id="3.15",
            breadcrumb="Article 3 > 3.15 Aerodynamic Flexibility > 3.15.17",
            text="Upper rear wing flexibility under load must not deflect more than 2mm.",
            tables_markdown=(),
            cross_refs=(),
            page_start=1,
            page_end=1,
            section_kind="normative",
            has_diagram=False,
            source_sha256=shas["fia_2025_technical"],
        ),
        ClauseRecord(
            chunk_id="fia_2025_technical::normative::4.1",
            doc_id="fia_2025_technical",
            year=2025,
            doc_type="technical",
            article="Article 4",
            clause_id="4.1",
            parent_clause_id=None,
            breadcrumb="Article 4 > 4.1 Minimum mass",
            text="The minimum mass of the car, without fuel, must not be less than 800kg.",
            tables_markdown=("| Parameter | Limit |\n| --- | --- |\n| Minimum mass | 800 kg |",),
            cross_refs=(),
            page_start=2,
            page_end=2,
            section_kind="normative",
            has_diagram=False,
            source_sha256=shas["fia_2025_technical"],
        ),
        ClauseRecord(
            chunk_id="fia_2024_sporting::normative::34.7",
            doc_id="fia_2024_sporting",
            year=2024,
            doc_type="sporting",
            article="Article 34",
            clause_id="34.7",
            parent_clause_id="34",
            breadcrumb="Article 34 > Pit Lane > 34.7",
            text="A pit lane speed limit of 80 km/h will be enforced throughout the event.",
            tables_markdown=(),
            cross_refs=(),
            page_start=1,
            page_end=1,
            section_kind="normative",
            has_diagram=False,
            source_sha256=shas["fia_2024_sporting"],
        ),
        ClauseRecord(
            chunk_id="fia_2024_sporting::appendix::App.3",
            doc_id="fia_2024_sporting",
            year=2024,
            doc_type="sporting",
            article="Appendix 3",
            clause_id="App.3",
            parent_clause_id=None,
            breadcrumb="Appendix 3 > Podium Ceremony",
            text="Podium ceremony procedures and parc ferme protocol after every Grand Prix.",
            tables_markdown=(),
            cross_refs=(),
            page_start=2,
            page_end=2,
            section_kind="appendix",
            has_diagram=False,
            source_sha256=shas["fia_2024_sporting"],
        ),
        ClauseRecord(
            chunk_id="fia_2025_sporting::normative::34.7",
            doc_id="fia_2025_sporting",
            year=2025,
            doc_type="sporting",
            article="Article 34",
            clause_id="34.7",
            parent_clause_id="34",
            breadcrumb="Article 34 > Pit Lane > 34.7",
            text=(
                "A pit lane speed limit of 80 km/h applies, reduced to 60 km/h at select circuits."
            ),
            tables_markdown=(),
            cross_refs=(),
            page_start=1,
            page_end=1,
            section_kind="normative",
            has_diagram=False,
            source_sha256=shas["fia_2025_sporting"],
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
            text="Updated 2025 podium ceremony and parc ferme post-race driver weigh-in.",
            tables_markdown=(),
            cross_refs=(),
            page_start=2,
            page_end=2,
            section_kind="appendix",
            has_diagram=False,
            source_sha256=shas["fia_2025_sporting"],
        ),
    ]

    pages = [
        CleanedPage(
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            page_num=1,
            section_kind="normative",
            text=(
                "Article 3.15.17 Upper rear wing flexibility under load "
                "must not deflect more than 5mm."
            ),
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2024_technical"],
        ),
        CleanedPage(
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            page_num=2,
            section_kind="normative",
            text=(
                "Article 4.1 Minimum mass of the car without fuel is 798kg. "
                "Article 5.1.1 Cooling ducts and power unit intake."
            ),
            tables_markdown=(),
            has_diagram=True,
            source_sha256=shas["fia_2024_technical"],
        ),
        CleanedPage(
            doc_id="fia_2024_technical",
            year=2024,
            doc_type="technical",
            page_num=3,
            section_kind="amendment_preview",
            text="Appendix 6 approved draft amendment: minimum mass 800kg and floor width 2000mm.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2024_technical"],
        ),
        CleanedPage(
            doc_id="fia_2025_technical",
            year=2025,
            doc_type="technical",
            page_num=1,
            section_kind="normative",
            text=(
                "Article 3.15.17 Upper rear wing flexibility under load "
                "must not deflect more than 2mm."
            ),
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2025_technical"],
        ),
        CleanedPage(
            doc_id="fia_2025_technical",
            year=2025,
            doc_type="technical",
            page_num=2,
            section_kind="normative",
            text="Article 4.1 Minimum mass of the car without fuel is 800kg.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2025_technical"],
        ),
        CleanedPage(
            doc_id="fia_2024_sporting",
            year=2024,
            doc_type="sporting",
            page_num=1,
            section_kind="normative",
            text="Article 34.7 Pit lane speed limit 80 km/h throughout the event.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2024_sporting"],
        ),
        CleanedPage(
            doc_id="fia_2024_sporting",
            year=2024,
            doc_type="sporting",
            page_num=2,
            section_kind="appendix",
            text="Appendix 3 Podium ceremony procedures and parc ferme protocol.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2024_sporting"],
        ),
        CleanedPage(
            doc_id="fia_2025_sporting",
            year=2025,
            doc_type="sporting",
            page_num=1,
            section_kind="normative",
            text="Article 34.7 Pit lane speed limit 80 km/h and 60 km/h at select circuits.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2025_sporting"],
        ),
        CleanedPage(
            doc_id="fia_2025_sporting",
            year=2025,
            doc_type="sporting",
            page_num=2,
            section_kind="appendix",
            text="Appendix 3 Updated 2025 podium ceremony and parc ferme protocol.",
            tables_markdown=(),
            has_diagram=False,
            source_sha256=shas["fia_2025_sporting"],
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
    summary = {
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "corpus_name": "fia-f1-retrieve-test",
        "documents_parsed": 4,
        "total_pages": len(pages),
        "total_clauses": len(clauses),
        "clauses_sha256": _sha256_bytes(clauses_bytes),
        "pages_sha256": _sha256_bytes(pages_bytes),
        "documents": [],
    }
    (processed_dir / "parse_summary.json").write_bytes(
        (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    encoder = _TopicEncoder()
    build_index(
        processed_dir=processed_dir,
        manifest_path=manifest_path,
        index_dir=index_dir,
        encoder=encoder,
        fixed_chunk_words=300,
        fixed_overlap_words=60,
    )
    return index_dir, encoder


@pytest.fixture()
def built_index(tmp_path: Path) -> tuple[Path, _TopicEncoder]:
    return _build_test_corpus_index(tmp_path)


def test_public_api_contract_and_no_eager_sentence_transformers_import() -> None:
    assert "sentence_transformers" not in sys.modules
    assert VALID_CONFIGS == (
        "bm25_fixed",
        "bm25_clause",
        "dense_clause",
        "hybrid_rrf",
        "hybrid_rerank",
    )
    assert DEFAULT_RRF_K == 60
    assert DEFAULT_RERANKER_MODEL == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert DEFAULT_RERANK_CANDIDATES == 20
    assert pytest.approx(0.82) == DEFAULT_MIN_DENSE_SCORE
    assert pytest.approx(0.01) == DEFAULT_MIN_BM25_SCORE
    assert pytest.approx(-1.5) == DEFAULT_MIN_RERANK_SCORE
    assert issubclass(RetrievalError, RuntimeError)
    assert issubclass(VersionLeakageError, RetrievalError)
    assert is_dataclass(RetrievalHit)
    assert is_dataclass(RetrievalResult)
    expected_all = {
        "RetrievalConfig",
        "VALID_CONFIGS",
        "DEFAULT_RRF_K",
        "DEFAULT_RERANKER_MODEL",
        "DEFAULT_RERANK_CANDIDATES",
        "DEFAULT_MIN_DENSE_SCORE",
        "DEFAULT_MIN_BM25_SCORE",
        "DEFAULT_MIN_RERANK_SCORE",
        "RetrievalError",
        "VersionLeakageError",
        "Reranker",
        "CrossEncoderReranker",
        "RetrievalHit",
        "RetrievalResult",
        "extract_query_years",
        "sanitize_fts5_query",
        "verify_version_isolation",
        "HybridRetriever",
    }
    assert set(retrieve_mod.__all__) == expected_all
    for symbol in retrieve_mod.__all__:
        assert hasattr(retrieve_mod, symbol)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What is the 2024 minimum mass?", (2024,)),
        ("Compare 2025 and 2024 wing flexibility in 2025.", (2024, 2025)),
        ("Maximum floor width is 2000mm and engine runs at 2000 rpm.", ()),
        ("Force of 2000 N, 2000kg, 2000Hz, 2000cm, 2000 kPa, 2000 bar, 2000 ms, 2000 km/h.", ()),
        ("Temperature 2000 C and 2000 deg, torque 2000 Nm, duration 2000 s, 2000 mph.", ()),
        ("Clause 12024 or 20240 should not match, but 2024/2025 should.", (2024, 2025)),
        ("2024 regulations with 2000mm wheelbase", (2024,)),
    ],
)
def test_extract_query_years_and_unit_exclusions(query: str, expected: tuple[int, ...]) -> None:
    assert extract_query_years(query) == expected


def test_sanitize_fts5_query_handles_clauses_and_injection_strings() -> None:
    assert sanitize_fts5_query("3.15.17 minimum mass") == '"3.15.17" OR "minimum" OR "mass"'
    assert sanitize_fts5_query("10.3.4") == '"10.3.4"'
    assert sanitize_fts5_query("() * ^ : --- !!!") == ""
    injected = sanitize_fts5_query("NOT () * ^ : NEAR/5 ---")
    assert injected == '"NOT" OR "NEAR" OR "5"'

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='porter unicode61')")
        con.execute("INSERT INTO docs(content) VALUES ('Article 3.15.17 minimum mass 798kg')")
        rows = con.execute(
            "SELECT content FROM docs WHERE docs MATCH ?",
            (injected,),
        ).fetchall()
        assert rows == []
        clause_rows = con.execute(
            "SELECT content FROM docs WHERE docs MATCH ?",
            (sanitize_fts5_query("3.15.17"),),
        ).fetchall()
        assert len(clause_rows) == 1
    finally:
        con.close()


def test_verify_version_isolation_and_explicit_sabotage_precondition(
    built_index: tuple[Path, _TopicEncoder],
) -> None:
    index_dir, encoder = built_index
    with HybridRetriever(index_dir, encoder=encoder, reranker=_MappingReranker([])) as retriever:
        res_2024 = retriever.retrieve("minimum mass", config="bm25_clause", year=2024, top_k=5)
        assert res_2024.abstained is False
        assert len(res_2024.hits) >= 1
        assert all(hit.year == 2024 for hit in res_2024.hits)

        # Clean hits must pass verification.
        verify_version_isolation(
            res_2024.hits,
            effective_year=2024,
            doc_type=None,
            allowed_section_kinds=("normative", "appendix"),
        )

        # Sabotage: inject a 2025 hit into the 2024 hit list and verify precondition before check.
        sabotaged_hit = replace(
            res_2024.hits[0],
            chunk_id="fia_2025_technical::normative::4.1",
            doc_id="fia_2025_technical",
            year=2025,
        )
        sabotaged_hits = [*res_2024.hits, sabotaged_hit]
        assert any(h.year == 2025 for h in sabotaged_hits), "Mutation precondition failed"
        with pytest.raises(VersionLeakageError):
            verify_version_isolation(
                sabotaged_hits,
                effective_year=2024,
                doc_type=None,
                allowed_section_kinds=("normative", "appendix"),
            )

        # Doc-type sabotage must also raise VersionLeakageError.
        wrong_doc_type_hit = replace(res_2024.hits[0], doc_type="sporting")
        with pytest.raises(VersionLeakageError):
            verify_version_isolation(
                [wrong_doc_type_hit],
                effective_year=2024,
                doc_type="technical",
                allowed_section_kinds=("normative", "appendix"),
            )

        # amendment_preview sabotage must raise VersionLeakageError.
        preview_hit = replace(res_2024.hits[0], section_kind="amendment_preview")
        with pytest.raises(VersionLeakageError):
            verify_version_isolation(
                [preview_hit],
                effective_year=2024,
                doc_type=None,
                allowed_section_kinds=("normative", "appendix"),
            )


@pytest.mark.parametrize("config", VALID_CONFIGS)
def test_version_and_doc_type_isolation_across_all_configs(
    built_index: tuple[Path, _TopicEncoder],
    config: RetrievalConfig,
) -> None:
    index_dir, encoder = built_index
    reranker = _MappingReranker([("4.1", 3.0), ("3.15.17", 2.0), ("34.7", 2.0), ("App.3", 1.5)])
    with HybridRetriever(index_dir, encoder=encoder, reranker=reranker) as retriever:
        r2024 = retriever.retrieve("minimum mass", config=config, year=2024, top_k=5)
        assert r2024.abstained is False
        assert r2024.requested_year == 2024
        assert r2024.effective_year == 2024
        assert len(r2024.hits) >= 1
        assert all(hit.year == 2024 for hit in r2024.hits)

        r2025 = retriever.retrieve("minimum mass", config=config, year=2025, top_k=5)
        assert r2025.abstained is False
        assert r2025.requested_year == 2025
        assert r2025.effective_year == 2025
        assert len(r2025.hits) >= 1
        assert all(hit.year == 2025 for hit in r2025.hits)

        # Implicit single year extracted from query when year=None.
        rimplicit = retriever.retrieve("2024 minimum mass", config=config, year=None, top_k=5)
        assert rimplicit.abstained is False
        assert rimplicit.requested_year is None
        assert rimplicit.effective_year == 2024
        assert len(rimplicit.hits) >= 1
        assert all(hit.year == 2024 for hit in rimplicit.hits)

        rsport = retriever.retrieve(
            "pit lane speed", config=config, doc_type="sporting", top_k=5
        )
        assert rsport.abstained is False
        assert rsport.requested_doc_type == "sporting"
        assert len(rsport.hits) >= 1
        assert all(hit.doc_type == "sporting" for hit in rsport.hits)


@pytest.mark.parametrize("config", VALID_CONFIGS)
def test_amendment_preview_isolation_across_all_configs(
    built_index: tuple[Path, _TopicEncoder],
    config: RetrievalConfig,
) -> None:
    index_dir, encoder = built_index
    reranker = _MappingReranker([("App.6", 5.0), ("800kg", 4.0), ("798kg", 2.0)])
    with HybridRetriever(index_dir, encoder=encoder, reranker=reranker) as retriever:
        excluded = retriever.retrieve(
            "800kg floor width",
            config=config,
            year=2024,
            top_k=5,
            include_amendment_preview=False,
        )
        assert "amendment_preview" not in excluded.allowed_section_kinds
        assert all(hit.section_kind != "amendment_preview" for hit in excluded.hits)
        assert all("App.6" not in hit.chunk_id for hit in excluded.hits)

        included = retriever.retrieve(
            "800kg floor width",
            config=config,
            year=2024,
            top_k=5,
            include_amendment_preview=True,
        )
        assert "amendment_preview" in included.allowed_section_kinds
        assert included.abstained is False
        assert any(hit.section_kind == "amendment_preview" for hit in included.hits)


def test_abstention_suite_and_multiyear_comparison(
    built_index: tuple[Path, _TopicEncoder],
) -> None:
    index_dir, encoder = built_index
    reranker = _MappingReranker([("Minimum mass", 3.0), ("798kg", 2.5), ("800kg", 2.5)])
    with HybridRetriever(index_dir, encoder=encoder, reranker=reranker) as retriever:
        # 1. empty_query
        for blank in ("", "   \t\n "):
            res_empty = retriever.retrieve(blank, config="hybrid_rrf")
            assert res_empty.abstained is True
            assert res_empty.abstention_reason == "empty_query"
            assert res_empty.hits == ()

        # 2. unsupported_year (explicit & implicit)
        for bad_year in (2023, 2026):
            res_bad_explicit = retriever.retrieve(
                "minimum mass", config="bm25_clause", year=bad_year
            )
            assert res_bad_explicit.abstained is True
            assert res_bad_explicit.abstention_reason == "unsupported_year"
            assert res_bad_explicit.hits == ()

        for q in (
            "What is the 2026 minimum mass?",
            "Compare 2023 and 2024 minimum mass",
        ):
            res_bad_implicit = retriever.retrieve(q, config="hybrid_rerank", year=None)
            assert res_bad_implicit.abstained is True
            assert res_bad_implicit.abstention_reason == "unsupported_year"
            assert res_bad_implicit.hits == ()

        # 3. conflicting_year_filter
        for q in (
            "What is the 2025 minimum mass?",
            "Compare 2024 and 2025 minimum mass",
        ):
            res_conflict = retriever.retrieve(q, config="dense_clause", year=2024)
            assert res_conflict.abstained is True
            assert res_conflict.abstention_reason == "conflicting_year_filter"
            assert res_conflict.hits == ()

        # 4. Multi-year comparison allowed when year=None and all years exist in corpus
        res_multi = retriever.retrieve(
            "Compare 2024 and 2025 minimum mass", config="hybrid_rrf", year=None, top_k=6
        )
        assert res_multi.abstained is False
        assert res_multi.abstention_reason is None
        assert res_multi.effective_year is None
        assert {h.year for h in res_multi.hits} == {2024, 2025}

        # 5. Measurement units like 2000mm / 2000 rpm must NOT trigger unsupported_year
        res_units = retriever.retrieve(
            "minimum mass with 2000mm wheelbase and 2000 rpm", config="bm25_clause", year=None
        )
        assert res_units.abstained is False
        assert res_units.effective_year is None

        # 6. no_matching_clauses (lexical miss & impossible doc_type)
        res_no_lex = retriever.retrieve("xylophone", config="bm25_clause")
        assert res_no_lex.abstained is True
        assert res_no_lex.abstention_reason == "no_matching_clauses"
        assert res_no_lex.hits == ()

        res_no_fixed = retriever.retrieve("xylophone", config="bm25_fixed")
        assert res_no_fixed.abstained is True
        assert res_no_fixed.abstention_reason == "no_matching_clauses"

        res_no_doc_type = retriever.retrieve(
            "minimum mass", config="dense_clause", doc_type="financial"
        )
        assert res_no_doc_type.abstained is True
        assert res_no_doc_type.abstention_reason == "no_matching_clauses"

        # 7. low_retrieval_score across dense, bm25, hybrid_rrf, hybrid_rerank
        res_low_dense = retriever.retrieve("quantum entanglement baking", config="dense_clause")
        assert res_low_dense.abstained is True
        assert res_low_dense.abstention_reason == "low_retrieval_score"
        assert res_low_dense.hits == ()

        res_low_rrf = retriever.retrieve("quantum entanglement baking", config="hybrid_rrf")
        assert res_low_rrf.abstained is True
        assert res_low_rrf.abstention_reason == "low_retrieval_score"

        res_low_bm25 = retriever.retrieve(
            "minimum mass", config="bm25_clause", min_bm25_score=999.0
        )
        assert res_low_bm25.abstained is True
        assert res_low_bm25.abstention_reason == "low_retrieval_score"

        low_reranker = _MappingReranker([], default_score=-9.0)
        with HybridRetriever(index_dir, encoder=encoder, reranker=low_reranker) as low_ret:
            res_low_rerank = low_ret.retrieve("minimum mass", config="hybrid_rerank")
            assert res_low_rerank.abstained is True
            assert res_low_rerank.abstention_reason == "low_retrieval_score"
            assert res_low_rerank.hits == ()


def test_bm25_positive_scores_and_fixed_chunk_shape(
    built_index: tuple[Path, _TopicEncoder],
) -> None:
    index_dir, encoder = built_index
    with HybridRetriever(index_dir, encoder=encoder, reranker=_MappingReranker([])) as retriever:
        fixed_res = retriever.retrieve("798kg", config="bm25_fixed", year=2024, top_k=3)
        assert fixed_res.abstained is False
        assert len(fixed_res.hits) >= 1
        top = fixed_res.hits[0]
        assert top.score > 0.0
        assert top.lexical_score == top.score
        assert top.dense_score is None
        assert top.rrf_score is None
        assert top.rerank_score is None
        assert top.rank == 1
        assert top.article == ""
        assert top.clause_id == ""
        assert top.parent_clause_id is None
        assert top.breadcrumb == ""
        assert top.tables_markdown == ()
        assert top.cross_refs == ()
        assert top.has_diagram is False

        clause_res = retriever.retrieve("3.15.17", config="bm25_clause", year=2025, top_k=3)
        assert clause_res.abstained is False
        assert clause_res.hits[0].chunk_id == "fia_2025_technical::normative::3.15.17"
        assert clause_res.hits[0].score > 0.0
        assert clause_res.hits[0].article == "Article 3"
        assert clause_res.hits[0].clause_id == "3.15.17"


def test_oracle_dense_cosine_similarity_and_tie_breaking(
    built_index: tuple[Path, _TopicEncoder],
) -> None:
    index_dir, encoder = built_index
    query = "rear wing flexibility"
    q_vec = encoder.encode_queries([query])[0]

    con = sqlite3.connect(index_dir / "index.sqlite")
    try:
        rows = con.execute(
            """
            SELECT chunk_id, year, doc_type, section_kind, embedding_blob
            FROM chunks_clause
            WHERE section_kind IN ('normative', 'appendix')
            """
        ).fetchall()
    finally:
        con.close()

    oracle_scores: list[tuple[float, str]] = []
    for chunk_id, _yr, _dt, _sk, blob in rows:
        vec = np.frombuffer(blob, dtype="<f4")
        cos = float(np.dot(vec, q_vec))
        oracle_scores.append((cos, chunk_id))
    oracle_ranked = sorted(oracle_scores, key=lambda item: (-item[0], item[1]))

    with HybridRetriever(index_dir, encoder=encoder, reranker=_MappingReranker([])) as retriever:
        res = retriever.retrieve(query, config="dense_clause", top_k=4)
        assert res.abstained is False
        assert [h.chunk_id for h in res.hits] == [cid for _, cid in oracle_ranked[:4]]
        for idx, (hit, (expected_cos, _)) in enumerate(
            zip(res.hits, oracle_ranked[:4], strict=True), 1
        ):
            assert hit.rank == idx
            assert hit.score == pytest.approx(expected_cos, abs=1e-5)
            assert hit.dense_score == pytest.approx(expected_cos, abs=1e-5)
            assert hit.lexical_score is None


def test_oracle_hybrid_rrf_and_hybrid_rerank(
    built_index: tuple[Path, _TopicEncoder],
) -> None:
    index_dir, encoder = built_index
    query = "minimum mass 798kg"

    # Reranker deliberately favors 2025 4.1 over 2024 4.1 to verify reranking changes order.
    reranker = _MappingReranker(
        [
            ("800kg", 4.5),
            ("798kg", 2.0),
            ("3.15.17", -1.0),
        ],
        default_score=-2.0,
    )

    with HybridRetriever(index_dir, encoder=encoder, reranker=reranker) as retriever:
        lex_res = retriever.retrieve(query, config="bm25_clause", top_k=100)
        dense_res = retriever.retrieve(query, config="dense_clause", top_k=100)
        rrf_res = retriever.retrieve(query, config="hybrid_rrf", top_k=5)
        rerank_res = retriever.retrieve(
            query, config="hybrid_rerank", top_k=3, rerank_candidates=4
        )

    lex_ranks = {hit.chunk_id: hit.rank for hit in lex_res.hits}
    lex_scores = {hit.chunk_id: hit.score for hit in lex_res.hits}
    dense_ranks = {hit.chunk_id: hit.rank for hit in dense_res.hits}
    dense_scores = {hit.chunk_id: hit.score for hit in dense_res.hits}

    oracle_rrf: list[tuple[float, str]] = []
    for cid in set(lex_ranks) | set(dense_ranks):
        score = 0.0
        if cid in lex_ranks:
            score += 1.0 / (DEFAULT_RRF_K + lex_ranks[cid])
        if cid in dense_ranks:
            score += 1.0 / (DEFAULT_RRF_K + dense_ranks[cid])
        oracle_rrf.append((score, cid))
    oracle_rrf.sort(key=lambda item: (-item[0], item[1]))

    assert rrf_res.abstained is False
    assert [h.chunk_id for h in rrf_res.hits] == [cid for _, cid in oracle_rrf[:5]]
    for hit, (expected_rrf, cid) in zip(rrf_res.hits, oracle_rrf[:5], strict=True):
        assert hit.score == pytest.approx(expected_rrf, abs=1e-9)
        assert hit.rrf_score == pytest.approx(expected_rrf, abs=1e-9)
        assert hit.lexical_score == (
            pytest.approx(lex_scores[cid], abs=1e-9) if cid in lex_scores else None
        )
        assert hit.dense_score == (
            pytest.approx(dense_scores[cid], abs=1e-5) if cid in dense_scores else None
        )
        assert hit.rerank_score is None

    assert rerank_res.abstained is False
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0][1]) == 4
    assert rerank_res.hits[0].chunk_id == "fia_2025_technical::normative::4.1"
    assert rerank_res.hits[0].rerank_score == pytest.approx(4.5)
    assert rerank_res.hits[0].score == pytest.approx(4.5)
    assert rerank_res.hits[0].rrf_score is not None


@pytest.mark.parametrize("config", VALID_CONFIGS)
def test_fts5_injection_resilience_across_all_configs(
    built_index: tuple[Path, _TopicEncoder],
    config: RetrievalConfig,
) -> None:
    index_dir, encoder = built_index
    reranker = _MappingReranker([], default_score=0.5)
    with HybridRetriever(index_dir, encoder=encoder, reranker=reranker) as retriever:
        for hostile in ("NOT () * ^ : NEAR/5 ---", '"unterminated ( [ *', "OR AND NEAR/2"):
            res = retriever.retrieve(hostile, config=config)
            assert isinstance(res, RetrievalResult)


def test_tampered_index_rejection_and_closed_guard(
    built_index: tuple[Path, _TopicEncoder],
    tmp_path: Path,
) -> None:
    index_dir, encoder = built_index
    tampered_dir = tmp_path / "tampered_index"
    shutil.copytree(index_dir, tampered_dir)

    con = sqlite3.connect(tampered_dir / "index.sqlite")
    try:
        con.execute(
            "UPDATE chunks_clause SET year = 2025 "
            "WHERE chunk_id = 'fia_2024_technical::normative::4.1'"
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(IndexIntegrityError):
        HybridRetriever(tampered_dir, encoder=encoder, verify_integrity=True)

    retriever = HybridRetriever(index_dir, encoder=encoder, reranker=_MappingReranker([]))
    retriever.close()
    with pytest.raises(RetrievalError):
        retriever.retrieve("minimum mass", config="bm25_clause")


def test_invalid_inputs_and_cross_encoder_reranker_lazy_import(
    built_index: tuple[Path, _TopicEncoder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_dir, encoder = built_index
    with HybridRetriever(index_dir, encoder=encoder, reranker=_MappingReranker([])) as retriever:
        with pytest.raises(RetrievalError):
            retriever.retrieve("minimum mass", config="unknown_mode")  # type: ignore[arg-type]
        with pytest.raises(RetrievalError):
            retriever.retrieve("minimum mass", config="bm25_clause", top_k=0)

    loaded: list[str] = []

    class _FakeCrossEncoder:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            loaded.append(model_name)

        def predict(self, pairs: list[tuple[str, str]], **kwargs: Any) -> np.ndarray:
            return np.array([1.25 for _ in pairs], dtype=np.float32)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.CrossEncoder = _FakeCrossEncoder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    reranker = CrossEncoderReranker(device="cpu")
    assert isinstance(reranker, Reranker)
    assert loaded == []
    assert reranker.score_pairs("mass", []) == []
    assert loaded == []
    scores = reranker.score_pairs("mass", ["passage a", "passage b"])
    assert loaded == [DEFAULT_RERANKER_MODEL]
    assert scores == pytest.approx([1.25, 1.25])

    with pytest.raises(ValueError):
        CrossEncoderReranker(model_name="   ")
    with pytest.raises(ValueError):
        CrossEncoderReranker(batch_size=0)


def test_runtime_sabotage_tie_break_and_verify_integrity_false(
    built_index: tuple[Path, _TopicEncoder],
    tmp_path: Path,
) -> None:
    index_dir, encoder = built_index

    # verify_integrity=False succeeds on valid index and raises RetrievalError on missing dir.
    with HybridRetriever(
        index_dir, encoder=encoder, reranker=_MappingReranker([]), verify_integrity=False
    ) as fast_ret:
        res_bm25_low = fast_ret.retrieve("798kg", config="bm25_fixed", min_bm25_score=999.0)
        assert res_bm25_low.abstained is True
        assert res_bm25_low.abstention_reason == "low_retrieval_score"

        # Deterministic tie-breaking on (-score, chunk_id) when reranker assigns equal scores.
        tied_reranker = _MappingReranker([], default_score=1.0)
        with HybridRetriever(
            index_dir, encoder=encoder, reranker=tied_reranker, verify_integrity=False
        ) as tied_ret:
            tied_res = tied_ret.retrieve("minimum mass", config="hybrid_rerank", top_k=5)
            assert tied_res.abstained is False
            cids = [h.chunk_id for h in tied_res.hits]
            assert cids == sorted(cids)

        # Runtime sabotage inside cached clauses must trigger VersionLeakageError from retrieve().
        sabotaged_clauses = list(fast_ret._clauses)
        target_idx = fast_ret._clause_idx_by_id["fia_2024_technical::normative::4.1"]
        assert sabotaged_clauses[target_idx].year == 2024
        sabotaged_clauses[target_idx] = replace(sabotaged_clauses[target_idx], year=2025)
        fast_ret._clauses = tuple(sabotaged_clauses)
        with pytest.raises(VersionLeakageError):
            fast_ret.retrieve("798kg", config="bm25_clause", year=2024)

    with pytest.raises(RetrievalError):
        HybridRetriever(tmp_path / "missing_index", verify_integrity=False)

    with pytest.raises(RetrievalError):
        HybridRetriever(index_dir, rrf_k=0)
    with pytest.raises(RetrievalError):
        HybridRetriever(index_dir, rerank_candidates=0)
    with pytest.raises(RetrievalError):
        HybridRetriever(index_dir, encoder="not_an_encoder")  # type: ignore[arg-type]
    with pytest.raises(RetrievalError):
        HybridRetriever(index_dir, reranker="not_a_reranker")  # type: ignore[arg-type]

