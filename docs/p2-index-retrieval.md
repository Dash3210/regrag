# Phase P2 Index & Version-Isolated Hybrid Retrieval Report

**Date:** 2026-11-27  
**Indexer:** `regrag.index.build` (`INDEX_SCHEMA_VERSION = 1`)  
**Retriever:** `regrag.retrieve.HybridRetriever`  
**Models:**
- Dense encoder: `intfloat/e5-small-v2` (`embedding_dim = 384`, asymmetric `"passage: "` / `"query: "` prefixes, L2-normalized `<f4` embeddings)
- Cross-encoder reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` (reranking top-20 `hybrid_rrf` candidates)

**Build Provenance (`data/index/index_meta.json`, gitignored):**
- Cold build duration: **440.97s** (including initial `intfloat/e5-small-v2` model download and CPU encoding of 6,704 clauses)
- `corpus_name`: `fia-f1-regulations`
- `clause_count`: **6,704** (`normative` + `appendix` + `amendment_preview`)
- `fixed_chunk_count`: **1,216** (`fixed_chunk_words = 300`, `fixed_overlap_words = 60` from `pages.jsonl`)
- `available_years`: `[2024, 2025]`
- `available_doc_types`: `["sporting", "technical"]`
- `available_section_kinds`: `["amendment_preview", "appendix", "normative"]`
- `manifest_sha256`: `f6115d710074b369466865b04ca7bde5c115be9d36fd9b9fafb5fe6ce4c114cb`
- `clauses_sha256`: `ba165dcc7a94568b08cd1777a48dfb12e246f6d517f44bb1440a59a36fda4e99`
- `pages_sha256`: `92af83092dee461c55f8307de722f41594a1fb053218a5e2557005942aea2bed`
- `content_sha256`: `23b5eced505d788e7b83bf12e8627969dadc5d1d22e5905c1c433d2377bb0763`
- `index_sqlite_sha256`: `1a3b4182927f3a9c9d41747531a04faddaac7bee18185b7c23fdc9af95828fbd`

---

## 1. Empirically Calibrated Abstention Thresholds

Measured on the live `data/index` corpus (`intfloat/e5-small-v2` + `cross-encoder/ms-marco-MiniLM-L-6-v2`) after model warmup:

| Constant | Calibrated Value | Observed In-Domain Range (Q1–Q5) | Observed OOD Range (OOD1–OOD3) | Rationale |
|---|---:|---|---|---|
| `DEFAULT_MIN_DENSE_SCORE` | **`0.82`** | `0.8250` – `0.8769` | `0.7757` – `0.8561` | Separates in-domain top-1 E5 cosine scores (`>= 0.8250`) from general OOD queries (`0.7757` World Cup, `0.8147` chocolate cake). `OOD3` (`liquid nitrogen`, `0.8561`) overlaps `16.9.1` (nitrogen/fuel cooling) in dense space and is caught by the cross-encoder gate. |
| `DEFAULT_MIN_BM25_SCORE` | **`0.01`** | `9.7846` – `31.9028` | `7.4635` – `13.9331` | Guards against empty/near-zero IDF lexical matches while keeping tiny synthetic test corpora valid. SQLite FTS5 `OR`-tokenized BM25 cannot reject OOD stop-word/lexical overlaps (`OOD3` `13.9331` > `Q2` `9.7846`), so lexical-only modes serve as uncalibrated P3 baselines. |
| `DEFAULT_MIN_RERANK_SCORE` | **`-1.5`** | **`+0.9537`** – **`+4.2720`** | **`-10.5971`** – **`-3.5046`** | Sits cleanly inside the **`4.46`-logit** separation between the highest OOD logit (`-3.5046` on `OOD3`) and the lowest in-domain top-1 logit (`+0.9537` on `Q2`), rejecting **3/3** OOD queries while passing **5/5** FIA queries. |

---

## 2. Five-Config Real-Corpus Retrieval Comparison (Q1–Q5)

All 5 queries were evaluated with `year=None` (testing automatic single-year extraction and pre-filter version isolation) and `include_amendment_preview=False` against `data/index` (`top_k=5`, warm CPU models):

- **Q1:** `"What is the minimum mass of a Formula 1 car in 2024?"` (`effective_year=2024`, target `2024 4.1` `798kg`)
- **Q2:** `"What is the minimum weight of a car in 2025?"` (`effective_year=2025`, target `2025 4.1` `800kg`)
- **Q3:** `"By how much must the rear wing slot gap not vary under load in 2025?"` (`effective_year=2025`, target `2025 3.15.17` `0.5mm`)
- **Q4:** `"How many Championship points are awarded for the Race and Sprint in 2024?"` (`effective_year=2024`, target `2024 Sporting 6.4`)
- **Q5:** `"Who may enter the parc fermé after the race in 2025?"` (`effective_year=2025`, target `2025 Sporting 60.1`)

| Query | `bm25_fixed` (top-1, score, ms) | `bm25_clause` (top-1, score, ms) | `dense_clause` (top-1, score, ms) | `hybrid_rrf` (top-1, score, ms) | `hybrid_rerank` (top-1, score, ms) |
|---|---|---|---|---|---|
| **Q1** | `fia_2024_technical::normative::fixed::0001` (`15.0779`, `26.03ms`) | `fia_2024_technical::normative::4.1` (`16.1930`, `51.70ms`) | `fia_2024_technical::normative::4.1` (`0.8769`, `35.08ms`) | `fia_2024_technical::normative::4.1` (`0.0328`, `108.17ms`) | **`fia_2024_technical::normative::4.1`** (`4.2172`, `1769.25ms`) |
| **Q2** | `fia_2025_technical::normative::fixed::0252` (`10.0573`, `26.21ms`) | `fia_2025_sporting::normative::35.3` (`9.7846`, `57.11ms`) | `fia_2025_technical::normative::5.5.1` (`0.8486`, `43.08ms`) | `fia_2025_sporting::normative::35.3` (`0.0320`, `114.28ms`) | **`fia_2025_technical::normative::4.1`** (`0.9537`, `811.89ms`) |
| **Q3** | `fia_2025_technical::normative::fixed::0089` (`23.2741`, `22.77ms`) | `fia_2025_technical::normative::3.15.15` (`31.9028`, `45.37ms`) | `fia_2025_technical::normative::3.15.15` (`0.8608`, `37.43ms`) | `fia_2025_technical::normative::3.15.15` (`0.0328`, `105.51ms`) | `fia_2025_technical::normative::3.15.15` (`2.7559`, `931.60ms`; `3.15.17` at **rank 4**, `0.5797`) |
| **Q4** | `fia_2024_sporting::appendix::fixed::0001` (`17.6974`, `25.12ms`) | `fia_2024_sporting::normative::45.3` (`21.3127`, `46.80ms`) | `fia_2024_sporting::normative::6.4` (`0.8529`, `30.71ms`) | `fia_2024_sporting::normative::6.4` (`0.0325`, `91.36ms`) | **`fia_2024_sporting::normative::6.4`** (`4.2720`, `1526.20ms`) |
| **Q5** | `fia_2025_sporting::normative::fixed::0168` (`21.3736`, `14.96ms`) | `fia_2025_sporting::normative::59.3` (`22.3777`, `35.28ms`) | `fia_2025_sporting::normative::59.3` (`0.8250`, `26.98ms`) | `fia_2025_sporting::normative::59.3` (`0.0328`, `78.25ms`) | **`fia_2025_sporting::normative::60.1`** (`3.0621`, `1528.29ms`) |

Key takeaways:
1. **Zero cross-year or `amendment_preview` leakage:** Every hit across all 25 `(query, config)` runs matched `effective_year` (`2024` for Q1/Q4, `2025` for Q2/Q3/Q5) and excluded `amendment_preview`.
2. **Cross-encoder disambiguation (`Q2`, `Q5`):** When lexical and dense alone mis-ranked synonyms (`"weight"` vs `"mass"` in Q2, `"enter the parc fermé"` in Q5), `hybrid_rerank` promoted the exact target clauses (`2025 4.1` and `2025 60.1`) to rank 1.
3. **Honest Q3 ranking note (`3.15.15` vs `3.15.17`):** Because the query uses `"rear wing slot gap"`, clause `3.15.15` (*Rear wing slot gap*) ranked `#1` across clause configs while `3.15.17` (*Rear wing mainplane tip flexibility*, `"must not vary more than 0.5mm"`) ranked `#4` (`0.5797`) in `hybrid_rerank`.

---

## 3. Out-of-Domain, Missing-Year, Amendment Preview & Sabotage Verification

### Out-of-Domain & Missing-Year Abstention (`data/index`, calibrated defaults)

| Query | `bm25_fixed` | `bm25_clause` | `dense_clause` | `hybrid_rrf` | `hybrid_rerank` |
|---|---|---|---|---|---|
| **OOD1:** `"What is the recipe for chocolate cake?"` | `7.4635` (`33.71ms`, no gate) | `9.5223` (`41.25ms`, no gate) | **Abstained (`low_retrieval_score`, `34.27ms`)** (raw `0.8147`) | `0.0174` (`89.06ms`, lex gate passed) | **Abstained (`low_retrieval_score`, `1559.20ms`)** (raw `-10.5971`) |
| **OOD2:** `"Who won the 1998 FIFA World Cup?"` | `8.4734` (`40.26ms`, no gate) | `10.5620` (`44.63ms`, no gate) | **Abstained (`low_retrieval_score`, `27.04ms`)** (raw `0.7757`) | `0.0313` (`99.67ms`, lex gate passed) | **Abstained (`low_retrieval_score`, `459.30ms`)** (raw `-8.5507`) |
| **OOD3:** `"What is the boiling point of liquid nitrogen?"` | `12.6646` (`44.91ms`, no gate) | `13.9331` (`45.53ms`, no gate) | `0.8561` (`27.22ms`, `16.9.1` mentions nitrogen) | `0.0328` (`104.67ms`, both gates passed) | **Abstained (`low_retrieval_score`, `1445.40ms`)** (raw `-3.5046`) |
| **MY1:** `"What is the 2026 minimum weight?"` | **Abstained (`unsupported_year`, `0.05ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** |
| **MY2:** `"What were the 2023 sprint points?"` | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** | **Abstained (`unsupported_year`, `0.01ms`)** |

### `amendment_preview` Isolation & Sabotage Verification

- Querying `"800kg minimum mass"` with `config="hybrid_rerank"`, `year=2024`, `top_k=10`:
  - `include_amendment_preview=False`: `fia_2024_technical::amendment_preview::4.1` present = **`False`** (draft `800kg` excluded).
  - `include_amendment_preview=True`: `fia_2024_technical::amendment_preview::4.1` present = **`True`** (`score = 6.0733`, alongside `fia_2024_technical::amendment_preview::4.6.2` at `-4.1454`).
- Sabotage check (injecting a `2025` `RetrievalHit` for `fia_2025_technical::normative::4.1` into a `year=2024` hit list after confirming the mutation precondition `any(h.year == 2025)`):
  - `verify_version_isolation(..., effective_year=2024, doc_type=None, allowed_section_kinds=("normative", "appendix"))` raised **`VersionLeakageError`**:
    `Year leakage detected for 'fia_2025_technical::normative::4.1': expected year 2024, got 2025`.
