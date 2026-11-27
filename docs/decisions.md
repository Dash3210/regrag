# Decisions

## ADR-001 — Makefile: POSIX-first common subset with quoted forward-slash paths
- Status: accepted
- Date: 2026-11-21
- Context: Local development runs on Windows where Make recipes may execute under `cmd.exe` or MSYS2/Git Bash `sh.exe`, while CI executes under `/bin/sh` on Linux. Paths may also contain spaces (e.g. `C:\Program Files\Python310\python.exe`). Unquoted forward-slash paths fail in `cmd.exe`, whereas unquoted backslashes are stripped by POSIX shells.
- Decision:
  1. Maintain a single POSIX-first `Makefile` using an `ifeq ($(OS),Windows_NT)` switch to set default `PYTHON` and `PY` paths (`PYTHON ?= python`, `PY ?= .venv/Scripts/python.exe` on Windows; `PYTHON ?= python3`, `PY ?= .venv/bin/python` elsewhere).
  2. Use forward slashes exclusively in interpreter paths and double-quote every `"$(PYTHON)"` and `"$(PY)"` expansion across all recipes.
  3. Restrict recipe bodies to the `cmd.exe`/`/bin/sh` common syntax subset and delegate conditional or destructive logic (venv creation, cleanup) to Python one-liners.
- Consequences: `make install`, `make test`, `make lint`, `make typecheck`, `make eval-integrity`, and `make check` run identically on local Windows workstations and Linux CI runners without PowerShell wrappers or task-runner indirection.
- Alternatives rejected: PowerShell build scripts (non-portable to Linux CI), Docker-wrapping every local target (slow iteration loop; containerisation arrives in P5), third-party task runners (extra dependency when Make is standard).

## ADR-002 — PDF parsing library: pdfplumber (MIT)
- Status: accepted
- Date: 2026-11-21
- Context: Regulatory documents (multi-year technical and sporting regulations) rely heavily on nested clause numbering, structured numeric-limit tables, running headers/footers, and revision markup. The parser library must extract both layout-preserving text and tabular boundaries reliably under a permissive license compatible with `regrag`'s MIT license.
- Decision: Use `pdfplumber>=0.11,<1` (MIT license, built on `pdfminer.six`) as the sole PDF parsing library. Evaluated via `clm_decide`:
  - Selected: `option_a_pdfplumber_mit` (confidence `0.9719`)
  - Candidate probabilities: `option_a_pdfplumber_mit=0.9789`, `option_d_pdfplumber_plus_pypdf_fallback=0.0201`, `option_b_pymupdf_agpl=0.0007`, `option_c_pypdf_only=0.0003`.
- Consequences: Permissive MIT licensing is preserved end-to-end; character-level coordinates, colors/lines, and `page.extract_tables()` are available directly for P1 clause/table parsing and revision-artifact filtering without introducing copyleft licensing risk or dual-parser maintenance burden.
- Alternatives rejected:
  - PyMuPDF / `fitz` (`option_b_pymupdf_agpl`, `0.0007`): AGPL-3.0 license is incompatible with an MIT-licensed codebase without purchasing a commercial license.
  - `pypdf` only (`option_c_pypdf_only`, `0.0003`): lacks native coordinate-aware table extraction required for numeric-limit regulatory tables.
  - `pdfplumber` + `pypdf` fallback (`option_d_pdfplumber_plus_pypdf_fallback`, `0.0201`): adds second parser surface area and inconsistent extraction semantics before any corpus failure justifies it.

## ADR-003 — Core dependency contract declared upfront in pyproject.toml
- Status: accepted
- Date: 2026-11-21
- Context: `regrag` spans PDF ingestion (P1), local hybrid lexical+dense retrieval (P2), offline evaluation (P3), and FastAPI serving with Prometheus metrics (P4). Declaring `sentence-transformers` and the rest of the runtime stack in P0 versus deferring ML dependencies to a P2 optional extra trades earlier installation time against verifying environment compatibility and avoiding later dependency churn.
- Decision: Declare the full runtime dependency stack upfront in `[project].dependencies` in `pyproject.toml`: `requests>=2.31,<3`, `pdfplumber>=0.11,<1`, `numpy>=2.0,<3`, `sentence-transformers>=3.0,<6`, `pydantic>=2.7,<3`, `fastapi>=0.110,<1`, `uvicorn>=0.30,<1`, and `prometheus-client>=0.20,<1`. Explicitly exclude LangChain, LlamaIndex, AGPL PDF libraries, and paid vector database SaaS SDKs. Evaluated via `clm_decide`:
  - Selected: `option_b_include_sentence_transformers_in_p0` (confidence `0.6741`)
  - Candidate probabilities: `option_b_include_sentence_transformers_in_p0=0.8370`, `option_a_lightweight_core_plus_p2_ml_extra=0.1630`.
- Consequences: `make install` validates the complete runtime dependency graph (including `pdfplumber`, `sentence-transformers`, `fastapi`, and `TestClient`) in P0 on the target Python 3.10+ environment, eliminating late-phase wheel or transitive-dependency surprises in P2 and P4.
- Dev-dependency note (`httpx2`): Verified against installed `fastapi==0.162.5` / `starlette==1.7.0` that `starlette.testclient` imports `httpx2 as httpx` (both under `TYPE_CHECKING` and as its primary runtime dependency). Declared `httpx2>=2.14,<3` directly in `[project.optional-dependencies].dev` instead of `httpx>=0.27,<1` so `from fastapi.testclient import TestClient` and `mypy` type-checking do not rely on a transitive dependency.
- Alternatives rejected:
  - Lightweight core with a deferred P2 ML optional extra (`option_a_lightweight_core_plus_p2_ml_extra`, `0.1630`): defers real dependency resolution risk until P2 and fragments install instructions across phases.
  - High-level RAG orchestration frameworks (LangChain, LlamaIndex) or hosted vector DB SaaS: obscure retrieval/citation mechanics and violate the inspectable `$0` local-first design.

## ADR-004 — Review scope: working-tree pre-commit review discipline
- Status: accepted
- Date: 2026-11-21
- Context: Phase quality gates run before commits are authored so defects are caught prior to entering git history. Treating uncommitted or unpushed repository state as a blocking review finding creates a circular deadlock where a phase cannot be committed until review passes, yet review fails because the phase is uncommitted.
- Decision:
  1. Reviewers of any phase gate assess the working tree only.
  2. Repository publication state — git commit status, push status, remote CI green status, or deployment — is explicitly out of scope for every pre-commit phase gate and can never be a blocking finding there (it may be noted as an observation).
  3. Post-gate commit, push, and fresh-clone verification happen strictly downstream after the pre-commit gate closes.
- Consequences: Pre-commit phase gates evaluate the actual code, documentation, and verification evidence cleanly without circular release deadlocks.
- Alternatives rejected:
  - Blocking pre-commit reviews on git commit/push/CI status (rejected: creates an unresolvable circular dependency).
  - Overriding reviewer verdicts manually to break the deadlock (rejected: undermines the integrity of independent review gates).

## ADR-005 — Corpus selection: FIA Formula 1 Regulations
- Status: accepted
- Date: 2026-11-21
- Context: `regrag` requires a publicly downloadable, multi-year regulatory corpus containing nested clause hierarchies, numeric-limit tables, cross-references, and concrete year-on-year rule changes to stress-test version-isolated retrieval and citation grounding without authentication walls or scraping fragility.
- Decision: Select **FIA Formula 1 Regulations** (`fia_f1_regulations`) as the project corpus, starting with the 2024 and 2025 Technical Regulations verified in [docs/p0-corpus-spike.md](p0-corpus-spike.md) (with 2024–2026 Sporting and Technical editions catalogued for P1 manifest planning). Evaluated via `clm_decide`:
  - Selected: `fia_f1_regulations` (confidence `0.3973`)
  - Candidate probabilities: `fia_f1_regulations=0.6987`, `mcc_icc_cricket_fallback=0.3013`.
- Verified P0 PDF Provenance (unauthenticated HTTP 200 `application/pdf`, stored in gitignored `data/raw/`):
  - **2024 FIA Formula 1 Technical Regulations — Issue 8 (2024-10-17)**: 178 pages, 2,247,377 bytes, SHA-256 `a940dd6c01b058c17f8da5392fb20b497b8967848c1c62905453b955b6d58c18`.
  - **2025 FIA Formula 1 Technical Regulations — Issue 3 (2025-04-07)**: 179 pages, 2,242,504 bytes, SHA-256 `454b76e2b388e61db50c0d116ff59477848a0b7e081f38142242808213dc0826`.
- Measured 30-Clause Raw `pdfplumber==0.11.10` Baseline (15 clauses from 2024 + 15 clauses from 2025, including 8 matched year-on-year changed clause pairs):
  - `PASS`: `19/30` (`63.3%`) — 2024: `9/15`, 2025: `10/15`
  - `PARTIAL`: `6/30` (`20.0%`) — 2024: `4/15`, 2025: `2/15`
  - `FAIL`: `5/30` (`16.7%`) — 2024: `2/15`, 2025: `3/15`
- Documented Parser Quirks & Concrete P1 Mitigations:
  1. **Revision strikethrough & colored amendment text** (2025 `3.15.17` `"0.5mm 2mm"`, 2025 `8.14` `"Medical Impact"`, 2024 Appendix 6 `"798kg 800kg"`): Apply a pre-extraction `page.filter()` removing characters overlapping thin horizontal strikethrough `rects`/`lines` (`height < 2.0pt`, midpoint within `±2.8pt`, covering narrow single-character/digit deletions down to `width >= 2pt`, e.g. `2.52pt` observed on 2024 p.178, rather than assuming `width > 4pt`).
  2. **Running footers & Appendix 6 future-year amendment previews**: Strip running 2-line FIA footers anywhere on the page (handling out-of-order PDF streams such as 2024 Appendix 3 p.160–164, or cropping `top > page.height - 65`) and isolate or exclude Appendix 6 (`"APPROVED CHANGES FOR 2025"`, e.g. `doc_section=amendment_preview`) so next-season draft values (`800kg`, `82kg`) never contaminate prior-year queries.
  3. **Coordinate subscripts & superscripts splitting across lines** (`X_A`, `X_F`, `X_R`, `X_C`, `CO_2`): Tune `extract_text(y_tolerance=6)` or merge small-font (`size < body_size * 0.85`) subscript tokens with the nearest preceding word within `|delta_y| <= 6pt` while guarding against merging separate bullet lines.
  4. **Multi-page & rotated tables (`Appendix 3`, `Appendix 5`)**: Stitch consecutive pages sharing table headers, append orphan continuation rows (empty `Ref.`/`Component` cells) onto the prior page's final row, and parse `Appendix 3` using explicit column x-intervals (`explicit_vertical_lines`) and canonical column headers.
  5. **Nested bullet continuity across page breaks & raster diagrams (`5.3.2`)**: Run a hierarchical clause state machine (`Article N` -> `N.M` -> `N.M.K` -> `a.`/`i.`) over the footer-stripped, page-stitched stream, and emit structured `[DIAGRAM_IMAGE: ...]` metadata markers when referenced diagrams are embedded raster images (`len(page.images) > 0`).
- Consequences: P1–P7 target a real, unauthenticated multi-year corpus with verified version-confusion deltas and empirically characterized PDF extraction hazards before writing parser code.
- Alternatives rejected:
  - `mcc_icc_cricket_fallback` (`0.3013`): MCC Laws of Cricket + ICC Playing Conditions was designated solely as a fallback in case `fia.com` blocked automated downloads; unnecessary once both official FIA PDFs downloaded cleanly with HTTP 200 and demonstrated dense numeric tables and year-on-year rule changes.

## ADR-006 — Corpus scope: 4 FIA PDFs (2024 & 2025 Technical + Sporting)
- Status: accepted
- Date: 2026-11-21
- Context: Expanding from the 2-PDF P0 spike to the full P1 corpus requires balancing cross-document retrieval realism (Technical <-> Sporting cross-references and year-on-year rule deltas) against deterministic parsing structure, offline CPU evaluation latency, and reserving an out-of-corpus season for abstention testing.
- Decision: Lock `corpus/manifest.json` (`fia-f1-regulations`, `manifest_version=1`) to **4 verified FIA Formula 1 PDFs** covering 2024 & 2025 Technical and Sporting Regulations (583 pages total). Evaluated via `clm_decide`:
  - Selected: `option_a_4_pdfs_2024_2025_tech_sporting` (confidence `0.3434`)
  - Candidate probabilities: `option_a_4_pdfs_2024_2025_tech_sporting=0.5623`, `option_b_2_pdfs_tech_only=0.4058`, `option_c_6_pdfs_2024_2025_2026=0.0319`.
- Verified Manifest Provenance (`corpus/manifest.json`, gitignored `data/raw/`):
  - `fia_2024_technical` (`Issue 8`, `2024-10-17`): 2,247,377 bytes, 178 pages, SHA-256 `a940dd6c01b058c17f8da5392fb20b497b8967848c1c62905453b955b6d58c18`.
  - `fia_2025_technical` (`Issue 3`, `2025-04-07`): 2,242,504 bytes, 179 pages, SHA-256 `454b76e2b388e61db50c0d116ff59477848a0b7e081f38142242808213dc0826`.
  - `fia_2024_sporting` (`Issue 7`, `2024-07-31`): 1,713,401 bytes, 107 pages, SHA-256 `82f8048dcc2c86c26e8d69321f2700ea9b274346e28bade43815dea31a3993a3`.
  - `fia_2025_sporting` (`Issue 5`, `2025-04-30`): 1,294,826 bytes, 119 pages, SHA-256 `525eef22a60f0755a4468281dd7c78c5ea5bd0eec38dc1b09cc31a0c0132854e`.
- Rationale & Consequences:
  1. Preserves consistent `Article N.M.K` clause hierarchy across both seasons and both document families while exercising genuine Technical <-> Sporting cross-references.
  2. Keeps full corpus parsing (~78s on CPU), indexing, and offline evaluation fast on standard developer hardware.
  3. Keeps 2026 regulations out of the indexed corpus so P2/P3 evaluation can test true missing-year abstention ("What is the 2026 minimum weight?") without synthetic redaction.
- Alternatives rejected:
  - `option_b_2_pdfs_tech_only` (`0.4058`): omits Sporting Regulations, forfeiting real cross-document references (`Sporting Regulations` / `Technical Regulations`) and multi-doc-type filtering.
  - `option_c_6_pdfs_2024_2025_2026` (`0.0319`): 2026 introduces a restructured regulatory taxonomy and removes the clean missing-year abstention test target while increasing CPU indexing time.

## ADR-007 — Parsed corpus contract: clauses.jsonl + pages.jsonl + parse_summary.json under gitignored data/processed/
- Status: accepted
- Date: 2026-11-21
- Context: Phase P1 produces parsed text from raw PDFs, while Phase P2 builds both a clause-aware retrieval index and a fixed-size chunk baseline for P3 ablation. If the two chunking strategies operated on differently cleaned text streams, or if P1 coupled directly to P2's SQLite schema, ablation comparisons would be confounded and phase boundaries blurred.
- Decision: Emit three deterministic, atomically written files under gitignored `data/processed/`:
  1. `clauses.jsonl`: one sorted-key JSON record per `ClauseRecord` with unique `chunk_id`, hierarchical breadcrumb, Markdown tables, extracted cross-references, `section_kind` (`normative` / `appendix` / `amendment_preview`), `has_diagram`, and `source_sha256`.
  2. `pages.jsonl`: one sorted-key JSON record per `CleanedPage` produced by the exact same footer-stripped, non-black-strikethrough-filtered, subscript-repaired pipeline.
  3. `parse_summary.json`: per-document and aggregate page/clause/table/diagram statistics plus deterministic SHA-256 digests of `clauses.jsonl`, `pages.jsonl`, and `corpus/manifest.json`.
  Evaluated via `clm_decide`:
  - Selected: `option_a_jsonl_clause_and_doc_stream` (confidence `0.7619`)
  - Candidate probabilities: `option_a_jsonl_clause_and_doc_stream=0.8412`, `option_b_clauses_jsonl_only=0.1382`, `option_c_sqlite_directly=0.0206`.
- Rationale & Consequences:
  1. **Fair fixed-vs-clause ablation**: P2's fixed-chunk baseline consumes `pages.jsonl` while the clause-aware index consumes `clauses.jsonl`, guaranteeing both share identical text cleaning and strikethrough filtering.
  2. **Clean P1/P2 boundary**: P1 owns deterministic PDF-to-JSONL normalization under gitignored `data/processed/`; P2 owns SQLite FTS5 and dense vector indexing under `data/index/`.
  3. **Inspectable determinism**: Repeat runs of `regrag parse` produce bitwise-identical `clauses.jsonl` and `pages.jsonl` SHA-256 digests.
- Alternatives rejected:
  - `option_b_clauses_jsonl_only` (`0.1382`): forces P2's fixed-chunk baseline either to re-parse PDFs (duplicating cleaning logic) or concatenate clause chunks (confounding clause boundaries into the baseline).
  - `option_c_sqlite_directly` (`0.0206`): couples P1 parsing to P2 storage schema and breaks human-inspectable line-oriented JSONL diffs.

## ADR-008 — CPU dense embedding model: `intfloat/e5-small-v2` (MIT)
- Status: accepted
- Date: 2026-11-27
- Context: Phase P2 requires a compact, permissively licensed dense text embedding model that runs deterministically on local CPU hardware without GPU dependencies or paid APIs, while preserving high retrieval accuracy on asymmetric regulatory questions versus dense technical clauses and numeric-limit tables.
- Decision: Standardize on **`intfloat/e5-small-v2`** (33M parameters, 384-dimensional embeddings, MIT license) using L2-normalized embeddings and E5's asymmetric `"passage: "` (index time) and `"query: "` (query time) prefixes, wrapped behind an injectable `DenseEncoder` protocol so unit tests run completely offline without model downloads. Evaluated via `clm_decide`:
  - Selected: `option_c_e5_small_v2` (confidence `0.8816`)
  - Candidate probabilities: `option_c_e5_small_v2=0.9211`, `option_b_all_minilm_l6_v2=0.0694`, `option_a_bge_small_en_v1_5=0.0095`.
- Consequences:
  1. Permissive MIT licensing matches `regrag` end-to-end while keeping the dense model footprint compact (~33M params, 384 dims) for fast local CPU indexing and query latency.
  2. Asymmetric `"passage: "` / `"query: "` prefixing aligns short regulatory queries with longer multi-sentence clauses and Markdown tables, and L2 normalization makes cosine similarity equivalent to a single dot product.
  3. Isolating encoding behind the injectable `DenseEncoder` protocol keeps the unit test suite deterministic, fast, and 100% offline.
- Alternatives rejected:
  - `sentence-transformers/all-MiniLM-L6-v2` (`option_b_all_minilm_l6_v2`, `0.0694`): smaller 22M symmetric encoder with weaker asymmetric retrieval precision on dense technical prose.
  - `BAAI/bge-small-en-v1.5` (`option_a_bge_small_en_v1_5`, `0.0095`): strong alternative, but `intfloat/e5-small-v2` was preferred by `clm_decide` for asymmetric passage/query retrieval balance and established CPU behavior.

## ADR-009 — Hybrid retrieval fusion and CPU reranking: RRF (`k=60`) + `cross-encoder/ms-marco-MiniLM-L-6-v2`
- Status: accepted
- Date: 2026-11-27
- Context: Regulatory queries mix exact clause/identifier lookups (e.g., `Article 4.1`, `798kg`, `X_A`) where lexical BM25 excels with semantic paraphrases where dense retrieval excels. Combining incompatible BM25 and cosine score scales directly via linear weights is brittle across queries, while P3 ablation requires cleanly separating fixed-chunk, clause-chunk, lexical, dense, hybrid, and reranked retrieval modes alongside strict version/section isolation and reliable abstention.
- Decision: Implement Reciprocal Rank Fusion (**RRF**, `k=60`, equal lexical and dense weights `1.0`) across **5 retrieval configurations** (`bm25_fixed`, `bm25_clause`, `dense_clause`, `hybrid_rrf`, `hybrid_rerank`), paired with an optional CPU CrossEncoder **`cross-encoder/ms-marco-MiniLM-L-6-v2`** (~22M parameters, Apache-2.0 license) behind an injectable `Reranker` protocol reranking the top-20 RRF-fused candidates. Enforce strict SQL pre-filter version/section isolation (never post-filtering) and calibrated abstention thresholds. Evaluated via `clm_decide`:
  - Selected: `option_a_rrf_k60_plus_ms_marco_minilm_l6_v2` (confidence `0.6918`)
  - Candidate probabilities: `option_a_rrf_k60_plus_ms_marco_minilm_l6_v2=0.7945`, `option_b_rrf_k60_plus_bge_reranker_base=0.2048`, `option_c_weighted_linear_score_fusion_no_cross_encoder=0.0007`.
- Consequences:
  1. Rank-based RRF (`k=60`) fuses BM25 and dense cosine rankings without brittle per-query score normalization or tuning drift.
  2. Reranking only the top-20 fused candidates with the lightweight ~22M-parameter Apache-2.0 CrossEncoder improves top-k precision while keeping CPU query latency practical, and the injectable `Reranker` protocol keeps tests offline and deterministic.
  3. Exposing all 5 retrieval modes (`bm25_fixed`, `bm25_clause`, `dense_clause`, `hybrid_rrf`, `hybrid_rerank`) enables apples-to-apples P3 ablation, while strict pre-filter version/section isolation prevents cross-season or `amendment_preview` leakage and calibrated abstention gates out-of-corpus queries.
- Alternatives rejected:
  - RRF (`k=60`) + `BAAI/bge-reranker-base` (`option_b_rrf_k60_plus_bge_reranker_base`, `0.2048`): ~278M-parameter reranker adds roughly an order of magnitude more CPU latency per query on local developer hardware.
  - Weighted linear score fusion without a cross-encoder (`option_c_weighted_linear_score_fusion_no_cross_encoder`, `0.0007`): suffers from uncalibrated BM25-vs-cosine score scales and forfeits cross-attention reranking gains for fine-grained regulatory disambiguation.

## ADR-010 — Local index storage layout and fixed-chunk baseline: single atomic SQLite DB (`index.sqlite`) + `index_meta.json`
- Status: accepted
- Date: 2026-11-27
- Context: Phase P2 must store both clause-aware chunks (with FTS5 lexical search and dense vector embeddings) and a fixed-size chunk baseline (derived from P1's `pages.jsonl`) locally at `$0` cost, with atomic rebuilds, deterministic provenance digests, and zero risk of split-file drift between metadata, lexical indexes, and dense vectors.
- Decision: Persist the complete retrieval index in a **single atomic SQLite database** (`data/index/index.sqlite`) paired with an atomically written provenance sidecar (`data/index/index_meta.json`):
  1. `data/index/index.sqlite` contains:
     - `index_info`: index configuration, schema/model metadata, and provenance fields.
     - `chunks_clause` + external-content FTS5 virtual table `chunks_clause_fts` (`porter unicode61` tokenizer) + packed little-endian `<f4` L2-normalized `embedding_blob` vectors.
     - `chunks_fixed` + external-content FTS5 virtual table `chunks_fixed_fts` (`porter unicode61` tokenizer), built deterministically from `data/processed/pages.jsonl` using `300`-word windows with `60`-word overlap.
  2. `data/index/index_meta.json` records `manifest_sha256`, `clauses_sha256`, `pages_sha256`, `index_sqlite_sha256`, and `content_sha256` for end-to-end reproducibility and staleness checks.
  Evaluated via `clm_decide`:
  - Selected: `option_b_sqlite_only_with_blob_embeddings` (confidence `0.8255`)
  - Candidate probabilities: `option_b_sqlite_only_with_blob_embeddings=0.8837`, `option_a_sqlite_fts5_plus_npy_and_meta_json=0.1042`, `option_c_separate_sqlite_per_year=0.0121`.
- Consequences:
  1. Storing `<f4` L2-normalized `embedding_blob` rows alongside `chunks_clause` inside one SQLite file eliminates row-order or stale-file desynchronization between FTS5 tables and dense embeddings, and enables atomic temp-file-and-rename index swaps.
  2. Hosting `chunks_fixed` / `chunks_fixed_fts` (`300`-word windows, `60`-word overlap from `pages.jsonl`) in the same database guarantees fair, single-artifact P3 ablation against `chunks_clause`.
  3. Recording `manifest_sha256`, `clauses_sha256`, `pages_sha256`, `index_sqlite_sha256`, and `content_sha256` in `data/index/index_meta.json` gives P3/P4 fast integrity verification without opening or hashing external vector sidecars.
- Alternatives rejected:
  - SQLite FTS5 + separate `.npy` embedding array + `meta.json` (`option_a_sqlite_fts5_plus_npy_and_meta_json`, `0.1042`): splits coupled rows across multiple files, complicating atomic replacement and risking silent index-to-vector row misalignment.
  - Separate SQLite database per season/year (`option_c_separate_sqlite_per_year`, `0.0121`): fragments cross-year queries and schema/provenance management when indexed pre-filter columns in a single database already enforce strict season isolation cleanly.
