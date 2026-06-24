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

## Known issues discovered 2026-06-24 (从零开始学散热 re-ingest, 528 images)

A full re-ingest of a 272-page Chinese thermal-design book exposed 5
quantifiable issues in the current image extraction + captioning pipeline.
All measurements below are from that run.

### Issue 1: MinerU's built-in `image_caption` is wasted on the API path

MinerU's `content_list` contains `image_caption` fields for **269/300** image
blocks (e.g. `["图11-14 汽蚀导致的叶片严重损坏"]`). The function that writes
these as sidecar `.caption.txt` files is `_stage_1_2_extract_from_mineru()`
(line ~1668), but **this function is only called on the opt-in pipeline CLI
path** (`IMPROVED_WIKI_PIPELINE_CLI=1`). The default API path uses
`_stage_1_2_harvest_images()` (line ~1341), which saves image files but does
NOT read `content_list`'s `image_caption` field — so 269 images with
pre-existing captions are sent to MiniMax VLM for redundant re-captioning.

**Fix needed**: `_stage_1_2_harvest_images()` should read `content_list` blocks
for `type=image/chart` and write `image_caption[0]` as a sidecar
`.caption.txt` before Stage 1.3 runs. This would skip ~50% of VLM calls for
typical books.

### Issue 2: 528 extracted images vs 340 content_list image/chart blocks

MinerU's API `images` dict (base64 figures) contains **528** entries, but
`content_list` has only **300 image + 40 chart = 340** blocks. The **188
extra images** are not accounted for by `content_list` — they are likely
page-level fragments, formula screenshots, or table crops that minerU's
layout analysis classified differently internally.

`_stage_1_2_harvest_images()` saves everything in `images` dict without
checking `content_list` membership, so these 188 low-value fragments are
extracted, stored, and sent to VLM captioning — wasting time and diluting
quality.

**Fix needed**: filter `images` dict to only keep entries whose basename
appears in a `content_list` `image`/`chart` block's `img_path` field.

### Issue 3: No retry for failed/uncaptioned images (single-pass only)

Caption dispatch is a single `ThreadPoolExecutor` pass — batches that fail
JSON parsing or have truncated responses are logged but **not retried**.
7 `json_truncation` events were logged (each salvaging 4-5 of 6 captions),
leaving **202 images (38%)** without any caption.

The `_stage_1_3_is_caption_failed()` function writes `[待重试]` fallbacks,
but there is no second pass to actually retry them. The next `ingest.py`
run would re-process them, but within a single ingest session they stay
uncaptioned.

**Fix needed**: after the main batch pass, collect images still lacking
captions and dispatch a second round (smaller batch_size=3, single-image
fallback for persistent failures).

### Issue 4: batch_size inconsistency

`CAPTION_BATCH_SIZE=8` (env var, line 66/1103) is the intended default, but
the minerU path call at line 990 hardcodes `batch_size=6`. JSON truncation
happens because 6-image batches can exceed MiniMax response token limits.
The 2024-06-11 HardwareWiki benchmark used 5 images/batch (optimal), but
this value was never parameterized.

**Fix needed**: remove the hardcoded `batch_size=6` at line 990, let it use
`CAPTION_BATCH_SIZE` default. Consider lowering default to 5.

### Issue 5: Formula images extracted as pictures instead of text

MinerU `content_list` marks **114 equation blocks** as LaTeX text (e.g.
`$$Q = P/d/C_p/\Delta t$$`) with `type=equation`, `text_format=latex`.
These are NOT in the `images` dict and NOT extracted as image files —
minerU handles them correctly as text.

However, ~112 narrow+short images (W/H > 2.5, height < 100px) were found
among the 528 extracted images, and 77 of their captions mention "公式"
or "formula". This means minerU's layout analysis sometimes classifies
formula regions as `image` blocks (with `img_path`) rather than `equation`
blocks (with LaTeX text), especially for complex multi-line formulas or
formulas embedded in figure captions.

**Mitigation** (not a fix): the current tiny-image filter
(`_is_image_too_small`, 20px threshold) deliberately keeps these because
MiniMax can transcribe them to LaTeX. The VLM caption prompt already
instructs LaTeX transcription. So the system degrades gracefully — but
these formula images consume VLM calls that shouldn't be needed if minerU
classified them correctly.

### Summary table

| Issue | Impact | VLM calls wasted | Fix difficulty |
|-------|--------|-----------------|----------------|
| 1: MinerU caption not used as sidecar | 269 redundant VLM calls | ~51% of total | Low (20 lines) |
| 2: 188 fragment images not filtered | 188 unnecessary VLM calls | ~36% of total | Low (10 lines) |
| 3: No retry for failed captions | 202 images uncaptioned (38%) | N/A (quality loss) | Medium (30 lines) |
| 4: batch_size=6 hardcoded | 7 JSON truncation events | ~7 batches affected | Low (2 lines) |
| 5: Formulas as images | ~112 formula VLM calls | ~21% of total | Medium (upstream) |

Combined effect of fixing issues 1+2: VLM calls drop from 528 → ~70
(340 content_list images − 269 pre-captioned − some formulas), an 85%+
reduction.

## Revision history

- **2026-06-11**: Initial version, 738-image benchmark
- **2026-06-17**: Unified Path A + Path B into single `_caption_images()`; parallel batch dispatch via ThreadPoolExecutor; grayscale→RGB preprocessing; VLM failure detection with retry; cache filter checks existing caption content for failures
- **2026-06-22**: LaTeX-only formula transcription rule in `CAPTION_SYSTEM_PROMPT` (no Unicode subscripts/Greek, 150-char limit for formulas); fixed `_is_image_too_small` NameError bug (undefined `MINERU_IMG_MIN_WIDTH/HEIGHT` silently disabled the filter — constants now defined at 20px, size check moved outside broad try/except)
- **2026-06-23**: functions moved from `ingest.py` to `_stage_1_extract.py` with explicit stage prefixes (`_caption_images` → `stage_1_3_caption_images`/`_stage_1_3_caption_images_batch`, etc.); PyMuPDF removed entirely from PDF image extraction (Path A description above is now historical only — see note at top of doc)
- **2026-06-24**: Documented 5 issues from 从零开始学散热 re-ingest (528 images, 202 uncaptioned): (1) minerU `image_caption` not used as sidecar on API path, (2) 188 fragment images not filtered by content_list, (3) no retry for failed captions, (4) batch_size=6 hardcoded vs env default 8, (5) ~112 formula images extracted as pictures instead of text
