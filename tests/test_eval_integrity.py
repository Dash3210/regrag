"""Repository hygiene and P0/P1/P2 evaluation integrity checks."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from regrag.corpus.fetch import load_manifest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_VERIFIED_2024_TECH_SHA256 = "a940dd6c01b058c17f8da5392fb20b497b8967848c1c62905453b955b6d58c18"
_VERIFIED_2025_TECH_SHA256 = "454b76e2b388e61db50c0d116ff59477848a0b7e081f38142242808213dc0826"
_VERIFIED_2024_SPORT_SHA256 = "82f8048dcc2c86c26e8d69321f2700ea9b274346e28bade43815dea31a3993a3"
_VERIFIED_2025_SPORT_SHA256 = "525eef22a60f0755a4468281dd7c78c5ea5bd0eec38dc1b09cc31a0c0132854e"

_VALID_VERDICTS = {"PASS", "PARTIAL", "FAIL"}
_VALID_SECTION_KINDS = {"normative", "appendix", "amendment_preview"}
_FORBIDDEN_EXTENSIONS = (".pdf", ".sqlite", ".sqlite3", ".db", ".npy")
_FORBIDDEN_DIRS = ("data/raw", "data/processed", "data/cache", "data/index", "spike")
_NON_GIT_IGNORED_DIRS = (
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "data/raw",
    "data/processed",
    "data/cache",
    "data/index",
)
_REQUIRED_CORE_PACKAGES = {
    "pdfplumber",
    "sentence-transformers",
    "fastapi",
    "prometheus-client",
    "requests",
    "numpy",
    "pydantic",
    "uvicorn",
}
_FORBIDDEN_PACKAGE_PREFIXES = (
    "pymupdf",
    "fitz",
    "langchain",
    "llama-index",
    "pinecone",
    "weaviate",
    "qdrant",
    "chromadb",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _strip_leading_dot_slash(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_forbidden_repo_path(path: str) -> bool:
    lowered = _strip_leading_dot_slash(path).lower()
    if not lowered:
        return False
    basename = lowered.rsplit("/", 1)[-1]
    if basename == ".env" or basename.startswith(".env."):
        return True
    if any(lowered.endswith(ext) for ext in _FORBIDDEN_EXTENSIONS):
        return True
    return any(
        lowered == forbidden_dir or lowered.startswith(f"{forbidden_dir}/")
        for forbidden_dir in _FORBIDDEN_DIRS
    )


def _is_non_git_ignored(path: str) -> bool:
    lowered = _strip_leading_dot_slash(path).lower()
    return any(
        lowered == prefix or lowered.startswith(f"{prefix}/")
        for prefix in _NON_GIT_IGNORED_DIRS
    )


def _find_non_git_forbidden_files(root: Path) -> list[str]:
    return [
        rel.as_posix()
        for candidate in root.rglob("*")
        if candidate.is_file()
        for rel in (candidate.relative_to(root),)
        if not _is_non_git_ignored(rel.as_posix()) and _is_forbidden_repo_path(rel.as_posix())
    ]


def _normalize_pkg_name(spec: str) -> str:
    raw_name = re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", raw_name).lower()


def _parse_leading_int(cell: str) -> int:
    cleaned = cell.replace("*", "").replace(",", "").strip()
    match = re.match(r"(\d+)", cleaned)
    assert match is not None, f"Expected leading integer in table cell {cell!r}"
    return int(match.group(1))


def _extract_markdown_section(content: str, heading: str, level: int = 2) -> str:
    assert heading in content, f"Missing heading {heading!r}"
    after = content.split(heading, maxsplit=1)[1]
    pattern = rf"\n#{{1,{level}}}\s+"
    return re.split(pattern, after, maxsplit=1)[0]


def test_gitignore_excludes_forbidden_artefacts(repo_root: Path) -> None:
    gitignore_path = repo_root / ".gitignore"
    assert gitignore_path.is_file(), ".gitignore must exist at repository root"

    lines = {line.strip() for line in gitignore_path.read_text(encoding="utf-8").splitlines()}
    required_entries = (
        "data/raw/",
        "data/processed/",
        "data/cache/",
        "data/index/",
        "*.pdf",
        "*.sqlite",
        "*.sqlite3",
        "*.db",
        "*.npy",
        ".env",
        "spike/",
    )
    for entry in required_entries:
        assert entry in lines, f".gitignore must exclude {entry}"


def test_zero_forbidden_files_tracked_or_unignored(repo_root: Path) -> None:
    git_dir = repo_root / ".git"
    if git_dir.exists():
        git_exe = shutil.which("git")
        if git_exe is None:
            pytest.skip("git binary not found on PATH; skipping git tracking check")

        tracked = subprocess.run(
            [git_exe, "-C", str(repo_root), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
        )
        tracked_forbidden = [
            path for path in tracked.stdout.splitlines() if _is_forbidden_repo_path(path)
        ]
        assert tracked_forbidden == [], (
            f"Forbidden artefacts must never be tracked in git: {tracked_forbidden}"
        )

        untracked = subprocess.run(
            [git_exe, "-C", str(repo_root), "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        )
        untracked_forbidden = [
            path for path in untracked.stdout.splitlines() if _is_forbidden_repo_path(path)
        ]
        assert untracked_forbidden == [], (
            f"Unignored forbidden artefacts must not appear in git: {untracked_forbidden}"
        )
    else:
        leaked = _find_non_git_forbidden_files(repo_root)
        assert leaked == [], f"Forbidden artefacts found in non-git tree: {leaked}"


def test_non_git_scan_allows_runtime_data_dirs_and_rejects_stray_forbidden(tmp_path: Path) -> None:
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "fia.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "data" / "processed").mkdir(parents=True)
    (tmp_path / "data" / "processed" / "clauses.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "data" / "index").mkdir(parents=True)
    (tmp_path / "data" / "index" / "fts.sqlite").write_bytes(b"")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "sample.pdf").write_bytes(b"")
    assert _find_non_git_forbidden_files(tmp_path) == []

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leaked.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "spike").mkdir()
    (tmp_path / "spike" / "scratch.py").write_text("pass\n", encoding="utf-8")
    leaked = sorted(_find_non_git_forbidden_files(tmp_path))
    assert leaked == [".env", "spike/scratch.py", "src/leaked.pdf"]


def test_pyproject_dependency_contract(repo_root: Path) -> None:
    pyproject_path = repo_root / "pyproject.toml"
    assert pyproject_path.is_file(), "pyproject.toml must exist"

    data: dict[str, Any] = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data["project"]
    core_deps = {_normalize_pkg_name(dep) for dep in project.get("dependencies", [])}
    missing_core = sorted(_REQUIRED_CORE_PACKAGES - core_deps)
    assert missing_core == [], f"pyproject.toml missing required core dependencies: {missing_core}"

    all_deps = set(core_deps)
    for extra_deps in project.get("optional-dependencies", {}).values():
        all_deps.update(_normalize_pkg_name(dep) for dep in extra_deps)
    forbidden_present = sorted(
        dep
        for dep in all_deps
        if any(dep.startswith(prefix) for prefix in _FORBIDDEN_PACKAGE_PREFIXES)
    )
    assert forbidden_present == [], (
        f"pyproject.toml must not declare forbidden dependency families: {forbidden_present}"
    )


def test_corpus_manifest_schema_and_provenance(repo_root: Path) -> None:
    manifest_path = repo_root / "corpus" / "manifest.json"
    assert manifest_path.is_file(), "corpus/manifest.json must exist"

    manifest = load_manifest(manifest_path)
    assert manifest.manifest_version == 1
    assert manifest.corpus_name == "fia-f1-regulations"
    assert len(manifest.documents) == 4
    assert [doc.doc_id for doc in manifest.documents] == [
        "fia_2024_technical",
        "fia_2025_technical",
        "fia_2024_sporting",
        "fia_2025_sporting",
    ]

    for doc in manifest.documents:
        assert _SHA256_RE.fullmatch(doc.sha256), f"{doc.doc_id} sha256 must be 64 lowercase hex"
        assert doc.url.startswith("https://"), f"{doc.doc_id} url must use https://"
        assert doc.filename.endswith(".pdf")
        assert "/" not in doc.filename and "\\" not in doc.filename and ".." not in doc.filename
        assert doc.expected_bytes > 0
        assert doc.expected_pages > 0
        assert doc.year in {2024, 2025}
        assert doc.doc_type in {"technical", "sporting"}


def test_decisions_doc_contains_adrs_and_corpus_provenance(repo_root: Path) -> None:
    decisions_path = repo_root / "docs" / "decisions.md"
    assert decisions_path.is_file(), "docs/decisions.md must exist"

    content = decisions_path.read_text(encoding="utf-8")
    for adr_num in ("001", "002", "003", "004", "005", "006", "007", "008", "009", "010"):
        assert re.search(rf"^## ADR-{adr_num}\b", content, flags=re.MULTILINE), (
            f"Missing '## ADR-{adr_num}' heading in docs/decisions.md"
        )

    adr_005_section = _extract_markdown_section(content, "## ADR-005", level=2)
    assert "fia_f1_regulations" in adr_005_section, "ADR-005 must reference fia_f1_regulations"
    assert _VERIFIED_2024_TECH_SHA256 in adr_005_section, (
        "ADR-005 must record the verified 2024 Technical Regulations SHA-256 digest"
    )
    assert _VERIFIED_2025_TECH_SHA256 in adr_005_section, (
        "ADR-005 must record the verified 2025 Technical Regulations SHA-256 digest"
    )

    adr_006_section = _extract_markdown_section(content, "## ADR-006", level=2)
    for sha in (
        _VERIFIED_2024_TECH_SHA256,
        _VERIFIED_2025_TECH_SHA256,
        _VERIFIED_2024_SPORT_SHA256,
        _VERIFIED_2025_SPORT_SHA256,
    ):
        assert sha in adr_006_section, f"ADR-006 missing verified manifest SHA-256 {sha}"

    adr_007_section = _extract_markdown_section(content, "## ADR-007", level=2)
    for required_token in ("clauses.jsonl", "pages.jsonl", "parse_summary.json", "data/processed/"):
        assert required_token in adr_007_section, f"ADR-007 missing {required_token!r}"

    for adr_id, expected_tokens in (
        ("ADR-008", ("Status: accepted", "intfloat/e5-small-v2")),
        (
            "ADR-009",
            (
                "Status: accepted",
                "cross-encoder/ms-marco-MiniLM-L-6-v2",
                "bm25_fixed",
                "bm25_clause",
                "dense_clause",
                "hybrid_rrf",
                "hybrid_rerank",
            ),
        ),
        ("ADR-010", ("Status: accepted", "index.sqlite", "index_meta.json")),
    ):
        section = _extract_markdown_section(content, f"## {adr_id}", level=2)
        for token in expected_tokens:
            assert token in section, f"{adr_id} missing required token {token!r}"


def test_p0_corpus_spike_has_30_hand_verified_clauses(repo_root: Path) -> None:
    spike_path = repo_root / "docs" / "p0-corpus-spike.md"
    assert spike_path.is_file(), "docs/p0-corpus-spike.md must exist"

    content = spike_path.read_text(encoding="utf-8")

    # 1. Verify Section 1 provenance contains both verified SHA-256 digests
    sec1 = _extract_markdown_section(content, "## 1. Corpus Provenance", level=2)
    assert _VERIFIED_2024_TECH_SHA256 in sec1, "Section 1 missing verified 2024 SHA-256"
    assert _VERIFIED_2025_TECH_SHA256 in sec1, "Section 1 missing verified 2025 SHA-256"

    # 2. Parse Section 3 rows (1..30) and verify completeness & non-empty fields
    sec3 = _extract_markdown_section(
        content, "## 3. Complete 30-Row Hand-Verified Clause Table", level=2
    )
    rows: list[dict[str, Any]] = []
    for raw_line in sec3.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 9 or not cells[0].isdigit():
            continue
        row_num = int(cells[0])
        year = cells[1]
        page, clause_id, category, summary, num_check, verdict_raw, quirks = cells[2:9]
        assert year in {"2024", "2025"}, f"Row {row_num} has unexpected year {year!r}"
        for field_name, value in (
            ("Page", page),
            ("Clause ID", clause_id),
            ("Category", category),
            ("Raw / Extracted Summary", summary),
            ("Table / Numeric Check", num_check),
            ("Parser Quirk Notes", quirks),
        ):
            assert value != "", f"Row {row_num} has empty {field_name}"
        verdict = verdict_raw.strip("* ").upper()
        assert verdict in _VALID_VERDICTS, (
            f"Row {row_num} has invalid verdict {verdict_raw!r}; expected one of {_VALID_VERDICTS}"
        )
        categories = {tag.strip() for tag in category.split(",") if tag.strip()}
        rows.append(
            {
                "num": row_num,
                "year": year,
                "categories": categories,
                "verdict": verdict,
            }
        )

    assert [r["num"] for r in rows] == list(range(1, 31)), (
        f"Expected numbered rows 1..30 in Section 3, found {[r['num'] for r in rows]}"
    )
    assert sum(1 for r in rows if r["year"] == "2024") == 15
    assert sum(1 for r in rows if r["year"] == "2025") == 15

    # 3. Reconcile Section 2a (overall verdict distribution) exactly against Section 3
    sec2a = _extract_markdown_section(content, "### 2a. Overall Verdict Distribution", level=3)
    sec2a_checked: set[str] = set()
    for raw_line in sec2a.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 6 or cells[0].startswith("---") or "Scope" in cells[0]:
            continue
        scope_label = cells[0].replace("*", "").strip()
        if "2024" in scope_label:
            subset = [r for r in rows if r["year"] == "2024"]
            key = "2024"
        elif "2025" in scope_label:
            subset = [r for r in rows if r["year"] == "2025"]
            key = "2025"
        elif "Combined" in scope_label:
            subset = rows
            key = "Combined"
        else:
            continue
        sec2a_checked.add(key)
        assert _parse_leading_int(cells[1]) == len(subset), f"Section 2a {key} Total mismatch"
        assert _parse_leading_int(cells[2]) == sum(1 for r in subset if r["verdict"] == "PASS"), (
            f"Section 2a {key} PASS mismatch"
        )
        assert _parse_leading_int(cells[3]) == sum(
            1 for r in subset if r["verdict"] == "PARTIAL"
        ), f"Section 2a {key} PARTIAL mismatch"
        assert _parse_leading_int(cells[4]) == sum(1 for r in subset if r["verdict"] == "FAIL"), (
            f"Section 2a {key} FAIL mismatch"
        )
    assert sec2a_checked == {"2024", "2025", "Combined"}, (
        f"Expected 2024, 2025, and Combined rows in Section 2a; checked {sec2a_checked}"
    )

    # 4. Reconcile Section 2b (accuracy by verification axis) exactly against Section 3
    sec2b = _extract_markdown_section(content, "### 2b. Accuracy by Verification Axis", level=3)
    axis_map = (
        ("Nested", "Nested"),
        ("Numeric", "Numeric"),
        ("Cross-reference", "Cross-ref"),
        ("Year-on-year", "YoY-diff"),
        ("Multi-page", "Table"),
    )
    sec2b_checked: set[str] = set()
    for raw_line in sec2b.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 7 or cells[0].startswith("---") or "Verification Axis" in cells[0]:
            continue
        axis_label = cells[0].replace("*", "").strip()
        matched_tag = next((tag for prefix, tag in axis_map if axis_label.startswith(prefix)), None)
        if matched_tag is None:
            continue
        sec2b_checked.add(matched_tag)
        subset = [r for r in rows if matched_tag in r["categories"]]
        assert _parse_leading_int(cells[2]) == len(subset), (
            f"Section 2b {matched_tag} Verified Count mismatch"
        )
        assert _parse_leading_int(cells[3]) == sum(1 for r in subset if r["verdict"] == "PASS"), (
            f"Section 2b {matched_tag} PASS mismatch"
        )
        assert _parse_leading_int(cells[4]) == sum(
            1 for r in subset if r["verdict"] == "PARTIAL"
        ), f"Section 2b {matched_tag} PARTIAL mismatch"
        assert _parse_leading_int(cells[5]) == sum(1 for r in subset if r["verdict"] == "FAIL"), (
            f"Section 2b {matched_tag} FAIL mismatch"
        )
    assert sec2b_checked == {"Nested", "Numeric", "Cross-ref", "YoY-diff", "Table"}, (
        f"Expected all 5 axes in Section 2b; checked {sec2b_checked}"
    )


def test_p1_parse_accuracy_report_reconciles_tables(repo_root: Path) -> None:
    report_path = repo_root / "docs" / "p1-parse-accuracy.md"
    assert report_path.is_file(), "docs/p1-parse-accuracy.md must exist"

    content = report_path.read_text(encoding="utf-8")

    # 0. Reconcile Section 1: 4-PDF Parse Statistics aggregate sums vs per-doc rows
    sec1 = _extract_markdown_section(content, "## 1. 4-PDF Parse Statistics", level=2)
    doc_stats: list[dict[str, int | str]] = []
    total_row: dict[str, int] | None = None
    expected_doc_ids = {
        "fia_2024_technical",
        "fia_2025_technical",
        "fia_2024_sporting",
        "fia_2025_sporting",
    }
    for raw_line in sec1.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 11 or cells[0].startswith("---") or "doc_id" in cells[0]:
            continue
        label = cells[0].strip("*` ")
        xref_cell = cells[10]
        xref_mentions = _parse_leading_int(xref_cell)
        xref_paren = re.search(r"\(([\d,]+)\)", xref_cell)
        xref_clauses = int(xref_paren.group(1).replace(",", "")) if xref_paren else 0
        row_nums = {
            "pages": _parse_leading_int(cells[3]),
            "normative": _parse_leading_int(cells[4]),
            "appendix": _parse_leading_int(cells[5]),
            "amendment_preview": _parse_leading_int(cells[6]),
            "total_clauses": _parse_leading_int(cells[7]),
            "table_pages": _parse_leading_int(cells[8]),
            "diagram_pages": _parse_leading_int(cells[9]),
            "xref_mentions": xref_mentions,
            "xref_clauses": xref_clauses,
        }
        if label in expected_doc_ids:
            assert (
                row_nums["normative"] + row_nums["appendix"] + row_nums["amendment_preview"]
                == row_nums["total_clauses"]
            ), f"Section 1 {label} clause breakdown does not sum to Total Clauses"
            doc_stats.append({"doc_id": label, **row_nums})
        elif label.lower() == "total":
            total_row = row_nums

    assert {str(row["doc_id"]) for row in doc_stats} == expected_doc_ids, (
        f"Section 1 must list all 4 manifest doc_ids; found {[row['doc_id'] for row in doc_stats]}"
    )
    assert total_row is not None, "Section 1 missing Total aggregate row"
    for metric in (
        "pages",
        "normative",
        "appendix",
        "amendment_preview",
        "total_clauses",
        "table_pages",
        "diagram_pages",
        "xref_mentions",
        "xref_clauses",
    ):
        expected_sum = sum(int(row[metric]) for row in doc_stats)
        assert total_row[metric] == expected_sum, (
            f"Section 1 Total row mismatch for {metric}: "
            f"reported {total_row[metric]}, sum of per-doc rows is {expected_sum}"
        )

    # 1. Parse Section 3: 30-row P0-vs-P1 Comparison
    sec3 = _extract_markdown_section(content, "## 3. 30-Row P0-vs-P1 Comparison", level=2)
    comparison_rows: list[dict[str, Any]] = []
    for raw_line in sec3.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 8 or not cells[0].isdigit():
            continue
        row_num = int(cells[0])
        year = cells[1]
        clause = cells[2]
        p0_verdict = cells[3].strip("* ").upper()
        p1_verdict = cells[4].strip("* ").upper()
        chunk_id = cells[5]
        section_kind = cells[6].strip("` ")
        assert year in {"2024", "2025"}, f"P1 comparison row {row_num} has bad year {year!r}"
        assert clause != "", f"P1 comparison row {row_num} has empty clause"
        assert p0_verdict in _VALID_VERDICTS, f"Row {row_num} invalid P0 verdict {p0_verdict!r}"
        assert p1_verdict in _VALID_VERDICTS, f"Row {row_num} invalid P1 verdict {p1_verdict!r}"
        assert chunk_id != "", f"Row {row_num} has empty chunk_id"
        assert section_kind in _VALID_SECTION_KINDS, (
            f"Row {row_num} invalid section_kind {section_kind!r}"
        )
        comparison_rows.append(
            {
                "num": row_num,
                "year": year,
                "p0_verdict": p0_verdict,
                "p1_verdict": p1_verdict,
            }
        )

    assert [r["num"] for r in comparison_rows] == list(range(1, 31)), (
        f"Expected numbered rows 1..30 in Section 3, found {[r['num'] for r in comparison_rows]}"
    )
    assert sum(1 for r in comparison_rows if r["year"] == "2024") == 15
    assert sum(1 for r in comparison_rows if r["year"] == "2025") == 15

    # 2. Reconcile Section 2 summary counts against Section 3 rows
    sec2 = _extract_markdown_section(content, "## 2. Summary Delta", level=2)
    sec2_checked: set[str] = set()
    for raw_line in sec2.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 8 or cells[0].startswith("---") or "Scope" in cells[0]:
            continue
        scope_label = cells[0].replace("*", "").strip()
        if "2024" in scope_label:
            subset = [r for r in comparison_rows if r["year"] == "2024"]
            key = "2024"
        elif "2025" in scope_label:
            subset = [r for r in comparison_rows if r["year"] == "2025"]
            key = "2025"
        elif "Combined" in scope_label:
            subset = comparison_rows
            key = "Combined"
        else:
            continue
        sec2_checked.add(key)
        assert _parse_leading_int(cells[1]) == len(subset), f"P1 Sec 2 {key} Total mismatch"
        assert _parse_leading_int(cells[2]) == sum(
            1 for r in subset if r["p0_verdict"] == "PASS"
        ), f"P1 Sec 2 {key} P0 PASS mismatch"
        assert _parse_leading_int(cells[3]) == sum(
            1 for r in subset if r["p0_verdict"] == "PARTIAL"
        ), f"P1 Sec 2 {key} P0 PARTIAL mismatch"
        assert _parse_leading_int(cells[4]) == sum(
            1 for r in subset if r["p0_verdict"] == "FAIL"
        ), f"P1 Sec 2 {key} P0 FAIL mismatch"
        assert _parse_leading_int(cells[5]) == sum(
            1 for r in subset if r["p1_verdict"] == "PASS"
        ), f"P1 Sec 2 {key} P1 PASS mismatch"
        assert _parse_leading_int(cells[6]) == sum(
            1 for r in subset if r["p1_verdict"] == "PARTIAL"
        ), f"P1 Sec 2 {key} P1 PARTIAL mismatch"
        assert _parse_leading_int(cells[7]) == sum(
            1 for r in subset if r["p1_verdict"] == "FAIL"
        ), f"P1 Sec 2 {key} P1 FAIL mismatch"

    assert sec2_checked == {"2024", "2025", "Combined"}, (
        f"Expected 2024, 2025, and Combined rows in P1 Summary Delta; checked {sec2_checked}"
    )

    # 3. Parse Section 4: Sporting Regulations Spot-Check (>= 10 rows)
    sec4 = _extract_markdown_section(content, "## 4. Sporting Regulations Spot-Check", level=2)
    sporting_rows: list[dict[str, Any]] = []
    for raw_line in sec4.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 7 or not cells[0].isdigit():
            continue
        row_num = int(cells[0])
        year = cells[1]
        clause = cells[2]
        verdict = cells[3].strip("* ").upper()
        chunk_id = cells[4]
        section_kind = cells[5].strip("` ")
        assert year in {"2024", "2025"}, f"Sporting row {row_num} has bad year {year!r}"
        assert clause != "", f"Sporting row {row_num} has empty clause"
        assert verdict in _VALID_VERDICTS, f"Sporting row {row_num} invalid verdict {verdict!r}"
        assert chunk_id != "", f"Sporting row {row_num} has empty chunk_id"
        assert section_kind in _VALID_SECTION_KINDS, (
            f"Sporting row {row_num} invalid section_kind {section_kind!r}"
        )
        sporting_rows.append({"num": row_num, "year": year, "verdict": verdict})

    assert len(sporting_rows) >= 10, (
        f"Expected >= 10 Sporting spot-check rows, found {len(sporting_rows)}"
    )
    assert [r["num"] for r in sporting_rows] == list(range(1, len(sporting_rows) + 1)), (
        f"Expected sequential Sporting row numbers, found {[r['num'] for r in sporting_rows]}"
    )
    assert {r["year"] for r in sporting_rows} == {"2024", "2025"}


def test_readme_contains_phase_status_table_and_corpus_links(repo_root: Path) -> None:
    readme_path = repo_root / "README.md"
    assert readme_path.is_file(), "README.md must exist"

    content = readme_path.read_text(encoding="utf-8")
    assert "## Phase Status" in content
    assert "| Phase | Scope | Status |" in content
    for phase in ("| P0 |", "| P1 |", "| P2 |", "| P3 |", "| P4 |", "| P5 |", "| P6 |", "| P7 |"):
        assert phase in content, f"Phase Status Table missing row for {phase}"
    assert "docs/p0-corpus-spike.md" in content, "README.md must link docs/p0-corpus-spike.md"
    assert "docs/p1-parse-accuracy.md" in content, "README.md must link docs/p1-parse-accuracy.md"
    assert "docs/p2-index-retrieval.md" in content, (
        "README.md must link docs/p2-index-retrieval.md"
    )
    assert "make index" in content, "README.md must document make index"
    assert "docs/decisions.md" in content, "README.md must link docs/decisions.md"
    assert "corpus/manifest.json" in content, "README.md must link corpus/manifest.json"
    assert "FIA Formula 1" in content, "README.md must name the FIA Formula 1 corpus"
