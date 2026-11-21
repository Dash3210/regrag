# Phase P0 PDF Corpus Spike — FIA Formula 1 Technical Regulations (2024 vs 2025)

**Date:** 2026-11-21  
**Target Repo:** `regrag`  
**Extractor Evaluated:** `pdfplumber==0.11.10` (`.venv/Scripts/python.exe`, Python 3.10.11)  
**Spike Script (gitignored):** `spike/inspect_clauses.py` (plus `spike/verify_30_clauses.py`)  
**Decision:** **GO with FIA F1** (no fallback required)

---

## 1. Corpus Provenance

Both PDFs were discovered directly from `https://www.fia.com/regulation/category/110` and downloaded via unauthenticated HTTP GET (`User-Agent: Mozilla/5.0 ...`) with **HTTP 200** (`Content-Type: application/pdf`), with no CAPTCHA, Cloudflare challenge, or login wall.

> **Gitignore note:** Raw PDFs are stored locally under `data/raw/` and are gitignored (`data/raw/`, `*.pdf` in `.gitignore`). They must never be committed.

| Year | Doc Type | Issue / Revision | Direct Official URL | Download Date (UTC) | HTTP Status | Pages | Size (bytes) | SHA-256 | Local Path (gitignored) |
|---|---|---|---|---|---|---:|---:|---|---|
| 2024 | Technical Regulations | Issue 8 (2024-10-17) | `https://www.fia.com/sites/default/files/fia_2024_formula_1_technical_regulations_-_issue_8_-_2024-10-17.pdf` | 2026-11-21T10:57:41Z | 200 | 178 | 2,247,377 | `a940dd6c01b058c17f8da5392fb20b497b8967848c1c62905453b955b6d58c18` | `data/raw/fia_2024_f1_technical_regulations_iss8.pdf` |
| 2025 | Technical Regulations | Issue 3 (2025-04-07) | `https://www.fia.com/sites/default/files/documents/fia_2025_formula_1_technical_regulations_-_issue_03_-_2025-04-07.pdf` | 2026-11-21T10:57:41Z | 200 | 179 | 2,242,504 | `454b76e2b388e61db50c0d116ff59477848a0b7e081f38142242808213dc0826` | `data/raw/fia_2025_f1_technical_regulations_iss03.pdf` |

### Discovered Sporting & Adjacent Regulation URLs (for P1 Manifest Planning)

Discovered on `https://www.fia.com/regulation/category/110` on 2026-11-21 (not downloaded during P0):

- **2024 Formula 1 Sporting Regulations — Issue 7 (2024-07-31):**  
  `https://www.fia.com/sites/default/files/fia_2024_formula_1_sporting_regulations_-_issue_7_-_2024-07-31.pdf`
- **2025 Formula 1 Sporting Regulations — Issue 5 (2025-04-30):**  
  `https://www.fia.com/system/files/documents/fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf`
- **2026 F1 Regulations — Section B [Sporting] — Iss 09 (2026-10-01):**  
  `https://www.fia.com/system/files/documents/fia_2026_f1_regulations_-_section_b_sporting_-_iss_09_-_2026-10-01.pdf`
- **2026 F1 Regulations — Section C [Technical] — Iss 21 (2026-10-19):**  
  `https://www.fia.com/system/files/documents/fia_2026_f1_regulations_-_section_c_technical_-_iss_21_-_2026-10-19.pdf`

---

## 2. Summary Extraction Accuracy (Raw `pdfplumber` Baseline)

30 clauses were hand-verified directly against raw `page.extract_text()` and `page.extract_tables()` outputs (15 from 2024 Issue 8 + 15 from 2025 Issue 3).

### 2a. Overall Verdict Distribution (30 Clauses)

| Scope | Total | PASS | PARTIAL | FAIL | Raw Pass Rate |
|---|---:|---:|---:|---:|---:|
| **2024 Technical Regulations (Iss 8)** | 15 | 9 | 4 | 2 | 60.0% |
| **2025 Technical Regulations (Iss 3)** | 15 | 10 | 2 | 3 | 66.7% |
| **Combined (P0 Baseline)** | **30** | **19** | **6** | **5** | **63.3%** |

### 2b. Accuracy by Verification Axis (Multi-Label Coverage)

*(Clauses frequently test multiple axes; counts below reflect every clause in the 30-row table that exercises each axis.)*

| Verification Axis | Required Min | Verified Count | PASS | PARTIAL | FAIL | Primary Failure / Partial Driver |
|---|---:|---:|---:|---:|---:|---|
| **Nested numbered clauses** (`3.10.10(g)`, `2.11.1(a)(i)-(vi)`, etc.) | 10 | 22 | 14 | 6 | 2 | Coordinate subscripts (`X_A`, `X_F`, `X_R`, `X_C`) drop onto separate lines; `5.3.2` raster diagram & `3.15.17` strikethrough collision |
| **Numeric-limit clauses & tables** (kg, mm, N, kg/h, W, MJ, table grids) | 8 | 23 | 16 | 5 | 2 | `3.15.17` inline strikethrough (`0.5mm 2mm`); `5.3.2` energy diagram stored as raster image; `3.4.2`, `3.15.4`, `4.4` subscript/superscript quirks |
| **Cross-reference clauses** (`Article X.Y`, `Sporting Regulations`) | 6 | 16 | 9 | 3 | 4 | `5.3.2` raster diagram, `8.14` strikethrough + stray `"C"` inside `Article 9.4`, `App. 5` / `App. 3` table cell splits |
| **Year-on-year changed clauses** (2024 vs 2025 matched pairs) | 6 (pairs: 3+) | 16 (8 pairs) | 12 | 4 | 0 | Subscript line breaks & page-break/colon splits (`3.15.4` ×2, `4.4` ×2); semantic & numeric diffs themselves extract accurately |
| **Multi-page / complex tables** (`16.3`, `16.8`, `8.2.8`, `App. 3`, `App. 5`) | — | 5 | 3 | 0 | 2 | Standard grid tables (`16.3`, `16.8`, `8.2.8`) pass 100%; multi-page/rotated tables (`App. 3`, `App. 5`) fail raw extraction |

---

## 3. Complete 30-Row Hand-Verified Clause Table

| # | Year | Page | Clause ID | Category | Raw / Extracted Summary | Table / Numeric Check | Verdict | Parser Quirk Notes |
|---:|---:|---:|---|---|---|---|---|---|
| 1 | 2024 | 8 | `2.9.3`–`2.9.4` | Nested, Cross-ref | Plane A-A lies on front bulkhead (Art. 12) or up to `100mm` behind it; Plane C-C parallel to A-A (Art. 12.1.4). | `100mm` exact; cross-refs `Article 12`, `Article 12.1.4` intact. | **PASS** | Clean prose extraction; zero line-order glitches. |
| 2 | 2024 | 9 | `2.11.1(a)(i)–(vi)` | Nested, Cross-ref | Local X-axis origins (`X_A=0mm`..`X_PU=0mm`); cross-refs Art. 2.11.3, 10.1.4, 9.6.1, 5.4.8. | `0mm` exact; all 4 cross-refs intact. | **PARTIAL** | Subscript tokens (`A`, `B`, `C`, `DIF`, `PU`) drop onto separate lines below `X =0mm` (e.g., `i. X =0mm...\nA`). |
| 3 | 2024 | 15 | `3.4.2` | Nested, Numeric | Wheelbase: distance between `X_F=0` and `X_R=0` must not exceed `3600mm`. | `3600mm` exact. | **PARTIAL** | Subscripts `F` and `R` split onto next line (`"X =0 and X =0...\nF R"`). |
| 4 | 2024 | 33–34 | `3.10.10(h)` | Nested, Numeric, Cross-ref, YoY-diff | DRS slot gap between two Rear Wing Profiles sections must lie between `10mm` and `85mm` (also Art. 3.10.1, 3.10.5, 3.10.6, 8.3). | `10mm`, `85mm`, `Y=490`, `20mm` exact. | **PASS** | Spans p.33→34 (`a.`–`f.` on p.33, `g.`–`h.` on p.34); chunker must stitch across page footer. |
| 5 | 2024 | 41 | `3.15.4` | Nested, Numeric, YoY-diff | Front Wing Bodywork Flexibility: `[0,0,-1000]N` load; max vertical deflection `15mm` (symmetric) / `20mm` (one-side). | `1000N`, `15mm`, `20mm`, `350mm×150mm`, `2kg` exact. | **PARTIAL** | Subscript `F` in `X_F=-1100mm` splits onto separate line (`"X =-1100mm.\nF"`). |
| 6 | 2024 | 43 | `3.15.12` | Nested, Numeric, YoY-diff | Beam Wing Flexibility: max `3mm` deflection under simultaneous `150N` loads at `Y=±270`, `5mm` forward of trailing edge. | `3mm`, `150N`, `5mm`, `60mm` exact. | **PASS** | Single paragraph in 2024 (restructured into `a.` and `b.` in 2025). |
| 7 | 2024 | 44 | `3.15.15` | Nested, Numeric, YoY-diff | Rear Wing Slot Gap Deflection: `85mm +0.00 / -0.05mm` spherical gauge must not pass under `10N` load with DRS deployed. | `85mm +0.00 / -0.05mm`, `10N` exact. | **PASS** | Clean extraction; tolerance sign preserved. |
| 8 | 2024 | 46 | `4.1` | Numeric, YoY-diff | Minimum car mass without fuel must not be less than `798kg` at all times during the Competition. | `798kg` exact. | **PASS** | Clean extraction. (Note: p.178 Appendix 6 contains dark-red struck-through `798kg` beside `800kg`.) |
| 9 | 2024 | 46 | `4.4(a)–(d)` | Nested, Numeric, Cross-ref, YoY-diff | Driver ballast (Art. 4.6.2) placement, FIA sealing, density `> 8000Kg/m3`; `10kg` nominal mass for Art. 13.2 test. | `8000Kg/m3`, `10kg` exact. | **PARTIAL** | Trailing colon splits onto own line (`"must\n:"`); superscript `3` flattened to `m3`. |
| 10 | 2024 | 46 | `4.6.2` | Nested, Numeric, YoY-diff | Reference mass of driver + designated ballast must not be less than `80kg`. | `80kg` exact. | **PASS** | Clean extraction; cross-referenced by `4.4` and `4.6.1`. |
| 11 | 2024 | 50 | `5.2.3`–`5.2.4` | Nested, Numeric | Fuel mass flow `<= 100kg/h`; below `10500rpm`, `Q (kg/h) = 0.009 N(rpm)+ 5.5`. | `100kg/h`, `10500rpm`, `0.009`, `5.5` exact. | **PASS** | Linear formula and units preserved intact on single lines. |
| 12 | 2024 | 51–52 | `5.3.2(a)–(g)` | Nested, Numeric, Cross-ref | ERS energy flow diagram & electrical DC sensors (`a`–`g`); fixed efficiency correction `0.95` for max MGU-K power. | `0.95` in prose; **diagram limits (4MJ/2MJ) MISSING**. | **FAIL** | Energy flow diagram on p.51 is a raster image (`372×567pt`, `images=1`, `lines=0`); numeric limits inside diagram are invisible to text extraction. |
| 13 | 2024 | 122 | `14.6.1`–`14.6.4` | Nested, Cross-ref, YoY-diff | Driver Cooling Systems: allows latent heat of vaporisation/sublimation, bans dry ice in cockpit; exceptions to Art. 12.3.5 & 12.5.4. | No thermal power/energy limits in 2024; cross-refs intact. | **PASS** | Clean 4-clause block (`14.6.1`–`14.6.4`). |
| 14 | 2024 | 129 | `16.3` | Numeric, Table | Fuel properties table: `(RON+MON)/2 >= 87`, `Oxygen >= 3.45 wt%`, `DVPE 45–68 kPa`, `Sulphur <= 10 mg/kg`, `FBP <= 210 oC`, etc. | `extract_tables()[1]` (2nd table on page: 1 header + 13 data rows × 5 cols) extracted cleanly. | **PASS** | `extract_tables()` preserves empty `Min`/`Max` cells (`''`), whereas raw `extract_text()` shifts values leftward visually. |
| 15 | 2024 | 170–171 | `App. 5 (2C, 3K)` | Table, Cross-ref | Component classification multi-page table (`2C` DRS adjuster Art. `3.10.10` `OSC`; `3K` Auxiliary Components Art. `9.5` `TRC`). | `3.10.10` wraps as `'3.10.1\n0'`. | **FAIL** | Multi-page table breaks rows across pages: `2C` spawns orphan row `['','','','','','and electronic\ncomponents','']` on p.171; `3K` truncated mid-sentence on p.171. |
| 16 | 2025 | 11 | `3.1.1(a)–(b)` | Nested, Cross-ref | Definition of Aerodynamic Components/Bodywork: `a.i–iv` included (cooling ducts, primary heat exchangers Art. 7.4.1(b)); `b.i–vi` excluded (cameras Art. 8.17, mirrors Art. 14.2). | 3 cross-refs and 2-level nesting (`a.i`, `b.i`) intact. | **PASS** | Clean hierarchical extraction. |
| 17 | 2025 | 33–34 | `3.10.10(g)` | Nested, Numeric, Cross-ref, YoY-diff | DRS `"state of deployment"` (`<400ms` transition time) & minimum gap `9.4mm`–`85mm` (cross-refs Art. 3.10.1, 3.10.5, 8.3). | `400ms`, `9.4mm`, `85mm` exact. | **PASS** | Tightened from `10mm` (2024 `3.10.10(h)`) to `9.4mm` (2025 `3.10.10(g)`) and re-lettered `(h)`→`(g)`. Spans p.33→34. |
| 18 | 2025 | 41–42 | `3.15.4` | Nested, Numeric, YoY-diff | Front Wing Bodywork Flexibility: `15mm`/`20mm`, tightened **from 28 May 2025** to `10mm` (symmetric) / `15mm` (one-side). | `15mm`, `20mm`, `10mm`, `15mm`, `1000N` exact. | **PARTIAL** | Effective-date clause splits across page break p.41→42; subscript `F` in `X_F` drops to separate line. |
| 19 | 2025 | 44 | `3.15.12(a)–(b)` | Nested, Numeric, Cross-ref, YoY-diff | Beam Wing Flexibility: `(a)` `3mm` linear under `150N`; **new `(b)`** `0.8deg` angular deflection under `150N` at `Y=±270`. | `3mm`, `0.8deg`, `150N`, `60mm` exact. | **PASS** | Cleanly captures new 2025 sub-clause `(b)` and angular limit. |
| 20 | 2025 | 44 | `3.15.15` | Nested, Numeric, YoY-diff | Rear Wing Slot Gap Deflection: `85mm +0.00 / -0.05mm` spherical gauge under **`30N`** load. | `85mm +0.00 / -0.05mm`, `30N` exact. | **PASS** | Clean extraction; direct load increase vs 2024 (`10N`→`30N`). |
| 21 | 2025 | 45 | `3.15.17` | Nested, Numeric | Rear wing mainplane tip flexibility: max `0.5mm` under `[0,0,-750]N` at `[X_R=330, ±525, 910]`; `15000mm2` adaptor. | Raw text extracts **`0.5mm 2mm`** (corrupted!). | **FAIL** | Pink `(1.0, 0.0, 1.0)` strikethrough on old `2mm` sits inline next to new `0.5mm`; raw `extract_text()` outputs `"must not vary more than 0.5mm 2mm"`. |
| 22 | 2025 | 47 | `4.1` | Numeric, Cross-ref, YoY-diff | Minimum car mass without fuel: `800kg` plus Heat Hazard Mass Increase (defined in Art. 4.7). | `800kg` exact; cross-ref `Article 4.7` intact. | **PASS** | Clean extraction (`798kg` in 2024 → `800kg + Art. 4.7` in 2025). |
| 23 | 2025 | 47 | `4.4(a)–(e)` | Nested, Numeric, Cross-ref, YoY-diff | Ballast: `100g` cockpit retention calc; density `> 7500Kg/m3` (was `8000Kg/m3`); new `(e)` no additional function; cross-refs Art. 4.6.2, 13.2. | `100g`, `7500Kg/m3`, `10kg` exact. | **PARTIAL** | Superscript `3` flattened (`7500Kg/m3`); otherwise clean. |
| 24 | 2025 | 48 | `4.6.2` | Nested, Numeric, YoY-diff | Reference mass of driver + designated ballast must not be less than `82kg` (was `80kg`). | `82kg` exact. | **PASS** | Clean extraction. |
| 25 | 2025 | 48 | `4.7(a)–(d)` | Nested, Numeric | Heat Hazard Mass Increase (new 2025): `+5kg` Race/Sprint (`b.i–ii` gear+cooling `>=5kg`); `+2kg` FP/SQ/Qualifying (`d` cooling `>=2kg`). | `5kg`, `2kg` exact. | **PASS** | Clean 2-level hierarchy (`4.7` → `a, b (i, ii), c, d`). |
| 26 | 2025 | 74 | `8.2.8` | Numeric, Table, Cross-ref | Custom control software version limits table: `ECU Team: 5`, `ECU PU: 1`, `ERS and PU-CE: 1` (2022–2025); cross-ref Art. 8.3.1. | 4 rows × 5 cols extracted cleanly via `extract_tables()`. | **PASS** | Clean grid extraction. |
| 27 | 2025 | 78 | `8.14` | Cross-ref | Impact warning system (renamed from Medical warning system): light recessed in survival cell (`Y=150`, `X_C=-1150`), near clutch disengagement (Art. 9.4). | `Y=150`, `X_C=-1150`, `Article 9.4` intact. | **FAIL** | Pink strikethrough on `Medical` and `a` extracts `"8.14 Medical Impact warning system"` & `"fitted with a an impact"`; plus `C` from `X_C` drops onto own line inside `"as described\nC\nin Article 9.4"`. |
| 28 | 2025 | 124 | `14.6.1(a)–(b)` | Nested, Numeric, Cross-ref, YoY-diff | Driver Cooling System (Heat Hazard): `>=200W` continuous at `40°C` ambient OR `>=1.1MJ` thermal store below `10°C`; Art. 5.3.2 cross-ref in `14.6.2`. | `200W`, `40°C`, `1.1MJ`, `10°C` exact. | **PASS** | Clean extraction (nearby `14.6.4` flattens subscript `CO 2` with a space). |
| 29 | 2025 | 133 | `16.8` | Numeric, Table | Engine oil properties table: `Kinematic Viscosity (100oC) >= 2.8 cSt`, `HTHS >= 1.4 mPa.s`, `IBP >= 210 oC`, `Flashpoint >= 93 oC`. | `extract_tables()[1]` (2nd table on page: 1 header + 4 data rows × 5 cols) extracted cleanly. | **PASS** | Clean grid extraction; superscript `10^6 s^-1` flattened to `106 s-1`. |
| 30 | 2025 | 162 | `App. 3 (Lines 1–8)` | Table, Cross-ref | PU Systems, Functions, Components & Supply Perimeter (`INC`/`EXC` across 7 columns; cross-refs Art. 28 Sporting Regs, 5.1.2, 5.4.7, 5.5.1, 5.5.2, 5.23). | Splits into 7 sub-tables; 24 sparse cols with `None` gaps. | **FAIL** | Rotated vertical header `"Item No."` extracts reversed as `'o\nN\nm\ne\nt\nI'`; 6 rotated sub-headers parse as separate 1-col tables; spans p.162–166. |

---

## 4. Documented PDF Parser Quirks & Concrete P1 Mitigation Plan

### Quirk 1: FIA Revision Strikethrough & Colored Amendment Text (CRITICAL)
- **Observed evidence:** Page 1 defines FIA's amendment convention (`Black text: unchanged`, `Pink text: Changes approved by WMSC`). In `2025` Issue 3, deleted words/numbers are rendered in magenta `(1.0, 0.0, 1.0)` with thin horizontal strikethrough rectangles (`height < 2.0pt`) drawn across the text:
  - `2025` p.45 (`3.15.17`): raw `extract_text()` emits `"must not vary more than 0.5mm 2mm"`, placing the deleted limit `2mm` right beside the active limit `0.5mm`.
  - `2025` p.78 (`8.14`): raw `extract_text()` emits `"8.14 Medical Impact warning system"` and `"fitted with a an impact warning light"`.
  - `2024` p.178 (`Appendix 6`): dark-red `(0.753, 0.0, 0.0)` strikethrough produces `"798kg 800kg"` and `"-2 'Symetrics' male fitting Parker Stratoflex Slide-Lok −2 Nipple"`.
- **P1 Mitigation (verified in `spike/verify_30_clauses.py`):** Before calling `page.extract_text()` or `extract_tables()`, filter out any `char` whose vertical midpoint `(top + bottom) / 2` falls within `±2.8pt` of a thin horizontal `rect` (`height < 2.0pt`, `width >= 2pt` to catch narrow single-character/digit deletions such as `2.52pt` observed on `2024` p.178) or `line` (`|top - bottom| < 2.0pt`, `width >= 2pt`) and horizontally overlaps `[x0 - 0.5, x1 + 0.5]`. Tested on `2025` p.45, `2025` p.78, and `2024` p.178:
  - `2025` p.45 cleaned to: `"must not vary more than 0.5mm when two loads..."`
  - `2025` p.78 cleaned to: `"8.14 Impact warning system ... fitted with an impact warning light"`
  - `2024` p.178 cleaned to: `"must not be less than 800kg, at all times"`
  - Additionally, **exclude Appendix 6 ("APPROVED CHANGES FOR 2025")** or tag it as `doc_section=amendment_preview` during 2024 indexing so future-year draft values (`800kg`, `82kg`) never pollute 2024 queries!

### Quirk 2: Running Footers (Including Out-of-Order Stream Placement)
- **Observed evidence:** Every page carries a 2-line footer:
  - `<YEAR> Formula 1 Technical Regulations <PAGE> <DATE>`
  - `© <YEAR> Fédération Internationale de l’Automobile Issue <N>`
  In `2025`, this matches the bottom of `179/179` pages. In `2024`, it matches the bottom of `173/178` pages — on the remaining 5 pages (`p.160–164`, Appendix 3), PDF stream order causes `extract_text()` to emit the footer near the **top** of the extracted text (`lines 3–4` on `2024 p.160` and `lines 1–2` on `2024 p.161–164`).
- **P1 Mitigation:** Strip lines matching `^(2024|2025) Formula 1 Technical Regulations \d+ \d{2} \w+ \d{4}$` and `^© (2024|2025) Fédération Internationale de l’Automobile Issue \d+$` anywhere on the page (or by `top > page.height - 65` coordinate crop before extraction), then stitch prose across page boundaries before clause-level chunking.

### Quirk 3: Coordinate Subscripts & Superscripts Dropping Onto Separate Lines
- **Observed evidence:** Subscripts (`X_A`, `X_B`, `X_C`, `X_F`, `X_R`, `X_W`, `X_DIF`, `X_PU`, `CO_2`) have slightly smaller font size and lower `top`/`bottom` baseline (`+2pt` to `+5pt`), causing `pdfplumber`'s default `y_tolerance=3` to place the subscript letter on the next line (e.g., `"X =0 and X =0\nF R"` on p.15, or `"as described\nC\nin Article 9.4"` on p.78). Superscripts (`Kg/m3`, `mm2`, `106 s-1`) are flattened inline.
- **P1 Mitigation:** In P1 `parse.py`, tune `extract_text(y_tolerance=6)` or post-process `page.extract_words()` by merging small-font (`size < body_size * 0.85`) subscript tokens with the horizontally closest preceding word within `|delta_y| <= 6pt` (producing `X_F`, `X_R`, `X_C`), while guarding against stray line merges across genuine bullet lines.

### Quirk 4: Multi-Page Tables, Orphan Continuation Rows, and Rotated Headers
- **Observed evidence:**
  - Standard bordered tables (`8.2.8` p.74, `16.3` p.129/131, `16.4.1` p.129/131, `16.8` p.131/133) extract cleanly via `page.extract_tables()`.
  - Multi-page `Appendix 5` (`p.170–177` in 2024; `p.172–179` in 2025) repeats its header on every page and splits tall rows across page breaks, creating orphan rows (`['','','','','','and electronic\ncomponents','']` on `2024` p.171) and mid-sentence truncations (`3K` at bottom of p.171).
  - Multi-page `Appendix 3` (`p.160–164` in 2024; `p.162–166` in 2025) uses rotated vertical column headers, causing `extract_tables()` to emit **7 tables per page**, reverse `"Item No."` into `'o\nN\nm\ne\nt\nI'`, and insert 14 empty `None` spacer columns.
- **P1 Mitigation:**
  1. Stitch consecutive pages that share the same header row (`Ref. | Component or Assembly | ...`).
  2. When a continuation table's first data row has empty `Ref.` and `Component` cells, append its non-empty cells onto the previous page's final row.
  3. For `Appendix 3`, parse with explicit column x-intervals (`explicit_vertical_lines`) and reconstruct rotated header strings or use fixed 10 canonical column names (`Item No.`, `List of PU functions/systems/components`, `Art. 5.1.2 Defin.`, `Art. 5.4.7 Legal Box`, `Art. 5.5.1 mass`, `Art. 5.5.2 Centre Gravity`, `Art. 5.23 Seal. Perim.`, `App. 4 PU Homol.`, `App. 4 - 5) & 7) Supply perim.`, `PU Element associated`).

### Quirk 5: Nested Bullet Continuity & Raster Diagrams
- **Observed evidence:**
  - Clauses frequently span page breaks mid-list (`3.10.10` p.33→34, `3.15.4` p.41→42, `3.16.4` p.44→45).
  - `5.3.2` energy flow diagram (`2024` p.51 / `2025` p.53) is an embedded raster image (`372×567pt`), so numeric energy-flow limits inside the diagram are absent from extracted text.
- **P1 Mitigation:**
  - Build a hierarchical clause state machine (`Article N` → `N.M` → `N.M.K` → `a.`/`i.`) operating on the **footer-stripped, page-stitched document stream**, attaching parent clause context (`"3.10.10 Drag Reduction System > (g)"`) to every leaf chunk.
  - Detect pages with `len(page.images) > 0` referenced by `"shown below"` / `"diagram"` (`5.3.2`, `6.6.4`) and emit structured `[DIAGRAM_IMAGE: ...]` metadata markers so retrieval/evals know when a visual diagram is referenced rather than silently missing text.

---

## 5. Recommendation: **GO with FIA F1**

We recommend **proceeding with the FIA Formula 1 Regulations corpus** (`GO with FIA F1`) rather than switching to the MCC/ICC fallback:

1. **Zero access friction:** Both `2024` and `2025` Technical Regulations (plus `2024`, `2025`, and `2026` Sporting Regulations) are directly downloadable as clean `application/pdf` files from `fia.com` with `HTTP 200`.
2. **Ideal stress-test for `regrag`:** The corpus natively exhibits every core RAG failure mode `regrag` is built to evaluate and solve—especially **version confusion** (e.g., `4.1` `798kg` vs `800kg + Art. 4.7`; `4.6.2` `80kg` vs `82kg`; `3.10.10` `10mm` vs `9.4mm`; `3.15.15` `10N` vs `30N`; `3.15.4` `15/20mm` vs `10/15mm`; `14.6` Heat Hazard thermal limits), **strikethrough revision traps** (`0.5mm 2mm`), **dense numeric tables** (`16.3`, `16.8`, `8.2.8`), and **multi-hop cross-references**.
3. **High-confidence P1 fixes:** All 5 `FAIL`s and 6 `PARTIAL`s in raw `pdfplumber` output stem from well-understood PDF layout patterns (strikethrough rects, stream-ordered footers, subscript y-offsets, and Appendix 3/5 tables), and our strikethrough filter prototype already eliminated the most dangerous numeric corruption (`0.5mm 2mm` → `0.5mm`, `798kg 800kg` → `800kg`) deterministically.
