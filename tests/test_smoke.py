"""Smoke tests for regrag packaging, CLI phase gating, Makefile, and stub modules."""

import importlib
import importlib.metadata
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import regrag
from regrag import _phase, cli


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


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["version"])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"regrag {regrag.__version__}"


def test_cli_empty_args_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("argv", [["--help"], ["version", "--help"]])
def test_cli_help_exits_zero(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()


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


def test_python_m_regrag_cli_version_and_fetch_subprocess() -> None:
    version_proc = subprocess.run(
        [sys.executable, "-m", "regrag.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert version_proc.returncode == 0
    assert version_proc.stdout.strip() == f"regrag {regrag.__version__}"

    fetch_proc = subprocess.run(
        [sys.executable, "-m", "regrag.cli", "fetch"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert fetch_proc.returncode != 0
    combined = fetch_proc.stdout + fetch_proc.stderr
    assert "P1" in combined
    assert "fetch" in combined


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

    fetch_proc = subprocess.run(
        [str(script_path), "fetch"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert fetch_proc.returncode != 0
    combined = fetch_proc.stdout + fetch_proc.stderr
    assert "P1" in combined
    assert "fetch" in combined


def test_makefile_help_is_shell_safe_and_executes(repo_root: Path) -> None:
    makefile_text = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert not re.search(r"@echo[^\n]*\(P[0-9]\)", makefile_text), (
        "Makefile must not use unquoted @echo with parenthesized phase tags (breaks POSIX sh)"
    )

    make_exe = shutil.which("make")
    if make_exe is not None:
        proc = subprocess.run(
            [make_exe, "-C", str(repo_root), "help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"make help failed: {proc.stderr}"
        assert "regrag targets:" in proc.stdout
        assert "(P1)" in proc.stdout
        assert "(P5)" in proc.stdout


def test_cli_unknown_subcommand_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["unknown-subcommand"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("subcommand", "expected_phase"),
    [
        ("fetch", "P1"),
        ("parse", "P1"),
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
    ("module_name", "expected_phase"),
    [
        ("regrag.corpus", "P1"),
        ("regrag.corpus.fetch", "P1"),
        ("regrag.corpus.parse", "P1"),
        ("regrag.index", "P2"),
        ("regrag.index.build", "P2"),
        ("regrag.retrieve", "P2"),
        ("regrag.answer", "P2"),
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
