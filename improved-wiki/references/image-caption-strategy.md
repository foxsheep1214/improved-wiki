---
description: "Image captioning — one VLM call per image with NashSU-style context-aware prompt, parallel dispatch, configurable primary + optional fallback VLM provider (~/.agents/config.json). No provider configured, or every configured provider failing consecutively, pauses the ingest."
tags: [vlm, captioning, strategy]
related: [ingest-stages-mandatory, known-issues]
---

# Image Captioning 策略 (configurable VLM provider)

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

> **无回退策略**：caption key 缺失，或**全部已配置 provider**（primary + fallback，见下）连续失败 → `raise RuntimeError`
> 暂停 ingest，不静默降级。这是对 NashSU "caption 永不中断 ingest" 哲学的**刻意 override**。
> **provider failover ≠ 违反此政策**：primary/fallback 之间的切换是两个真实 VLM 之间的转移（每次切换打一行日志，非静默），政策针对的是"完全跳过 caption、退回图片文字/空描述"这类静默降级——只要还有一个 provider 在真实产出 caption，就不算违反。详见 `ingest-stages-mandatory.md`。

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
                │     └── _stage_1_3_caption_one_image_with_failover()
                │           ├── _stage_1_3_caption_one_image() on PRIMARY (retry x3)
                │           │     ├── _stage_1_3_preprocess_image()  (grayscale→RGB, downscale, base64)
                │           │     └── _stage_1_3_build_user_prompt()  (context-aware, NashSU buildCaptionPromptWithContext)
                │           └── on primary exhaustion + fallback configured (2026-07-08):
                │                 log one line → _stage_1_3_caption_one_image() on FALLBACK (retry x3)
                │           → still failed on every configured provider: placeholder / systemic pause
                └── _stage_1_3_is_caption_failed()  (detect VLM error responses)
```

## Key parameters

| Parameter | Default | Env var | Description |
|-----------|---------|---------|-------------|
| Max workers | 4 | `CAPTION_MAX_WORKERS` | Per-image parallel concurrency. 4 stays under GLM-5v-turbo free-tier rate limit (12 trips HTTP 429). |
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

- **Normalize to RGB**: palette/alpha 模式转 RGB（VLM 能处理灰度，normalization 是防御性）
- **Downscale**: >1568px 缩略（VLM context 限制）
- 处理模式 L/LA/P/PA/RGB/RGBA

## VLM failure detection (`_stage_1_3_is_caption_failed`)

检测 VLM 错误响应：`解析失败`/`无法识别`/`无法描述`/`抱歉`/`sorry`/`unable to`/`cannot describe`/`I can't`/`not clear`，或文本 <15 字符。

- **孤立单图失败**（重试 3 次后）：写 `[待重试]` 占位符（本身是 pending，下次运行重试）。
- **连续 `CONSECUTIVE_FAIL_PAUSE=3` 次失败**：判定 VLM 主路径宕机 → `raise RuntimeError`
  暂停（不静默产出一波占位符）。**这是对 NashSU "单图失败只跳过该图"的刻意 override。**

## Formula transcription (LaTeX-only)

VLM 转录公式图成功率因模型而异。`CAPTION_SYSTEM_PROMPT` 规则：
- **公式逐符号用 LaTeX 转录**：`$x_{k+1}=Ax_k+Bu_k$`、`$\dot{T}=\frac{1}{mc_p}\dot{Q}$`
- **不用 Unicode 下标/上标/希腊字母**：写 `x_1`/`\eta`/`\alpha`，不写 `x₁`/`η`/`α`（LaTeX 可渲染可复用，Unicode 不可解析）
- 未知符号用 `?` 占位

## Tiny-image filter (`_is_image_too_small`)

阈值 `MINERU_IMG_MIN_WIDTH/HEIGHT=20px`（故意低），只过滤真噪声（1×1/2×2 artifact）。29-70px 高的公式截图要保留——VLM 能转录。

## Caption inlining into the digest (NashSU Step 0.6 parity)

`_stage_1_3_inline_captions()` 在 `_ingest_prepare.py` 中、Stage 1.3 之后、chunk pipeline
之前调用：把 minerU chunk markdown 里的 `![](images/x.jpg)` 改写成 `![<caption>](images/x.jpg)`。
对齐 NashSU `ingest.ts` Step 0.6——captioned alt text 让生成 LLM 看到图的语义，从而在正确
段落保留图引用，而不是把空-alt 图静默改写掉。幂等：alt 已非空则跳过。

## Usage

```bash
export CAPTION_MAX_WORKERS=8    # 谨慎调高：GLM 免费档 12 并发即触发 429（默认 4）
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

## Provider endpoint configuration

| endpoint | 多图支持 | auth header | 适用 |
|---|---|---|---|
| (configured via `~/.agents/config.json`) | depends on provider | depends on protocol | **caption 调用** |

配置 caption provider 时，确保 protocol 与 endpoint 匹配：
- `anthropic` → 代码拼 `{base_url}/anthropic/v1/messages`（Anthropic Messages API）
- `openai` → 代码拼 `{base_url}/v1/chat/completions`（OpenAI 兼容）

⚠️ **openai 分支的 `/v1` 陷阱**：代码硬编码 `/v1/chat/completions`。智谱 GLM 的 OpenAI 兼容端点是 `/api/paas/v4/chat/completions`（v4 无 v1），走 openai 分支会 404。**智谱 GLM 必须走 anthropic 协议**（见下），不要走 openai 分支。

## Recommended setup: GLM-5v-turbo primary + local Ollama fallback (2026-07-08)

主用智谱 `glm-5v-turbo`（远程 anthropic 端点，质量最优），配一个本地 Ollama
`qwen3-vl:8b-instruct`（openai 协议）作 fallback——`caption_provider` 耗尽自身
3 次重试后，自动切到 `caption_fallback_provider` 再试 3 次，每次切换打一行日志
（`_stage_1_3_caption_one_image_with_failover`，见上方 Architecture）。远程端点
支持真并行，但智谱 GLM-5v-turbo 免费档限流紧——默认 `CAPTION_MAX_WORKERS=4`
（12 会触发 429，见下方"限流"节）。付费/高频账号可调高；fallback（本地）命中的图
**不受此并发上限约束，而是代码强制一张一张来**（见下方"并发：本地 fallback
provider 严格串行"）。

```json
// ~/.agents/config.json
{
  "caption_provider": "glm",
  "caption_fallback_provider": "ollama",
  "providers": {
    "glm": {
      "api_key": "<智谱 sk-key，与 Claude Code 的 ANTHROPIC_AUTH_TOKEN 同一个>",
      "base_url": "https://open.bigmodel.cn/api",
      "protocol": "anthropic",
      "model": "glm-5v-turbo",
      "models": {"caption": "glm-5v-turbo", "vision": "glm-5v-turbo"}
    },
    "ollama": {
      "api_key": "ollama-local",
      "base_url": "http://127.0.0.1:11434",
      "protocol": "openai",
      "model": "qwen3-vl:8b-instruct",
      "models": {"caption": "qwen3-vl:8b-instruct", "vision": "qwen3-vl:8b-instruct"}
    }
  }
}
```

`caption_fallback_provider` 是可选键——不设就没有 fallback，行为退回 2026-07-08
之前（primary 耗尽即置 `[待重试]` 占位，round 级重试；见下方"无回退策略"节）。
fallback 是否启用只看 `base_url`+`model` 是否非空（`_stage_1_3_provider_bundles`），
**不检查 `api_key`**——所以本地无鉴权服务器可以留空 api_key；但示例仍写了个
`"ollama-local"` 占位字符串，纯为可读性（一眼看出这条是"本地免鉴权"而非"漏填"）。

`base_url` 设到 `/api` 这一层，代码拼 `/anthropic/v1/messages` → 命中
`https://open.bigmodel.cn/api/anthropic/v1/messages` ✓。`x-api-key` header 也匹配。

**模型选型教训（2026-07-06 实测）**：视觉任务必须用 `glm-5v-turbo`。
- `glm-5v-turbo` **不在** `GET /api/paas/v4/models` 的返回列表里（该列表只返回 8 个
  文本模型），只能靠直接 POST 调用探测发现——别因列表里没有就以为不存在。
- GLM-5 系列文本模型（`glm-5`/`glm-5.2`/`glm-5-turbo`/`glm-4.7` 等）虽然接受 image
  输入不报错，但**不理解图片、产生幻觉**——同一张雷达脉冲图被 glm-5-turbo 描述成
  "暗巷里的人"、glm-5.2 描述成"年轻女性"、glm-4.7 描述成"白狗"。只有 `glm-5v-turbo`
  正确识别了波束几何/距离门/CLOCK 信号。**不要拿文本模型凑数做 caption。**

key 直接写文件（`~/.agents/config.json` 权限 600、不进 git）。

⚠️ **限流：必须降并发**。智谱 GLM 端点对并发敏感，`CAPTION_MAX_WORKERS=12`（旧默认）会触发 `HTTP 429: Too Many Requests`——代码重试 3 次（1s/2s/4s 退避）全落在限流窗口内，连续 3 张失败触发 `CONSECUTIVE_FAIL_PAUSE=3` 硬停（防静默降级策略）。实测 Wehner 书 12 并发跑出 39 个 429 占位 + 78 张没跑到。**代码默认已降为 `CAPTION_MAX_WORKERS=4`**（2026-07-07），降到 4 后 Wehner 书 117 张 pending 全部成功（0 占位）。Sidecar 是 cache，重跑只处理 pending（`[待重试]` 占位 + 缺失），已成功的跳过。若 4 仍 429（限流窗口期/账号日配额耗尽）：2026-07-08 起**不必再手动改并发或切 provider**——配好 `caption_fallback_provider` 后，429 耗尽 primary 重试即自动切本地 Ollama，一行日志记录切换。仍可手动降到 1 排查（详见下节）。

## 并发：本地 fallback provider 严格串行（`_FALLBACK_SEMAPHORE`，2026-07-08）

fallback（本地 Ollama）调用**代码强制一张一张来**，与 primary 的并发无关——
`_stage_1_3_caption.py` 顶部一个进程级 `threading.Semaphore(1)`，
`_stage_1_3_caption_one_image_with_failover` 里只把非 primary 的调用包在这把信号量里。
primary 仍按 `CAPTION_MAX_WORKERS`（默认 4）并发；同一批里多张图同时 failover 到本地时，
后到的线程会在信号量上排队等，日志打一行 `waiting for the fallback provider ... — one
image at a time`，等前一张本地 caption 做完才轮到。

**为什么不像 primary 一样吃 `CAPTION_MAX_WORKERS`**：`CAPTION_MAX_WORKERS=4` 这个默认值
是为智谱云端限流设的（HTTP 429 配额），对本地 Ollama 不成立——本地约束是硬件吞吐。
Ollama 默认单模型单请求串行处理（除非服务端设了 `OLLAMA_NUM_PARALLEL` 开多槽位，本
skill 不假设），并发发多个请求本来就只会在 Ollama 自己的队列里堆着、不会真并行；
用客户端信号量显式串行化，比让 4 个线程都卡在同一个单线程本地推理队列上更干净，
也避免真发生过的空响应/思考块泄漏这类本地模型高负载下的输出质量问题。
无需手动设 `CAPTION_MAX_WORKERS=1`——那样会连 primary 的正常并发也一起拖慢，
现在是 primary/fallback 各自独立限流，互不影响。

## 历史 caption「解析失败」可重试修复

旧 ingest 里 `（图N，解析失败）` 类 caption 大多不是图片问题（A/B 验证灰度图
不被拒），而是早期 VLM 版本/旧 prompt 缺上下文所致——直接重试通常就成功。
现行管线已内置：缓存过滤检测失败 caption 并重试（`_stage_1_3_is_caption_failed`），
预处理做 RGB 归一化 + 超大图缩放。批量补录用上面 Usage 一节的直接调用方式。

## Open issues

| Issue | Status | Impact |
|-------|--------|--------|
| minerU 偶尔把公式区域分类为 `image` 而非 `equation`（LaTeX 文本） | Open | ~112 公式图被当图片送 VLM（上游 minerU 问题） |
