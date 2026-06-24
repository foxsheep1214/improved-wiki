---
description: "Image captioning — unified pipeline over minerU-extracted (PDF) + zipfile-extracted (PPTX/DOCX) images, parallel batch dispatch, MiniMax VLM. No fallback: missing key or batch failure pauses the ingest."
tags: [vlm, captioning, batch, minimax, strategy]
related: [ingest-stages-mandatory, multimodal-vlm-pitfalls, known-issues]
---

# Image Captioning 策略

Unified image captioning. Implemented as `stage_1_3_caption_images()` / `_stage_1_3_caption_images_batch()` / `_stage_1_3_caption_one_batch()` in `scripts/_stage_1_extract.py`。

两个图片来源：
- **PDF** — minerU 在 Stage 1.1 chunk 处理时由 `_stage_1_2_harvest_images()` 内联提取
- **PPTX/DOCX** — `_stage_1_2_extract_images_office()` 从 zip 内 `ppt/media`/`word/media` 取图

> **无回退策略（2026-06-24）**：caption key 缺失或批次重试耗尽 → `raise RuntimeError` 暂停 ingest，不写占位符、不静默降级。详见 `ingest-stages-mandatory.md`。

## Architecture

```
PDF (minerU harvest)                  PPTX/DOCX (zipfile office extract)
  → media_dir / p0007-mineru_a1b2.png   → media_dir / image1.png
  → {"filename":..., "page":N, ...}     → {"filename":..., "page":0, ...}
                │                              │
                └──────────┬───────────────────┘
                           ▼
        _stage_1_3_caption_images_batch()  ← unified entry point
                │
                ├── _stage_1_3_preprocess_image()  (grayscale→RGB, downscale, base64)
                ├── ThreadPoolExecutor (CAPTION_MAX_WORKERS batches parallel)
                │     └── _stage_1_3_caption_one_batch(): multi-image API request
                └── _stage_1_3_is_caption_failed()  (detect VLM error responses)
```

## Key parameters

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Batch size | 8 | `CAPTION_BATCH_SIZE` | Images per API call |
| Max workers | 6 | `CAPTION_MAX_WORKERS` | Parallel batch concurrency |
| Image max dim | 1568 | — | Downscale threshold (vision limit) |
| Tiny-image min | 20px | `MINERU_IMG_MIN_WIDTH/HEIGHT` | 过滤噪声（故意低，保留公式截图） |

## Preprocessing (`_stage_1_3_preprocess_image`)

- **Normalize to RGB**: palette/alpha 模式转 RGB（MiniMax M3 能处理灰度，normalization 是防御性）
- **Downscale**: >1568px 缩略（VLM context 限制）
- 处理模式 L/LA/P/PA/RGB/RGBA

## VLM failure detection (`_stage_1_3_is_caption_failed`)

检测 VLM 错误响应：`解析失败`/`无法识别`/`无法描述`/`抱歉`/`sorry`/`unable to`/`cannot describe`/`I can't`/`not clear`，或文本 <15 字符。

失败批次重试 3 次后仍失败 → **`raise RuntimeError` 暂停**（不再写 `[待重试]` 占位符——2026-06-24 删除该 fallback）。已写的失败 caption（<20 字符）会在下次运行被 pending 过滤器重新处理。

## Formula transcription (LaTeX-only)

MiniMax-M3 转录公式图 ~81%。`CAPTION_SYSTEM_PROMPT` 规则：
- **公式逐符号用 LaTeX 转录**：`$x_{k+1}=Ax_k+Bu_k$`、`$\dot{T}=\frac{1}{mc_p}\dot{Q}$`
- **不用 Unicode 下标/上标/希腊字母**：写 `x_1`/`\eta`/`\alpha`，不写 `x₁`/`η`/`α`（LaTeX 可渲染可复用，Unicode 不可解析）
- 公式图字数上限放宽到 150（默认 100），避免截断长公式
- 未知符号用 `?` 占位

## Tiny-image filter (`_is_image_too_small`)

阈值 `MINERU_IMG_MIN_WIDTH/HEIGHT=20px`（故意低），只过滤真噪声（1×1/2×2 artifact）。29-70px 高的公式截图要保留——MiniMax 能转录。

## Performance

并行批量调度（ThreadPoolExecutor）。40 图（5 批）~16s（串行 ~90s，5.7×）。HardwareWiki 验证（2026-06-17）：18709 图，18701 caption（99.96% 覆盖）。

## Usage

```bash
export CAPTION_BATCH_SIZE=10    # more images per call
export CAPTION_MAX_WORKERS=8    # more parallel workers
```

直接调用补 caption：
```python
from _stage_1_extract import _stage_1_3_caption_images_batch
media_dir = Path("wiki/media/Book/Some Book - 2024 - Author")
images = [{"filename": f.name, "page": 0, "width": 0, "height": 0}
          for f in sorted(media_dir.iterdir())
          if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
captioned = _stage_1_3_caption_images_batch(images, config, media_dir, source_label="repair")
```

## Open issues

| Issue | Status | Impact |
|-------|--------|--------|
| `batch_size=6` hardcoded on minerU path（line 990）vs `CAPTION_BATCH_SIZE=8` env | Open | 6-图批次可能超 MiniMax token 限制导致 JSON 截断 |
| minerU 偶尔把公式区域分类为 `image` 而非 `equation`（LaTeX 文本） | Open | ~112 公式图被当图片送 VLM（上游 minerU 问题） |

## 修订记录

- **2026-06-24**：无回退策略——caption key 缺失/批次重试耗尽 → raise 暂停，删 `[待重试]` 占位符 fallback。精简文档，删 Path A/B（已统一）、fixed-issue 深挖（issues 1&2 已修，root cause: `content_list` 是 JSON 字符串，`json.loads` + minerU `image_caption` 写 sidecar）。
- **2026-06-23**：PyMuPDF 从 PDF 图片提取整体移除，统一 minerU；函数移到 `_stage_1_extract.py`。
- **2026-06-22**：LaTeX-only 公式转录规则；修 `_is_image_too_small` NameError（常量未定义被 try/except 吞）。
