---
description: "Image captioning strategy — unified pipeline over office-extracted (PPTX/DOCX zipfile) + minerU-extracted (PDF) images, parallel batch dispatch via ThreadPoolExecutor, grayscale→RGB preprocessing, VLM failure detection. Verified: HardwareWiki 18,709 images (2026-06-17), EMC book fix (2026-06-17)."
tags: [vlm, captioning, batch, minimax, strategy, parallel, preprocessing]
related: [multimodal-vlm-pitfalls, ingest-stages-mandatory §0.6, known-issues]
---

# Image Captioning 策略

Unified image captioning pipeline. Implemented as `stage_1_3_caption_images()` / `_stage_1_3_caption_images_batch()` in `scripts/_stage_1_extract.py` (moved out of `ingest.py` during the 2026-06-22 explicit-stage-naming refactor — old name was `_caption_images()`).

**2026-06-23 update**: the "Path A / Path B" split below is now stale terminology. PyMuPDF no longer extracts any images (PDF image extraction moved to minerU — see `ingest-stages-mandatory.md` Stage 1.2). The two sources captioning actually sees today are:
- **PDF images** — harvested by minerU (`_stage_1_2_harvest_images()` inline during Stage 1.1 chunk processing, or `_stage_1_2_extract_from_mineru()` for the opt-in CLI pipeline path)
- **PPTX/DOCX images** — extracted via `_stage_1_2_extract_images_office()` (zipfile, not PyMuPDF)

The code still passes `source_label="pyMuPDF"` as a hardcoded literal in `stage_1_3_caption_images()` — that label is leftover from before the migration and no longer reflects where the images actually came from. Harmless (it's just a log/manifest label, not used for branching), but don't trust it as a source-of-truth indicator.

---

## Architecture (2026-06-17, function names updated 2026-06-23)

```
PDF (minerU harvest)                  PPTX/DOCX (zipfile office extract)
  → media_dir / p0007-mineru_a1b2.png   → media_dir / image1.png
  → {"filename":..., "page":N, ...}     → {"filename":..., "page":0, ...}
                │                              │
                └──────────┬───────────────────┘
                           ▼
        _stage_1_3_caption_images_batch()  ← unified entry point
                │
                ├── _stage_1_3_preprocess_image()
                │     • grayscale (mode L/LA/P/PA) → RGB
                │     • oversized (>1568px) → thumbnail
                │     • base64 encode
                │
                ├── ThreadPoolExecutor (max CAPTION_MAX_WORKERS batches parallel)
                │     • _stage_1_3_caption_one_batch(): builds multi-image request
                │
                └── _stage_1_3_is_caption_failed()
                      • Detects VLM error responses
                      • Writes "[待重试]" fallback for later retry
```

(The "Path A/B" labels still in the code itself — `source_label="pyMuPDF"` default, the docstring inside `_stage_1_3_caption_images_batch()` — are stale leftovers from before the 2026-06-23 PyMuPDF-removal; harmless, just don't read them as describing current image sourcing.)

## Key parameters

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Batch size | 8 | `CAPTION_BATCH_SIZE` | Images per API call |
| Max workers | 6 | `CAPTION_MAX_WORKERS` | Parallel batch concurrency |
| Image max dim | 1568 | — | Downscale threshold (Anthropic vision limit) |

## Performance

| Metric | Before (serial) | After (parallel) | Speedup |
|--------|----------------|------------------|---------|
| 40 images (5 batches) | ~90s | 15.8s | **5.7×** |
| 200 images (25 batches) | ~450s | ~25s (est.) | **~18×** |
| 2,196 images (The Art of Electronics) | ~6,600s | ~90s (est.) | **~73×** |

HardwareWiki verified (2026-06-17): 18,709 images, 18,701 captions (99.96% coverage).

## Image preprocessing

`_preprocess_image_for_caption()` in `ingest.py`:
- **Normalize to RGB**: palette/alpha modes converted to plain RGB for consistent encoding (MiniMax M3 handles grayscale fine per A/B test; normalization is defensive)
- **Downscale oversized**: images >1568px in any dimension are thumbnailed (VLM context window limit)
- Handles modes L, LA, P, PA, RGB, RGBA

## VLM failure detection

`_is_caption_failed()` detects these failure patterns:
- `解析失败`, `无法识别`, `无法描述`, `抱歉`, `sorry`
- `unable to`, `cannot describe`, `I can't`, `not clear`
- Text length < 15 characters

Failed captions are written as `[待重试] 图片 <filename>，尺寸 W×H` — the cache filter in `_caption_images()` will re-process them on next run.

## Formula transcription (LaTeX-only)

MiniMax-M3 transcribes formula images ~81% of the time (verified on Tudoroiu
2021: 52/64 tiny formula strips successfully transcribed). `CAPTION_SYSTEM_PROMPT`
includes a special rule for formula images:

- **Transcribe formula content symbol-by-symbol in LaTeX** — e.g.
  `$x_{k+1}=Ax_k+Bu_k$`, `$\sum_{i=0}^{2n} W_c^{(i)}[Y^i-\hat{y}]$`,
  `$\dot{T}=\frac{1}{mc_p}\dot{Q}$`
- **Do NOT use Unicode subscripts/superscripts or Greek letters** — write
  `x_1`, `\eta`, `\alpha`, `\Sigma`, NOT `x₁`, `η`, `α`, `Σ`. Rationale: LaTeX
  renders in markdown and is reusable downstream; Unicode subscripts are
  unparseable and don't render.
- **Word limit relaxed to 150 chars** for formula images (vs 100 default) to
  avoid truncating long equations.
- Unknown symbols use `?` placeholder.

## Tiny-image filter (`_is_image_too_small`)

Filters only true noise (1×1/2×2 pixel artifacts). Threshold is deliberately
conservative (`MINERU_IMG_MIN_WIDTH=20`, `MINERU_IMG_MIN_HEIGHT=20`, env-overridable)
because tiny formula strips (29-70px tall) are valuable — MiniMax transcribes
them. The filter must NOT be aggressive or it throws away recoverable formula
content.

> **Bug fixed 2026-06-22**: `MINERU_IMG_MIN_WIDTH`/`MINERU_IMG_MIN_HEIGHT` were
> referenced but never defined → `_is_image_too_small()` raised `NameError`,
> silently swallowed by the surrounding `try/except Exception: pass`, so the
> filter was completely non-functional (every image was kept). Fix: defined the
> constants AND moved the size check outside the broad try/except so future
> regressions surface instead of being swallowed.

## Usage

```bash
# Env vars for tuning
export CAPTION_BATCH_SIZE=10    # more images per call
export CAPTION_MAX_WORKERS=8    # more parallel workers
```

Direct call from Python (e.g., to repair failed captions for a specific book):
```python
from _stage_1_extract import _stage_1_3_caption_images_batch
media_dir = Path("wiki/media/Book/Some Book - 2024 - Author")
images = [{"filename": f.name, "page": 0, "width": 0, "height": 0}
          for f in sorted(media_dir.iterdir())
          if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
captioned = _stage_1_3_caption_images_batch(images, config, media_dir, source_label="repair")
```

## Revision history

- **2026-06-11**: Initial version, 738-image benchmark
- **2026-06-17**: Unified Path A + Path B into single `_caption_images()`; parallel batch dispatch via ThreadPoolExecutor; grayscale→RGB preprocessing; VLM failure detection with retry; cache filter checks existing caption content for failures
- **2026-06-22**: LaTeX-only formula transcription rule in `CAPTION_SYSTEM_PROMPT` (no Unicode subscripts/Greek, 150-char limit for formulas); fixed `_is_image_too_small` NameError bug (undefined `MINERU_IMG_MIN_WIDTH/HEIGHT` silently disabled the filter — constants now defined at 20px, size check moved outside broad try/except)
- **2026-06-23**: functions moved from `ingest.py` to `_stage_1_extract.py` with explicit stage prefixes (`_caption_images` → `stage_1_3_caption_images`/`_stage_1_3_caption_images_batch`, etc.); PyMuPDF removed entirely from PDF image extraction (Path A description above is now historical only — see note at top of doc)
