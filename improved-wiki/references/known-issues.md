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
   check was skipped for only 4 method names, but Stage 1.1 returns multiple
   `mineru-*` labels (currently `mineru-api` / `mineru-api-ocr` / `mineru-pipeline`
   / `*-low-quality`; formerly `mineru-api-txt` / `mineru-vlm` / `mineru-api-mixed`
   / `*-low-quality` before the 2026-06-23 single-path refactor). OCR text
   then tripped false warnings. Now skipped via `method.startswith("mineru")`,
   which is label-agnostic and survived the refactor.
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

Use `~/.venv/bin/python3` or pre-install PyMuPDF. **Also**: `ingest.py` and
stage modules use PEP 604 union syntax (`str | None`), which requires Python
3.10+. macOS system `/usr/bin/python3` is 3.9 and will throw
`TypeError: unsupported operand type(s) for |` — this is the #1 first-run
failure even when PyMuPDF is installed system-wide.

### Wikilink enrichment merge loop after Stage 3.1 (2026-06-24)

After Stage 3.1 write, the pipeline generates many `LLM-task-*.md` merge prompts
in `.llm-wiki/conversation/<hash>/`. Each asks the agent to merge an existing wiki
page with new content from the latest ingest. The pipeline also re-discovers and
re-merges pages across `ingest.py` re-runs as new wikilinks are found — the same
page may appear in multiple merge rounds.

**Efficient handling**: use `delegate_task` with `['terminal', 'file']` toolsets
to batch-process these. A leaf subagent reads each `.md`, writes the merged body
to `.txt`, and re-runs `ingest.py` in a loop until exit code 0 or a non-merge
LLM stage (Review/quality) is reached.

**Wikilink suggestion JSON tasks**: the pipeline also generates a wikilink
enrichment task expecting a JSON object mapping page paths to `[{term, target}]`
lists. Outputting `{}` safely skips it with no quality loss if pages already have
inline wikilinks from Stage 2.4 generation.

### Stage 2.1 sends only sampled text (2026-06-24)

The Global Digest prompt includes only a small text sample (~4K chars from the
middle of the book), NOT the full extracted text. The full text is in
`.llm-wiki/extract-tmp/<book-stem>/p*.txt` (one file per page, 272 pages = 540K
chars for a typical book). **When answering Stage 2.1, read page samples from
the extract-tmp directory** to produce an accurate outline and chunk plan. The
sampled text alone is insufficient for a complete digest.

### OCR timeout for 200+ page books (2026-06-24)

minerU OCR processes 50 pages/chunk serially. A 272-page book (6 chunks) can
exceed the 600s terminal timeout. **Re-running `ingest.py` resumes from cache**
— completed chunks are skipped, only the interrupted chunk re-runs. Using
`--stop-after-stage 0` separates OCR from the LLM stages and avoids timeout
pressure on the LLM steps.

### Image extraction + captioning: 5 issues from 2026-06-24 re-ingest

Full analysis in `references/image-caption-strategy.md` § "Known issues
discovered 2026-06-24". Summary:

1. **MinerU `image_caption` wasted on API path** — `_stage_1_2_harvest_images()`
   doesn't write sidecars from `content_list`'s `image_caption` field; only
   `_stage_1_2_extract_from_mineru()` (CLI path) does. 269 images with
   pre-existing captions redundantly sent to VLM.
2. **188 fragment images not filtered** — API `images` dict has 528 entries vs
   340 `content_list` image/chart blocks. Extra 188 are fragments/noise.
3. **No retry for failed captions** — single-pass `ThreadPoolExecutor`; 202/528
   images (38%) left uncaptioned after 7 JSON truncation events.
4. **`batch_size=6` hardcoded** at line 990 (minerU path) vs `CAPTION_BATCH_SIZE=8`
   env default — causes JSON truncation on 6-image batches.
5. **~112 formula images extracted as pictures** — minerU classifies some
   formula regions as `image` blocks rather than `equation` blocks, so they
   get VLM-captioned instead of using the LaTeX text minerU already extracted.

### `--delete` for re-ingest (2026-06-24)

To re-ingest a book for comparison or correction:
`ingest.py --delete "raw/Book/<file>.pdf"` removes the source page, orphan
concepts/entities, media directory, and cache entry. Then re-run without
`--delete` to start fresh. This is the clean way to redo an ingest without
leaving orphaned pages from the previous run.

## Batch digest patterns

Batch ingestion (looping `ingest.py` over many PDFs) has its own pitfalls:
`claude -p` cannot loop, failure-mode table, dedup signal, and concurrency
rules. See `references/batch-digest-patterns.md` for the full write-up.

The one-line summary: call `ingest.py` directly from a Python loop (not through
`claude -p`), dedup on `wiki/sources/<stem>.md` existence, and run serially.

## Legacy artifacts

### `.digested` files in `raw/` subdirectories

Markers from an older pipeline version. Current pipeline (Stage 0.2) uses `wiki/sources/` as sole dedup signal. Cleanup: see `references/maintenance-cleanup.md`.
