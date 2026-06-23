# Known issues / bugs in `improved-wiki`

## Open issues

### Shell scripts lack `set -euo pipefail`

`run-queue.sh`, `wiki-lint.sh`, `wiki-monitor.sh` — should add strict error handling.

### Several files exceed the 800-line guideline

`ingest.py` (~2062 lines), `_stage_2_4_generation.py` (~632 lines).

## Design decisions (not bugs)

### `ingest.py` uses `urllib.request` not `httpx` / `requests`

Deliberate choice to avoid `pip install` in the cron context.

### Must run with venv Python (system Python lacks PyMuPDF)

Use `~/.venv/bin/python3` or pre-install PyMuPDF.

## Batch digest patterns

Batch ingestion (looping `ingest.py` over many PDFs) has its own pitfalls:
`claude -p` cannot loop, failure-mode table, dedup signal, and concurrency
rules. See `references/batch-digest-patterns.md` for the full write-up.

The one-line summary: call `ingest.py` directly from a Python loop (not through
`claude -p`), dedup on `wiki/sources/<stem>.md` existence, and run serially.

## Legacy artifacts

### `.digested` files in `raw/` subdirectories

Markers from an older pipeline version. Current pipeline (Stage 0.2) uses `wiki/sources/` as sole dedup signal. Cleanup: see `references/maintenance-cleanup.md`.
