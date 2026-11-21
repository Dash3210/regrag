# PYTHON is the interpreter used once, to create the venv. Override it if your
# python 3.10+ is not the one on PATH:  make install PYTHON=/path/to/python3.10
ifeq ($(OS),Windows_NT)
PYTHON ?= python
PY ?= .venv/Scripts/python.exe
else
PYTHON ?= python3
PY ?= .venv/bin/python
endif

EMPTY :=

.PHONY: help install corpus index eval gate-negative test lint typecheck eval-integrity check serve docker clean
.DEFAULT_GOAL := help

help:
	$(info regrag targets:)
	$(info $(EMPTY)  install         - Create virtual environment and install dependencies)
	$(info $(EMPTY)  corpus          - Fetch and parse regulation PDFs into clause corpus (P1))
	$(info $(EMPTY)  index           - Build lexical and dense retrieval indexes (P2))
	$(info $(EMPTY)  eval            - Run deterministic evaluation benchmark and gate (P3))
	$(info $(EMPTY)  gate-negative   - Run negative-control evaluation gate check (P3))
	$(info $(EMPTY)  test            - Run test suite with coverage report)
	$(info $(EMPTY)  lint            - Run ruff linter)
	$(info $(EMPTY)  typecheck       - Run mypy type checker)
	$(info $(EMPTY)  eval-integrity  - Verify repository hygiene and evaluation integrity invariants)
	$(info $(EMPTY)  check           - Run lint, typecheck, test, and eval-integrity)
	$(info $(EMPTY)  serve           - Run citation-grounded QA API service (P4))
	$(info $(EMPTY)  docker          - Build production Docker image (P5))
	$(info $(EMPTY)  clean           - Remove virtual environment, caches, and build artefacts)
	@cd .

install:
	"$(PYTHON)" -c "import sys; sys.version_info >= (3, 10) or sys.exit('regrag requires Python >= 3.10; found ' + sys.version.split()[0] + '. Override with: make install PYTHON=/path/to/python3.10')"
	"$(PYTHON)" -c "import os, shutil, subprocess, sys; py = '$(PY)'; ok = os.path.isfile(py) and os.access(py, os.X_OK) and subprocess.run([py, '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'], check=False).returncode == 0; (shutil.rmtree('.venv', ignore_errors=True) if os.path.exists('.venv') and not ok else None); ok or subprocess.run([sys.executable, '-m', 'venv', '.venv'], check=True)"
	"$(PY)" -m pip install --upgrade pip setuptools
	"$(PY)" -m pip install -e ".[dev]"

corpus:
	"$(PY)" -m regrag fetch
	"$(PY)" -m regrag parse

index:
	"$(PY)" -m regrag index

eval:
	"$(PY)" -m regrag eval

gate-negative:
	"$(PY)" -m regrag gate-negative

test:
	"$(PY)" -m pytest --cov --cov-report=term-missing

lint:
	"$(PY)" -m ruff check src tests

typecheck:
	"$(PY)" -m mypy

eval-integrity:
	"$(PY)" -m pytest tests/test_eval_integrity.py

check: lint typecheck test eval-integrity

serve:
	"$(PY)" -m regrag serve

docker:
	"$(PY)" -c "from regrag._phase import todo; todo('P5', 'docker')"

clean:
	"$(PYTHON)" -c "import glob, os, shutil; [shutil.rmtree(p, ignore_errors=True) for p in ['.venv', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'build', 'dist', 'htmlcov', *glob.glob('src/*.egg-info'), *glob.glob('*.egg-info')]]; [os.remove(f) for f in glob.glob('**/*.pyc', recursive=True)]; [shutil.rmtree(d, ignore_errors=True) for d in glob.glob('**/__pycache__', recursive=True)]; [os.remove(f) for f in ['.coverage', 'coverage.xml'] if os.path.exists(f)]"
