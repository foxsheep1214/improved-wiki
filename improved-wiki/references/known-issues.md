# Known issues / bugs in `improved-wiki`

## Open issues

### Shell scripts lack `set -euo pipefail`

`run-queue.sh`, `wiki-lint.sh`, `wiki-monitor.sh` — should add strict error handling.

### Several files exceed the 800-line guideline

`ingest.py` (~2062 lines), `_stage_2_4_generation.py` (~632 lines).

### Fixed: per-block language warnings on OCR / math-heavy text (2026-06-23)

`expected French, got Chinese/Greek` warnings during ingest had three
compounding causes, all fixed (`tests/test_language.py`):

1. **minerU skip-set incomplete** (`ingest.py`): the per-block language
   check was skipped for only 4 method names, but Stage 1.1 returns
   `mineru-pipeline` / `mineru-api-txt` / `mineru-api-mixed` /
   `mineru-vlm-low-quality` / `mineru-api-mixed-low-quality`. OCR text
   then tripped false warnings. Now skipped via `method.startswith("mineru")`.
2. **Greek false positive on math symbols** (`_language.py`): isolated
   Greek letters (λ σ θ Δ …) hit the ≥2-count threshold. Now requires a
   ≥2-letter word run (`_has_greek_word_run`); isolated singletons are
   treated as math notation.
3. **Latin over-eager single-token match** (`_language.py`): one stray
   token like `le` inside English text flipped it to French. German /
   French / Spanish / Italian / Dutch / Indonesian now require ≥2
   distinct function words (Spanish keeps ñ/¿/¡ as a solo signal).

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
