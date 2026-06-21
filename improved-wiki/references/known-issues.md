# Known issues / bugs in `improved-wiki`

## Open issues

### `--dry-run` gives a wrong impression of what the LLM call will cost

`--dry-run` does NOT print estimated prompt length or token cost. User can't tell if the upcoming call will be 30s or 30min. Fix: add `--dry-run-extract` mode that counts chars in extracted text.

### Shell scripts lack `set -euo pipefail`

`run-queue.sh`, `wiki-lint.sh`, `wiki-monitor.sh` — should add strict error handling.

### Several files exceed the 800-line guideline

`ingest.py` (~1800 lines), `_stage_2_4_generation.py` (~628 lines).

## Design decisions (not bugs)

### `ingest.py` uses `urllib.request` not `httpx` / `requests`

Deliberate choice to avoid `pip install` in the cron context.

### Must run with venv Python (system Python lacks PyMuPDF)

Use `~/.venv/bin/python3` or pre-install PyMuPDF.

## Legacy artifacts

### `.digested` files in `raw/` subdirectories

Markers from an older pipeline version. Current pipeline (Stage 0.2) uses `wiki/sources/` as sole dedup signal. Cleanup: see `references/maintenance-cleanup.md`.
