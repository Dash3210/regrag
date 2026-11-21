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

