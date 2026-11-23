"""Clause-aware and numeric-table PDF parser built on pdfplumber (Phase P1)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pdfplumber

from regrag.corpus.fetch import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_RAW_DIR,
    ManifestDocument,
    compute_sha256,
    load_manifest,
)

DEFAULT_PROCESSED_DIR = Path("data/processed")

_PDF_MAGIC = b"%PDF-"
_STRIKE_MAX_HEIGHT_PT = 2.0
_STRIKE_MIN_WIDTH_PT = 2.0
_STRIKE_Y_TOLERANCE_PT = 2.8
_TABLE_MIN_RECTS = 9
_TABLE_MIN_LINES = 2
_TEXT_Y_TOLERANCE_PT = 6.0

_ROTATED_HEADER_MAP: dict[str, str] = {
    "itemno": "Item No.",
    "itemno.": "Item No.",
}

_APP3_CANONICAL_HEADER: list[str] = [
    "Item No.",
    "List of PU functions/systems/components",
    "Art. 5.1.2 Defin.",
    "Art. 5.4.7 Legal Box",
    "Art. 5.5.1 mass",
    "Art. 5.5.2 Centre Gravity",
    "Art. 5.23 Seal. Perim.",
    "App. 4 PU Homol.",
    "App. 4 - 5) & 7) Supply perim.",
    "PU Element associated",
]

_FOOTER_TECH_SPORT_RE = re.compile(
    r"^\s*20\d{2}\s+Formula\s+1\s+(?:Technical|Sporting)\s+Regulations\s+"
    r"\d+(?:/\d+)?\s+\d{1,2}\s+[A-Za-z]+\s+20\d{2}\s*$",
    re.IGNORECASE,
)
_FOOTER_COPYRIGHT_RE = re.compile(
    r"^\s*(?:©|\(c\))\s*20\d{2}\s+F[eé]d[eé]ration\s+Internationale\s+"
    r"de\s+l[’']Automobile\s+Issue\s+\d+\s*$",
    re.IGNORECASE,
)

_SUBSCRIPT_PLACEHOLDER_RE = re.compile(r"\b([XYZ]) += *")
_SUBSCRIPT_LINE_RE = re.compile(r"^(?:DIF|PU|[ABCFPRW])(?:\s+(?:DIF|PU|[ABCFPRW]))*$")
_INLINE_SUBSCRIPT_RE = re.compile(r"\b([XYZ])(DIF|PU|[ABCFPRW])\s*=\s*(?=[-0-9])")

_CROSS_REF_RE = re.compile(
    r"\b(?:Articles?\s+\d+(?:\.\d+)*(?:\([a-z]+\))?|"
    r"Appendix\s+\d+|"
    r"Sporting\s+Regulations|"
    r"Technical\s+Regulations)\b"
)
_DIAGRAM_REF_RE = re.compile(r"\b(?:diagram|shown\s+below|figure|drawings?)\b", re.IGNORECASE)

_ARTICLE_HEADING_RE = re.compile(r"^(?:ARTICLE|Article)\s+(\d+)\s*(?:[:\-–—]\s*(.*))?$")
_SPORTING_ARTICLE_RE = re.compile(
    r"^(\d+)\)\s+([A-Z\u00C0-\u00D6\u00D8-\u00DE]"
    r"[A-Z\u00C0-\u00D6\u00D8-\u00DE0-9\s,\-–—/&()’']{2,})$"
)
_INLINE_BULLET_START_RE = re.compile(r"^(a)[.)]\s+(?=[A-Z0-9“\"\[(]).+$")
_APPENDIX_HEADING_RE = re.compile(r"^APPENDIX\s+(\d+)\s*(?:[:\-–—]\s*(.*))?$", re.IGNORECASE)
_CLAUSE_NUM_RE = re.compile(r"^(\d+(?:\.\d+)+)(?:\s+(.*))?$")
_BULLET_LETTER_RE = re.compile(r"^(?:\(([a-z])\)|([a-z])[.)])\s+(.+)$")
_BULLET_ROMAN_RE = re.compile(
    r"^(?:\(([ivxlcdm]+)\)|([ivxlcdm]+)[.)])\s+(.+)$",
    re.IGNORECASE,
)
_TOC_TRAILING_PAGE_RE = re.compile(r"^(.*\S)\s+\d{1,3}$")
_SPORTING_TOC_HEADER_RE = re.compile(r"^(?:ART\s+CONTENTS\s+PAGE\b|CONTENTS$)", re.IGNORECASE)
_UNIT_START_RE = re.compile(
    r"^(?:mm|cm|m|kg|g|mg|kPa|Pa|bar|deg|ms|s|wt%|%|l/|MJ|kW|W|N|V|Hz|rpm|oC|°C|cSt|mPa)\b"
)

_ROMAN_VALUES: tuple[str, ...] = (
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
    "xi",
    "xii",
    "xiii",
    "xiv",
    "xv",
)


class ParseError(RuntimeError):
    """Raised when a PDF is missing, unreadable, malformed, or contains zero pages."""


@dataclass(frozen=True)
class CleanedPage:
    """Normalized page-level text and tables sharing the clause parser's cleaning pipeline."""

    doc_id: str
    year: int
    doc_type: str
    page_num: int
    section_kind: str
    text: str
    tables_markdown: tuple[str, ...]
    has_diagram: bool
    source_sha256: str


@dataclass(frozen=True)
class ClauseRecord:
    """Clause-level chunk with hierarchy metadata, tables, and extracted cross-references."""

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


@dataclass(frozen=True)
class ParseReport:
    """Summary metadata and SHA-256 digests produced by parse_corpus."""

    manifest_sha256: str
    documents_parsed: int
    total_pages: int
    total_clauses: int
    clauses_path: Path
    pages_path: Path
    summary_path: Path
    clauses_sha256: str
    pages_sha256: str


@dataclass
class _ClauseBuilder:
    article: str
    clause_id: str
    parent_clause_id: str | None
    breadcrumb: str
    short_label: str
    section_kind: str
    page_start: int
    page_end: int
    lines: list[str]
    raw_tables: list[tuple[int, list[list[str]]]]
    has_diagram: bool = False
    bullet_x0: float | None = None


def _is_black_or_white_color(color: Any) -> bool:
    """Return True if color represents black, near-black, white, or None."""
    if color is None:
        return True
    if isinstance(color, (int, float)):
        val = float(color)
        return val <= 0.05 or val >= 0.98
    if isinstance(color, (tuple, list)):
        if len(color) == 1:
            return _is_black_or_white_color(color[0])
        if len(color) == 3:
            r, g, b = (float(c) for c in color)
            is_black = r <= 0.05 and g <= 0.05 and b <= 0.05
            is_white = r >= 0.98 and g >= 0.98 and b >= 0.98
            return is_black or is_white
        if len(color) == 4:
            c, m, y, k = (float(v) for v in color)
            is_black = c <= 0.05 and m <= 0.05 and y <= 0.05 and k >= 0.95
            is_white = c <= 0.05 and m <= 0.05 and y <= 0.05 and k <= 0.05
            return is_black or is_white
    return False


def _segment_color(seg: dict[str, Any], *, is_rect: bool) -> Any:
    if is_rect:
        if seg.get("fill", True) is not False and seg.get("non_stroking_color") is not None:
            return seg.get("non_stroking_color")
        return seg.get("stroking_color")
    if seg.get("stroking_color") is not None:
        return seg.get("stroking_color")
    return seg.get("non_stroking_color")


def _extract_strikethrough_segments(page: Any) -> list[tuple[float, float, float]]:
    """Collect (x0, x1, mid_y) for thin horizontal non-black rects and lines."""
    segments: list[tuple[float, float, float]] = []
    for is_rect, items in ((True, getattr(page, "rects", ())), (False, getattr(page, "lines", ()))):
        for seg in items:
            x0 = float(seg["x0"])
            x1 = float(seg["x1"])
            top = float(seg["top"])
            bottom = float(seg["bottom"])
            width = abs(x1 - x0)
            height = abs(bottom - top)
            if height >= _STRIKE_MAX_HEIGHT_PT or width < _STRIKE_MIN_WIDTH_PT:
                continue
            color = _segment_color(seg, is_rect=is_rect)
            if _is_black_or_white_color(color):
                continue
            segments.append((min(x0, x1), max(x0, x1), (top + bottom) / 2.0))
    return segments


def filter_strikethrough_page(page: Any) -> Any:
    """Filter out characters crossed by non-black horizontal strikethrough rects or lines.

    Pure black borders and underlines (0 / (0.0, 0.0, 0.0)) never trigger deletion.
    """
    segments = _extract_strikethrough_segments(page)
    if not segments:
        return page

    def _keep_obj(obj: dict[str, Any]) -> bool:
        if obj.get("object_type") != "char":
            return True
        char_mid_y = (float(obj["top"]) + float(obj["bottom"])) / 2.0
        char_mid_x = (float(obj["x0"]) + float(obj["x1"])) / 2.0
        for sx0, sx1, smid_y in segments:
            if abs(char_mid_y - smid_y) <= _STRIKE_Y_TOLERANCE_PT and sx0 <= char_mid_x <= sx1:
                return False
        return True

    return page.filter(_keep_obj)


def strip_running_footers(text: str) -> str:
    """Remove Technical and Sporting FIA 2-line running footers anywhere in a page stream."""
    normalized = text.replace("\xa0", " ")
    kept: list[str] = []
    for raw_line in normalized.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", raw_line).strip()
        if not line:
            continue
        if _FOOTER_TECH_SPORT_RE.match(line) or _FOOTER_COPYRIGHT_RE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def repair_coordinate_subscripts(text: str) -> str:
    """Repair dropped coordinate subscripts (e.g. 'X =0\\nF' -> 'X_F=0') without mangling prose."""
    lines = text.splitlines()
    repaired: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if i + 1 < len(lines):
            next_stripped = lines[i + 1].strip()
            if _SUBSCRIPT_LINE_RE.match(next_stripped):
                tokens = next_stripped.split()
                matches = list(_SUBSCRIPT_PLACEHOLDER_RE.finditer(line))
                if matches and len(tokens) == len(matches):
                    parts: list[str] = []
                    cursor = 0
                    for match, token in zip(matches, tokens, strict=True):
                        parts.append(line[cursor : match.start()])
                        parts.append(f"{match.group(1)}_{token}=")
                        cursor = match.end()
                    parts.append(line[cursor:])
                    repaired.append("".join(parts))
                    i += 2
                    continue
        repaired.append(_INLINE_SUBSCRIPT_RE.sub(r"\1_\2=", line))
        i += 1
    return "\n".join(repaired)


def normalize_rotated_header_cell(cell: str) -> str:
    """Recover vertically rotated header text such as 'o\\nN\\nm\\ne\\nt\\nI' -> 'Item No.'."""
    if "\n" not in cell:
        return cell.strip()
    lines = [part.strip() for part in cell.splitlines() if part.strip()]
    if not lines:
        return ""

    # Pattern 1: single-character vertical lines emitted bottom-to-top ('o\nN\nm\ne\nt\nI')
    if len(lines) >= 4 and sum(1 for part in lines if len(part) <= 2) >= len(lines) - 1:
        joined = "".join(reversed(lines))
        key = re.sub(r"\s+", "", joined).lower()
        if key in _ROTATED_HEADER_MAP:
            return _ROTATED_HEADER_MAP[key]
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", joined)
        return spaced

    # Pattern 2: reversed multi-char lines ('oN\nmetI')
    if len(lines) >= 2:
        reversed_lines = [part[::-1] for part in reversed(lines)]
        joined_rev = " ".join(reversed_lines)
        key_rev = re.sub(r"\s+", "", joined_rev).lower()
        if key_rev in _ROTATED_HEADER_MAP:
            return _ROTATED_HEADER_MAP[key_rev]

    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def clean_table_rows(
    rows: Sequence[Sequence[str | None]],
    *,
    drop_empty_cols: bool = True,
) -> list[list[str]]:
    """Normalize table cells and optionally drop all-empty spacer columns and empty rows."""
    if not rows:
        return []
    max_cols = max((len(r) for r in rows), default=0)
    if max_cols == 0:
        return []

    normalized: list[list[str]] = []
    for row in rows:
        norm_row: list[str] = []
        for col_idx in range(max_cols):
            raw_cell = row[col_idx] if col_idx < len(row) else None
            if raw_cell is None:
                norm_row.append("")
            else:
                cell_text = normalize_rotated_header_cell(str(raw_cell).replace("\xa0", " "))
                norm_row.append(re.sub(r"\s+", " ", cell_text).strip())
        if any(cell != "" for cell in norm_row):
            normalized.append(norm_row)

    if not normalized:
        return []

    if not drop_empty_cols:
        return normalized

    non_empty_cols = [
        col_idx
        for col_idx in range(max_cols)
        if any(row[col_idx] != "" for row in normalized)
    ]
    if not non_empty_cols:
        return []

    return [[row[col_idx] for col_idx in non_empty_cols] for row in normalized]


def stitch_table_pages(
    tables: Sequence[list[list[str]]],
    *,
    page_nums: Sequence[int] | None = None,
) -> list[list[list[str]]]:
    """Stitch consecutive multi-page tables sharing headers and merge orphan continuation rows."""
    if page_nums is not None and len(page_nums) != len(tables):
        raise ValueError("page_nums length must match tables length")
    stitched: list[list[list[str]]] = []
    stitched_pages: list[int] = []
    for idx, table in enumerate(tables):
        if not table:
            continue
        curr_page = page_nums[idx] if page_nums is not None else idx
        copied = [list(row) for row in table]
        can_stitch = (
            bool(stitched)
            and len(copied) >= 2
            and len(stitched[-1][0]) == len(copied[0])
            and stitched[-1][0] == copied[0]
            and (page_nums is None or curr_page > stitched_pages[-1])
        )
        if can_stitch:
            target = stitched[-1]
            data_rows = copied[1:]
            if data_rows and len(target) >= 2:
                first_row = data_rows[0]
                is_orphan = first_row[0] == "" and (len(first_row) < 2 or first_row[1] == "")
                if is_orphan:
                    prev_row = target[-1]
                    for col_idx, val in enumerate(first_row):
                        if val:
                            prev_row[col_idx] = (
                                f"{prev_row[col_idx]} {val}".strip()
                                if prev_row[col_idx]
                                else val
                            )
                    data_rows = data_rows[1:]
            target.extend(data_rows)
            stitched_pages[-1] = curr_page
        else:
            stitched.append(copied)
            stitched_pages.append(curr_page)
    return stitched


def format_table_markdown(rows: Sequence[Sequence[str | None]]) -> str:
    """Format table rows into deterministic GitHub-flavored Markdown preserving empty cells."""
    cleaned = clean_table_rows(rows)
    if not cleaned:
        return ""
    header = cleaned[0]
    width = len(header)

    def _escape(cell: str) -> str:
        return cell.replace("|", "\\|")

    lines = [
        "| " + " | ".join(_escape(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in cleaned[1:]:
        padded = list(row) + [""] * (width - len(row))
        lines.append("| " + " | ".join(_escape(c) for c in padded[:width]) + " |")
    return "\n".join(lines)


def extract_cross_refs(text: str) -> tuple[str, ...]:
    """Extract regulatory cross-references deduplicated in order of first appearance."""
    refs = [" ".join(match.group(0).split()) for match in _CROSS_REF_RE.finditer(text)]
    return tuple(dict.fromkeys(refs))


def detect_section_kind(
    header_or_text: str,
    current_kind: str = "normative",
    *,
    doc_type: str = "technical",
) -> str:
    """Classify section kind into 'normative', 'appendix', or 'amendment_preview'."""
    upper = header_or_text.upper()
    if "APPROVED CHANGES FOR" in upper:
        return "amendment_preview"
    app_match = _APPENDIX_HEADING_RE.match(header_or_text.strip())
    if app_match:
        app_num = int(app_match.group(1))
        if (doc_type != "sporting" and app_num == 6) or (doc_type == "sporting" and app_num == 9):
            return "amendment_preview"
        return "appendix"
    if (
        _ARTICLE_HEADING_RE.match(header_or_text.strip())
        or _SPORTING_ARTICLE_RE.match(header_or_text.strip())
    ) and current_kind == "normative":
        return "normative"
    return current_kind


def _is_toc_article_line(
    art_num_str: str,
    title: str,
    page_num: int,
    *,
    body_started: bool,
    in_toc: bool,
) -> bool:
    if page_num > 12:
        return False
    trailing = _TOC_TRAILING_PAGE_RE.match(title.strip())
    if not trailing:
        return False
    trailing_num = int(title.strip().rsplit(maxsplit=1)[-1])
    if trailing_num == int(art_num_str):
        return False
    return not body_started or in_toc


def _is_toc_appendix_line(
    line: str,
    page_num: int,
    *,
    body_started: bool,
    in_toc: bool,
) -> bool:
    if page_num > 12:
        return False
    if in_toc:
        return True
    return not body_started and bool(re.search(r"\d{1,3}\s*$", line.strip()))


def _update_section_kind_for_page(
    cleaned_text: str,
    current_kind: str,
    page_num: int,
    *,
    doc_type: str,
    in_toc: bool,
    body_started: bool,
) -> tuple[str, str, bool, bool]:
    """Determine page-level section_kind and updated trailing state, skipping TOC entries."""
    page_kind = current_kind
    saw_first_heading = False
    kind = current_kind
    for raw_line in cleaned_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        if page_num <= 12 and not body_started and _SPORTING_TOC_HEADER_RE.match(stripped):
            in_toc = True
            continue

        art_match = _ARTICLE_HEADING_RE.match(stripped)
        if art_match:
            art_num, art_title = art_match.group(1), (art_match.group(2) or "").strip()
            if _is_toc_article_line(
                art_num, art_title, page_num, body_started=body_started, in_toc=in_toc
            ):
                in_toc = True
                continue
            in_toc = False
            body_started = True
            kind = "normative"
            if not saw_first_heading:
                page_kind = kind
            saw_first_heading = True
            continue

        if _SPORTING_ARTICLE_RE.match(stripped):
            in_toc = False
            body_started = True
            kind = "normative"
            if not saw_first_heading:
                page_kind = kind
            saw_first_heading = True
            continue

        app_match = _APPENDIX_HEADING_RE.match(stripped)
        if app_match:
            if _is_toc_appendix_line(
                stripped, page_num, body_started=body_started, in_toc=in_toc
            ):
                continue
            in_toc = False
            body_started = True
            kind = detect_section_kind(stripped, kind, doc_type=doc_type)
            if not saw_first_heading:
                page_kind = kind
            saw_first_heading = True
            continue

        if in_toc or (page_num <= 12 and not body_started):
            continue

        new_kind = detect_section_kind(stripped, kind, doc_type=doc_type)
        if new_kind != kind:
            kind = new_kind
            if not saw_first_heading:
                page_kind = kind
    return page_kind, kind, in_toc, body_started


def _normalize_app3_table(table: Any, raw_rows: list[list[str | None]]) -> list[list[str]] | None:
    """Normalize Technical Appendix 3 tables onto 10 canonical columns so pages stitch cleanly."""
    if len(raw_rows) < 4:
        return None
    row0_norm = [normalize_rotated_header_cell(str(c or "")) for c in raw_rows[0]]
    has_item_no = any("Item No." in c for c in row0_norm)
    has_pu_list = any("List of PU" in str(c or "") for c in raw_rows[0])
    if not (has_item_no and has_pu_list):
        return None
    if not getattr(table, "rows", None) or len(table.rows) < 4:
        return None
    real_cols = [i for i, cell in enumerate(table.rows[3].cells) if cell is not None]
    if len(real_cols) != len(_APP3_CANONICAL_HEADER):
        return None
    projected = [[r[c] if c < len(r) else None for c in real_cols] for r in raw_rows[3:]]
    cleaned_data = clean_table_rows(projected, drop_empty_cols=False)
    if not cleaned_data:
        return None
    return [list(_APP3_CANONICAL_HEADER), *cleaned_data]


def _extract_page_tables(filtered_page: Any) -> list[tuple[float, list[list[str]], str]]:
    """Extract cleaned multi-column tables and Markdown from a filtered pdfplumber page."""
    rects = getattr(filtered_page, "rects", ())
    lines = getattr(filtered_page, "lines", ())
    if len(rects) < _TABLE_MIN_RECTS and len(lines) < _TABLE_MIN_LINES:
        return []

    results: list[tuple[float, list[list[str]], str]] = []
    for table in filtered_page.find_tables():
        raw_rows = table.extract()
        app3_normalized = _normalize_app3_table(table, raw_rows)
        cleaned = app3_normalized if app3_normalized is not None else clean_table_rows(raw_rows)
        if len(cleaned) < 2 or max(len(r) for r in cleaned) < 2:
            continue
        # Reject degenerate prose boxes where fewer than 2 rows have >= 2 non-empty cells
        multi_cell_rows = sum(1 for row in cleaned if sum(1 for c in row if c != "") >= 2)
        if multi_cell_rows < 2:
            continue
        md = format_table_markdown(cleaned)
        if md:
            top_y = float(table.bbox[1]) if getattr(table, "bbox", None) else 0.0
            results.append((top_y, cleaned, md))
    return results


def _extract_cleaned_lines_with_coords(
    filtered_page: Any,
) -> tuple[str, list[tuple[float, float]]]:
    """Extract footer-stripped, subscript-repaired text with (top_y, x0) per line."""
    raw_entries = filtered_page.extract_text_lines(y_tolerance=_TEXT_Y_TOLERANCE_PT)
    if not raw_entries:
        raw_text = filtered_page.extract_text(y_tolerance=_TEXT_Y_TOLERANCE_PT) or ""
        cleaned_text = repair_coordinate_subscripts(strip_running_footers(raw_text))
        return cleaned_text, [(0.0, 0.0) for _ in cleaned_text.splitlines()]

    filtered_pairs: list[tuple[float, float, str]] = []
    for entry in raw_entries:
        normalized = str(entry["text"]).replace("\xa0", " ")
        line = re.sub(r"[ \t]{2,}", " ", normalized).strip()
        if not line:
            continue
        if _FOOTER_TECH_SPORT_RE.match(line) or _FOOTER_COPYRIGHT_RE.match(line):
            continue
        filtered_pairs.append((float(entry["top"]), float(entry.get("x0", 0.0)), line))

    repaired_lines: list[str] = []
    repaired_coords: list[tuple[float, float]] = []
    i = 0
    while i < len(filtered_pairs):
        top_y, x0, line = filtered_pairs[i]
        if i + 1 < len(filtered_pairs):
            next_stripped = filtered_pairs[i + 1][2].strip()
            if _SUBSCRIPT_LINE_RE.match(next_stripped):
                tokens = next_stripped.split()
                matches = list(_SUBSCRIPT_PLACEHOLDER_RE.finditer(line))
                if matches and len(tokens) == len(matches):
                    parts: list[str] = []
                    cursor = 0
                    for match, token in zip(matches, tokens, strict=True):
                        parts.append(line[cursor : match.start()])
                        parts.append(f"{match.group(1)}_{token}=")
                        cursor = match.end()
                    parts.append(line[cursor:])
                    repaired_lines.append("".join(parts))
                    repaired_coords.append((top_y, x0))
                    i += 2
                    continue
        repaired_lines.append(_INLINE_SUBSCRIPT_RE.sub(r"\1_\2=", line))
        repaired_coords.append((top_y, x0))
        i += 1
    return "\n".join(repaired_lines), repaired_coords


def _make_unique_chunk_id(
    doc_id: str,
    section_kind: str,
    clause_id: str,
    seen_counts: dict[str, int],
) -> str:
    base = f"{doc_id}::{section_kind}::{clause_id}"
    count = seen_counts.get(base, 0) + 1
    seen_counts[base] = count
    return base if count == 1 else f"{base}::{count}"


_WRAPPED_CONNECTOR_RE = re.compile(
    r"(?:\b(?:to|in|of|with|from|by|at|into|for|under|than|on|and|or)|\)\s+(?:or|and))\s*$",
    re.IGNORECASE,
)
_BULLET_SEP_RE = re.compile(r"[;:,]\s*(?:and/or|and|or)\s*$", re.IGNORECASE)


def _is_wrapped_mid_sentence(
    prev_line: str,
    body: str,
    *,
    line_x0: float | None = None,
    expected_bullet_x0: float | None = None,
) -> bool:
    """Return True if a lowercase-starting bullet candidate continues a wrapped connector line."""
    if (
        line_x0 is not None
        and expected_bullet_x0 is not None
        and line_x0 > 0
        and abs(line_x0 - expected_bullet_x0) <= 4.0
    ):
        return False
    if not prev_line or not body or not body[0].islower():
        return False
    stripped_prev = prev_line.rstrip()
    if stripped_prev.endswith((".", ":", ";", ",")):
        return False
    if _BULLET_SEP_RE.search(stripped_prev):
        return False
    return bool(_WRAPPED_CONNECTOR_RE.search(stripped_prev))


def _is_valid_clause_start(
    cid: str,
    rest: str,
    *,
    doc_type: str,
    current_section_kind: str,
    active_article_num: int | None,
    current_appendix_num: str | None,
) -> bool:
    """Reject wrapped decimals, units, cross-article refs, and table-cell refs."""
    parts = cid.split(".")
    lead_num = int(parts[0])
    if lead_num > 99:
        return False
    if _UNIT_START_RE.match(rest):
        return False
    if rest and not (
        rest[0].isupper()
        or rest[0].isdigit()
        or rest[0] in '“"[('
        or bool(_INLINE_BULLET_START_RE.match(rest))
    ):
        return False
    if (
        current_section_kind == "normative"
        and active_article_num is not None
        and lead_num != active_article_num
    ):
        return False
    return not (
        current_section_kind == "appendix"
        and (
            (doc_type == "technical" and current_appendix_num == "5")
            or (doc_type == "sporting" and current_appendix_num == "7")
        )
    )


def _build_clauses_from_pages(
    doc: ManifestDocument,
    pages: Sequence[CleanedPage],
    page_tables_raw: Sequence[list[tuple[float, list[list[str]], str]]],
    page_line_coords: Sequence[Sequence[tuple[float, float]]],
    seen_counts: dict[str, int],
) -> list[ClauseRecord]:
    """Run hierarchical clause state machine over cleaned pages."""
    records: list[ClauseRecord] = []
    # Stack depths: 0=N.M, 1=N.M.K+, 2=lettered bullet (a), 3=roman bullet (i)
    open_stack: dict[int, _ClauseBuilder] = {}
    ordered_builders: list[_ClauseBuilder] = []

    current_article = "Preamble"
    active_article_num: int | None = None
    current_section_kind = "normative"
    in_toc = False
    body_started = False
    last_letter: str | None = None
    last_roman: str | None = None
    last_sub_letter: str | None = None
    last_sub_roman: str | None = None
    last_nonempty_line = ""
    just_opened_appendix = False
    current_appendix_num: str | None = None

    def _close_from(min_depth: int) -> None:
        for d in sorted(list(open_stack)):
            if d >= min_depth:
                del open_stack[d]

    def _open_clause(
        depth: int,
        clause_id: str,
        short_label: str,
        first_line: str,
        page_num: int,
        *,
        bullet_x0: float | None = None,
    ) -> _ClauseBuilder:
        _close_from(depth)
        ancestors = [open_stack[d] for d in sorted(open_stack) if d < depth]
        for anc in ancestors:
            anc.page_end = max(anc.page_end, page_num)
        parent_id = ancestors[-1].clause_id if ancestors else None
        crumb_parts = [current_article, *(anc.short_label for anc in ancestors), short_label]
        builder = _ClauseBuilder(
            article=current_article,
            clause_id=clause_id,
            parent_clause_id=parent_id,
            breadcrumb=" > ".join(crumb_parts),
            short_label=short_label,
            section_kind=current_section_kind,
            page_start=page_num,
            page_end=page_num,
            lines=[first_line] if first_line else [],
            raw_tables=[],
            has_diagram=False,
            bullet_x0=bullet_x0,
        )
        open_stack[depth] = builder
        ordered_builders.append(builder)
        return builder

    for page, tables_on_page, line_coords in zip(
        pages, page_tables_raw, page_line_coords, strict=True
    ):
        pnum = page.page_num
        clauses_touched_on_page: list[_ClauseBuilder] = []
        page_clause_anchors: list[tuple[float, _ClauseBuilder]] = []

        raw_lines = page.text.splitlines()
        coords_iter = (
            list(line_coords)
            if len(line_coords) == len(raw_lines)
            else [(0.0, 0.0)] * len(raw_lines)
        )

        for raw_line, (line_y, line_x0) in zip(raw_lines, coords_iter, strict=True):
            line = raw_line.strip()
            if not line:
                continue

            if pnum <= 12 and not body_started and _SPORTING_TOC_HEADER_RE.match(line):
                in_toc = True
                continue

            # Check Appendix heading (skip TOC appendix entries)
            app_match = _APPENDIX_HEADING_RE.match(line)
            if app_match:
                if _is_toc_appendix_line(
                    line, pnum, body_started=body_started, in_toc=in_toc
                ):
                    continue
                in_toc = False
                body_started = True
                app_num = app_match.group(1)
                current_appendix_num = app_num
                active_article_num = None
                current_section_kind = detect_section_kind(
                    line, current_section_kind, doc_type=doc.doc_type
                )
                current_article = f"Appendix {app_num}"
                last_letter = None
                last_roman = None
                last_sub_letter = None
                last_sub_roman = None
                last_nonempty_line = line
                just_opened_appendix = True
                builder = _open_clause(0, f"App.{app_num}", f"Appendix {app_num}", line, pnum)
                clauses_touched_on_page.append(builder)
                page_clause_anchors.append((line_y, builder))
                continue

            # Handle Sporting "APPROVED CHANGES FOR ..." on line after APPENDIX 9
            if just_opened_appendix and "APPROVED CHANGES FOR" in line.upper():
                current_section_kind = "amendment_preview"
                if 0 in open_stack:
                    open_stack[0].section_kind = "amendment_preview"
                    open_stack[0].lines.append(line)
                last_nonempty_line = line
                just_opened_appendix = False
                continue
            just_opened_appendix = False

            # Check Technical ARTICLE heading
            art_match = _ARTICLE_HEADING_RE.match(line)
            if art_match:
                art_num, art_title = art_match.group(1), (art_match.group(2) or "").strip()
                if _is_toc_article_line(
                    art_num, art_title, pnum, body_started=body_started, in_toc=in_toc
                ):
                    in_toc = True
                    continue
                in_toc = False
                body_started = True
                active_article_num = int(art_num)
                current_appendix_num = None
                current_article = f"Article {art_num}"
                current_section_kind = "normative"
                last_letter = None
                last_roman = None
                last_sub_letter = None
                last_sub_roman = None
                last_nonempty_line = ""
                _close_from(0)
                continue

            # Check Sporting "N) TITLE" heading
            sport_match = _SPORTING_ARTICLE_RE.match(line)
            if sport_match:
                art_num = sport_match.group(1)
                in_toc = False
                body_started = True
                active_article_num = int(art_num)
                current_appendix_num = None
                current_article = f"Article {art_num}"
                current_section_kind = "normative"
                last_letter = None
                last_roman = None
                last_sub_letter = None
                last_sub_roman = None
                last_nonempty_line = ""
                _close_from(0)
                continue

            if in_toc:
                continue

            # Check numbered clause (e.g., 3.10, 3.10.10, 28.1.1)
            num_match = _CLAUSE_NUM_RE.match(line)
            if num_match:
                cid = num_match.group(1)
                rest = (num_match.group(2) or "").strip()
                if _is_valid_clause_start(
                    cid,
                    rest,
                    doc_type=doc.doc_type,
                    current_section_kind=current_section_kind,
                    active_article_num=active_article_num,
                    current_appendix_num=current_appendix_num,
                ):
                    body_started = True
                    parts = cid.split(".")
                    if current_article == "Preamble":
                        current_article = f"Article {parts[0]}"
                        active_article_num = int(parts[0])
                    depth = 0 if len(parts) == 2 else 1
                    # If 3-part clause opens without an active 2-part parent, attach at depth 0/1
                    if depth == 1 and 0 in open_stack:
                        parent_cid = open_stack[0].clause_id
                        if not cid.startswith(parent_cid + "."):
                            _close_from(0)
                    inline_bullet = _INLINE_BULLET_START_RE.match(rest) if rest else None
                    if inline_bullet:
                        ltok = inline_bullet.group(1)
                        parent_builder = _open_clause(depth, cid, cid, cid, pnum)
                        clauses_touched_on_page.append(parent_builder)
                        page_clause_anchors.append((line_y, parent_builder))
                        child_builder = _open_clause(
                            2, f"{cid}({ltok})", f"({ltok})", rest, pnum, bullet_x0=None
                        )
                        clauses_touched_on_page.append(child_builder)
                        page_clause_anchors.append((line_y, child_builder))
                        last_letter = ltok
                        last_roman = None
                        last_sub_letter = None
                        last_sub_roman = None
                        last_nonempty_line = line
                        continue

                    short_label = line if len(line) <= 80 else cid
                    builder = _open_clause(depth, cid, short_label, line, pnum)
                    clauses_touched_on_page.append(builder)
                    page_clause_anchors.append((line_y, builder))
                    last_letter = None
                    last_roman = None
                    last_sub_letter = None
                    last_sub_roman = None
                    last_nonempty_line = line
                    continue

            # Check sub-bullets (roman vs lettered disambiguation)
            roman_match = _BULLET_ROMAN_RE.match(line)
            letter_match = _BULLET_LETTER_RE.match(line)

            handled_bullet = False
            indented_under_depth3 = (
                3 in open_stack
                and open_stack[3].bullet_x0 is not None
                and line_x0 > open_stack[3].bullet_x0 + 6.0
            )
            if roman_match and indented_under_depth3:
                rtok = (roman_match.group(1) or roman_match.group(2)).lower()
                rbody = roman_match.group(3)
                is_fifth_letter_ambig = (
                    (rtok == "i" and last_sub_letter == "h" and last_sub_roman is None)
                    or (rtok == "v" and last_sub_letter == "u" and last_sub_roman != "iv")
                    or (rtok == "x" and last_sub_letter == "w" and last_sub_roman != "ix")
                )
                sub_roman_ok = (
                    rtok in _ROMAN_VALUES
                    and not is_fifth_letter_ambig
                    and (
                        (rtok == "i" and (4 not in open_stack or last_sub_roman is None))
                        or (
                            last_sub_roman is not None
                            and _ROMAN_VALUES.index(rtok) > _ROMAN_VALUES.index(last_sub_roman)
                        )
                    )
                )
                expected_sub_roman_x0 = (
                    open_stack[4].bullet_x0
                    if (4 in open_stack and last_sub_roman is not None)
                    else None
                )
                if sub_roman_ok and not _is_wrapped_mid_sentence(
                    last_nonempty_line,
                    rbody,
                    line_x0=line_x0,
                    expected_bullet_x0=expected_sub_roman_x0,
                ):
                    parent_cid = open_stack[3].clause_id
                    cid = f"{parent_cid}({rtok})"
                    builder = _open_clause(
                        4, cid, f"({rtok})", f"{rtok}. {rbody}", pnum, bullet_x0=line_x0
                    )
                    clauses_touched_on_page.append(builder)
                    page_clause_anchors.append((line_y, builder))
                    last_sub_roman = rtok
                    last_sub_letter = None
                    last_nonempty_line = line
                    handled_bullet = True
            elif roman_match:
                rtok = (roman_match.group(1) or roman_match.group(2)).lower()
                rbody = roman_match.group(3)
                seq_ok = rtok in _ROMAN_VALUES and (
                    (rtok == "i" and (3 not in open_stack or last_roman is None))
                    or (
                        last_roman is not None
                        and _ROMAN_VALUES.index(rtok) > _ROMAN_VALUES.index(last_roman)
                    )
                )
                is_roman = False
                expected_roman_x0 = open_stack[3].bullet_x0 if 3 in open_stack else None
                if seq_ok and not _is_wrapped_mid_sentence(
                    last_nonempty_line,
                    rbody,
                    line_x0=line_x0,
                    expected_bullet_x0=expected_roman_x0,
                ):
                    if rtok not in {"i", "v", "x"}:
                        is_roman = True
                    elif rtok == "i":
                        if 2 in open_stack:
                            letter_x0 = open_stack[2].bullet_x0
                            if letter_x0 is not None and line_x0 > letter_x0 + 6.0:
                                is_roman = True
                            elif (
                                3 in open_stack
                                and open_stack[3].bullet_x0 is not None
                                and letter_x0 is not None
                                and open_stack[3].bullet_x0 > letter_x0 + 6.0
                                and abs(line_x0 - letter_x0) <= 6.0
                                and last_letter == "h"
                            ):
                                is_roman = False
                            elif last_letter != "h":
                                is_roman = True
                        elif last_letter != "h":
                            is_roman = True
                    elif rtok == "v":
                        is_roman = (3 in open_stack or last_roman == "iv") and last_letter != "u"
                    elif rtok == "x":
                        is_roman = (3 in open_stack or last_roman == "ix") and last_letter != "w"

                if is_roman and (2 in open_stack or 1 in open_stack or 0 in open_stack):
                    parent_depth = 2 if 2 in open_stack else (1 if 1 in open_stack else 0)
                    parent_cid = open_stack[parent_depth].clause_id
                    cid = f"{parent_cid}({rtok})"
                    builder = _open_clause(
                        3, cid, f"({rtok})", f"{rtok}. {rbody}", pnum, bullet_x0=line_x0
                    )
                    clauses_touched_on_page.append(builder)
                    page_clause_anchors.append((line_y, builder))
                    last_roman = rtok
                    last_sub_letter = None
                    last_sub_roman = None
                    last_nonempty_line = line
                    handled_bullet = True

            if not handled_bullet and letter_match:
                ltok = (letter_match.group(1) or letter_match.group(2)).lower()
                lbody = letter_match.group(3)
                if indented_under_depth3:
                    sub_mono_ok = (
                        ltok == "a"
                        or (last_sub_letter is not None and ord(ltok) > ord(last_sub_letter))
                    )
                    expected_sub_x0 = (
                        open_stack[4].bullet_x0
                        if (4 in open_stack and last_sub_letter is not None)
                        else None
                    )
                    if sub_mono_ok and not _is_wrapped_mid_sentence(
                        last_nonempty_line,
                        lbody,
                        line_x0=line_x0,
                        expected_bullet_x0=expected_sub_x0,
                    ):
                        parent_cid = open_stack[3].clause_id
                        cid = f"{parent_cid}({ltok})"
                        builder = _open_clause(
                            4, cid, f"({ltok})", f"{ltok}. {lbody}", pnum, bullet_x0=line_x0
                        )
                        clauses_touched_on_page.append(builder)
                        page_clause_anchors.append((line_y, builder))
                        last_sub_letter = ltok
                        last_sub_roman = None
                        last_nonempty_line = line
                        handled_bullet = True
                else:
                    mono_ok = ltok == "a" or last_letter is None or ord(ltok) > ord(last_letter)
                    is_next_letter = (
                        last_letter is not None and ord(ltok) == ord(last_letter) + 1
                    )
                    expected_letter_x0 = (
                        open_stack[2].bullet_x0
                        if (2 in open_stack and is_next_letter)
                        else None
                    )
                    if (
                        mono_ok
                        and not _is_wrapped_mid_sentence(
                            last_nonempty_line,
                            lbody,
                            line_x0=line_x0,
                            expected_bullet_x0=expected_letter_x0,
                        )
                        and (1 in open_stack or 0 in open_stack)
                    ):
                        parent_depth = 1 if 1 in open_stack else 0
                        parent_cid = open_stack[parent_depth].clause_id
                        cid = f"{parent_cid}({ltok})"
                        builder = _open_clause(
                            2, cid, f"({ltok})", f"{ltok}. {lbody}", pnum, bullet_x0=line_x0
                        )
                        clauses_touched_on_page.append(builder)
                        page_clause_anchors.append((line_y, builder))
                        last_letter = ltok
                        last_roman = None
                        last_sub_letter = None
                        last_sub_roman = None
                        last_nonempty_line = line
                        handled_bullet = True

            if handled_bullet:
                continue

            # Continuation prose line (reset bullet stack when prose outdents left of active bullet)
            if open_stack:
                if (
                    2 in open_stack
                    and open_stack[2].bullet_x0 is not None
                    and line_x0 > 0
                    and line_x0 <= open_stack[2].bullet_x0 - 4.0
                ):
                    _close_from(2)
                    last_roman = None
                    last_sub_letter = None
                    last_sub_roman = None
                elif (
                    3 in open_stack
                    and open_stack[3].bullet_x0 is not None
                    and line_x0 > 0
                    and line_x0 <= open_stack[3].bullet_x0 - 4.0
                ):
                    _close_from(3)
                    last_sub_letter = None
                    last_sub_roman = None
                elif (
                    4 in open_stack
                    and open_stack[4].bullet_x0 is not None
                    and line_x0 > 0
                    and line_x0 <= open_stack[4].bullet_x0 - 4.0
                ):
                    _close_from(4)
                    last_sub_letter = None
                    last_sub_roman = None

                if open_stack:
                    deepest_depth = max(open_stack)
                    target = open_stack[deepest_depth]
                    target.lines.append(line)
                    for anc in open_stack.values():
                        anc.page_end = max(anc.page_end, pnum)
                    if target not in clauses_touched_on_page:
                        clauses_touched_on_page.append(target)
                    page_clause_anchors.append((line_y, target))
            elif body_started or current_appendix_num is not None:
                # Fallback clause if prose appears directly under an Article without N.M
                fallback_id = current_article.replace("Article ", "").replace("Appendix ", "App.")
                builder = _open_clause(0, fallback_id, current_article, line, pnum)
                clauses_touched_on_page.append(builder)
                page_clause_anchors.append((line_y, builder))
            last_nonempty_line = line

        # Attach each table by vertical Y-position (nearest preceding clause above table_top_y)
        if tables_on_page:
            fallback_builder = (
                clauses_touched_on_page[-1]
                if clauses_touched_on_page
                else (open_stack[max(open_stack)] if open_stack else None)
            )
            for table_top_y, cleaned_rows, _ in tables_on_page:
                target_builder: _ClauseBuilder | None = None
                if page_clause_anchors:
                    preceding = [
                        b for anchor_y, b in page_clause_anchors if anchor_y <= table_top_y + 2.0
                    ]
                    target_builder = (
                        preceding[-1] if preceding else page_clause_anchors[0][1]
                    )
                if target_builder is None:
                    target_builder = fallback_builder
                if target_builder is not None:
                    target_builder.page_end = max(target_builder.page_end, pnum)
                    target_builder.raw_tables.append((pnum, cleaned_rows))

        if page.has_diagram:
            for b in clauses_touched_on_page:
                joined = "\n".join(b.lines)
                if _DIAGRAM_REF_RE.search(joined) or len(clauses_touched_on_page) == 1:
                    b.has_diagram = True
            if not clauses_touched_on_page and open_stack:
                open_stack[max(open_stack)].has_diagram = True

    for builder in ordered_builders:
        full_text = "\n".join(builder.lines).strip()
        if builder.raw_tables:
            stitched_tables = stitch_table_pages(
                [tbl for _, tbl in builder.raw_tables],
                page_nums=[p for p, _ in builder.raw_tables],
            )
        else:
            stitched_tables = []
        tables_md = tuple(md for tbl in stitched_tables if (md := format_table_markdown(tbl)))
        if not full_text and not tables_md:
            continue
        combined_for_refs = full_text
        if tables_md:
            combined_for_refs = full_text + "\n" + "\n".join(tables_md)
        chunk_id = _make_unique_chunk_id(
            doc.doc_id, builder.section_kind, builder.clause_id, seen_counts
        )
        records.append(
            ClauseRecord(
                chunk_id=chunk_id,
                doc_id=doc.doc_id,
                year=doc.year,
                doc_type=doc.doc_type,
                article=builder.article,
                clause_id=builder.clause_id,
                parent_clause_id=builder.parent_clause_id,
                breadcrumb=builder.breadcrumb,
                text=full_text,
                tables_markdown=tables_md,
                cross_refs=extract_cross_refs(combined_for_refs),
                page_start=builder.page_start,
                page_end=builder.page_end,
                section_kind=builder.section_kind,
                has_diagram=builder.has_diagram,
                source_sha256=doc.sha256,
            )
        )
    return records


def _validate_pdf_file(pdf_path: Path) -> None:
    if not pdf_path.is_file():
        raise ParseError(f"PDF file not found: {pdf_path}")
    try:
        with pdf_path.open("rb") as handle:
            magic = handle.read(len(_PDF_MAGIC))
    except OSError as exc:
        raise ParseError(f"Failed reading PDF file {pdf_path}: {exc}") from exc
    if magic != _PDF_MAGIC:
        raise ParseError(f"File is not a valid PDF (missing %PDF- header): {pdf_path}")


def parse_document(
    doc: ManifestDocument,
    pdf_path: Path,
    *,
    _seen_chunk_ids: dict[str, int] | None = None,
) -> tuple[list[CleanedPage], list[ClauseRecord]]:
    """Parse a single regulation PDF into cleaned pages and hierarchical clause records.

    Args:
        doc: Manifest entry describing document provenance and metadata.
        pdf_path: Local path to the PDF file.

    Returns:
        Tuple of (cleaned_pages, clause_records).

    Raises:
        ParseError: If the file is missing, non-PDF, corrupt, or has 0 pages.
    """
    path = Path(pdf_path)
    _validate_pdf_file(path)
    actual_sha256 = compute_sha256(path)
    if actual_sha256 != doc.sha256.lower():
        raise ParseError(
            f"SHA-256 mismatch for {path}: expected {doc.sha256.lower()}, got {actual_sha256}"
        )

    cleaned_pages: list[CleanedPage] = []
    page_tables_raw: list[list[tuple[float, list[list[str]], str]]] = []
    page_line_coords: list[list[tuple[float, float]]] = []
    current_kind = "normative"
    in_toc = False
    body_started = False

    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) == 0:
                raise ParseError(f"PDF has 0 pages (empty): {path}")
            if len(pdf.pages) != doc.expected_pages:
                raise ParseError(
                    f"Page count mismatch for {path}: "
                    f"expected {doc.expected_pages}, got {len(pdf.pages)}"
                )

            for page_num, page in enumerate(pdf.pages, start=1):
                filtered = filter_strikethrough_page(page)
                cleaned_text, line_coords = _extract_cleaned_lines_with_coords(filtered)
                page_kind, current_kind, in_toc, body_started = _update_section_kind_for_page(
                    cleaned_text,
                    current_kind,
                    page_num,
                    doc_type=doc.doc_type,
                    in_toc=in_toc,
                    body_started=body_started,
                )
                tables = _extract_page_tables(filtered)
                has_diagram = bool(getattr(page, "images", ())) and (
                    bool(_DIAGRAM_REF_RE.search(cleaned_text)) or not cleaned_text.strip()
                )
                cleaned_pages.append(
                    CleanedPage(
                        doc_id=doc.doc_id,
                        year=doc.year,
                        doc_type=doc.doc_type,
                        page_num=page_num,
                        section_kind=page_kind,
                        text=cleaned_text,
                        tables_markdown=tuple(md for _, _, md in tables),
                        has_diagram=has_diagram,
                        source_sha256=actual_sha256,
                    )
                )
                page_tables_raw.append(tables)
                page_line_coords.append(line_coords)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"Failed to parse PDF {path}: {exc}") from exc

    seen = _seen_chunk_ids if _seen_chunk_ids is not None else {}
    clauses = _build_clauses_from_pages(
        doc, cleaned_pages, page_tables_raw, page_line_coords, seen
    )
    return cleaned_pages, clauses


def _atomic_write_text(dest: Path, content: str) -> str:
    """Write UTF-8 text atomically via a temporary .part file and return its SHA-256."""
    part = dest.with_name(f"{dest.name}.part")
    data = content.encode("utf-8")
    try:
        part.write_bytes(data)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


def parse_corpus(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    raw_dir: Path = DEFAULT_RAW_DIR,
    processed_dir: Path = DEFAULT_PROCESSED_DIR,
    doc_ids: Iterable[str] | None = None,
) -> ParseReport:
    """Parse all (or selected) manifest PDFs and write deterministic JSONL/JSON outputs.

    Args:
        manifest_path: Path to corpus/manifest.json.
        raw_dir: Directory containing downloaded PDFs.
        processed_dir: Output directory for clauses.jsonl, pages.jsonl, and parse_summary.json.
        doc_ids: Optional iterable of doc_id values to parse; None parses all manifest docs.

    Returns:
        ParseReport with output paths, counts, and SHA-256 digests.

    Raises:
        ParseError: If any requested doc_id is unknown or any PDF fails parsing.
    """
    mpath = Path(manifest_path)
    manifest = load_manifest(mpath)
    manifest_sha256 = compute_sha256(mpath)

    if doc_ids is None:
        selected_docs = list(manifest.documents)
    else:
        if isinstance(doc_ids, str):
            raise ParseError("doc_ids must be an iterable of document IDs, not a bare string")
        requested = set(doc_ids)
        known_ids = {d.doc_id for d in manifest.documents}
        unknown = sorted(requested - known_ids)
        if unknown:
            raise ParseError(f"Unknown doc_id(s) requested: {', '.join(unknown)}")
        selected_docs = [d for d in manifest.documents if d.doc_id in requested]

    all_pages: list[CleanedPage] = []
    all_clauses: list[ClauseRecord] = []
    per_doc_stats: list[dict[str, Any]] = []
    seen_chunk_ids: dict[str, int] = {}

    raw_base = Path(raw_dir)
    for doc in selected_docs:
        pdf_path = raw_base / doc.filename
        pages, clauses = parse_document(doc, pdf_path, _seen_chunk_ids=seen_chunk_ids)
        all_pages.extend(pages)
        all_clauses.extend(clauses)
        per_doc_stats.append(
            {
                "doc_id": doc.doc_id,
                "year": doc.year,
                "doc_type": doc.doc_type,
                "pages": len(pages),
                "clauses": len(clauses),
                "diagram_pages": sum(1 for p in pages if p.has_diagram),
                "table_pages": sum(1 for p in pages if p.tables_markdown),
                "source_sha256": pages[0].source_sha256 if pages else doc.sha256,
            }
        )

    out_dir = Path(processed_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    clauses_path = out_dir / "clauses.jsonl"
    pages_path = out_dir / "pages.jsonl"
    summary_path = out_dir / "parse_summary.json"

    clauses_content = "\n".join(
        json.dumps(asdict(rec), ensure_ascii=False, sort_keys=True) for rec in all_clauses
    ) + ("\n" if all_clauses else "")
    pages_content = "\n".join(
        json.dumps(asdict(pg), ensure_ascii=False, sort_keys=True) for pg in all_pages
    ) + ("\n" if all_pages else "")

    clauses_sha256 = _atomic_write_text(clauses_path, clauses_content)
    pages_sha256 = _atomic_write_text(pages_path, pages_content)

    summary_payload = {
        "manifest_sha256": manifest_sha256,
        "corpus_name": manifest.corpus_name,
        "documents_parsed": len(selected_docs),
        "total_pages": len(all_pages),
        "total_clauses": len(all_clauses),
        "clauses_sha256": clauses_sha256,
        "pages_sha256": pages_sha256,
        "documents": per_doc_stats,
    }
    _atomic_write_text(
        summary_path,
        json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    return ParseReport(
        manifest_sha256=manifest_sha256,
        documents_parsed=len(selected_docs),
        total_pages=len(all_pages),
        total_clauses=len(all_clauses),
        clauses_path=clauses_path,
        pages_path=pages_path,
        summary_path=summary_path,
        clauses_sha256=clauses_sha256,
        pages_sha256=pages_sha256,
    )


__all__ = [
    "CleanedPage",
    "ClauseRecord",
    "DEFAULT_PROCESSED_DIR",
    "ParseError",
    "ParseReport",
    "clean_table_rows",
    "detect_section_kind",
    "extract_cross_refs",
    "filter_strikethrough_page",
    "format_table_markdown",
    "normalize_rotated_header_cell",
    "parse_corpus",
    "parse_document",
    "repair_coordinate_subscripts",
    "stitch_table_pages",
    "strip_running_footers",
]
