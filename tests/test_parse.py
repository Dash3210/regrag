"""Unit and integration tests for the Phase P1 clause-aware PDF parser."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from regrag.corpus.fetch import ManifestDocument
from regrag.corpus.parse import (
    DEFAULT_PROCESSED_DIR,
    ClauseRecord,
    CleanedPage,
    ParseError,
    ParseReport,
    clean_table_rows,
    detect_section_kind,
    extract_cross_refs,
    filter_strikethrough_page,
    format_table_markdown,
    normalize_rotated_header_cell,
    parse_corpus,
    parse_document,
    repair_coordinate_subscripts,
    stitch_table_pages,
    strip_running_footers,
)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_synthetic_pdf(
    page_streams: list[str],
    *,
    include_image_on_pages: set[int] | None = None,
) -> bytes:
    """Build a minimal valid PDF 1.4 document with Helvetica text, vector graphics, and images."""
    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add_obj(b"")  # placeholder 1
    pages_id = add_obj(b"")  # placeholder 2
    font_id = add_obj(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    image_id: int | None = None
    if include_image_on_pages:
        img_data = b"\x00\xff\xff\x00"
        image_id = add_obj(
            b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 4 >>\n"
            b"stream\n" + img_data + b"\nendstream"
        )

    page_ids: list[int] = []
    for idx, stream_str in enumerate(page_streams, start=1):
        stream_bytes = stream_str.encode("latin-1")
        content_id = add_obj(
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        xobj_entry = (
            f" /XObject << /Im1 {image_id} 0 R >>"
            if image_id is not None
            and include_image_on_pages is not None
            and idx in include_image_on_pages
            else ""
        )
        resources = f"<< /Font << /F1 {font_id} 0 R >>{xobj_entry} >>"
        page_id = add_obj(
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
            f"/Resources {resources} /Contents {content_id} 0 R >>".encode("ascii")
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
    )

    chunks = [b"%PDF-1.4\n"]
    offsets = [0]
    for obj_num, body in enumerate(objects, start=1):
        offsets.append(sum(len(c) for c in chunks))
        chunks.append(f"{obj_num} 0 obj\n".encode("ascii") + body + b"\nendobj\n")

    xref_offset = sum(len(c) for c in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


def _text_lines_stream(
    lines: list[str], *, start_x: int = 60, start_y: int = 780, step: int = 16
) -> str:
    ops = ["BT", "/F1 11 Tf"]
    y = start_y
    for line in lines:
        ops.append(f"1 0 0 1 {start_x} {y} Tm ({_pdf_escape(line)}) Tj")
        y -= step
    ops.append("ET")
    return "\n".join(ops)


def _table_stream(
    rows: list[list[str]],
    col_widths: list[int],
    *,
    start_x: int = 50,
    start_y: int = 720,
    row_height: int = 24,
) -> str:
    ops: list[str] = ["0 0 0 RG 0.5 w"]
    total_w = sum(col_widths)
    num_rows = len(rows)

    for r in range(num_rows + 1):
        y = start_y - r * row_height
        ops.append(f"{start_x} {y} m {start_x + total_w} {y} l S")

    x = start_x
    for width in col_widths:
        ops.append(f"{x} {start_y} m {x} {start_y - num_rows * row_height} l S")
        x += width
    end_y = start_y - num_rows * row_height
    ops.append(f"{start_x + total_w} {start_y} m {start_x + total_w} {end_y} l S")

    ops.append("BT /F1 9 Tf")
    for r, row in enumerate(rows):
        cx = start_x + 4
        cy = start_y - r * row_height - 15
        for cell, width in zip(row, col_widths, strict=True):
            if cell:
                ops.append(f"1 0 0 1 {cx} {cy} Tm ({_pdf_escape(cell)}) Tj")
            cx += width
    ops.append("ET")
    return "\n".join(ops)


def _make_manifest_doc(
    pdf_path: Path,
    *,
    doc_id: str = "fia_2025_technical",
    year: int = 2025,
    doc_type: str = "technical",
    page_count: int = 1,
) -> ManifestDocument:
    raw = pdf_path.read_bytes()
    return ManifestDocument(
        doc_id=doc_id,
        year=year,
        doc_type=doc_type,
        issue="Issue 1",
        published_date="2025-01-01",
        title=f"Synthetic {year} {doc_type}",
        filename=pdf_path.name,
        url=f"https://example.com/{pdf_path.name}",
        sha256=hashlib.sha256(raw).hexdigest(),
        expected_bytes=len(raw),
        expected_pages=page_count,
    )


@dataclass
class _FakePage:
    """Lightweight fake page matching pdfplumber's char/rect/line/filter interface."""

    chars: list[dict[str, Any]] = field(default_factory=list)
    rects: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    height: float = 842.0
    width: float = 595.0

    def filter(self, test_fn: Any) -> _FakePage:
        return _FakePage(
            chars=[c for c in self.chars if test_fn(c)],
            rects=[r for r in self.rects if test_fn(r)],
            lines=[ln for ln in self.lines if test_fn(ln)],
            images=list(self.images),
            height=self.height,
            width=self.width,
        )

    def extract_text(self, **_: Any) -> str:
        if not self.chars:
            return ""
        ordered = sorted(self.chars, key=lambda c: (round(c["top"], 1), c["x0"]))
        return "".join(str(c["text"]) for c in ordered)


def _chars_for_text(
    text: str, *, start_x: float, top: float, bottom: float, char_w: float = 5.0
) -> list[dict[str, Any]]:
    chars: list[dict[str, Any]] = []
    x = start_x
    for ch in text:
        chars.append(
            {
                "object_type": "char",
                "text": ch,
                "x0": x,
                "x1": x + char_w,
                "top": top,
                "bottom": bottom,
            }
        )
        x += char_w
    return chars


# ---------------------------------------------------------------------------
# 1. Nested numbered clauses + multi-page stitching (Technical & Sporting)
# ---------------------------------------------------------------------------


def test_parse_nested_clauses_and_multipage_stitching(tmp_path: Path) -> None:
    page1 = _text_lines_stream(
        [
            "ARTICLE 3: AERODYNAMIC COMPONENTS",
            "3.10 Rear Wing",
            "3.10.10 Drag Reduction System",
            "The DRS bodywork must comply with:",
            "a. Flap incidence may vary in accordance with Article 3.10.1.",
            "b. Fail-safe mechanism:",
            "i. Spring return must close within 400ms.",
            "ii. Mechanical stop required.",
            "2025 Formula 1 Technical Regulations 33 07 April 2025",
            "© 2025 Fédération Internationale de l'Automobile Issue 3",
        ]
    )
    page2 = _text_lines_stream(
        [
            "c. Minimum gap between 9.4mm and 85mm when deployed.",
            "2025 Formula 1 Technical Regulations 34 07 April 2025",
            "© 2025 Fédération Internationale de l'Automobile Issue 3",
        ]
    )
    pdf_path = tmp_path / "tech_nested.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([page1, page2]))
    doc = _make_manifest_doc(pdf_path, page_count=2)

    pages, clauses = parse_document(doc, pdf_path)

    assert len(pages) == 2
    assert all(isinstance(p, CleanedPage) for p in pages)
    assert all("Fédération Internationale" not in p.text for p in pages)

    by_id = {c.clause_id: c for c in clauses}
    assert "3.10" in by_id
    assert "3.10.10" in by_id
    assert "3.10.10(a)" in by_id
    assert "3.10.10(b)" in by_id
    assert "3.10.10(b)(i)" in by_id
    assert "3.10.10(b)(ii)" in by_id
    assert "3.10.10(c)" in by_id

    c_parent = by_id["3.10.10"]
    assert c_parent.article == "Article 3"
    assert c_parent.parent_clause_id == "3.10"
    assert c_parent.page_start == 1
    assert c_parent.page_end == 2
    assert c_parent.section_kind == "normative"

    c_a = by_id["3.10.10(a)"]
    assert c_a.parent_clause_id == "3.10.10"
    assert "Article 3" in c_a.breadcrumb
    assert "3.10.10" in c_a.breadcrumb
    assert "(a)" in c_a.breadcrumb
    assert "Article 3.10.1" in c_a.cross_refs

    c_i = by_id["3.10.10(b)(i)"]
    assert c_i.parent_clause_id == "3.10.10(b)"
    assert "(b)" in c_i.breadcrumb
    assert "(i)" in c_i.breadcrumb

    c_c = by_id["3.10.10(c)"]
    assert c_c.parent_clause_id == "3.10.10"
    assert c_c.page_start == 2
    assert c_c.page_end == 2
    assert "9.4mm" in c_c.text


def test_parse_sporting_style_article_headers(tmp_path: Path) -> None:
    page1 = _text_lines_stream(
        [
            "ARTICLE 28 - POWER UNIT",
            "28.1 Unless otherwise stated in the Technical Regulations:",
            "28.1.1 Each driver may use no more than 4 ICE elements.",
            "a. Penalties apply under Article 28.3.",
            "2025 Formula 1 Sporting Regulations 25 30 April 2025",
            "© 2025 Fédération Internationale de l'Automobile Issue 5",
        ]
    )
    pdf_path = tmp_path / "sporting.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([page1]))
    doc = _make_manifest_doc(
        pdf_path, doc_id="fia_2025_sporting", doc_type="sporting", page_count=1
    )

    pages, clauses = parse_document(doc, pdf_path)

    assert len(pages) == 1
    by_id = {c.clause_id: c for c in clauses}
    assert by_id["28.1.1"].article == "Article 28"
    assert by_id["28.1.1"].parent_clause_id == "28.1"
    assert by_id["28.1.1(a)"].doc_type == "sporting"
    assert "Technical Regulations" in by_id["28.1"].cross_refs
    assert "Article 28.3" in by_id["28.1.1(a)"].cross_refs


# ---------------------------------------------------------------------------
# 2. Numeric-limit tables, orphan continuation stitching, App. 3 rotation
# ---------------------------------------------------------------------------


def test_table_markdown_preserves_empty_min_max_and_units(tmp_path: Path) -> None:
    rows = [
        ["Property", "Units", "Min", "Max", "Test Method"],
        ["(RON+MON)/2", "", ">= 87", "", "ASTM D 2699"],
        ["Oxygen", "wt%", "3.45", "", "Elemental"],
        ["DVPE", "kPa", "45", "68", "EN 13016"],
        ["Sulphur", "mg/kg", "", "<= 10", "ASTM D 5453"],
        ["FBP", "oC", "", "<= 210", "ASTM D 86"],
    ]
    stream = _text_lines_stream(["ARTICLE 16: FUEL", "16.3 Fuel properties"], start_y=790)
    stream += "\n" + _table_stream(rows, [110, 60, 60, 60, 120], start_y=730)

    pdf_path = tmp_path / "fuel_table.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([stream]))
    doc = _make_manifest_doc(pdf_path, page_count=1)

    pages, clauses = parse_document(doc, pdf_path)

    assert len(pages[0].tables_markdown) == 1
    md = pages[0].tables_markdown[0]
    assert "| Property | Units | Min | Max | Test Method |" in md
    assert "| (RON+MON)/2 |  | >= 87 |  | ASTM D 2699 |" in md
    assert "| Oxygen | wt% | 3.45 |  | Elemental |" in md
    assert "| DVPE | kPa | 45 | 68 | EN 13016 |" in md
    assert "| Sulphur | mg/kg |  | <= 10 | ASTM D 5453 |" in md
    assert "| FBP | oC |  | <= 210 | ASTM D 86 |" in md
    assert clauses[-1].tables_markdown == pages[0].tables_markdown


def test_stitch_table_pages_app5_orphan_continuation_row() -> None:
    page1_table = [
        ["Ref.", "Component", "Art.", "Class.", "Included Components", "Excluded Components"],
        ["2C", "DRS adjuster", "3.10.10", "OSC", "Mechanical actuators", ""],
    ]
    page2_table = [
        ["Ref.", "Component", "Art.", "Class.", "Included Components", "Excluded Components"],
        ["", "", "", "", "and electronic components", ""],
        ["2E", "Wheel drum", "3.13.1", "LTC", "Drum and deflector", ""],
    ]

    stitched = stitch_table_pages([page1_table, page2_table])

    assert len(stitched) == 1
    rows = stitched[0]
    assert len(rows) == 3
    assert rows[1][0] == "2C"
    assert "Mechanical actuators" in rows[1][4]
    assert "and electronic components" in rows[1][4]
    assert rows[2][0] == "2E"


def test_rotated_vertical_header_reversal_and_spacer_column_removal() -> None:
    assert normalize_rotated_header_cell("o\nN\nm\ne\nt\nI") == "Item No."
    assert normalize_rotated_header_cell("oN\nmetI") == "Item No."
    assert normalize_rotated_header_cell("Normal Header") == "Normal Header"

    sparse_rows: list[list[str | None]] = [
        [None, "o\nN\nm\ne\nt\nI", None, "List of PU components", None],
        [None, "1", None, "ICE Crankcase", None],
    ]
    cleaned = clean_table_rows(sparse_rows)
    assert cleaned == [
        ["Item No.", "List of PU components"],
        ["1", "ICE Crankcase"],
    ]
    md = format_table_markdown(cleaned)
    assert "| Item No. | List of PU components |" in md
    assert "| 1 | ICE Crankcase |" in md


# ---------------------------------------------------------------------------
# 3. Cross-reference extraction
# ---------------------------------------------------------------------------


def test_extract_cross_refs_deduplicated_in_appearance_order() -> None:
    sample = (
        "Defined in Article 4.7 and Article 12.1.4. "
        "See Appendix 5, Sporting Regulations, Technical Regulations, "
        "and again Article 4.7 and Appendix 5."
    )
    refs = extract_cross_refs(sample)
    assert refs == (
        "Article 4.7",
        "Article 12.1.4",
        "Appendix 5",
        "Sporting Regulations",
        "Technical Regulations",
    )


# ---------------------------------------------------------------------------
# 4. Non-black strikethrough deletion filter (with mutation-precondition test)
# ---------------------------------------------------------------------------


def test_non_black_strikethrough_filter_with_mutation_precondition() -> None:
    # Build "0.5mm 2mm" where "2mm" has a pink (1.0, 0.0, 1.0) thin rect over it,
    # and "0.5mm" has a pure black (0.0, 0.0, 0.0) thin underline/border rect over it.
    chars = (
        _chars_for_text("0.5mm", start_x=100.0, top=160.0, bottom=171.0)
        + _chars_for_text(" ", start_x=125.0, top=160.0, bottom=171.0)
        + _chars_for_text("2mm", start_x=130.0, top=160.0, bottom=171.0)
    )
    pink_strike = {
        "object_type": "rect",
        "x0": 130.0,
        "x1": 145.0,
        "top": 165.0,
        "bottom": 165.72,
        "width": 15.0,
        "height": 0.72,
        "non_stroking_color": (1.0, 0.0, 1.0),
        "stroking_color": 0,
    }
    black_border = {
        "object_type": "rect",
        "x0": 100.0,
        "x1": 125.0,
        "top": 165.0,
        "bottom": 165.72,
        "width": 25.0,
        "height": 0.72,
        "non_stroking_color": (0.0, 0.0, 0.0),
        "stroking_color": 0,
    }
    page = _FakePage(chars=chars, rects=[pink_strike, black_border])

    # Mutation precondition: raw unfiltered text MUST contain "0.5mm 2mm"
    assert page.extract_text() == "0.5mm 2mm"

    filtered = filter_strikethrough_page(page)
    assert filtered.extract_text().strip() == "0.5mm"


def test_dark_red_narrow_strikethrough_and_black_line_pdf_roundtrip(tmp_path: Path) -> None:
    # Real PDF with "0.5mm 2mm" (pink rect over "2mm"), "798kg 800kg" (dark-red rect over "798kg"),
    # and a black thin rect over "keep" that must NOT delete "keep".
    stream = "\n".join(
        [
            "BT /F1 11 Tf",
            "1 0 0 1 70 700 Tm (0.5mm 2mm) Tj",
            "1 0 0 1 70 670 Tm (798kg 800kg) Tj",
            "1 0 0 1 70 640 Tm (keep) Tj",
            "ET",
            # Pink rect over "2mm" (starts after "0.5mm " around x=107, width=24, y=703.5)
            "1 0 1 rg 107 703.5 24 0.72 re f",
            # Dark red rect over "798kg" (starts around x=70, width=30, y=673.5)
            "0.753 0 0 rg 70 673.5 31 0.72 re f",
            # Pure black rect over "keep" (must NOT delete!)
            "0 0 0 rg 70 643.5 26 0.72 re f",
        ]
    )
    pdf_path = tmp_path / "strike.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([stream]))

    with pdfplumber.open(pdf_path) as pdf:
        raw_page = pdf.pages[0]
        raw_text = raw_page.extract_text() or ""
        # Mutation precondition: raw text contains both deleted & active values
        assert "0.5mm 2mm" in raw_text
        assert "798kg 800kg" in raw_text

        cleaned_page = filter_strikethrough_page(raw_page)
        cleaned_text = cleaned_page.extract_text() or ""
        assert "0.5mm" in cleaned_text
        assert "2mm" not in cleaned_text
        assert "800kg" in cleaned_text
        assert "798kg" not in cleaned_text
        assert "keep" in cleaned_text


# ---------------------------------------------------------------------------
# 5. Running footer stripping (bottom AND top of stream)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_text,expected_fragment",
    [
        (
            "3.10.10 Rear Wing\n"
            "2024 Formula 1 Technical Regulations 33 17 October 2024\n"
            "© 2024 Fédération Internationale de l’Automobile Issue 8",
            "3.10.10 Rear Wing",
        ),
        (
            "2024 Formula 1 Technical Regulations 162 17 October 2024\n"
            "© 2024 Fédération Internationale de l’Automobile Issue 8\n"
            "APPENDIX 3: POWER UNIT",
            "APPENDIX 3: POWER UNIT",
        ),
        (
            "2025 Formula 1 Sporting Regulations 14 30 April 2025\n"
            "© 2025 Fédération Internationale de l'Automobile Issue 5\n"
            "28.1 Power Unit",
            "28.1 Power Unit",
        ),
    ],
)
def test_strip_running_footers_top_and_bottom(raw_text: str, expected_fragment: str) -> None:
    stripped = strip_running_footers(raw_text)
    assert "Fédération Internationale" not in stripped
    assert "Formula 1 Technical Regulations" not in stripped
    assert "Formula 1 Sporting Regulations" not in stripped
    assert stripped.strip() == expected_fragment


# ---------------------------------------------------------------------------
# 6. Coordinate subscript repair
# ---------------------------------------------------------------------------


def test_repair_coordinate_subscripts_without_mangling_prose() -> None:
    assert repair_coordinate_subscripts("X =0 and X =0\nF R") == "X_F=0 and X_R=0"
    assert repair_coordinate_subscripts("X =-1100mm.\nF") == "X_F=-1100mm."
    assert repair_coordinate_subscripts("rearward of X = -1150\nC") == "rearward of X_C=-1150"

    prose = "a. Item one\nb. Item two\nC is a constant in Article 9.4."
    assert repair_coordinate_subscripts(prose) == prose


# ---------------------------------------------------------------------------
# 7. section_kind isolation (normative vs appendix vs amendment_preview)
# ---------------------------------------------------------------------------


def test_section_kind_isolation_appendix_6_never_normative(tmp_path: Path) -> None:
    assert detect_section_kind("ARTICLE 4: MASS") == "normative"
    assert detect_section_kind("APPENDIX 5: COMPONENTS") == "appendix"
    assert detect_section_kind("APPENDIX 6: APPROVED CHANGES FOR 2025") == "amendment_preview"

    p1 = _text_lines_stream(
        [
            "ARTICLE 4: MASS",
            "4.1 Minimum mass",
            "The mass of the car must not be less than 798kg.",
        ]
    )
    p2 = _text_lines_stream(
        [
            "APPENDIX 5: COMPONENTS CLASSIFICATION",
            "Components are classified below.",
        ]
    )
    p3 = _text_lines_stream(
        [
            "APPENDIX 6: APPROVED CHANGES FOR 2025",
            "4.1 Minimum mass",
            "The mass of the car must not be less than 800kg.",
        ]
    )
    pdf_path = tmp_path / "sections.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1, p2, p3]))
    doc = _make_manifest_doc(
        pdf_path, doc_id="fia_2024_technical", year=2024, page_count=3
    )

    pages, clauses = parse_document(doc, pdf_path)

    assert [p.section_kind for p in pages] == ["normative", "appendix", "amendment_preview"]

    c_norm = [c for c in clauses if c.clause_id == "4.1" and "798kg" in c.text]
    c_preview = [c for c in clauses if c.clause_id == "4.1" and "800kg" in c.text]
    assert len(c_norm) == 1
    assert c_norm[0].section_kind == "normative"
    assert len(c_preview) == 1
    assert c_preview[0].section_kind == "amendment_preview"
    assert c_preview[0].section_kind != "normative"
    assert c_norm[0].chunk_id != c_preview[0].chunk_id


# ---------------------------------------------------------------------------
# 8. Raster diagram marker
# ---------------------------------------------------------------------------


def test_raster_diagram_detection(tmp_path: Path) -> None:
    p1 = (
        _text_lines_stream(
            [
                "ARTICLE 5: POWER UNIT",
                "5.3.2 The energy flow diagram is shown below.",
            ]
        )
        + "\nq 100 0 0 100 70 400 cm /Im1 Do Q"
    )
    p2 = _text_lines_stream(["5.3.3 Text only without diagram."])
    pdf_path = tmp_path / "diagram.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1, p2], include_image_on_pages={1}))
    doc = _make_manifest_doc(pdf_path, page_count=2)

    pages, clauses = parse_document(doc, pdf_path)

    assert pages[0].has_diagram is True
    assert pages[1].has_diagram is False
    by_id = {c.clause_id: c for c in clauses}
    assert by_id["5.3.2"].has_diagram is True
    assert by_id["5.3.3"].has_diagram is False


# ---------------------------------------------------------------------------
# 9. Malformed / corrupt / empty PDF guards (with mutation-precondition test)
# ---------------------------------------------------------------------------


def test_malformed_corrupt_and_empty_pdf_guards(tmp_path: Path) -> None:
    valid_pdf = tmp_path / "valid.pdf"
    valid_stream = _text_lines_stream(["ARTICLE 1: GENERAL", "1.1 Scope."])
    valid_pdf.write_bytes(_build_synthetic_pdf([valid_stream]))
    valid_doc = _make_manifest_doc(valid_pdf, page_count=1)

    # Mutation precondition: valid synthetic PDF parses cleanly
    pages, clauses = parse_document(valid_doc, valid_pdf)
    assert len(pages) == 1
    assert len(clauses) >= 1
    assert all(isinstance(c, ClauseRecord) for c in clauses)

    # Missing file
    missing_pdf = tmp_path / "missing.pdf"
    with pytest.raises(ParseError, match="missing|exist|not found"):
        parse_document(valid_doc, missing_pdf)

    # Non-PDF bytes
    non_pdf = tmp_path / "not_a_pdf.pdf"
    non_pdf.write_bytes(b"<html>not a pdf</html>")
    with pytest.raises(ParseError):
        parse_document(valid_doc, non_pdf)

    # Truncated / corrupt PDF
    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog")
    corrupt_doc = _make_manifest_doc(corrupt_pdf, page_count=1)
    with pytest.raises(ParseError):
        parse_document(corrupt_doc, corrupt_pdf)

    # 0-page PDF
    empty_pdf = tmp_path / "zero_pages.pdf"
    empty_pdf.write_bytes(_build_synthetic_pdf([]))
    empty_doc = _make_manifest_doc(empty_pdf, page_count=1)
    with pytest.raises(ParseError, match="0 pages|empty"):
        parse_document(empty_doc, empty_pdf)

    # SHA-256 mismatch guard
    wrong_sha_doc = ManifestDocument(
        doc_id=valid_doc.doc_id,
        year=valid_doc.year,
        doc_type=valid_doc.doc_type,
        issue=valid_doc.issue,
        published_date=valid_doc.published_date,
        title=valid_doc.title,
        filename=valid_doc.filename,
        url=valid_doc.url,
        sha256="f" * 64,
        expected_bytes=valid_doc.expected_bytes,
        expected_pages=1,
    )
    with pytest.raises(ParseError, match="SHA-256|checksum|mismatch"):
        parse_document(wrong_sha_doc, valid_pdf)

    # expected_pages mismatch guard
    wrong_pages_doc = _make_manifest_doc(valid_pdf, page_count=99)
    with pytest.raises(ParseError, match="(?i)expected_pages|page count|pages"):
        parse_document(wrong_pages_doc, valid_pdf)


# ---------------------------------------------------------------------------
# 10. Deterministic output, atomic writes, unique chunk_ids across corpus
# ---------------------------------------------------------------------------


def test_parse_corpus_deterministic_sha256_and_unique_chunk_ids(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    processed_dir = tmp_path / "processed"

    pdf1 = raw_dir / "doc_2024.pdf"
    pdf1.write_bytes(
        _build_synthetic_pdf(
            [
                _text_lines_stream(
                    [
                        "ARTICLE 4: MASS",
                        "4.1 Minimum mass",
                        "Minimum car mass is 798kg.",
                        "APPENDIX 6: APPROVED CHANGES FOR 2025",
                        "4.1 Minimum mass",
                        "Minimum car mass is 800kg.",
                    ]
                )
            ]
        )
    )
    pdf2 = raw_dir / "doc_2025.pdf"
    pdf2.write_bytes(
        _build_synthetic_pdf(
            [
                _text_lines_stream(
                    [
                        "ARTICLE 4: MASS",
                        "4.1 Minimum mass",
                        "Minimum car mass is 800kg under Article 4.7.",
                    ]
                )
            ]
        )
    )

    manifest_path = tmp_path / "manifest.json"
    manifest_payload = {
        "manifest_version": 1,
        "corpus_name": "fia-f1-regulations",
        "documents": [
            {
                "doc_id": "fia_2024_technical",
                "year": 2024,
                "doc_type": "technical",
                "issue": "Issue 8",
                "published_date": "2024-10-17",
                "title": "2024 Tech",
                "filename": pdf1.name,
                "url": "https://example.com/2024.pdf",
                "sha256": hashlib.sha256(pdf1.read_bytes()).hexdigest(),
                "expected_bytes": pdf1.stat().st_size,
                "expected_pages": 1,
            },
            {
                "doc_id": "fia_2025_technical",
                "year": 2025,
                "doc_type": "technical",
                "issue": "Issue 3",
                "published_date": "2025-04-07",
                "title": "2025 Tech",
                "filename": pdf2.name,
                "url": "https://example.com/2025.pdf",
                "sha256": hashlib.sha256(pdf2.read_bytes()).hexdigest(),
                "expected_bytes": pdf2.stat().st_size,
                "expected_pages": 1,
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

    assert Path("data/processed") == DEFAULT_PROCESSED_DIR

    report1 = parse_corpus(
        manifest_path=manifest_path,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
    )
    assert isinstance(report1, ParseReport)
    assert report1.documents_parsed == 2
    assert report1.total_pages == 2
    assert report1.total_clauses >= 3
    assert report1.clauses_path.is_file()
    assert report1.pages_path.is_file()
    assert report1.summary_path.is_file()

    clause_rows = [
        json.loads(line)
        for line in report1.clauses_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chunk_ids = [row["chunk_id"] for row in clause_rows]
    assert len(chunk_ids) == len(set(chunk_ids)), "chunk_ids must be 100% unique across corpus"

    summary = json.loads(report1.summary_path.read_text(encoding="utf-8"))
    assert summary["clauses_sha256"] == report1.clauses_sha256
    assert summary["pages_sha256"] == report1.pages_sha256

    # Repeat run must produce bitwise identical digests
    report2 = parse_corpus(
        manifest_path=manifest_path,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
    )
    assert report2.clauses_sha256 == report1.clauses_sha256
    assert report2.pages_sha256 == report1.pages_sha256

    # Filtered doc_ids run
    report_filtered = parse_corpus(
        manifest_path=manifest_path,
        raw_dir=raw_dir,
        processed_dir=tmp_path / "processed_filtered",
        doc_ids=["fia_2025_technical"],
    )
    assert report_filtered.documents_parsed == 1
    assert report_filtered.total_pages == 1


def test_toc_skipping_and_sporting_appendix_9_amendment_preview(tmp_path: Path) -> None:
    tech_p1 = _text_lines_stream(
        [
            "ARTICLE 4: MASS 47",
            "4.1 Minimum mass",
            "APPENDIX 6: APPROVED CHANGES FOR 2025 178",
        ]
    )
    tech_p2 = _text_lines_stream(
        [
            "ARTICLE 4: MASS",
            "4.1 Minimum mass",
            "The mass must not be less than 798kg.",
        ]
    )
    tech_pdf = tmp_path / "tech_toc.pdf"
    tech_pdf.write_bytes(_build_synthetic_pdf([tech_p1, tech_p2]))
    tech_doc = _make_manifest_doc(tech_pdf, page_count=2)

    tech_pages, tech_clauses = parse_document(tech_doc, tech_pdf)
    assert [p.section_kind for p in tech_pages] == ["normative", "normative"]
    assert len(tech_clauses) == 1
    assert tech_clauses[0].clause_id == "4.1"
    assert tech_clauses[0].section_kind == "normative"
    assert tech_clauses[0].page_start == 2

    sport_p1 = _text_lines_stream(
        [
            "28) POWER UNIT USAGE",
            "28.1 Each driver may use 4 ICE elements.",
            "APPENDIX 6",
            "Sporting appendix text.",
            "APPENDIX 9",
            "APPROVED CHANGES FOR SUBSEQUENT YEARS",
            "28.1 Future 3 ICE limit.",
        ]
    )
    sport_pdf = tmp_path / "sport_app9.pdf"
    sport_pdf.write_bytes(_build_synthetic_pdf([sport_p1]))
    sport_doc = _make_manifest_doc(
        sport_pdf, doc_id="fia_2025_sporting", doc_type="sporting", page_count=1
    )

    _, sport_clauses = parse_document(sport_doc, sport_pdf)
    kinds = [(c.clause_id, c.section_kind) for c in sport_clauses]
    assert ("28.1", "normative") in kinds
    assert ("App.6", "appendix") in kinds
    assert ("App.9", "amendment_preview") in kinds
    assert ("28.1", "amendment_preview") in kinds


def test_strikethrough_line_cmyk_grayscale_and_unfilled_rect() -> None:
    chars = (
        _chars_for_text("keep", start_x=10.0, top=100.0, bottom=111.0)
        + _chars_for_text(" ", start_x=30.0, top=100.0, bottom=111.0)
        + _chars_for_text("drop", start_x=35.0, top=100.0, bottom=111.0)
    )
    black_cmyk = {
        "object_type": "rect",
        "x0": 10.0,
        "x1": 30.0,
        "top": 105.0,
        "bottom": 105.7,
        "non_stroking_color": (0.0, 0.0, 0.0, 1.0),
    }
    white_gray = {
        "object_type": "rect",
        "x0": 10.0,
        "x1": 30.0,
        "top": 105.0,
        "bottom": 105.7,
        "non_stroking_color": 1.0,
    }
    pink_line = {
        "object_type": "line",
        "x0": 35.0,
        "x1": 55.0,
        "top": 105.0,
        "bottom": 105.5,
        "stroking_color": (1.0, 0.0, 1.0),
    }
    page = _FakePage(chars=chars, rects=[black_cmyk, white_gray], lines=[pink_line])
    assert page.extract_text() == "keep drop"
    filtered = filter_strikethrough_page(page)
    assert filtered.extract_text().strip() == "keep"


def test_table_helpers_edge_cases_and_multipage_clause_table_stitching(tmp_path: Path) -> None:
    assert clean_table_rows([]) == []
    assert clean_table_rows([[]]) == []
    assert clean_table_rows([[None, ""]]) == []
    assert format_table_markdown([]) == ""
    assert normalize_rotated_header_cell(" \n ") == ""
    assert "| A \\| B | C |" in format_table_markdown([["A | B", "C"], ["1", "2"]])

    p1 = _text_lines_stream(["APPENDIX 5: CLASSIFICATION"], start_y=790) + "\n" + _table_stream(
        [["Ref.", "Component", "Included"], ["2C", "DRS", "Mechanical"]],
        [60, 120, 180],
        start_y=740,
    )
    p2 = _table_stream(
        [["Ref.", "Component", "Included"], ["", "", "and electronic"], ["2E", "Drum", "Supply"]],
        [60, 120, 180],
        start_y=740,
    )
    pdf_path = tmp_path / "app5_stitch.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1, p2]))
    doc = _make_manifest_doc(pdf_path, page_count=2)

    pages, clauses = parse_document(doc, pdf_path)
    assert len(pages) == 2
    app5 = [c for c in clauses if c.clause_id == "App.5"]
    assert len(app5) == 1
    assert app5[0].page_start == 1
    assert app5[0].page_end == 2
    assert len(app5[0].tables_markdown) == 1
    assert "Mechanical and electronic" in app5[0].tables_markdown[0]


def test_parse_corpus_invalid_doc_ids_raise_parse_error(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "corpus_name": "test",
                "documents": [
                    {
                        "doc_id": "d1",
                        "year": 2025,
                        "doc_type": "technical",
                        "issue": "Issue 1",
                        "published_date": "2025-01-01",
                        "title": "D1",
                        "filename": "d1.pdf",
                        "url": "https://example.com/d1.pdf",
                        "sha256": "0" * 64,
                        "expected_bytes": 100,
                        "expected_pages": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ParseError, match="bare string"):
        parse_corpus(manifest_path=manifest_path, doc_ids="d1")
    with pytest.raises(ParseError, match="Unknown doc_id"):
        parse_corpus(manifest_path=manifest_path, doc_ids=["missing"])


def test_strikethrough_zero_gap_adjacent_chars_not_deleted() -> None:
    # Pink strikethrough over "no" in "30.5no)" and over "1," in "Article 51.1,"
    # Adjacent non-struck characters ("30.5", ")", "51.") must remain intact.
    chars = (
        _chars_for_text("30.5", start_x=10.0, top=100.0, bottom=111.0, char_w=5.0)
        + _chars_for_text("no", start_x=30.0, top=100.0, bottom=111.0, char_w=5.0)
        + _chars_for_text(")", start_x=40.0, top=100.0, bottom=111.0, char_w=5.0)
    )
    pink_no = {
        "object_type": "rect",
        "x0": 30.0,
        "x1": 40.0,
        "top": 105.0,
        "bottom": 105.7,
        "non_stroking_color": (1.0, 0.0, 1.0),
    }
    page = _FakePage(chars=chars, rects=[pink_no])
    assert page.extract_text() == "30.5no)"
    filtered = filter_strikethrough_page(page)
    assert filtered.extract_text() == "30.5)"


def test_wrapped_cross_refs_decimals_and_a_prefixed_clauses(tmp_path: Path) -> None:
    p1 = _text_lines_stream(
        [
            "ARTICLE 2: DEFINITIONS",
            "2.11 Coordinate system",
            "2.11.1 A right-handed Cartesian coordinate system will be used.",
            "a. Reference planes per",
            "3.9 of the Technical Regulations.",
            "1.25 times the load.",
            "3.45 wt% oxygen limit.",
            "ARTICLE 34: PIT LANE",
            "34.7 A speed limit of 80km/h will be imposed.",
            "APPENDIX 5: COMPONENTS CLASSIFICATION",
            "6.6.6 pressurisation system inside table cell.",
            "APPENDIX 7: AERODYNAMIC TESTING",
            "3.9 of the Technical Regulations applies here.",
        ]
    )
    pdf_path = tmp_path / "hijack_and_a.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1]))
    doc = _make_manifest_doc(pdf_path, page_count=1)

    _, clauses = parse_document(doc, pdf_path)
    cids = [c.clause_id for c in clauses]
    assert "2.11.1" in cids
    assert "34.7" in cids
    assert "3.9" not in cids
    assert "1.25" not in cids
    assert "3.45" not in cids
    assert "6.6.6" not in cids


def test_sporting_page1_toc_remains_normative_and_bullet_disambiguation(tmp_path: Path) -> None:
    p1 = _text_lines_stream(
        [
            "2025 FORMULA ONE SPORTING REGULATIONS",
            "ART CONTENTS PAGE ART CONTENTS PAGE",
            "1 REGULATIONS 2 35 WEIGHING 43",
            "35 WEIGHING 43 APPENDIX 9: APPROVED CHANGES FOR",
            "SUBSEQUENT YEARS 119",
        ]
    )
    p2 = _text_lines_stream(
        [
            "35) WEIGHING",
            "35.1 Weighing procedure:",
            "a) First item.",
            "b) Second item:",
            "i) Roman one.",
            "ii) Roman two.",
            "iii) Roman three.",
            "iv) Roman four.",
            "v) Roman five.",
            "g) Letter g.",
            "h) Letter h.",
            "i) Letter i after h.",
            "APPENDIX 9",
            "APPROVED CHANGES FOR SUBSEQUENT YEARS",
            "35.1 Future weighing rule.",
        ]
    )
    pdf_path = tmp_path / "sport_toc_bullets.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1, p2]))
    doc = _make_manifest_doc(
        pdf_path, doc_id="fia_2025_sporting", doc_type="sporting", page_count=2
    )

    pages, clauses = parse_document(doc, pdf_path)
    assert pages[0].section_kind == "normative"
    assert pages[1].section_kind == "normative"

    cids = {c.clause_id for c in clauses}
    assert "35.1(b)(i)" in cids
    assert "35.1(b)(iv)" in cids
    assert "35.1(b)(v)" in cids
    assert "35.1(i)" in cids
    assert any(c.clause_id == "35.1" and c.section_kind == "amendment_preview" for c in clauses)


def test_same_page_tables_prose_box_rejection_and_y_attachment(tmp_path: Path) -> None:
    # Two identical-header tables on the same page (Sporting 6.4 pattern) must NOT merge,
    # and a table positioned above a later clause must attach to the preceding clause.
    race_tbl = _table_stream(
        [["Position", "Points"], ["1st", "25"], ["2nd", "18"]],
        [80, 80],
        start_x=60,
        start_y=710,
        row_height=20,
    )
    sprint_tbl = _table_stream(
        [["Position", "Points"], ["1st", "8"], ["2nd", "7"]],
        [80, 80],
        start_x=60,
        start_y=620,
        row_height=20,
    )
    p1 = (
        _text_lines_stream(["6) WORLD CHAMPIONSHIP", "6.4 Points scale:"], start_y=760)
        + "\n"
        + race_tbl
        + "\n"
        + sprint_tbl
        + "\n"
        + _text_lines_stream(["6.5 Dead heat rule follows."], start_y=520)
    )

    # Page 2: degenerate prose box (1 non-empty cell per row) must be rejected;
    # real table at y=600 above c) at y=500 must attach to a)(ii), NOT c).
    prose_box = _table_stream(
        [
            ["", "only one cell populated"],
            ["only one cell on row two", ""],
        ],
        [180, 180],
        start_x=60,
        start_y=720,
        row_height=20,
    )
    coeff_tbl = _table_stream(
        [["Classification", "P", "1", "2"], ["Value of C", "%", "70", "75"]],
        [100, 40, 40, 40],
        start_x=60,
        start_y=620,
        row_height=20,
    )
    p2 = (
        _text_lines_stream(
            [
                "APPENDIX 7: ATR",
                "a) Limits:",
                "i) Position P:",
                "ii) Coefficient C below:",
            ],
            start_y=790,
            step=14,
        )
        + "\n"
        + prose_box
        + "\n"
        + coeff_tbl
        + "\n"
        + _text_lines_stream(["c) Subsequent adjustment clause."], start_y=500)
    )

    pdf_path = tmp_path / "tables_y.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1, p2]))
    doc = _make_manifest_doc(
        pdf_path, doc_id="fia_2025_sporting", doc_type="sporting", page_count=2
    )

    pages, clauses = parse_document(doc, pdf_path)
    assert len(pages[0].tables_markdown) == 2
    assert len(pages[1].tables_markdown) == 1  # prose_box rejected!

    by_id = {c.clause_id: c for c in clauses}
    assert len(by_id["6.4"].tables_markdown) == 2
    assert by_id["6.5"].tables_markdown == ()
    assert len(by_id["App.7(a)(ii)"].tables_markdown) == 1
    assert "Value of C" in by_id["App.7(a)(ii)"].tables_markdown[0]
    assert by_id["App.7(c)"].tables_markdown == ()


def test_image_only_diagram_page_duplicate_suffix_and_outputs(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    processed_dir = tmp_path / "processed"

    p1 = _text_lines_stream(["ARTICLE 4: MASS", "4.1 First occurrence."])
    p2 = "q 100 0 0 100 70 400 cm /Im1 Do Q"  # image-only page with empty text
    p3 = _text_lines_stream(["4.1 Duplicate normative occurrence."])

    pdf_path = raw_dir / "dup_and_img.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1, p2, p3], include_image_on_pages={2}))

    manifest_path = tmp_path / "manifest.json"
    raw = pdf_path.read_bytes()
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "corpus_name": "test-corpus",
                "documents": [
                    {
                        "doc_id": "doc_a",
                        "year": 2025,
                        "doc_type": "technical",
                        "issue": "Issue 1",
                        "published_date": "2025-01-01",
                        "title": "Doc A",
                        "filename": pdf_path.name,
                        "url": "https://example.com/a.pdf",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "expected_bytes": len(raw),
                        "expected_pages": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = parse_corpus(
        manifest_path=manifest_path, raw_dir=raw_dir, processed_dir=processed_dir
    )

    page_rows = [
        json.loads(ln)
        for ln in report.pages_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(page_rows) == 3
    assert page_rows[1]["has_diagram"] is True

    clause_rows = [
        json.loads(ln)
        for ln in report.clauses_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    cids = [r["chunk_id"] for r in clause_rows]
    assert "doc_a::normative::4.1" in cids
    assert "doc_a::normative::4.1::2" in cids

    summary = json.loads(report.summary_path.read_text(encoding="utf-8"))
    assert summary["documents"][0]["diagram_pages"] == 1
    assert summary["documents"][0]["pages"] == 3
    assert not list(processed_dir.glob("*.part"))


def _positioned_lines_stream(entries: list[tuple[int, int, str]]) -> str:
    ops = ["BT", "/F1 11 Tf"]
    for x, y, text in entries:
        ops.append(f"1 0 0 1 {x} {y} Tm ({_pdf_escape(text)}) Tj")
    ops.append("ET")
    return "\n".join(ops)


def test_roman_list_nesting_without_letter_parent_and_x0_indentation(tmp_path: Path) -> None:
    # (a) 3.15.7 has roman i./ii. directly with no letter parent (x0=110)
    # (b) 3.5.1 has letter h. (x0=110) -> indented roman i..iii (x0=136) -> letter i. (x0=110)
    p1 = _positioned_lines_stream(
        [
            (70, 780, "ARTICLE 3: AERODYNAMIC COMPONENTS"),
            (70, 760, "3.15.7 Load tests:"),
            (110, 740, "i. Front load test."),
            (110, 720, "ii. Rear load test."),
            (70, 690, "3.5.1 Floor bodywork:"),
            (110, 670, "h. Have edge rules:"),
            (136, 650, "i. First sub-rule."),
            (136, 630, "ii. Second sub-rule."),
            (136, 610, "iii. Third sub-rule."),
            (110, 590, "i. Next top-level letter rule."),
        ]
    )
    pdf_path = tmp_path / "roman_x0.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1]))
    doc = _make_manifest_doc(pdf_path, page_count=1)

    _, clauses = parse_document(doc, pdf_path)
    cids = [c.clause_id for c in clauses]
    assert "3.15.7(i)" in cids
    assert "3.15.7(ii)" in cids
    assert "3.15.7(i)(ii)" not in cids

    assert "3.5.1(h)" in cids
    assert "3.5.1(h)(i)" in cids
    assert "3.5.1(h)(ii)" in cids
    assert "3.5.1(h)(iii)" in cids
    assert "3.5.1(i)" in cids
    assert "3.5.1(h)(i)(ii)" not in cids


def test_wrapped_mid_sentence_bullet_references_not_split(tmp_path: Path) -> None:
    p1 = _text_lines_stream(
        [
            "ARTICLE 3: BODYWORK",
            "3.13.3 Brake ducts:",
            "a. First requirement;",
            "b. Second requirement;",
            "c. Third requirement;",
            "d. Fourth requirement, permitted in",
            "c. and d. below.",
            "3.13.5 Wheel covers:",
            "a. Components defined in (iii), (iv) and",
            "(v) may be fitted.",
            "ARTICLE 9: TRANSMISSION",
            "9.1 Drive shafts must lie tangent to",
            "(a) at that point.",
            "9.2 Valid conditions:",
            "a. first condition; and",
            "b. second condition.",
            "ARTICLE 54: INCIDENTS",
            "54.3 Penalties:",
            "a) drive through;",
            "b) stop and go;",
            "c) grid drop;",
            "d) time penalty in the case of a) or",
            "b) or both.",
        ]
    )
    pdf_path = tmp_path / "wrapped_bullets.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1]))
    doc = _make_manifest_doc(pdf_path, page_count=1)

    _, clauses = parse_document(doc, pdf_path)
    cids = [c.clause_id for c in clauses]

    assert cids.count("3.13.3(c)") == 1
    assert "3.13.5(v)" not in cids
    assert "9.1(a)" not in cids
    assert "9.2(a)" in cids
    assert "9.2(b)" in cids
    assert cids.count("54.3(b)") == 1


def test_bullet_separators_and_x0_column_alignment(tmp_path: Path) -> None:
    p1 = _positioned_lines_stream(
        [
            (70, 780, "ARTICLE 5: POWER UNIT"),
            (70, 760, "5.2.6 Restricting the possibility for such a supplier:"),
            (110, 740, "a. to consider any request; and/or"),
            (110, 720, "b. to supply under reasonable terms."),
            (70, 690, "ARTICLE 3: AERODYNAMIC"),
            (70, 670, "3.9.8 Cuboids:"),
            (136, 650, "i. first condition;"),
            (136, 630, "ii. symmetrical about a cuboid, and"),
            (136, 610, "iii. intersect the wing profiles."),
            (70, 580, "ARTICLE 10: SUSPENSION"),
            (70, 560, "10.3.4 Attachments must:"),
            (110, 540, "b. no more than 25mm from the load line it is attaching to"),
            (110, 520, "c. together with the inboard point."),
            (70, 490, "10.3.5 Wrapped ref must not split:"),
            (110, 470, "a. must conform to"),
            (130, 450, "b. at the inboard end only."),
        ]
    )
    pdf_path = tmp_path / "bullet_seps.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1]))
    doc = _make_manifest_doc(pdf_path, page_count=1)

    _, clauses = parse_document(doc, pdf_path)
    cids = [c.clause_id for c in clauses]

    assert "5.2.6(a)" in cids
    assert "5.2.6(b)" in cids
    assert "3.9.8(ii)" in cids
    assert "3.9.8(iii)" in cids
    assert "10.3.4(b)" in cids
    assert "10.3.4(c)" in cids
    assert "10.3.5(a)" in cids
    assert "10.3.5(b)" not in cids


def test_outdented_prose_resets_stack_and_fifth_level_sub_bullets(tmp_path: Path) -> None:
    # 13.5.1 pattern (2024 restart at i. + 2025 continuation vi.) and 10.10 5th-level (a)-(b)
    p1 = _positioned_lines_stream(
        [
            (70, 780, "ARTICLE 13: SAFETY STRUCTURES"),
            (70, 760, "13.5.1 Side impact structures:"),
            (110, 740, "d. Internal volumes must be empty:"),
            (136, 720, "i. Upper structure volume."),
            (136, 700, "ii. Lower structure volume."),
            (105, 680, "No parts may be present in this volume."),
            (105, 660, "The following components may be placed in this volume;"),
            (136, 640, "i. Bodywork"),
            (136, 620, "ii. Oil and coolant components"),
            (105, 600, "Provided that;"),
            (136, 580, "vi. A thermal store homologated."),
            (105, 560, "Provided that;"),
            (136, 540, "i. Restarted roman condition."),
            (70, 500, "ARTICLE 10: TRACK RUNNING TIME"),
            (70, 480, "10.10 Testing of Mule Cars:"),
            (99, 460, "k) Maximum of forty car days:"),
            (128, 440, "i) Drivers must hold a Super Licence."),
            (128, 420, "ii) Testing must satisfy one of:"),
            (156, 400, "a) Comply with current Technical Regulations."),
            (156, 380, "b) Comply with prior Technical Regulations."),
            (128, 360, "iii) Subsequent tests after first Competition."),
            (70, 320, "30) TYRES"),
            (70, 300, "30.5 Tyre allocation:"),
            (99, 280, "j) From twelve sets:"),
            (128, 270, "i) One set for Q3."),
            (128, 260, "iv) One set returned unless:"),
            (156, 240, "i) driver was replaced, only one set returned,"),
            (156, 220, "ii) or both P1 and P2 are wet, retain one set."),
            (128, 200, "v) Two further sets returned after P3."),
        ]
    )
    pdf_path = tmp_path / "depth5_and_reset.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1]))
    doc = _make_manifest_doc(pdf_path, page_count=1)

    _, clauses = parse_document(doc, pdf_path)
    by_cid_list = [c.clause_id for c in clauses]

    assert "13.5.1(d)(i)" in by_cid_list
    assert "13.5.1(d)(ii)" in by_cid_list
    assert "13.5.1(i)" in by_cid_list
    assert "13.5.1(ii)" in by_cid_list
    assert "13.5.1(vi)" in by_cid_list
    assert by_cid_list.count("13.5.1(i)") == 2
    d_clause = [c for c in clauses if c.clause_id == "13.5.1(d)"][0]
    assert "No parts may be present" not in d_clause.text

    assert "10.10(k)(ii)(a)" in by_cid_list
    assert "10.10(k)(ii)(b)" in by_cid_list
    assert "10.10(k)(iii)" in by_cid_list
    assert "10.10(a)" not in by_cid_list
    sub_a = [c for c in clauses if c.clause_id == "10.10(k)(ii)(a)"][0]
    assert sub_a.parent_clause_id == "10.10(k)(ii)"

    assert "30.5(j)(iv)(i)" in by_cid_list
    assert "30.5(j)(iv)(ii)" in by_cid_list
    assert "30.5(j)(v)" in by_cid_list
    sub_ii = [c for c in clauses if c.clause_id == "30.5(j)(iv)(ii)"][0]
    assert sub_ii.parent_clause_id == "30.5(j)(iv)"
    assert "P1 and P2 are wet" in sub_ii.text


def test_app3_normalizer_split_subscript_coords_and_atomic_write_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from regrag.corpus import parse as parse_mod

    # 1. _normalize_app3_table 10-column projection
    class _FakeRow:
        def __init__(self, cells: list[tuple[float, float, float, float] | None]) -> None:
            self.cells = cells

    class _FakeApp3Table:
        def __init__(self) -> None:
            box = (0.0, 0.0, 10.0, 10.0)
            self.rows = [
                _FakeRow([None] * 12),
                _FakeRow([None] * 12),
                _FakeRow([None] * 12),
                _FakeRow([box, None, box, box, box, box, box, box, box, box, box, None]),
            ]

    raw_rows: list[list[str | None]] = [
        [
            "",
            "o\nN\nm\ne\nt\nI",
            "List of PU\nfunctions",
            "Art.",
            "Art.",
            "Art.",
            "Art.",
            "Art.",
            "App.",
            "App. 4",
            "PU Element",
            None,
        ],
        [None] * 12,
        [None] * 12,
        ["C", None, "Transport", None, None, None, None, None, None, "EXC", "-", None],
    ]
    norm = parse_mod._normalize_app3_table(_FakeApp3Table(), raw_rows)
    assert norm is not None
    assert len(norm) == 2
    assert len(norm[0]) == 10
    assert norm[0][0] == "Item No."
    assert norm[1][0] == "C"
    assert norm[1][8] == "EXC"

    # 2. Split-line subscript coords inside _extract_cleaned_lines_with_coords
    class _FakeLinePage:
        def extract_text_lines(self, **_: Any) -> list[dict[str, Any]]:
            return [
                {
                    "top": 100.0,
                    "x0": 70.0,
                    "text": "2025 Formula 1 Technical Regulations 15 07 April 2025",
                },
                {"top": 120.0, "x0": 70.0, "text": "X =0 and X =0"},
                {"top": 124.0, "x0": 70.0, "text": "F R"},
            ]

    text_out, coords_out = parse_mod._extract_cleaned_lines_with_coords(_FakeLinePage())
    assert text_out == "X_F=0 and X_R=0"
    assert coords_out == [(120.0, 70.0)]

    # 3. _atomic_write_text removes .part file if os.replace raises OSError
    dest = tmp_path / "clauses.jsonl"

    def _fail_replace(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(parse_mod.os, "replace", _fail_replace)
    with pytest.raises(OSError, match="disk full"):
        parse_mod._atomic_write_text(dest, "hello\n")
    assert not dest.exists()
    assert not dest.with_name("clauses.jsonl.part").exists()


def test_sporting_accented_article_heading_and_inline_bullet_clauses(tmp_path: Path) -> None:
    p1 = _positioned_lines_stream(
        [
            (71, 780, "23) PERSONNEL"),
            (71, 760, "23.4 A list of all personnel must be submitted."),
            (71, 740, "23.5 a) At each Competition, team personnel are not permitted"),
            (128, 720, "Article 23.5e) to 23.5g)."),
            (99, 700, "b) For the avoidance of doubt, drivers are exempt:"),
            (128, 680, "i) Subject to prior FIA approval."),
            (71, 640, "42) GRID"),
            (71, 620, "42.4 The provisional grid will be published:"),
            (99, 600, "c) The grid position of any car withdrawn after the time referred to by"),
            (128, 580, "42.4 b) will remain vacant."),
            (71, 540, "59) FINISH"),
            (71, 520, "59.3 Cars must proceed to parc ferme."),
            (71, 490, "60) POST SPRINT AND POST RACE PARC FERM\u00c9"),
            (71, 470, "60.1 Only officials charged with supervision may enter."),
            (71, 450, "60.2 Parc ferme regulations will apply."),
        ]
    )
    pdf_path = tmp_path / "sporting_round5.pdf"
    pdf_path.write_bytes(_build_synthetic_pdf([p1]))
    doc = _make_manifest_doc(
        pdf_path, doc_id="fia_2025_sporting", doc_type="sporting", page_count=1
    )

    _, clauses = parse_document(doc, pdf_path)
    by_cid = {c.clause_id: c for c in clauses}

    assert "23.5" in by_cid
    assert "23.5(a)" in by_cid
    assert "23.5(b)" in by_cid
    assert "23.5(b)(i)" in by_cid
    assert by_cid["23.5(a)"].parent_clause_id == "23.5"
    assert by_cid["23.5(b)"].parent_clause_id == "23.5"
    assert by_cid["23.5(b)(i)"].parent_clause_id == "23.5(b)"
    assert "Article 23.5e) to 23.5g)." in by_cid["23.5(a)"].text
    assert not any(c.parent_clause_id == "23.4" for c in clauses)

    assert "42.4(b)" not in by_cid
    assert "42.4(c)" in by_cid
    assert "42.4 b) will remain vacant." in by_cid["42.4(c)"].text

    assert "60.1" in by_cid
    assert "60.2" in by_cid
    assert by_cid["60.1"].article == "Article 60"
    assert by_cid["60.2"].article == "Article 60"
    assert "60.1" not in by_cid["59.3"].text
