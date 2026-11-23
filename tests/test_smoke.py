"""Smoke tests for regrag packaging, CLI wiring, phase gating, Makefile, and stub modules."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import regrag
from regrag import _phase, cli
from regrag.corpus.fetch import ChecksumMismatchError, FetchError, FetchResult
from regrag.corpus.parse import ParseError, ParseReport


def _build_minimal_pdf(lines: list[str]) -> bytes:
    """Build a valid single-page PDF 1.4 binary parseable by pdfplumber."""
    text_ops = ["BT", "/F1 11 Tf", "72 720 Td"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx > 0:
            text_ops.append("0 -16 Td")
        text_ops.append(f"({escaped}) Tj")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream\nendobj\n"
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def test_version_matches_metadata() -> None:
    assert isinstance(regrag.__version__, str)
    assert len(regrag.__version__) > 0
    installed_version = importlib.metadata.version("regrag")
    assert regrag.__version__ == installed_version


def test_version_fallback_when_package_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_version(package_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    reloaded = importlib.reload(regrag)
    try:
        assert reloaded.__version__ == "0.0.0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(regrag)


def test_py_typed_marker_present(repo_root: Path) -> None:
    assert (repo_root / "src" / "regrag" / "py.typed").is_file()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["version"])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"regrag {regrag.__version__}"


def test_cli_empty_args_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [["--help"], ["version", "--help"], ["fetch", "--help"], ["parse", "--help"]],
)
def test_cli_help_exits_zero(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()


def test_cli_fetch_forwards_flags_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_kwargs: dict[str, Any] = {}
    manifest = tmp_path / "manifest.json"
    raw_dir = tmp_path / "raw"

    def fake_fetch_corpus(**kwargs: Any) -> list[FetchResult]:
        captured_kwargs.update(kwargs)
        return [
            FetchResult(
                doc_id="fia_2024_technical",
                path=raw_dir / "doc.pdf",
                status="skipped_verified",
                sha256="a" * 64,
                size_bytes=1234,
                attempts=0,
            )
        ]

    monkeypatch.setattr(cli, "fetch_corpus", fake_fetch_corpus)

    code = cli.main(
        [
            "fetch",
            "--manifest",
            str(manifest),
            "--raw-dir",
            str(raw_dir),
            "--doc-id",
            "fia_2024_technical",
            "--doc-id",
            "fia_2025_technical",
            "--force",
        ]
    )
    assert code == 0
    assert captured_kwargs == {
        "manifest_path": manifest,
        "raw_dir": raw_dir,
        "doc_ids": ["fia_2024_technical", "fia_2025_technical"],
        "force": True,
    }
    out = capsys.readouterr().out
    assert "fia_2024_technical" in out
    assert "skipped_verified" in out


def test_cli_fetch_error_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_fetch_corpus(**kwargs: Any) -> list[FetchResult]:
        raise FetchError("simulated download failure")

    monkeypatch.setattr(cli, "fetch_corpus", failing_fetch_corpus)

    code = cli.main(["fetch", "--manifest", str(tmp_path / "manifest.json")])
    assert code == 1
    captured = capsys.readouterr()
    assert "simulated download failure" in captured.err


def test_cli_fetch_checksum_mismatch_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def mismatch_fetch_corpus(**kwargs: Any) -> list[FetchResult]:
        raise ChecksumMismatchError("SHA-256 mismatch for doc")

    monkeypatch.setattr(cli, "fetch_corpus", mismatch_fetch_corpus)

    code = cli.main(["fetch", "--manifest", str(tmp_path / "manifest.json")])
    assert code == 1
    assert "SHA-256 mismatch" in capsys.readouterr().err


def test_cli_fetch_and_parse_missing_manifest_error_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing_manifest.json"
    assert cli.main(["fetch", "--manifest", str(missing)]) == 1
    assert "regrag fetch:" in capsys.readouterr().err

    assert cli.main(["parse", "--manifest", str(missing)]) == 1
    assert "regrag parse:" in capsys.readouterr().err


def test_cli_parse_end_to_end_with_synthetic_pdf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    raw_dir.mkdir()

    pdf_bytes = _build_minimal_pdf(
        [
            "ARTICLE 4: MASS",
            "4.1 Minimum mass",
            "The mass of the car, without fuel, must not be less than 798kg.",
        ]
    )
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_name = "synthetic_2024_tech.pdf"
    (raw_dir / pdf_name).write_bytes(pdf_bytes)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "corpus_name": "synthetic-test",
                "documents": [
                    {
                        "doc_id": "syn_2024_technical",
                        "year": 2024,
                        "doc_type": "technical",
                        "issue": "Issue 1",
                        "published_date": "2024-01-01",
                        "title": "Synthetic 2024 Technical Regulations",
                        "filename": pdf_name,
                        "url": "https://example.com/synthetic_2024_tech.pdf",
                        "sha256": pdf_sha,
                        "expected_bytes": len(pdf_bytes),
                        "expected_pages": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code = cli.main(
        [
            "parse",
            "--manifest",
            str(manifest_path),
            "--raw-dir",
            str(raw_dir),
            "--processed-dir",
            str(processed_dir),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Parsed 1 document(s): 1 pages" in out
    assert (processed_dir / "clauses.jsonl").is_file()
    assert (processed_dir / "pages.jsonl").is_file()
    assert (processed_dir / "parse_summary.json").is_file()
    clauses_text = (processed_dir / "clauses.jsonl").read_text(encoding="utf-8")
    assert "798kg" in clauses_text


def test_cli_parse_forwards_flags_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_kwargs: dict[str, Any] = {}
    manifest = tmp_path / "manifest.json"
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"

    def fake_parse_corpus(**kwargs: Any) -> ParseReport:
        captured_kwargs.update(kwargs)
        return ParseReport(
            manifest_sha256="1" * 64,
            documents_parsed=2,
            total_pages=20,
            total_clauses=85,
            clauses_path=processed_dir / "clauses.jsonl",
            pages_path=processed_dir / "pages.jsonl",
            summary_path=processed_dir / "parse_summary.json",
            clauses_sha256="2" * 64,
            pages_sha256="3" * 64,
        )

    monkeypatch.setattr(cli, "parse_corpus", fake_parse_corpus)

    code = cli.main(
        [
            "parse",
            "--manifest",
            str(manifest),
            "--raw-dir",
            str(raw_dir),
            "--processed-dir",
            str(processed_dir),
            "--doc-id",
            "fia_2024_sporting",
        ]
    )
    assert code == 0
    assert captured_kwargs == {
        "manifest_path": manifest,
        "raw_dir": raw_dir,
        "processed_dir": processed_dir,
        "doc_ids": ["fia_2024_sporting"],
    }
    out = capsys.readouterr().out
    assert "Parsed 2 document(s)" in out
    assert "85 clauses" in out


def test_cli_parse_error_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_parse_corpus(**kwargs: Any) -> ParseReport:
        raise ParseError("missing raw PDF")

    monkeypatch.setattr(cli, "parse_corpus", failing_parse_corpus)

    code = cli.main(["parse", "--raw-dir", str(tmp_path / "raw")])
    assert code == 1
    captured = capsys.readouterr()
    assert "missing raw PDF" in captured.err


def test_run_module_as_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["regrag", "version"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("regrag", run_name="__main__")
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"regrag {regrag.__version__}"


def test_run_cli_module_as_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["regrag", "version"])
    assert cli.__file__ is not None
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(cli.__file__, run_name="__main__")
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"regrag {regrag.__version__}"


def test_python_m_regrag_version_subprocess() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "regrag", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"regrag {regrag.__version__}"


def test_python_m_regrag_cli_version_and_index_subprocess() -> None:
    version_proc = subprocess.run(
        [sys.executable, "-m", "regrag.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert version_proc.returncode == 0
    assert version_proc.stdout.strip() == f"regrag {regrag.__version__}"

    index_proc = subprocess.run(
        [sys.executable, "-m", "regrag.cli", "index"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert index_proc.returncode != 0
    combined = index_proc.stdout + index_proc.stderr
    assert "P2" in combined
    assert "index" in combined


def test_console_script_entry_point() -> None:
    eps = importlib.metadata.entry_points(group="console_scripts")
    matches = [ep for ep in eps if ep.name == "regrag"]
    assert len(matches) == 1
    assert matches[0].value == "regrag.cli:main"


def test_installed_console_script_executable() -> None:
    script_name = "regrag.exe" if sys.platform == "win32" else "regrag"
    script_path = Path(sys.executable).parent / script_name
    assert script_path.is_file(), f"Expected installed console script at {script_path}"

    version_proc = subprocess.run(
        [str(script_path), "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert version_proc.returncode == 0
    assert version_proc.stdout.strip() == f"regrag {regrag.__version__}"

    index_proc = subprocess.run(
        [str(script_path), "index"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert index_proc.returncode != 0
    combined = index_proc.stdout + index_proc.stderr
    assert "P2" in combined
    assert "index" in combined


def test_makefile_help_and_docker_phase_gate(repo_root: Path) -> None:
    makefile_text = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert not re.search(r"@echo[^\n]*\(P[0-9]\)", makefile_text), (
        "Makefile must not use unquoted @echo with parenthesized phase tags (breaks POSIX sh)"
    )
    assert "todo('P5', 'docker')" in makefile_text

    make_exe = shutil.which("make")
    if make_exe is not None:
        help_proc = subprocess.run(
            [make_exe, "-C", str(repo_root), "help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert help_proc.returncode == 0, f"make help failed: {help_proc.stderr}"
        assert "regrag targets:" in help_proc.stdout
        assert "(P1)" in help_proc.stdout
        assert "(P5)" in help_proc.stdout

        docker_proc = subprocess.run(
            [make_exe, "-C", str(repo_root), "docker", f"PY={sys.executable}"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert docker_proc.returncode != 0
        docker_combined = docker_proc.stdout + docker_proc.stderr
        assert "P5" in docker_combined
        assert "docker" in docker_combined
        assert "is not implemented yet" in docker_combined


def test_cli_unknown_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["unknown-subcommand"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("subcommand", "expected_phase"),
    [
        ("index", "P2"),
        ("eval", "P3"),
        ("gate-negative", "P3"),
        ("serve", "P4"),
    ],
)
def test_cli_unimplemented_subcommands_raise_phase_exit(
    subcommand: str, expected_phase: str
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([subcommand])
    message = str(exc_info.value)
    assert expected_phase in message
    assert subcommand in message
    assert "see README status table" in message


@pytest.mark.parametrize(
    "module_name",
    [
        "regrag.corpus",
        "regrag.corpus.fetch",
        "regrag.corpus.parse",
    ],
)
def test_implemented_p1_corpus_modules_export_public_api(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    exported = getattr(mod, "__all__", None)
    assert isinstance(exported, list) and len(exported) > 0
    for symbol in exported:
        assert hasattr(mod, symbol), f"{module_name}.__all__ lists missing symbol {symbol!r}"


@pytest.mark.parametrize(
    ("module_name", "expected_phase"),
    [
        ("regrag.index", "P2"),
        ("regrag.index.build", "P2"),
        ("regrag.retrieve", "P2"),
        ("regrag.answer", "P3"),
        ("regrag.eval", "P3"),
        ("regrag.eval.metrics", "P3"),
        ("regrag.eval.run", "P3"),
        ("regrag.serve", "P4"),
    ],
)
def test_stub_modules_expose_empty_all(module_name: str, expected_phase: str) -> None:
    mod = importlib.import_module(module_name)
    assert getattr(mod, "__all__", None) == []
    assert isinstance(mod.__doc__, str)
    assert expected_phase in mod.__doc__


def test_phase_todo_message_content() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _phase.todo("P2", "index")
    message = str(exc_info.value)
    assert "regrag:" in message
    assert "P2" in message
    assert "index" in message
    assert "see README status table" in message
