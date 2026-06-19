---
description: "Image captioning strategy — unified Path A (PyMuPDF) + Path B (minerU) pipeline, parallel batch dispatch via ThreadPoolExecutor, grayscale→RGB preprocessing, VLM failure detection. Verified: HardwareWiki 18,709 images (2026-06-17), EMC book fix (2026-06-17)."
tags: [vlm, captioning, batch, minimax, strategy, parallel, preprocessing]
related: [multimodal-vlm-pitfalls, ingest-stages-mandatory §0.6, known-issues]
---

# Image Captioning 策略

Unified image captioning pipeline covering both Path A (PyMuPDF embedded images) and Path B (minerU scanned-page images). Implemented as `_caption_images()` in `scripts/ingest.py`.

---

## Architecture (2026-06-17)

```
Path A: PyMuPDF get_images()          Path B: minerU VLM OCR
  → media_dir / filename.png            → media_dir / filename.jpg
  → {"filename":..., "page":N, ...}     → {"filename":..., "path":"/abs/..."}
                │                              │
                └──────────┬───────────────────┘
                           ▼
              _caption_images()  ← unified entry point
                │
                ├── _preprocess_image_for_caption()
                │     • grayscale (mode L/LA/P/PA) → RGB
                │     • oversized (>1568px) → thumbnail
                │     • base64 encode
                │
                ├── ThreadPoolExecutor (max CAPTION_MAX_WORKERS batches parallel)
                │     • _caption_one_batch(): builds multi-image request
                │     • Handles both Path A (page annotation) and Path B (index annotation)
                │
                └── _is_caption_failed()
                      • Detects VLM error responses
                      • Writes "[待重试]" fallback for later retry
```

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

## Usage

```bash
# Env vars for tuning
export CAPTION_BATCH_SIZE=10    # more images per call
export CAPTION_MAX_WORKERS=8    # more parallel workers
```

Direct call from Python (e.g., to repair failed captions for a specific book):
```python
from ingest import _caption_images
media_dir = Path("wiki/media/book/Some Book - 2024 - Author")
images = [{"filename": f.name, "page": 0, "width": 0, "height": 0}
          for f in sorted(media_dir.iterdir())
          if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
captioned = _caption_images(images, config, media_dir, source_label="repair")
```

## Revision history

- **2026-06-11**: Initial version, 738-image benchmark
- **2026-06-17**: Unified Path A + Path B into single `_caption_images()`; parallel batch dispatch via ThreadPoolExecutor; grayscale→RGB preprocessing; VLM failure detection with retry; cache filter checks existing caption content for failures
