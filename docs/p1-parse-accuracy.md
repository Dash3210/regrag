# Phase P1 Parse Accuracy & Corpus Verification Report

**Date:** 2026-11-23  
**Parser:** `regrag.corpus.parse` (`pdfplumber==0.11.10`)  
**Input Manifest:** `corpus/manifest.json` (`fia-f1-regulations`, SHA-256 `f6115d710074b369466865b04ca7bde5c115be9d36fd9b9fafb5fe6ce4c114cb`)  
**Processed Outputs (`data/processed/`, gitignored):**
- `clauses.jsonl`: **6,704** clauses (**6,704** unique `chunk_id`s), SHA-256 `ba165dcc7a94568b08cd1777a48dfb12e246f6d517f44bb1440a59a36fda4e99`
- `pages.jsonl`: **583** cleaned pages, SHA-256 `92af83092dee461c55f8307de722f41594a1fb053218a5e2557005942aea2bed`
- **Determinism:** Deterministic `clauses.jsonl`, `pages.jsonl`, and `parse_summary.json` emission (`sort_keys=True`, atomic `.part` swap) across all 4 manifest PDFs.

---

## 1. 4-PDF Parse Statistics

| `doc_id` | Year | `doc_type` | Pages | Normative Clauses | Appendix Clauses | Amendment Preview Clauses | Total Clauses | Table Pages | Diagram Pages | Cross-Ref Mentions (Clauses w/ Refs) |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| `fia_2024_technical` | 2024 | `technical` | 178 | 1,840 | 445 | 4 | 2,289 | 18 | 4 | 451 (332) |
| `fia_2025_technical` | 2025 | `technical` | 179 | 1,856 | 445 | 0 | 2,301 | 18 | 4 | 465 (341) |
| `fia_2024_sporting` | 2024 | `sporting` | 107 | 918 | 134 | 1 | 1,053 | 7 | 0 | 408 (274) |
| `fia_2025_sporting` | 2025 | `sporting` | 119 | 925 | 135 | 1 | 1,061 | 5 | 0 | 450 (304) |
| **Total** | — | — | **583** | **5,539** | **1,159** | **6** | **6,704** | **48** | **8** | **1,774 (1,251)** |

---

## 2. Summary Delta (P0 Baseline vs P1 Clause Parser)

Comparing the exact 30 hand-verified clauses from `docs/p0-corpus-spike.md` Section 3 against the real JSONL records emitted in `data/processed/clauses.jsonl`:

| Scope | Total | P0 PASS | P0 PARTIAL | P0 FAIL | P1 PASS | P1 PARTIAL | P1 FAIL | Pass Rate Delta |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| **2024 Technical Regulations (Iss 8)** | 15 | 9 | 4 | 2 | 12 | 2 | 1 | `60.0%` -> `80.0%` (+3 PASS) |
| **2025 Technical Regulations (Iss 3)** | 15 | 10 | 2 | 3 | 13 | 2 | 0 | `66.7%` -> `86.7%` (+3 PASS) |
| **Combined (30 Clauses)** | **30** | **19** | **6** | **5** | **25** | **4** | **1** | **`63.3%` -> `83.3%` (+6 PASS, -4 FAIL)** |

Key improvements driven by P1 mitigations:
- **Strikethrough deletion filter (`3.15.17`, `8.14`, `App. 6`):** Midpoint-only char filtering (`sx0 <= char_mid_x <= sx1` without `_STRIKE_X_PAD_PT`) removes pink `(1.0, 0.0, 1.0)` and dark-red `(0.753, 0.0, 0.0)` struck-through text (`0.5mm 2mm` -> `0.5mm`; `Medical Impact` / `a an` -> `Impact` / `an`; `798kg 800kg` -> `800kg`) with `0` non-struck black characters dropped across all 226 Sporting pages.
- **Coordinate subscript & clause-split fixes (`2.11.1`, `3.4.2`, `3.15.4`, `8.14`, `13.5.1`, `10.8`/`10.10`, `23.5`/`54.2`, `30.5(j)(iv)`, `60)`):** `y_tolerance=6` via `_extract_cleaned_lines_with_coords()` (with `repair_coordinate_subscripts()` fallback) fused dropped coordinate tokens inline (`X_A=0mm`..`X_PU=0mm`, `X_F=0`, `X_R=0`, `X_F=-1100mm.`, `X_C=-1150`); removing `A` from `_UNIT_START_RE`, enforcing article/appendix prefix guards, supporting `; and/or` / `x0`-aligned bullet continuations, accented Sporting article headings (`60) ... PARC FERMÉ`), inline initial `a)` clauses (`23.5 a)`, `54.2 a)`), bullet-stack resets on outdented prose, and 5th-level (`depth=4`) lettered/roman sub-bullets eliminated all swallowed clauses and sub-clauses across all 6,704 emitted clauses.
- **Running footer stripping & clause stitching (`3.10.10`, `3.15.4`):** Removed 2-line FIA footers regardless of stream order and stitched multi-page clauses cleanly (`page_start` -> `page_end`).
- **Amendment preview & TOC isolation (`2024` `App. 6`, `Sporting` p.1 & `App. 9`):** Future-year changes (`800kg`, `82kg`, `1 Changes for 2026`) are isolated strictly in `section_kind="amendment_preview"` (`2024 Tech p.178`, `2024 Sport p.107`, `2025 Sport p.119`), with all Page 1 TOCs cleanly `normative`.
- **Multi-page & rotated table cleanup (`App. 5`, `App. 3`, `Sporting 6.4`):** `App. 5` stays one unified clause spanning `p.170-177` (2024) / `p.172-179` (2025); `App. 3` normalizes 10 canonical columns and stitches all 5 pages into one 57-line Markdown table; `Sporting 6.4` keeps Race and Sprint scales separate on `p.5`.

---

## 3. 30-Row P0-vs-P1 Comparison

| # | Year | Clause | P0 Verdict | P1 Verdict | Emitted `chunk_id` | `section_kind` | Verified Record Check (Numbers / Tables / Cross-Refs) | Honest Residual Limits |
|---:|---|---|---|---|---|---|---|---|
| 1 | 2024 | `2.9.3`–`2.9.4` | PASS | **PASS** | `fia_2024_technical::normative::2.9.3`, `fia_2024_technical::normative::2.9.4` | `normative` | `p.8-8`; `100mm` exact; cross-refs `['Article 12']` and `['Article 12.1.4']` extracted. | None. |
| 2 | 2024 | `2.11.1(a)(i)–(vi)` | PARTIAL | **PASS** | `fia_2024_technical::normative::2.11.1(a)(i)` .. `(vi)` | `normative` | `p.9-9`; exact `2.11.1(a)(i)–(vi)` hierarchy preserved; subscripts repaired (`X_A=0mm`..`X_PU=0mm`, `X_F=0`, `X_R=0`); cross-refs (`Article 2.11.3`, `Article 10.1.4`, `Article 9.6.1`, `Article 5.4.8`) intact. | None. |
| 3 | 2024 | `3.4.2` | PARTIAL | **PASS** | `fia_2024_technical::normative::3.4.2` | `normative` | `p.15-15`; `"The distance between the planes X_F=0 and X_R=0 must not exceed 3600mm."` | None. |
| 4 | 2024 | `3.10.10(h)` | PASS | **PASS** | `fia_2024_technical::normative::3.10.10`, `fia_2024_technical::normative::3.10.10(h)` | `normative` | Parent spans `p.33-34` with cross-refs `['Article 3.10.1', 'Article 3.10.5']`; leaf `(h)` on `p.34-34` has `10mm` and `85mm` exact. | Footers stripped across page boundary; `Article 8.3` appears in sibling `3.10.10(g)`. |
| 5 | 2024 | `3.15.4` | PARTIAL | **PASS** | `fia_2024_technical::normative::3.15.4`, `fia_2024_technical::normative::3.15.4(d)`, `fia_2024_technical::normative::3.15.4(f)` | `normative` | `p.41-41`; `[0, 0, -1000]N`, `350mm`×`150mm`, `2kg`, `15mm`, `20mm` exact; `X_F=-1100mm.` repaired inline inside `3.15.4(d)`. | Coordinate `[XF, Y, Z]` without `=` stays `XF` (expected bracket notation). |
| 6 | 2024 | `3.15.12` | PASS | **PASS** | `fia_2024_technical::normative::3.15.12` | `normative` | `p.43-43`; `3mm`, `150N`, `Y=270`, `5mm`, `60mm` exact; cross-ref `['Sporting Regulations']`. | None. |
| 7 | 2024 | `3.15.15` | PASS | **PASS** | `fia_2024_technical::normative::3.15.15` | `normative` | `p.44-44`; `85mm +0.00 / -0.05mm`, `10N` exact. | None. |
| 8 | 2024 | `4.1` | PASS | **PASS** | `fia_2024_technical::normative::4.1` | `normative` | `p.46-46`; `798kg` exact; 2024 Appendix 6 `800kg` cleanly isolated in `fia_2024_technical::amendment_preview::4.1` (`p.178-178`). | None. |
| 9 | 2024 | `4.4(a)–(d)` | PARTIAL | **PARTIAL** | `fia_2024_technical::normative::4.4`, `4.4(a)`, `4.4(c)`, `4.4(d)` | `normative` | `p.46-46`; `Article 4.6.2` on parent, `8000Kg/m3`, `10kg`, `Article 13.2` on `4.4(d)` exact. | Trailing colon wraps onto its own line (`"must\n:"`) in `2024 4.4`, and superscript `3` remains flattened (`8000Kg/m3`). |
| 10 | 2024 | `4.6.2` | PASS | **PASS** | `fia_2024_technical::normative::4.6.2` | `normative` | `p.46-46`; `80kg` exact; future `82kg` isolated in `fia_2024_technical::amendment_preview::4.6.2` (`p.178-178`). | None. |
| 11 | 2024 | `5.2.3`–`5.2.4` | PASS | **PASS** | `fia_2024_technical::normative::5.2.3`, `fia_2024_technical::normative::5.2.4` | `normative` | `p.50-50`; `100kg/h`, `10500rpm`, `Q (kg/h) = 0.009 N(rpm)+ 5.5` exact. | None. |
| 12 | 2024 | `5.3.2(a)–(g)` | FAIL | **FAIL** | `fia_2024_technical::normative::5.3.2`, `5.3.2(a)`..`(g)` | `normative` | `p.51-52`; `has_diagram=True` accurately flagged on `5.3.2`; cross-ref `['Sporting Regulations']` and sensor clauses `(a)–(g)` intact. | Raster energy-flow diagram (`372×567pt` image) numeric limits (`4MJ`/`2MJ`) remain absent from text without OCR/vision. |
| 13 | 2024 | `14.6.1`–`14.6.4` | PASS | **PASS** | `fia_2024_technical::normative::14.6.1` .. `14.6.4` | `normative` | `p.122-122`; `Article 12.3.5` and `Article 12.5.4` extracted on `14.6.4`; dry ice ban intact. | None. |
| 14 | 2024 | `16.3` | PASS | **PASS** | `fia_2024_technical::normative::16.3` | `normative` | `p.129-129`; `tables=1` attached directly to `16.3` with empty `Min`/`Max` cells preserved (`| (RON+MON)/2 |  | 87 |  | ASTM D 2699/D 2700 |`, `3.45 wt%`, `| Nitrogen | mg/kg |  | 500 | ASTM D 5762 |`). | Inline `extract_text()` table echo also appears in `text`; degree symbol remains ASCII `oC`. |
| 15 | 2024 | `App. 5 (2C, 3K)` | FAIL | **PARTIAL** | `fia_2024_technical::appendix::App.5` | `appendix` | `p.170-177`; entire 8-page table stitched into ONE `App.5` chunk (`tables=1`, `16,364`-char Markdown table); `2C` orphan row `"and electronic components"` merged and `3K` description `"essential for the functioning of the gearbox."` complete; `Article 9.1`, `Article 11` extracted. | Narrow `Art.` cell wraps `3.10.10` as `"3.10.1 0"`; all App. 5 rows live in one coarse `App.5` chunk rather than per-component records. |
| 16 | 2025 | `3.1.1(a)–(b)` | PASS | **PASS** | `fia_2025_technical::normative::3.1.1`, `3.1.1(a)(i)`, `3.1.1(b)(i)` | `normative` | `p.11-11`; 2-level nesting `(a)(i)-(iv)` and `(b)(i)-(vi)` intact; `Article 3`, `Article 8.17` extracted. | None. |
| 17 | 2025 | `3.10.10(g)` | PASS | **PASS** | `fia_2025_technical::normative::3.10.10`, `fia_2025_technical::normative::3.10.10(g)` | `normative` | Parent spans `p.33-34` (`['Article 3.10.1', 'Article 3.10.5', 'Articles 3.10.1', 'Article 8.3']`); leaf `(g)` on `p.34-34` has `9.4mm`, `85mm`, and `['Article 3.10.1']` exact. | None. |
| 18 | 2025 | `3.15.4` | PARTIAL | **PASS** | `fia_2025_technical::normative::3.15.4`, `3.15.4(d)`, `3.15.4(f)` | `normative` | Parent spans `p.41-42`; `3.15.4(d)` repairs `X_F=-1100mm.`; `3.15.4(f)` stitches p.41–42 cleanly (`15mm`/`20mm`, `"From 28 May 2025 onwards:"` `10mm`/`15mm`). | None. |
| 19 | 2025 | `3.15.12(a)–(b)` | PASS | **PASS** | `fia_2025_technical::normative::3.15.12(a)`, `fia_2025_technical::normative::3.15.12(b)` | `normative` | `p.44-44`; `(a)` `3mm` / `150N` and `(b)` `0.8deg` / `150N` exact; `Sporting Regulations` extracted on both. | None. |
| 20 | 2025 | `3.15.15` | PASS | **PASS** | `fia_2025_technical::normative::3.15.15` | `normative` | `p.44-44`; `85mm +0.00 / -0.05mm`, `30N` exact. | None. |
| 21 | 2025 | `3.15.17` | FAIL | **PASS** | `fia_2025_technical::normative::3.15.17` | `normative` | `p.45-45`; pink strikethrough removed deleted `"2mm"`, emitting exact `"must not vary more than 0.5mm when two loads of [0,0, -750]N"`. | None. |
| 22 | 2025 | `4.1` | PASS | **PASS** | `fia_2025_technical::normative::4.1` | `normative` | `p.47-47`; `800kg` exact; cross-ref `['Article 4.7']` extracted. | None. |
| 23 | 2025 | `4.4(a)–(e)` | PARTIAL | **PARTIAL** | `fia_2025_technical::normative::4.4`, `4.4(a)`, `4.4(e)` | `normative` | `p.47-47`; `Article 4.6.2`, `100g`, `7500Kg/m3`, `10kg`, `Article 13.2` exact; new `(e)` extracted; `"Article 4.6.2 must:"` intact on single line. | Flattened superscript `m3` (`7500Kg/m3`) remains. |
| 24 | 2025 | `4.6.2` | PASS | **PASS** | `fia_2025_technical::normative::4.6.2` | `normative` | `p.48-48`; `82kg` exact. | None. |
| 25 | 2025 | `4.7(a)–(d)` | PASS | **PASS** | `fia_2025_technical::normative::4.7` .. `4.7(d)` | `normative` | `p.48-48`; full 2-level hierarchy (`4.7`, `(a)`, `(b)`, `(b)(i)`, `(b)(ii)`, `(c)`, `(d)`) with `5kg` and `2kg` exact. | None. |
| 26 | 2025 | `8.2.8` | PASS | **PASS** | `fia_2025_technical::normative::8.2.8` | `normative` | `p.74-74`; `tables=1` (`ECU Team: 5` across `2022–2025`; `ECU PU: 5` in `2022` and `1` across `2023–2025`; `ERS and PU-CE: 5` in `2022` and `1` across `2023–2025`) exact. | None. |
| 27 | 2025 | `8.14` | FAIL | **PASS** | `fia_2025_technical::normative::8.14` | `normative` | `p.78-78`; pink strikethroughs removed (`"8.14 Impact warning system"`, `"fitted with an impact warning light"`); `X_C=-1150` repaired inline; `['Article 9.4', 'Sporting Regulations']` extracted. | None. |
| 28 | 2025 | `14.6.1(a)–(b)` | PASS | **PASS** | `fia_2025_technical::normative::14.6.1`, `14.6.1(a)`, `14.6.1(b)`, `14.6.2` | `normative` | `p.124-124`; `200W`, `40°C`, `1.1MJ`, `10°C` exact; `Article 5.3.2` extracted on `14.6.2`. | None. |
| 29 | 2025 | `16.8` | PASS | **PASS** | `fia_2025_technical::normative::16.8` | `normative` | `p.133-133`; `tables=1` preserves empty `Max` cells (`2.8 cSt`, `1.4 mPa.s`, `106 s-1`). | Superscript `10^6 s^-1` flattened to `106 s-1` in text and table. |
| 30 | 2025 | `App. 3 (Lines 1–8)` | FAIL | **PARTIAL** | `fia_2025_technical::appendix::App.3` | `appendix` | `p.162-166`; `tables=2`: all 5 pages of the PU perimeter table stitched into one 10-column, 57-line Markdown table with canonical headers (`Item No.`, `List of PU functions/systems/components`, `Art. 5.1.2 Defin.` .. `PU Element associated`), plus the 11-line specification-upgrade table on `p.166`; cross-refs (`Article 28`, `Sporting Regulations`, `Appendix 4`, `Technical Regulations`) extracted. | Raw `text` still contains the rotated vertical header echo (`oN\nm etI`) alongside clean `tables_markdown`, and all rows stay in one coarse `App.3` chunk. |

---

## 4. Sporting Regulations Spot-Check

Ten clauses (5 from `fia_2024_sporting`, 5 from `fia_2025_sporting`) verified directly between `data/processed/clauses.jsonl` and the raw Sporting PDFs:

| # | Year | Clause | Verdict | Emitted `chunk_id` | `section_kind` | Category | Verified Check (Raw PDF vs Emitted JSONL) |
|---:|---|---|---|---|---|---|---|
| 1 | 2024 | `6.4` | **PASS** | `fia_2024_sporting::normative::6.4` | `normative` | YoY-diff, Table, Cross-ref | `p.5-5`; awards `25..1` Race pts, `8..1` Sprint pts, plus `1` fastest-lap point (`Article 62` ref); Race and Sprint scales emitted as 2 separate tables (`tables=2`). |
| 2 | 2024 | `28.1` | **PASS** | `fia_2024_sporting::normative::28.1` | `normative` | Cross-ref | `p.29-29`; 2022–2025 PU homologation conformity intact; cross-refs `['Appendix 4', 'Technical Regulations']` exact. |
| 3 | 2024 | `28.2` | **PASS** | `fia_2024_sporting::normative::28.2` | `normative` | Numeric, Cross-ref, Multi-page | `p.29-30`; `4` ICE, `4` MGU-H, `4` TC, `2` ES, `2` CE limits stitched across page break; cross-ref `['Article 28.4']` exact. |
| 4 | 2024 | `35.1(a)–(e)` | **PASS** | `fia_2024_sporting::normative::35.1` | `normative` | Nested (`N)` Article) | `p.43-43`; `35) WEIGHING` article header mapped to `Article 35`; parent + 5 child chunks `35.1(a)`–`(e)` (`a)` bullet style) exact. |
| 5 | 2024 | `55.14` | **PASS** | `fia_2024_sporting::normative::55.14` | `normative` | YoY-diff, Multi-page | `p.63-64`; `"LAPPED CARS MAY NOW OVERTAKE"` procedure stitched cleanly across p.63–64 (`"has been sent"` wording). |
| 6 | 2025 | `6.4` | **PASS** | `fia_2025_sporting::normative::6.4` | `normative` | YoY-diff, Table | `p.5-5`; fastest-lap bonus point **removed** vs 2024 (`cross_refs=[]`); Race (`25..1`) and Sprint (`8..1`) scales emitted as 2 separate tables (`tables=2`). |
| 7 | 2025 | `28.1` | **PASS** | `fia_2025_sporting::normative::28.1` | `normative` | Cross-ref | `p.31-31`; matches 2024 wording (`2022-2025` PU dossier conformity); cross-refs `['Appendix 4', 'Technical Regulations']` exact. |
| 8 | 2025 | `35.1(a)–(e)` | **PASS** | `fia_2025_sporting::normative::35.1` | `normative` | Nested (`N)` Article) | `p.43-43`; `35) WEIGHING` -> `Article 35`, `35.1` + 5 child chunks `(a)`–`(e)` exact. |
| 9 | 2025 | `55.14` | **PASS** | `fia_2025_sporting::normative::55.14` | `normative` | YoY-diff, Cross-ref | `p.66-66`; updated wording (`"will be sent"` + safety car green light, pink strikethrough removed) and new cross-ref `['Article 55.8']` exact. |
| 10 | 2025 | `App. 9` | **PASS** | `fia_2025_sporting::amendment_preview::App.9` | `amendment_preview` | Section Isolation | `p.119-119`; `APPENDIX 9\nAPPROVED CHANGES FOR SUBSEQUENT YEARS` (`1 Changes for 2026`) cleanly isolated as `amendment_preview` (unlike Sporting `App. 6` which stays `appendix`). |

---

## 5. Honest Residual Failure Modes for P2 / P3

1. **Raster diagram opacity (`5.3.2` energy flow limits, `Appendix 2` drawings):** Embedded raster images (`8` pages total across 2024–2025 Technical PDFs: `2024` p.51, 67, 158, 159 and `2025` p.53, 69, 160, 161, including image-only pages) set `has_diagram=True`, so P2/P3 evaluators can explicitly detect visual-only specs, but numeric values inside images (`4MJ`/`2MJ`) require vision/OCR outside P1 scope.
2. **Coarse Appendix table chunks (`App. 3`, `App. 5`):** `App. 3` and `App. 5` each stitch into one `ClauseRecord` containing a multi-kilobyte Markdown table, and `App. 3`'s raw `text` still retains the unparsed rotated header characters (`oN\nm etI`) alongside its clean `tables_markdown`.
3. **Flattened superscripts (`m3`, `106 s-1`) & line-wrapped table cell tokens (`3.10.1 0`):** Exponents remain flattened inline and narrow `Art.` cells can split digits across a space; P2 lexical/BM25 normalization should tolerate minor spacing variance inside table cells.
4. **Minor punctuation line-wrap (`2024 4.4`):** Trailing `:` in `2024 4.4` wraps onto its own line (`"must\n:"`), which does not affect `2025 4.4` (`"must:"`).
