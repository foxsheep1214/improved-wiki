---
description: "Image captioning — one VLM call per image with NashSU-style context-aware prompt, parallel dispatch, MiniMax VLM. No fallback: missing key or consecutive batch failure pauses the ingest."
tags: [vlm, captioning, minimax, strategy]
related: [ingest-stages-mandatory, known-issues]
---

# Image Captioning 策略

Unified image captioning. Implemented as `stage_1_3_caption_images()` /
`_stage_1_3_caption_images_batch()` / `_stage_1_3_caption_one_image()` in
`scripts/_stage_1_3_caption.py`（facade re-export `stage_1_3_caption_images`；
`_stage_1_3_caption_images_batch` 等内部函数须直接从 `_stage_1_3_caption` 导入）。

两个图片来源：
- **PDF** — minerU 在 Stage 1.1 chunk 处理时由 `_stage_1_2_harvest_images()` 内联提取
- **PPTX/DOCX** — `_stage_1_2_extract_images_office()` 从 zip 内 `ppt/media`/`word/media` 取图

> **NashSU parity**：caption 调用模型与 prompt 结构直接对齐
> NashSU 的 `src/lib/vision-caption.ts`（`captionImage` 一图一调用 +
> `buildCaptionPromptWithContext`）与 `src/lib/image-caption-pipeline.ts`
> （`CONTEXT_CHARS=150`、SHA-256 缓存、markdown alt 重写）+ `ingest.ts` Step 0.6
> （提图后、生成 LLM 前 inline caption）。

> **无回退策略**：caption key 缺失或连续失败 → `raise RuntimeError`
> 暂停 ingest，不静默降级。这是对 NashSU "caption 永不中断 ingest" 哲学的**刻意 override**。
> 详见 `ingest-stages-mandatory.md`。

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
                ├── _stage_1_3_build_context_map()  (minerU content_list → before/after text)
                ├── ThreadPoolExecutor (CAPTION_MAX_WORKERS per-image parallel)
                │     └── _stage_1_3_caption_one_image(): ONE image per VLM call
                │           ├── _stage_1_3_preprocess_image()  (grayscale→RGB, downscale, base64)
                │           ├── _stage_1_3_build_user_prompt()  (context-aware, NashSU buildCaptionPromptWithContext)
                │           └── retry x3, then placeholder / systemic pause
                └── _stage_1_3_is_caption_failed()  (detect VLM error responses)
```

## Key parameters

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Max workers | 12 | `CAPTION_MAX_WORKERS` | Per-image parallel concurrency |
| Image max dim | 1568 | — | Downscale threshold (vision limit) |
| Context window | 150 chars/side | — | before/after body text fed as anchoring context (NashSU `CONTEXT_CHARS`, matched) |
| Tiny-image min | 20px | `MINERU_IMG_MIN_WIDTH/HEIGHT` | 过滤噪声（故意低，保留公式截图） |

## One image per call (NashSU parity)

每张图一次 VLM 调用、纯文本回复（对齐 `vision-caption.ts:captionImage`）。
一图一调用让每张图独占 prompt 预算，单图失败只影响自己（批量模式下一张图的 JSON 截断会污染整批）。

## Context-aware prompt (NashSU parity)

`_stage_1_3_build_user_prompt()` 是 NashSU `buildCaptionPromptWithContext(before, after)`
的 port：每张 minerU 图带上其在 content_list 中**前后最近的文本块**作为 ANCHORING CONTEXT，
加上 minerU 自己的 `image_caption`——这些**只作参考、永不直接作为最终 caption**。

这是阻止 VLM 把纯几何图塌缩成印刷 figure label 的关键。

上下文窗口 `CONTEXT_CHARS = 150`，对齐 NashSU 实证调优值：更大窗口带入无关正文、模型要主动过滤、token 成本翻 3 倍而收益微小。

## Preprocessing (`_stage_1_3_preprocess_image`)

- **Normalize to RGB**: palette/alpha 模式转 RGB（MiniMax M3 能处理灰度，normalization 是防御性）
- **Downscale**: >1568px 缩略（VLM context 限制）
- 处理模式 L/LA/P/PA/RGB/RGBA

## VLM failure detection (`_stage_1_3_is_caption_failed`)

检测 VLM 错误响应：`解析失败`/`无法识别`/`无法描述`/`抱歉`/`sorry`/`unable to`/`cannot describe`/`I can't`/`not clear`，或文本 <15 字符。

- **孤立单图失败**（重试 3 次后）：写 `[待重试]` 占位符（本身是 pending，下次运行重试）。
- **连续 `CONSECUTIVE_FAIL_PAUSE=3` 次失败**：判定 VLM 主路径宕机 → `raise RuntimeError`
  暂停（不静默产出一波占位符）。**这是对 NashSU "单图失败只跳过该图"的刻意 override。**

## Formula transcription (LaTeX-only)

MiniMax-M3 转录公式图 ~81%。`CAPTION_SYSTEM_PROMPT` 规则：
- **公式逐符号用 LaTeX 转录**：`$x_{k+1}=Ax_k+Bu_k$`、`$\dot{T}=\frac{1}{mc_p}\dot{Q}$`
- **不用 Unicode 下标/上标/希腊字母**：写 `x_1`/`\eta`/`\alpha`，不写 `x₁`/`η`/`α`（LaTeX 可渲染可复用，Unicode 不可解析）
- 未知符号用 `?` 占位

## Tiny-image filter (`_is_image_too_small`)

阈值 `MINERU_IMG_MIN_WIDTH/HEIGHT=20px`（故意低），只过滤真噪声（1×1/2×2 artifact）。29-70px 高的公式截图要保留——MiniMax 能转录。

## Caption inlining into the digest (NashSU Step 0.6 parity)

`_stage_1_3_inline_captions()` 在 `_ingest_prepare.py` 中、Stage 1.3 之后、chunk pipeline
之前调用：把 minerU chunk markdown 里的 `![](images/x.jpg)` 改写成 `![<caption>](images/x.jpg)`。
对齐 NashSU `ingest.ts` Step 0.6——captioned alt text 让生成 LLM 看到图的语义，从而在正确
段落保留图引用，而不是把空-alt 图静默改写掉。幂等：alt 已非空则跳过。

## Usage

```bash
export CAPTION_MAX_WORKERS=16    # more parallel workers
```

直接调用补 caption：
```python
from _stage_1_3_caption import _stage_1_3_caption_images_batch
media_dir = Path("wiki/media/Book/Some Book - 2024 - Author")
images = [{"filename": f.name, "page": 0, "width": 0, "height": 0}
          for f in sorted(media_dir.iterdir())
          if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
captioned = _stage_1_3_caption_images_batch(images, config, media_dir, source_label="repair")
```

## MiniMax endpoint matrix（勿混用，2026-06-11 教训）

| endpoint | 多图支持 | auth header | 适用 |
|---|---|---|---|
| `https://api.minimaxi.com/anthropic/v1/messages` | ✅ content blocks 数组（Anthropic 协议原生） | `Authorization: Bearer <key>` 或 `x-api-key: <key>`（均可） | **caption 调用（现行管线用此 endpoint，一图一调用）** |
| `https://api.minimaxi.com/v1/coding_plan/vlm` | ❌ 单图（`image_url` 必须是单字符串） | `Authorization: Bearer <key>` | mmx CLI 内部用 |

常见错配：`v1/coding_plan/vlm` + `image_url=[多图数组]` → 2013 invalid_params；
`anthropic/v1/messages` 缺 auth → 1004 "carry the API secret key"。调任何
MiniMax endpoint 前先对照本表，别在误导性错误信息上试错。

## 历史 caption「解析失败」可重试修复

旧 ingest 里 `（图N，解析失败）` 类 caption 大多不是图片问题（A/B 验证灰度图
不被拒），而是早期 VLM 版本/旧 prompt 缺上下文所致——直接重试通常就成功。
现行管线已内置：缓存过滤检测失败 caption 并重试（`_stage_1_3_is_caption_failed`），
预处理做 RGB 归一化 + 超大图缩放。批量补录用上面 Usage 一节的直接调用方式。

## Open issues

| Issue | Status | Impact |
|-------|--------|--------|
| minerU 偶尔把公式区域分类为 `image` 而非 `equation`（LaTeX 文本） | Open | ~112 公式图被当图片送 VLM（上游 minerU 问题） |
