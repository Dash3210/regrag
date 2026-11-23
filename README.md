# regrag

Citation-grounded retrieval and evaluation over multi-year **FIA Formula 1 Technical & Sporting Regulations** (4 verified 2024 & 2025 PDFs pinned by SHA-256 in [corpus/manifest.json](corpus/manifest.json)).

`regrag` is built with an explicit, inspectable Python stack — **no LangChain, no LlamaIndex, and no paid vector database SaaS** — starting from a deterministic, extractive-first `$0` baseline with version-aware citation verification and offline evaluation gates.

## Phase Status

| Phase | Scope | Status |
|---|---|---|
| P0 | Setup & 30-Clause Corpus Spike | Complete |
| P1 | Corpus Fetch & Clause-Aware Parser | Complete |
| P2 | Hybrid Index, Retrieval & Version-Isolation Guards | Planned |
| P3 | Three Answer Modes, 150-Question Eval Harness & Ablation Gate | Planned |
| P4 | FastAPI Service, Static Query UI & Observability | Planned |
| P5 | Containerisation & CI Gate | Planned |
| P6 | The Failing-Gate Pull Request | Planned |
| P7 | Failure Taxonomy, Static Pages Report & Ship | Planned |

## Quickstart

**Prerequisites:** Python 3.10+ and GNU `make`. Run `make help` at any time to list all available targets.

Create the virtual environment and install `regrag` with dev dependencies:

```sh
make install
```

> **Note:** If Python 3.10+ is not the default `python`/`python3` on your `PATH`, override `PYTHON`:
>
> ```sh
> make install PYTHON=/path/to/python3.10
> ```
>
> To wipe and recreate `.venv` cleanly at any time, run `make clean` before `make install`.

Fetch the 4 official FIA regulation PDFs into gitignored `data/raw/` and parse them into deterministic JSONL artifacts under gitignored `data/processed/`:

```sh
make corpus
```

Or invoke the P1 CLI subcommands directly (both accept `--manifest`, `--raw-dir`, and repeatable `--doc-id` from `fia_2024_technical`, `fia_2025_technical`, `fia_2024_sporting`, `fia_2025_sporting`; `fetch` also supports `--force`, and `parse` accepts `--processed-dir`):

```sh
.venv/bin/regrag fetch            # Linux / macOS (idempotent SHA-256 verification)
.venv/bin/regrag parse            # Linux / macOS (writes clauses.jsonl, pages.jsonl, parse_summary.json)

.venv/Scripts/regrag.exe fetch    # Windows (Git Bash / MSYS2)
.venv\Scripts\regrag.exe fetch    # Windows (cmd.exe / PowerShell)
.venv\Scripts\regrag.exe parse    # Windows (cmd.exe / PowerShell)
```

> **Note:** Running `regrag parse --doc-id <id>` without a separate `--processed-dir` overwrites `data/processed/clauses.jsonl`, `pages.jsonl`, and `parse_summary.json` with only the selected document(s). Pass `--processed-dir <dir>` when inspecting a subset.

Run the full verification suite (`lint`, `typecheck`, `test`, `eval-integrity`):

```sh
make check
```

Check the installed CLI version:

```sh
.venv/bin/regrag version          # Linux / macOS
.venv/Scripts/regrag.exe version  # Windows (Git Bash / MSYS2)
.venv\Scripts\regrag.exe version  # Windows (cmd.exe / PowerShell)
```

Unimplemented phase subcommands (`index`, `eval`, `gate-negative`, `serve`) and `make docker` (`P5`) intentionally fail loudly with the phase in which they arrive.

## Corpus Licensing & Provenance

The corpus comprises 4 official **FIA Formula 1 Technical & Sporting Regulations** PDFs (`fia_2024_technical`, `fia_2025_technical`, `fia_2024_sporting`, `fia_2025_sporting`; 583 pages total), pinned by exact HTTPS URL, byte size, page count, and SHA-256 digest in [corpus/manifest.json](corpus/manifest.json). `regrag fetch` verifies `%PDF-` headers, rejects HTTPS-to-HTTP redirect downgrades, and validates SHA-256 digests atomically before placing PDFs under gitignored `data/raw/`; `regrag parse` validates manifest provenance and writes deterministic JSONL outputs (`clauses.jsonl`, `pages.jsonl`, `parse_summary.json`) under gitignored `data/processed/`. Source regulation PDFs remain the property of the Fédération Internationale de l'Automobile (FIA) and are **never committed** to this repository.

## Documentation & Decisions

- Architecture Decision Records (`ADR-001`–`ADR-007`): [docs/decisions.md](docs/decisions.md)
- Phase P0 30-Clause PDF Corpus Spike: [docs/p0-corpus-spike.md](docs/p0-corpus-spike.md)
- Phase P1 Parse Accuracy & 30-Clause Comparison Report: [docs/p1-parse-accuracy.md](docs/p1-parse-accuracy.md)
- Corpus Manifest (4 FIA PDFs with SHA-256 Provenance): [corpus/manifest.json](corpus/manifest.json)
