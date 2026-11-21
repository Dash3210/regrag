# regrag

Citation-grounded retrieval and evaluation over multi-year **FIA Formula 1 Technical & Sporting Regulations** (starting with the 2024 and 2025 Technical Regulations verified in the [Phase P0 PDF corpus spike](docs/p0-corpus-spike.md)).

`regrag` is built with an explicit, inspectable Python stack — **no LangChain, no LlamaIndex, and no paid vector database SaaS** — starting from a deterministic, extractive-first `$0` baseline with version-aware citation verification and offline evaluation gates.

## Phase Status

| Phase | Scope | Status |
|---|---|---|
| P0 | Setup & 30-Clause Corpus Spike | In Review |
| P1 | Corpus Fetch & Clause-Aware Parser | Planned |
| P2 | Hybrid Index, Retrieval & Version-Isolation Guards | Planned |
| P3 | Three Answer Modes, 150-Question Eval Harness & Ablation Gate | Planned |
| P4 | FastAPI Service, Static Query UI & Observability | Planned |
| P5 | Containerisation & CI Gate | Planned |
| P6 | The Failing-Gate Pull Request | Planned |
| P7 | Failure Taxonomy, Static Pages Report & Ship | Planned |

## P0 Quickstart

**Prerequisites:** Python 3.10+ and GNU `make`.

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

Run the full P0 verification suite (`lint`, `typecheck`, `test`, `eval-integrity`):

```sh
make check
```

Check the installed CLI version:

```sh
.venv/bin/regrag version          # POSIX
.venv\Scripts\regrag.exe version  # Windows (cmd.exe / PowerShell)
```

Unimplemented phase subcommands (`fetch`, `parse`, `index`, `eval`, `gate-negative`, `serve`) intentionally fail loudly with the phase in which they arrive.

## Corpus Licensing & Provenance

The selected corpus is the official **FIA Formula 1 Technical & Sporting Regulations** (2024 and 2025 Technical Regulations verified in [docs/p0-corpus-spike.md](docs/p0-corpus-spike.md); Sporting Regulations and 2026 editions catalogued for P1). Source regulation PDFs remain the property of the Fédération Internationale de l'Automobile (FIA) and are **never committed** to this repository. Raw downloads live under gitignored `data/raw/` and are fetched at runtime from official URLs recorded in the corpus manifest (arriving in P1).

## Documentation & Decisions

- Architecture Decision Records (`ADR-001`–`ADR-005`): [docs/decisions.md](docs/decisions.md)
- Phase P0 30-Clause PDF Corpus Spike: [docs/p0-corpus-spike.md](docs/p0-corpus-spike.md)
