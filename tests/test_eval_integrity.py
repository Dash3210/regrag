"""Repository hygiene and P0 evaluation integrity checks."""

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_VERIFIED_2024_SHA256 = "a940dd6c01b058c17f8da5392fb20b497b8967848c1c62905453b955b6d58c18"
_VERIFIED_2025_SHA256 = "454b76e2b388e61db50c0d116ff59477848a0b7e081f38142242808213dc0826"
_VALID_VERDICTS = {"PASS", "PARTIAL", "FAIL"}
_FORBIDDEN_EXTENSIONS = (".pdf", ".sqlite", ".sqlite3", ".db", ".npy")
_FORBIDDEN_DIRS = ("data/raw", "data/cache", "data/index", "spike")
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
_FORBIDDEN_PACKAGES = {
    "pymupdf",
    "fitz",
    "langchain",
    "llama-index",
    "pinecone",
    "weaviate",
    "qdrant-client",
    "chromadb",
}


def _is_forbidden_repo_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/").lstrip("./")
    lowered = normalized.lower()
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


def _normalize_pkg_name(spec: str) -> str:
    raw_name = re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", raw_name).lower()


def _parse_leading_int(cell: str) -> int:
    cleaned = cell.replace("*", "").strip()
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
        tracked = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
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
            ["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard"],
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
        leaked = [
            rel.as_posix()
            for candidate in repo_root.rglob("*")
            if candidate.is_file()
            for rel in (candidate.relative_to(repo_root),)
            if ".venv" not in rel.parts and _is_forbidden_repo_path(rel.as_posix())
        ]
        assert leaked == [], f"Forbidden artefacts found in non-git tree: {leaked}"


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
    forbidden_present = sorted(_FORBIDDEN_PACKAGES & all_deps)
    assert forbidden_present == [], (
        f"pyproject.toml must not declare forbidden dependencies: {forbidden_present}"
    )


def test_decisions_doc_contains_initial_adrs_and_corpus_provenance(repo_root: Path) -> None:
    decisions_path = repo_root / "docs" / "decisions.md"
    assert decisions_path.is_file(), "docs/decisions.md must exist"

    content = decisions_path.read_text(encoding="utf-8")
    for adr_num in ("001", "002", "003", "004", "005"):
        assert re.search(rf"^## ADR-{adr_num}\b", content, flags=re.MULTILINE), (
            f"Missing '## ADR-{adr_num}' heading in docs/decisions.md"
        )

    adr_005_section = _extract_markdown_section(content, "## ADR-005", level=2)
    assert "fia_f1_regulations" in adr_005_section, "ADR-005 must reference fia_f1_regulations"
    assert _VERIFIED_2024_SHA256 in adr_005_section, (
        "ADR-005 must record the verified 2024 Technical Regulations SHA-256 digest"
    )
    assert _VERIFIED_2025_SHA256 in adr_005_section, (
        "ADR-005 must record the verified 2025 Technical Regulations SHA-256 digest"
    )


def test_p0_corpus_spike_has_30_hand_verified_clauses(repo_root: Path) -> None:
    spike_path = repo_root / "docs" / "p0-corpus-spike.md"
    assert spike_path.is_file(), "docs/p0-corpus-spike.md must exist"

    content = spike_path.read_text(encoding="utf-8")

    # 1. Verify Section 1 provenance contains both verified SHA-256 digests
    sec1 = _extract_markdown_section(content, "## 1. Corpus Provenance", level=2)
    assert _VERIFIED_2024_SHA256 in sec1, "Section 1 missing verified 2024 SHA-256"
    assert _VERIFIED_2025_SHA256 in sec1, "Section 1 missing verified 2025 SHA-256"

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


def test_readme_contains_phase_status_table_and_corpus_links(repo_root: Path) -> None:
    readme_path = repo_root / "README.md"
    assert readme_path.is_file(), "README.md must exist"

    content = readme_path.read_text(encoding="utf-8")
    assert "## Phase Status" in content
    assert "| Phase | Scope | Status |" in content
    for phase in ("| P0 |", "| P1 |", "| P2 |", "| P3 |", "| P4 |", "| P5 |", "| P6 |", "| P7 |"):
        assert phase in content, f"Phase Status Table missing row for {phase}"
    assert "docs/p0-corpus-spike.md" in content, "README.md must link docs/p0-corpus-spike.md"
    assert "docs/decisions.md" in content, "README.md must link docs/decisions.md"
    assert "FIA Formula 1" in content, "README.md must name the FIA Formula 1 corpus"
