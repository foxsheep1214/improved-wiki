"""Stage 1.3 unified image captioning (MiniMax VLM via Anthropic protocol).

Extracted from _stage_1_extract.py on 2026-06-24. Owns the parallel batch
caption dispatch, JSON truncation recovery, VLM-failure detection, image
preprocessing, and the no-API-key hard-stop (no silent fallback per the
2026-06-24 policy). Path A (PyMuPDF) and Path B (minerU) images are merged.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Shared infrastructure
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import Config  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

CAPTION_BATCH_SIZE = int(os.environ.get("CAPTION_BATCH_SIZE", "8"))
CAPTION_MAX_WORKERS = int(os.environ.get("CAPTION_MAX_WORKERS", "6"))


CAPTION_SYSTEM_PROMPT = (
    "你是硬件知识库的图像解读专家。每次给你若干张图，按图顺序逐张描述："
    "1-3 句中文，不超过 100 字。聚焦：图类型（电路/波形/框图/PCB/曲线/参数表/公式/实物/示意等）"
    "+ 关键内容 + 关键参数/标注。"
    "\n\n特殊规则——公式图：如果图是数学公式、表达式或方程，不要只笼统说「公式图」，"
    "应尽量逐符号转录公式内容，统一用 LaTeX 语法表达（如 $x_{k+1}=Ax_k+Bu_k$、"
    "$\\sum_{i=0}^{2n} W_c^{(i)}[Y^i-\\hat{y}]$、$\\dot{T}=\\frac{1}{mc_p}\\dot{Q}$），"
    "不要用 Unicode 上下标或希腊字母（写 x_1、\\eta、\\alpha、\\Sigma 而非 x₁、η、α、Σ）。"
    "转录时字数上限放宽至 150 字，避免长公式被截断。转录不确定的符号用 ? 占位。"
    "\n\n输出格式：严格按以下 JSON 数组：\n```json\n[\n  {\"idx\": 1, \"caption\": \"...\"},\n"
    "  {\"idx\": 2, \"caption\": \"...\"},\n  ...\n]\n```\n\n"
    "每个对象都要有，idx 与图顺序一致。即使图不清楚也尽量给个最合理的简短描述。"
)


# ══════════════════════════════════════════════════════════════════════════════
# VLM failure detection + no-API-key hard stop
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_3_is_caption_failed(text: str) -> bool:
    """Detect VLM failure responses that shouldn't be treated as valid captions."""
    if not text or len(text) < 15:
        return True
    failure_markers = ["解析失败", "无法识别", "unable to", "cannot describe",
                       "抱歉", "sorry", "I can't", "not clear", "无法描述"]
    text_lower = text.lower()
    return any(m.lower() in text_lower for m in failure_markers)


def _emit_caption_skip_review(config, source_label: str, media_dir: Path,
                              total_images: int, already_captioned: int) -> None:
    """Write a high-severity REVIEW item documenting that VLM captioning was
    skipped because the caption provider has no API key. (See bug 2026-06-24:
    an Erickson re-ingest produced 773 OCR captions but 0 VLM captions with
    no warning.) Does not itself raise — call _caption_no_key_pause() for the
    full warn + REVIEW + pause behavior."""
    import time
    date_str = time.strftime("%Y-%m-%d")
    safe_source = re.sub(r'[^\w\s-]', '', source_label or media_dir.parent.name).strip()[:40]
    if not safe_source:
        safe_source = "unknown"
    reviews_dir = config.wiki_dir / "REVIEW" / "suggestion"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{date_str}-{safe_source}-vlm-captioning-skipped-no-api-key.md"
    page_path = reviews_dir / filename
    pending = max(0, total_images - already_captioned)
    md = f"""---
type: review
review_type: suggestion
severity: high
affected_pages: []
resolved: false
created: {date_str}
source_ingest: "{source_label or media_dir.parent.name}"
---

# [suggestion] VLM image captioning skipped — no caption provider API key

Stage 1.3 (MiniMax VLM captioning) was **entirely skipped** because
`caption_api_key` is empty: `~/.agents/config.json` is absent and neither
`CAPTION_API_KEY` nor `LLM_API_KEY` is set in the environment.

**Impact:** {total_images} image(s) were NOT captioned by the VLM.
{already_captioned} already had a caption (minerU OCR figure-text or prior
run); **{pending} have no VLM description** — they fall back to minerU's OCR
figure-caption text (if any) or remain uncaptioned. Image search/retrieval
quality is degraded.

**Fix:** configure the MiniMax caption provider — create `~/.agents/config.json`
with a `providers.minimax` entry (`api_key` + `base_url`), or
`export CAPTION_API_KEY=...`, then re-run ingest. Stage 1.3 resumes from
cache and only captions pending images.

## Resolution
_配置 caption provider API key 后重跑 ingest 即可补齐；处理完成后将 `resolved: false` 改为 `resolved: true`。_
"""
    tmp = page_path.with_suffix(page_path.suffix + ".tmp")
    tmp.write_text(md, encoding="utf-8")
    tmp.rename(page_path)


def _caption_no_key_pause(config, source_label: str, media_dir: Path,
                          total_images: int, already_captioned: int) -> None:
    """Handle a missing caption-provider API key: warn loudly, write a REVIEW
    item, and **PAUSE the ingest** (raise RuntimeError). No fallback, no opt-out.

    Policy (2026-06-24): the ingest process allows NO silent fallback. A
    missing required external dependency is a hard stop — the main path (VLM
    captioning) cannot run, so the pipeline pauses rather than silently
    producing degraded output (OCR figure-text in place of VLM descriptions).
    Extraction work is cached, so re-running after configuring the key resumes
    from Stage 1.3 with no re-extraction.
    """
    pending = max(0, total_images - already_captioned)
    print(f"\n⚠️  [caption] VLM SKIPPED — no API key for caption provider. "
          f"{already_captioned}/{total_images} images have prior captions, "
          f"{pending} will get NO VLM description.")
    print(f"⚠️  [caption] PAUSING ingest — no silent fallback. Configure "
          f"~/.agents/config.json (providers.minimax.api_key) or export "
          f"CAPTION_API_KEY, then re-run (cached, resumes here).\n")
    _emit_caption_skip_review(config, source_label, media_dir, total_images, already_captioned)
    raise RuntimeError(
        "Caption provider API key missing — VLM captioning (Stage 1.3) cannot run. "
        "No fallback: configure ~/.agents/config.json (providers.minimax.api_key) or "
        "export CAPTION_API_KEY, then re-run (extraction is cached, resumes from "
        "Stage 1.3)."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Image preprocessing + per-batch VLM call
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_3_preprocess_image(img_path: Path, max_dim: int = 1568) -> str:
    """Load image, normalize to RGB, downscale if > max_dim, return base64.

    Normalizes palette/alpha modes to plain RGB for consistent base64 encoding.
    Downscales oversized images to stay within VLM context limits.
    """
    import io, base64
    from PIL import Image
    im = Image.open(img_path)
    w, h = im.size

    # Normalize to RGB (harmless: caption model handles grayscale fine; this
    # just ensures consistent encoding across PDF extraction variants)
    if im.mode in ('L', 'LA', 'P', 'PA'):
        im = im.convert('RGB')

    # Downscale oversized images (VLM context window limits)
    if w > max_dim or h > max_dim:
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buf = io.BytesIO()
    fmt = 'JPEG' if img_path.suffix.lower() in ('.jpg', '.jpeg') else 'PNG'
    im.save(buf, format=fmt, quality=85)
    im.close()
    return base64.standard_b64encode(buf.getvalue()).decode()


def _stage_1_3_caption_one_batch(batch: list[dict], batch_idx: int, total_batches: int,
                       config: Config, media_dir: Path) -> tuple[str | None, str | None]:
    """Call caption provider multi-image API for one batch. Returns (text, error).

    Handles both Path A (filename + page/width/height in media_dir) and
    Path B (absolute path in img['path']) images transparently."""
    import urllib.request, urllib.error, base64

    # Build descriptive preamble
    first = batch[0]
    if first.get("page") is not None:
        last_page = batch[-1].get("page", first["page"])
        preamble = (f"这是第 {batch_idx+1}/{total_batches} 批（页 {first['page']}-{last_page}），"
                    f"请按顺序描述每张图：\n\n")
    else:
        preamble = (f"这是第 {batch_idx+1}/{total_batches} 批扫描版文档中提取的技术图表，"
                    f"请按顺序描述每张图：\n\n")

    content: list[dict] = [{"type": "text", "text": preamble}]
    for i, img in enumerate(batch):
        # Resolve image path — Path B uses absolute path, Path A uses media_dir + filename
        if "path" in img:
            img_path = Path(img["path"])
        else:
            img_path = media_dir / img["filename"]
        if not img_path.exists():
            continue
        # Preprocess: normalize to RGB + downscale oversized images
        img_data = _stage_1_3_preprocess_image(img_path)
        ext = img_path.suffix.lstrip(".").lower()
        media_type = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"

        # Annotation: page and size if available (Path A), or index only (Path B)
        if img.get("page") is not None:
            content.append({"type": "text",
                "text": f"[图{i+1}] p{img['page']}, {img.get('width','?')}x{img.get('height','?')}\n"})
        else:
            content.append({"type": "text", "text": f"[图{i+1}]\n"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}})
        if img.get("page") is not None:
            content.append({"type": "text", "text": f"[/图{i+1}]\n"})

    url = f"{config.caption_base_url.rstrip('/')}/anthropic/v1/messages"
    body = json.dumps({
        "model": config.caption_model,
        "max_tokens": 8192,
        "system": CAPTION_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
    }).encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "x-api-key": config.caption_api_key,
                "anthropic-version": "2023-06-01",
            })
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
            text = "".join(c["text"] for c in data.get("content", []) if c.get("type") == "text")
            return text.strip(), None
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            # No fallback: a caption-batch failure after retries means the VLM
            # main path is not working — pause rather than write placeholder
            # captions that silently degrade quality (policy 2026-06-24).
            print(f"\n⚠️  [caption] VLM batch failed after {attempt+1} attempts "
                  f"({type(e).__name__}: {e}) — PAUSING, no fallback.")
            raise RuntimeError(
                f"Caption VLM batch failed after {attempt+1} attempts "
                f"({type(e).__name__}: {e}). No fallback — the main captioning "
                f"path is not working. Fix the provider and re-run (cached, "
                f"resumes from Stage 1.3)."
            ) from e
    return None, "max-retries"



# ══════════════════════════════════════════════════════════════════════════════
# Batch dispatch
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_3_caption_images_batch(images: list[dict], config: Config, media_dir: Path,
                    source_label: str = "",
                    batch_size: int = CAPTION_BATCH_SIZE,
                    max_workers: int = CAPTION_MAX_WORKERS) -> int:
    """Unified image captioning for both Path A (PyMuPDF) and Path B (minerU).

    Images dict can come from either path:
      - Path A: {"filename": "...", "page": N, "width": W, "height": H}
        Image files are at media_dir / filename.
      - Path B: {"filename": "...", "path": "/abs/path/to/img.jpg"}
        Image files are at the absolute path.

    Batches are processed in PARALLEL via ThreadPoolExecutor to minimize
    total wall-clock time. Each batch sends multi-image API request to
    the caption provider (via Anthropic protocol).

    Saves one .caption.txt per image."""
    # Late import: log_event lives in the scanned module (_stage_1_1_scanned)
    # which imports this module at top level. Late import avoids a load-time
    # circular dependency. log_event no-ops when _log_file is None (Stage 1.3
    # entry via stage_1_3_caption_images outside the OCR path).
    from _stage_1_1_scanned import log_event

    if not images:
        return 0
    if not config.caption_api_key:
        # VLM main path cannot run — pause (no fallback, no silent degradation).
        already = sum(1 for img in images
                      if (media_dir / (img["filename"] + ".caption.txt")).exists())
        _caption_no_key_pause(config, source_label, media_dir, len(images), already)
        return 0  # unreachable — _caption_no_key_pause always raises

    # Filter to pending (uncaptioned or VLM-failed) images
    pending = []
    for img in images:
        cap_path = media_dir / (img["filename"] + ".caption.txt")
        if not cap_path.exists():
            pending.append(img)
        elif cap_path.stat().st_size < 20:
            pending.append(img)
        else:
            # Re-check: existing caption might be a VLM failure from previous run
            try:
                existing = cap_path.read_text(encoding="utf-8").strip()
                if _stage_1_3_is_caption_failed(existing):
                    pending.append(img)
            except Exception:
                pending.append(img)
    if not pending:
        label = f" [{source_label}]" if source_label else ""
        print(f"[caption]{label} (cached) All {len(images)} images already captioned")
        return 0

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    label = f" [{source_label}]" if source_label else ""
    print(f"[caption]{label} {len(pending)}/{len(images)} pending images "
          f"→ {len(batches)} batches (parallel, max {max_workers} workers)")

    # Parallel dispatch: all batches submitted at once, results collected as they complete
    from concurrent.futures import as_completed

    captioned = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
        future_to_batch = {
            executor.submit(_stage_1_3_caption_one_batch, b, i, len(batches), config, media_dir): i
            for i, b in enumerate(batches)
        }
        for future in as_completed(future_to_batch):
            bi = future_to_batch[future]
            batch = batches[bi]
            try:
                text, err = future.result()
            except Exception as e:
                print(f"  batch {bi+1}: unhandled {type(e).__name__}: {e}")
                continue
            if err:
                print(f"  batch {bi+1}: {err}")
                continue
            # Parse JSON array from LLM response (with truncation recovery)
            if text.startswith("```"):
                text = text.split("```", 2)[1]
                if text.startswith("json"):
                    text = text[4:]
                if text.endswith("```"):
                    text = text[:-3]
            text = text.strip()
            text_len = len(text)
            try:
                captions = json.loads(text)
            except json.JSONDecodeError:
                import re
                # Recovery 1: salvage complete {"idx": N, "caption": "..."} objects
                salvaged = re.findall(
                    r'\{\s*"idx"\s*:\s*(\d+)\s*,\s*"caption"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
                    text
                )
                if salvaged:
                    captions = [{"idx": int(idx), "caption": cap} for idx, cap in salvaged]
                    print(f"  batch {bi+1}: JSON truncated ({text_len} chars) — salvaged {len(captions)}/{len(batch)} captions")
                    log_event("json_truncation", batch=bi+1, response_len=text_len,
                             recovered=len(captions), total=len(batch))
                else:
                    # Recovery 2: single caption truncated mid-string (no closing quote)
                    m = re.search(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)$', text)
                    if m and text.count('"idx"') == 1:
                        cap_text = m.group(1).rstrip('，、, \t')
                        if len(cap_text) >= 15:
                            captions = [{"idx": 1, "caption": cap_text}]
                            print(f"  batch {bi+1}: JSON truncated mid-caption ({text_len} chars) — salvaged 1/{len(batch)} caption")
                        else:
                            print(f"  batch {bi+1}: JSON parse failed ({text_len} chars), unable to recover")
                            continue
                    else:
                        print(f"  batch {bi+1}: JSON parse failed ({text_len} chars), text[:100]: {text[:100]}")
                        continue
            for cap in captions:
                idx = cap.get("idx", 0) - 1
                if 0 <= idx < len(batch):
                    caption_text = cap.get("caption", "").strip()
                    # VLM failure detection: if the LLM returns "解析失败" or similar,
                    # write a retry-able fallback instead of a useless permanent caption
                    if _stage_1_3_is_caption_failed(caption_text):
                        caption_text = f"[待重试] 图片 {batch[idx]['filename']}，尺寸 {batch[idx].get('width','?')}×{batch[idx].get('height','?')}"
                    cap_path = media_dir / (batch[idx]["filename"] + ".caption.txt")
                    cap_path.write_text(caption_text, encoding="utf-8")
                    captioned += 1
            print(f"  [{bi+1}/{len(batches)}] {len(captions)}/{len(batch)} captions ✓")

    print(f"[caption] Done — {captioned} captions written")
    return captioned


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1.3 entry point
# ══════════════════════════════════════════════════════════════════════════════

def stage_1_3_caption_images(config: Config, stage_1_2_result: dict, batch_size: int = CAPTION_BATCH_SIZE) -> dict:
    """Caption extracted images using unified caption pipeline (Path A + Path B merged).

    Thin wrapper around _stage_1_3_caption_images_batch() for backward compatibility with the
    Stage 1.3 pipeline checkpoint. Internal implementation delegates to the
    unified function which supports both PyMuPDF-extracted images (Path A)
    and minerU-extracted images (Path B), with parallel batch dispatch."""
    images = stage_1_2_result.get("images", [])
    if not images:
        print("[stage 1.3] No images to caption — skipping")
        return {"captioned": 0, "total": 0}
    if not config.caption_api_key:
        # VLM main path cannot run — pause (no fallback, no silent degradation).
        media_dir = Path(stage_1_2_result.get("media_dir", "."))
        already = sum(1 for img in images
                      if (media_dir / (img["filename"] + ".caption.txt")).exists())
        _caption_no_key_pause(config, "stage-1.3", media_dir, len(images), already)
        return {"captioned": 0, "total": len(images), "skipped": True, "reason": "no-api-key"}

    media_dir = Path(stage_1_2_result["media_dir"])
    captioned = _stage_1_3_caption_images_batch(images, config, media_dir,
                                source_label="stage-1.3",
                                batch_size=batch_size)
    return {"captioned": captioned, "total": len(images)}
