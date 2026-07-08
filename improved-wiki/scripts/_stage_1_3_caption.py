"""Stage 1.3 unified image captioning (configurable VLM provider, with failover).

Extracted from _stage_1_extract.py on 2026-06-24. Owns the per-image caption
dispatch, VLM-failure detection, image preprocessing, the no-API-key hard-stop
(no silent fallback per the 2026-06-24 policy), and the NashSU-style
context-aware prompt (one image per call, 2026-06-24 port).

Provider is configured via ~/.agents/config.json caption_provider entry
(primary) + an optional caption_fallback_provider entry. Supported protocols:
anthropic (Anthropic Messages API), openai (OpenAI chat/completions compatible
— e.g. Ollama / local models).

Failover (2026-07-08): when a fallback provider is configured, each image
tries the primary first (its normal 3-attempt retry); only on primary
exhaustion does it try the fallback (also 3 attempts). This is NOT the
"no-silent-fallback" degradation the rest of the policy forbids — both
providers are real VLM captioning, so using the fallback is loud (one log
line per failover) rather than silent, and the final boundary is unchanged:
if BOTH are exhausted, Stage 1.3 still pauses (raise). Recommended pairing:
primary GLM-5v-turbo (cloud, higher quality), fallback a local Ollama model
(qwen3-vl:8b-instruct) for when the cloud path is rate-limited or down.

Design (NashSU parity, 2026-06-24):
  - One image per LLM call (was: 8-image batches). Each figure gets the full
    prompt budget and a plain-text reply, so a single figure can't be
    dropped by JSON truncation and a single VLM lapse can't corrupt a batch.
  - Context-aware: each minerU figure is captioned with its surrounding
    document text (text blocks immediately before/after it in the content
    list) plus minerU's own image_caption, passed as ANCHORING CONTEXT —
    never as the final caption. This is the direct port of NashSU's
    buildCaptionPromptWithContext(before, after). It is what stops the VLM
    from collapsing to the printed figure label on bare geometric figures
    (bug 2026-06-24: Figure 2.14 / 3.6 captions were minerU's own
    image_caption written as a sidecar and then skipped — see
    _stage_1_2_images.py, which no longer writes that sidecar).
  - temperature 0 for deterministic, cache-friendly captions.
  - Parallel across images (ThreadPoolExecutor), I/O-bound only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Shared infrastructure
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import Config  # noqa: E402
from _paths import atomic_write  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Default 4 parallel VLM calls — captioning is pure I/O-bound (one HTTP call
# per image), so threads give real speedup. 12 (the former default) overruns the
# GLM-5v-turbo free-tier rate limit and trips HTTP 429 after ~3 images; 4 stays
# under it while still parallelizing. Override per run with the
# CAPTION_MAX_WORKERS env var. This cap applies to the PRIMARY provider only —
# see _FALLBACK_SEMAPHORE below for why the fallback provider is not scaled by it.
CAPTION_MAX_WORKERS = int(os.environ.get("CAPTION_MAX_WORKERS", "4"))

# The fallback provider (2026-07-08 failover, typically a local single-instance
# server like Ollama) gets exactly ONE concurrent call, independent of
# CAPTION_MAX_WORKERS: unlike GLM's cloud endpoint, a local model has no real
# parallel capacity — Ollama serializes inference per model unless the server
# is explicitly configured with OLLAMA_NUM_PARALLEL, which this skill does not
# assume. Sending it 4 concurrent requests wouldn't error, but would only
# queue at the server (see image-caption-strategy.md "并发：本地 fallback
# provider 需要单独限流吗"). Serializing client-side avoids piling up threads
# that are all just waiting on the same single-threaded local inference queue.
_FALLBACK_SEMAPHORE = threading.Semaphore(1)

# How many chars of before/after body text to pass as anchoring context.
# NashSU parity (image-caption-pipeline.ts CONTEXT_CHARS): NashSU tuned this
# DOWN from 500 → 150 because larger windows dragged in unrelated body text
# the model had to filter out and tripled input-token cost for tiny upside;
# 150 chars/side covers the figure-caption sweet spot (a figure label + the
# referring sentence) while staying cheap. We match 150 exactly.
CONTEXT_CHARS = 150
# A per-image call (all configured providers already tried, see
# _stage_1_3_caption_one_image_with_failover) is declared systemically failed
# after this many consecutive failures — at that point every configured VLM
# path is assumed down and we pause (no silent fallback to a non-caption
# path). Isolated single failures get a retryable placeholder.
CONSECUTIVE_FAIL_PAUSE = 3


CAPTION_SYSTEM_PROMPT = (
    "You are an image-interpretation expert for a knowledge base (any domain: "
    "hardware / radar / natural sciences / etc.). Be factual and do not speculate: "
    "describe only what is actually visible in the image and what the provided "
    "context explicitly states; do not invent details not present in the image."
    "\n\nLanguage (NashSU parity — language-NEUTRAL): describe the image in the "
    "SAME language as the surrounding source text (an English source → English "
    "caption; a Chinese source → Chinese caption). Capture any text printed "
    "inside the image VERBATIM in its original language — do NOT translate it. "
    "Keep technical terms, axis labels, and unit symbols in their original form."
    "\n\nFocus on: 1) image type (circuit / waveform / block-diagram / PCB / plot / "
    "parameter-table / formula / photo / schematic / geometry, etc.); 2) key "
    "content and structure (geometric relations, connection paths, module "
    "composition, axes, labeled symbols, data trends, etc.); 3) key parameters "
    "and annotations (capture visible text, axis values, and labels verbatim)."
    "\n\n⚠️ Figure-label handling (CRITICAL): the provided context/figure-caption "
    "may contain the source's figure number and label (e.g. \"Figure 2.14 A "
    "backward-tilted antenna geometry.\"). Use these ONLY to understand what the "
    "figure depicts — do NOT output the label verbatim as the caption, and do not "
    "include figure numbers (\"Figure N\" / \"图N\" / \"Fig.\") in the caption. "
    "Describe what is drawn in your own words."
    "\n\nFormulas: transcribe as LaTeX ($inline$ / $$display$$), not Unicode "
    "subscripts or Greek letters (write x_1, \\eta, \\alpha — not x₁, η, α)."
    "\n\n⚠️ IMPORTANT: ALL mathematical symbols, parameters, numbers, and "
    "expressions in your caption MUST be wrapped in LaTeX ($...$). For "
    "example: write $T=30$, $B=4$, $f_0=0$, $t=0$, $-20$ — NOT T=30, f₀, "
    "t=0, -20. This is mandatory."
    "\n\nOutput format: plain text, 2-4 sentences, no markdown, no preamble, no numbering."
)


# ══════════════════════════════════════════════════════════════════════════════
# VLM failure detection + no-API-key hard stop
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_3_is_caption_failed(text: str) -> bool:
    """Detect VLM failure responses that shouldn't be treated as valid captions."""
    if not text or len(text) < 15:
        return True
    # The "[待重试]" placeholder itself (written on retry-exhaustion — see
    # _stage_1_3_caption_images_batch) must always be re-detected as pending.
    # Bug 2026-07-06: it wasn't, because its trailing {err} text (e.g.
    # "TimeoutError: timed out") rarely matches the markers below, so
    # _stage_1_3_pending_images silently treated the placeholder as a
    # permanent cached caption and never retried it.
    if text.startswith("[待重试]"):
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

Stage 1.3 (VLM image captioning) was **entirely skipped** because
`caption_api_key` is empty: `~/.agents/config.json` has no `caption_provider`
entry configured.

**Impact:** {total_images} image(s) were NOT captioned by the VLM.
{already_captioned} already had a caption (prior run); **{pending} have no
VLM description** and remain uncaptioned. Image search/retrieval quality is
degraded.

**Fix:** configure a caption provider — create `~/.agents/config.json`
with a `caption_provider` field and a matching `providers.<name>` entry
(`api_key` + `base_url` + `protocol` + `model`), then re-run ingest.
Stage 1.3 resumes from cache and only captions pending images.

## Resolution
_配置 caption provider API key 后重跑 ingest 即可补齐；处理完成后将 `resolved: false` 改为 `resolved: true`。_
"""
    atomic_write(page_path, md)


def _caption_no_key_pause(config, source_label: str, media_dir: Path,
                          total_images: int, already_captioned: int) -> None:
    """Handle a missing caption-provider API key: warn loudly, write a REVIEW
    item, and **PAUSE the ingest** (raise RuntimeError). No fallback, no opt-out.

    Policy (2026-06-24): the ingest process allows NO silent fallback. A
    missing required external dependency is a hard stop — the main path (VLM
    captioning) cannot run, so the pipeline pauses rather than silently
    producing degraded output. Extraction work is cached, so re-running after
    configuring the key resumes from Stage 1.3 with no re-extraction.
    """
    pending = max(0, total_images - already_captioned)
    print(f"\n⚠️  [caption] VLM SKIPPED — no API key for caption provider. "
          f"{already_captioned}/{total_images} images have prior captions, "
          f"{pending} will get NO VLM description.")
    print(f"⚠️  [caption] PAUSING ingest — no silent fallback. Configure "
          f"~/.agents/config.json (caption_provider + providers.<name>.api_key), "
          f"then re-run (cached, resumes here).\n")
    _emit_caption_skip_review(config, source_label, media_dir, total_images, already_captioned)
    raise RuntimeError(
        "Caption provider API key missing — VLM captioning (Stage 1.3) cannot run. "
        "No fallback: configure ~/.agents/config.json with a caption_provider entry, "
        "then re-run (extraction is cached, resumes from Stage 1.3)."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Image preprocessing
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

    if im.mode in ('L', 'LA', 'P', 'PA'):
        im = im.convert('RGB')

    if w > max_dim or h > max_dim:
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buf = io.BytesIO()
    fmt = 'JPEG' if img_path.suffix.lower() in ('.jpg', '.jpeg') else 'PNG'
    im.save(buf, format=fmt, quality=85)
    im.close()
    return base64.standard_b64encode(buf.getvalue()).decode()


# ══════════════════════════════════════════════════════════════════════════════
# Context map — NashSU-style before/after text, sourced from minerU content_list
# ══════════════════════════════════════════════════════════════════════════════

# Module-level cache so the two call sites (per-chunk OCR dispatch + final
# Stage 1.3) don't rescan the content_list files. Keyed by the mineru-api-out
# root path. Invalidated only by process restart (a single ingest run does not
# change the content_list files mid-run).
_CONTEXT_MAP_CACHE: dict[str, dict[str, dict]] = {}

# Block types that carry describable body text for context anchoring.
_TEXT_BLOCK_TYPES = ("text", "header", "ref_text", "page_number")


def _stage_1_3_build_context_map(config: Config) -> dict[str, dict]:
    """Scan persisted minerU content_list files and build {md5_8: context}.

    For each image/chart block, captures:
      - mineru_caption: minerU's own image_caption (the book's figure label),
        joined into one string. Used as ANCHORING CONTEXT only, never as the
        final caption.
      - context_before / context_after: text from the immediately preceding
        and following text-bearing blocks (NashSU's before/after mechanism).

    The original image bytes (at the block's img_path, relative to the
    content_list file) are md5-hashed to match against the saved
    `p{page}-mineru_{md5_8}.{ext}` files in wiki/media/. This matching is
    robust because harvest saves the raw minerU bytes verbatim.

    Returns {} when no mineru-api-out exists (Path A / PyMuPDF images have no
    content_list — those images are captioned with the no-context prompt).
    """
    api_out = config.runtime_dir / "mineru-api-out"
    cache_key = str(api_out)
    if cache_key in _CONTEXT_MAP_CACHE:
        return _CONTEXT_MAP_CACHE[cache_key]
    if not api_out.exists():
        _CONTEXT_MAP_CACHE[cache_key] = {}
        return _CONTEXT_MAP_CACHE[cache_key]

    import hashlib
    ctx_map: dict[str, dict] = {}
    for cl_path in sorted(api_out.glob("*/chunk/hybrid_auto/chunk_content_list.json")):
        try:
            blocks = json.loads(cl_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(blocks, list):
            continue
        for i, b in enumerate(blocks):
            if b.get("type") not in ("image", "chart"):
                continue
            img_path = b.get("img_path", "")
            if not img_path:
                continue
            img_file = cl_path.parent / img_path
            if not img_file.exists():
                continue
            try:
                md5_8 = hashlib.md5(img_file.read_bytes()).hexdigest()[:8]
            except Exception:
                continue

            # chart blocks carry their printed label in chart_caption (not
            # image_caption); read both so a chart's figure label still anchors
            # the caption.
            caps = b.get("image_caption", []) or b.get("chart_caption", [])
            mineru_caption = " ".join(c.strip() for c in caps if c and c.strip())

            before = _collect_block_text(blocks, i, -1, CONTEXT_CHARS)
            after = _collect_block_text(blocks, i, +1, CONTEXT_CHARS)
            ctx_map[md5_8] = {
                "mineru_caption": mineru_caption,
                "context_before": before,
                "context_after": after,
            }
    _CONTEXT_MAP_CACHE[cache_key] = ctx_map
    return ctx_map


def _collect_block_text(blocks: list, start: int, step: int, max_chars: int) -> str:
    """Walk from `start` in direction `step` (+1/-1) collecting text from
    text-bearing blocks until `max_chars` is reached or a non-text block
    (image/chart/table/equation) interrupts. Returns the joined text."""
    chunks: list[str] = []
    total = 0
    i = start + step
    while 0 <= i < len(blocks):
        b = blocks[i]
        if b.get("type") not in _TEXT_BLOCK_TYPES:
            break
        txt = (b.get("text") or "").strip()
        if txt:
            chunks.append(txt)
            total += len(txt)
            if total >= max_chars:
                break
        i += step
    text = " ".join(chunks).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def _stage_1_3_md5_8(img_path: Path) -> str:
    """md5[:8] of an image file's bytes — the key into the context map."""
    import hashlib
    return hashlib.md5(img_path.read_bytes()).hexdigest()[:8]


# ══════════════════════════════════════════════════════════════════════════════
# Caption inlining — feed captions to the generation LLM as alt text
# (NashSU parity: ingest.ts Step 0.6 rewrites ![](url) → ![caption](url) so
# the summarizer sees figure semantics instead of empty-alt image refs that
# get silently paraphrased away).
# ══════════════════════════════════════════════════════════════════════════════

# minerU embeds figures in the chunk markdown as ![](images/<sha256>.jpg).
# The saved + captioned file is wiki/media/<slug>/p<page>-mineru_<md5_8>.jpg
# with a sibling .caption.txt. This regex matches the minerU image ref.
_MINERU_IMG_REF_RE = re.compile(r'!\[[^\]]*\]\(images/([^)]+)\)')

# Module cache: {media_dir_key: {mineru_basename: caption}}.
_CAPTION_BY_BASENAME_CACHE: dict[str, dict[str, str]] = {}


def _stage_1_3_build_caption_by_basename_map(config: Config, media_dir: Path) -> dict[str, str]:
    """Map minerU image basename (e.g. ``<sha256>.jpg``) → VLM caption text.

    Scans persisted minerU content_list files; for each image/chart block,
    reads the original image bytes (at the block's img_path), md5-hashes them
    to find the saved ``p<page>-mineru_<md5_8>.jpg`` in ``media_dir``, and
    reads its ``.caption.txt`` sidecar. The minerU basename is the key because
    that is what appears in the chunk markdown's ``![](images/<basename>)``.

    Returns ``{}`` when there is no mineru-api-out (Path A / PyMuPDF images —
    no chunk markdown image refs to inline).
    """
    cache_key = str(media_dir)
    if cache_key in _CAPTION_BY_BASENAME_CACHE:
        return _CAPTION_BY_BASENAME_CACHE[cache_key]

    import hashlib
    api_out = config.runtime_dir / "mineru-api-out"
    out: dict[str, str] = {}
    if not api_out.exists():
        _CAPTION_BY_BASENAME_CACHE[cache_key] = out
        return out

    # Pre-index saved files by md5_8 → caption sidecar text (one glob pass).
    saved_by_md5: dict[str, str] = {}
    pat = re.compile(r"mineru_([0-9a-f]{8})\.")
    for f in media_dir.glob("p*-mineru_*.*"):
        if f.name.endswith(".caption.txt"):
            continue
        m = pat.search(f.name)
        if not m:
            continue
        cap_path = media_dir / (f.name + ".caption.txt")
        if cap_path.exists() and cap_path.stat().st_size >= 20:
            try:
                cap = cap_path.read_text(encoding="utf-8").strip()
            except Exception:
                cap = ""
            if cap and not _stage_1_3_is_caption_failed(cap):
                saved_by_md5[m.group(1)] = cap

    for cl_path in sorted(api_out.glob("*/chunk/hybrid_auto/chunk_content_list.json")):
        try:
            blocks = json.loads(cl_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if b.get("type") not in ("image", "chart"):
                continue
            ip = b.get("img_path", "")
            if not ip:
                continue
            img_file = cl_path.parent / ip
            if not img_file.exists():
                continue
            basename = os.path.basename(ip)
            if basename in out:
                continue
            try:
                md5_8 = hashlib.md5(img_file.read_bytes()).hexdigest()[:8]
            except Exception:
                continue
            cap = saved_by_md5.get(md5_8)
            if cap:
                out[basename] = cap

    _CAPTION_BY_BASENAME_CACHE[cache_key] = out
    return out


def _stage_1_3_sanitize_alt(caption: str) -> str:
    """Sanitize a caption for safe inclusion as markdown alt text.

    Alt text is delimited by `[...]`; a literal `]` or a newline would
    terminate the image ref early and corrupt the surrounding markdown.
    NashSU applies the same sanitization (vision-caption.ts / ingest.ts)."""
    alt = caption.replace("\r", " ").replace("\n", " ")
    alt = alt.replace("]", "")  # would close the alt block early
    alt = re.sub(r"\s+", " ", alt).strip()
    return alt


def _stage_1_3_inline_captions(text: str, config: Config, media_dir: Path) -> str:
    """Rewrite minerU image refs in ``text`` to carry their VLM caption as alt
    text: ``![](images/x.jpg)`` → ``![<caption>](images/x.jpg)``.

    NashSU parity (ingest.ts Step 0.6): captioned alt text gives the
    generation LLM enough semantic load to preserve figure references inline
    at the right paragraph, instead of silently paraphrasing empty-alt images
    away. Call this AFTER Stage 1.3 (captions exist) and BEFORE Stage 2.2/2.4
    (chunk analysis / page generation).

    Idempotent: refs whose alt text is already non-empty are left untouched,
    so re-running on already-inlined text is a no-op. Unmatched refs (no
    caption sidecar) are left as-is (empty alt) rather than dropped.
    """
    cap_map = _stage_1_3_build_caption_by_basename_map(config, media_dir)
    if not cap_map:
        return text

    def _repl(m: re.Match) -> str:
        basename = m.group(1)
        # Skip if alt text already populated (idempotency).
        full = m.group(0)
        alt_match = re.match(r'!\[([^\]]*)\]\(', full)
        if alt_match and alt_match.group(1).strip():
            return full
        cap = cap_map.get(basename)
        if not cap:
            return full
        return f'![{_stage_1_3_sanitize_alt(cap)}](images/{basename})'

    return _MINERU_IMG_REF_RE.sub(_repl, text)


def _stage_1_3_build_user_prompt(img: dict, ctx: dict | None) -> str:
    """Build the per-image user prompt. Context-aware when surrounding text or
    a minerU figure caption is available; otherwise the no-context fallback.

    Mirrors NashSU's buildCaptionPromptWithContext framing: the surrounding
    text MAY identify the figure (and should be anchored to) or MAY be
    unrelated body text (in which case the model describes what it sees)."""
    page = img.get("page")
    page_hint = f" (source page {page})" if page is not None else ""

    has_ctx = bool(ctx and (ctx.get("context_before") or ctx.get("context_after")
                            or ctx.get("mineru_caption")))
    if not has_ctx:
        return (
            f"Describe this image factually{page_hint} for a knowledge-base index. "
            "Include: any visible text verbatim (in its original language — do not "
            "translate), chart axes and values, diagram structure (boxes/arrows/labels), "
            "key visual elements. Describe in the language of the surrounding source "
            "text. Do not speculate. 2 to 4 sentences, plain text, no markdown."
        )

    before = (ctx or {}).get("context_before", "")
    after = (ctx or {}).get("context_after", "")
    mineru_cap = (ctx or {}).get("mineru_caption", "")
    parts = [
        f"This image{page_hint} is embedded in a technical document. Below is the "
        "text that appears IMMEDIATELY BEFORE and AFTER it in the source. This "
        "surrounding text MAY identify/label the image (e.g. a figure caption, or "
        "body text explaining what the figure shows) — if so, anchor your caption "
        "to it. It MAY ALSO be unrelated body text that merely happens to flank the "
        "image — if so, ignore it and describe what you see.",
    ]
    if mineru_cap:
        parts.append(f"[Figure caption (reference only — do NOT copy)] {mineru_cap}")
    parts.append(f"[Text before image]\n{before or '(none)'}")
    parts.append(f"[Text after image]\n{after or '(none)'}")
    parts.append(
        "Now describe this image factually for a knowledge-base index. Include: any "
        "visible text verbatim (original language, do not translate), chart axes and "
        "values, diagram/geometric structure (boxes/arrows/connections/labels), key "
        "visual elements. If the surrounding text/caption explains what the figure "
        "shows, use it to convey the figure's meaning — but do NOT output the figure "
        "label or figure number as the caption. Do not invent details not visible in "
        "the image. Describe in the language of the surrounding source text. "
        "2 to 4 sentences, plain text, no markdown."
    )
    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Per-image VLM call (one image, one call, plain-text reply)
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_3_caption_one_image(img: dict, provider: dict, media_dir: Path,
                                 ctx_map: dict[str, dict]) -> tuple[str | None, str | None]:
    """Caption a single image against ONE provider with one VLM call.

    ``provider`` is a flat bundle (api_key/base_url/model/protocol) — see
    ``_stage_1_3_provider_bundles``. Callers needing failover between a
    primary and fallback provider use ``_stage_1_3_caption_one_image_with_failover``.

    On a transient API failure, retries up to 3 times. A final failure is
    surfaced as an error string so the caller can decide (placeholder vs
    systemic pause); it does NOT silently write a degraded caption.
    """
    import urllib.request, urllib.error
    if "path" in img and img["path"]:
        img_path = Path(img["path"])
        if not img_path.is_absolute():
            img_path = media_dir / img["filename"]
    else:
        img_path = media_dir / img["filename"]
    if not img_path.exists():
        return None, f"missing image file: {img.get('filename')}"

    img_data = _stage_1_3_preprocess_image(img_path)
    ext = img_path.suffix.lstrip(".").lower()
    media_type = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"

    # Look up anchoring context by md5_8 of the saved bytes.
    ctx = None
    try:
        ctx = ctx_map.get(_stage_1_3_md5_8(img_path))
    except Exception:
        ctx = None
    prompt_text = _stage_1_3_build_user_prompt(img, ctx)

    # ── Protocol dispatch: anthropic vs openai ──
    protocol = (provider["protocol"] or "anthropic").lower()

    if protocol == "openai":
        # OpenAI chat/completions format (Ollama /v1, vLLM, etc.)
        data_url = f"data:{media_type};base64,{img_data}"
        content_parts = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        url = f"{provider['base_url'].rstrip('/')}/v1/chat/completions"
        body = json.dumps({
            "model": provider["model"],
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": CAPTION_SYSTEM_PROMPT},
                {"role": "user", "content": content_parts},
            ],
            "temperature": 0,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if provider["api_key"]:
            headers["Authorization"] = f"Bearer {provider['api_key']}"

        last_err = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, method="POST", headers=headers)
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read())
                choices = data.get("choices", [])
                if choices:
                    msg = choices[0].get("message", {})
                    text = (msg.get("content") or "").strip()
                    if text:
                        return text, None
                last_err = "empty VLM response"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(2 ** attempt)
        return None, last_err

    # Default: Anthropic Messages API
    content = [
        {"type": "text", "text": prompt_text},
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
    ]
    url = f"{provider['base_url'].rstrip('/')}/anthropic/v1/messages"
    body = json.dumps({
        "model": provider["model"],
        "max_tokens": 1024,
        "system": CAPTION_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
    }).encode("utf-8")

    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "x-api-key": provider["api_key"],
                "anthropic-version": "2023-06-01",
            })
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            text = "".join(c["text"] for c in data.get("content", [])
                           if c.get("type") == "text").strip()
            if text:
                return text, None
            last_err = "empty VLM response"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None, last_err


def _stage_1_3_provider_bundles(config: Config) -> list[tuple[str, dict]]:
    """``[("primary", {...})]`` plus ``("fallback", {...})`` when a fallback
    provider is configured (non-empty base_url/model — an api_key can
    legitimately be empty for an unauthenticated local server)."""
    bundles = [("primary", {
        "api_key": config.caption_api_key,
        "base_url": config.caption_base_url,
        "model": config.caption_model,
        "protocol": config.caption_protocol,
    })]
    if config.caption_fallback_base_url and config.caption_fallback_model:
        bundles.append(("fallback", {
            "api_key": config.caption_fallback_api_key,
            "base_url": config.caption_fallback_base_url,
            "model": config.caption_fallback_model,
            "protocol": config.caption_fallback_protocol,
        }))
    return bundles


def _stage_1_3_caption_one_image_with_failover(
    img: dict, config: Config, media_dir: Path, ctx_map: dict[str, dict],
) -> tuple[str | None, str | None, str]:
    """Try the primary provider, then the fallback (if configured) on primary
    exhaustion. Returns (caption, error, provider_label) — label is whichever
    provider produced the final result (or attempted last, on total failure).

    Loud, not silent: a fallback attempt prints one line so the operator can
    see how often the primary is failing. The no-silent-fallback boundary is
    unchanged — if every configured provider fails, the caller still treats
    this as a failure (placeholder / circuit-breaker / final raise).

    Non-primary (fallback) calls are serialized process-wide via
    _FALLBACK_SEMAPHORE — one image at a time, regardless of how many primary
    calls are running concurrently (see the constant's comment for why).
    """
    bundles = _stage_1_3_provider_bundles(config)
    last_err = None
    for i, (label, provider) in enumerate(bundles):
        if label == "primary":
            caption, err = _stage_1_3_caption_one_image(img, provider, media_dir, ctx_map)
        else:
            if not _FALLBACK_SEMAPHORE.acquire(blocking=False):
                print(f"    [caption] {img['filename']}: waiting for the fallback "
                      f"provider ({provider['model']}) — one image at a time...")
                _FALLBACK_SEMAPHORE.acquire()
            try:
                caption, err = _stage_1_3_caption_one_image(img, provider, media_dir, ctx_map)
            finally:
                _FALLBACK_SEMAPHORE.release()
        if caption:
            return caption, None, label
        last_err = err
        if i < len(bundles) - 1:
            print(f"    [caption] {img['filename']}: {label} ({provider['model']}) "
                  f"failed ({err}) — trying fallback ({bundles[i+1][1]['model']})...")
    return None, last_err, bundles[-1][0]


# ══════════════════════════════════════════════════════════════════════════════
# Parallel per-image dispatch
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_3_pending_images(images: list[dict], media_dir: Path) -> list[dict]:
    """Return images that still need a VLM caption.

    An image is pending if it has no .caption.txt, or the existing sidecar is
    a VLM-failure placeholder / undersized (so transient failures get retried
    on the next run)."""
    pending = []
    for img in images:
        cap_path = media_dir / (img["filename"] + ".caption.txt")
        if not cap_path.exists() or cap_path.stat().st_size < 20:
            pending.append(img)
            continue
        try:
            existing = cap_path.read_text(encoding="utf-8").strip()
        except Exception:
            pending.append(img)
            continue
        if _stage_1_3_is_caption_failed(existing):
            pending.append(img)
    return pending


# C-caption (2026-07-08): rounds + backoff before Stage 1.3 is allowed to hand
# off to Stage 2. Diagnosed on "EW and Radar Systems Handbook" — a single pass
# left 17/331 images as `[待重试]` placeholders, and nothing forced a retry
# before generation ran against the (still-incomplete) media directory; the
# only thing that ever picked them back up was a human happening to re-run
# ingest.py for that exact book again. Isolated stragglers now get up to
# _MAX_CAPTION_ROUNDS passes with cooldown between rounds; only if they are
# STILL failing after all rounds does the batch raise (see final check in
# _stage_1_3_caption_images_batch) — so Stage 1.3 either finishes clean or
# pauses loudly, it no longer silently hands off a partial result.
_MAX_CAPTION_ROUNDS = 3
_ROUND_BACKOFF_SECONDS = (15, 45)  # before round 2, round 3 (rate-limit cooldown)


def _stage_1_3_caption_one_round(pending: list[dict], config: Config, media_dir: Path,
                                  ctx_map: dict, label: str) -> int:
    """One parallel pass over `pending`. Returns captioned count.

    CONSECUTIVE_FAIL_PAUSE failures in a row within THIS round still raises
    immediately (fast circuit breaker for a fully-down provider — no point
    burning a whole round's backoff on a config that's simply broken).
    Isolated failures write a `[待重试]` placeholder and are left for the
    next round (or the caller's final no-fallback raise).
    """
    captioned = 0
    fallback_used = 0
    consecutive_fail = 0
    workers = min(CAPTION_MAX_WORKERS, len(pending)) or 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_img = {
            executor.submit(_stage_1_3_caption_one_image_with_failover,
                            img, config, media_dir, ctx_map): img
            for img in pending
        }
        done = 0
        for future in as_completed(future_to_img):
            img = future_to_img[future]
            done += 1
            try:
                caption, err, used = future.result()
            except Exception as e:
                caption, err, used = None, f"unhandled {type(e).__name__}: {e}", "primary"
            if caption:
                consecutive_fail = 0
                cap_text = caption.strip()
                if _stage_1_3_is_caption_failed(cap_text):
                    cap_text = (f"[待重试] 图片 {img['filename']}，"
                                f"尺寸 {img.get('width','?')}×{img.get('height','?')}")
                (media_dir / (img["filename"] + ".caption.txt")).write_text(
                    cap_text, encoding="utf-8")
                captioned += 1
                tag = " (fallback)" if used == "fallback" else ""
                if used == "fallback":
                    fallback_used += 1
                print(f"  [{done}/{len(pending)}] {img['filename']} ✓{tag}")
            else:
                consecutive_fail += 1
                placeholder = (f"[待重试] 图片 {img['filename']}，"
                               f"尺寸 {img.get('width','?')}×{img.get('height','?')} "
                               f"— {err}")
                (media_dir / (img["filename"] + ".caption.txt")).write_text(
                    placeholder, encoding="utf-8")
                print(f"  [{done}/{len(pending)}] {img['filename']} ✗ {err}")
                if consecutive_fail >= CONSECUTIVE_FAIL_PAUSE:
                    raise RuntimeError(
                        f"Caption VLM failed {consecutive_fail} images in a row "
                        f"(last: {err}). All configured providers exhausted — the "
                        f"main captioning path is not working. Fix the provider(s) "
                        f"and re-run (cached, resumes from Stage 1.3)."
                    )
    if fallback_used:
        print(f"  [caption] {fallback_used}/{captioned} images this round used the fallback provider")
    return captioned


def _stage_1_3_caption_images_batch(images: list[dict], config: Config, media_dir: Path,
                    source_label: str = "",
                    max_workers: int = CAPTION_MAX_WORKERS) -> int:
    """Caption every pending image, one VLM call per image, in parallel.

    NashSU parity (2026-06-24): one image per call with a context-aware
    prompt. `max_workers` caps the parallel calls.

    No-silent-fallback policy: a missing API key pauses the ingest. Each
    image tries the primary provider, then a configured fallback provider
    (2026-07-08, loud not silent — see _stage_1_3_caption_one_image_with_failover);
    that is provider failover, not the policy's "silent degrade to no caption"
    case. Isolated per-image failures (both providers exhausted) get up to
    _MAX_CAPTION_ROUNDS retry rounds with backoff (C-caption, 2026-07-08) —
    Stage 1.3 does NOT hand off to Stage 2 with placeholders still in place.
    CONSECUTIVE_FAIL_PAUSE failures in a row within any round means every
    configured provider's main path is down → pause (raise) immediately,
    without waiting for the remaining rounds.
    """
    if not images:
        return 0
    if not config.caption_api_key:
        already = sum(1 for img in images
                      if (media_dir / (img["filename"] + ".caption.txt")).exists())
        _caption_no_key_pause(config, source_label, media_dir, len(images), already)
        return 0  # unreachable — _caption_no_key_pause always raises

    label = f" [{source_label}]" if source_label else ""
    first_pending = _stage_1_3_pending_images(images, media_dir)
    if not first_pending:
        print(f"[caption]{label} (cached) All {len(images)} images already captioned")
        return 0

    ctx_map = _stage_1_3_build_context_map(config)
    total_captioned = 0
    pending = first_pending
    for round_idx in range(_MAX_CAPTION_ROUNDS):
        if round_idx == 0:
            print(f"[caption]{label} {len(pending)}/{len(images)} pending images "
                  f"→ one VLM call each (parallel, max {max_workers} workers, "
                  f"{len(ctx_map)} figures with context)")
        else:
            wait = _ROUND_BACKOFF_SECONDS[min(round_idx - 1, len(_ROUND_BACKOFF_SECONDS) - 1)]
            print(f"[caption]{label} round {round_idx+1}/{_MAX_CAPTION_ROUNDS}: "
                  f"{len(pending)} still pending — waiting {wait}s "
                  f"(rate-limit cooldown) before retry...")
            time.sleep(wait)
        total_captioned += _stage_1_3_caption_one_round(pending, config, media_dir, ctx_map, label)
        pending = _stage_1_3_pending_images(images, media_dir)
        if not pending:
            break

    if pending:
        names = ", ".join(img["filename"] for img in pending[:8])
        more = f" (+{len(pending) - 8} more)" if len(pending) > 8 else ""
        raise RuntimeError(
            f"Caption: {len(pending)}/{len(images)} images still failing after "
            f"{_MAX_CAPTION_ROUNDS} rounds: {names}{more}. All configured "
            f"providers (primary + fallback, if any) exhausted — the ingest "
            f"does not advance to Stage 2 with incomplete captions. Fix the "
            f"provider (rate limit window, or add/switch caption_fallback_provider "
            f"in ~/.agents/config.json) and re-run (cached, resumes from "
            f"Stage 1.3, only re-does the images still pending)."
        )

    print(f"[caption]{label} Done — {total_captioned} new captions written, "
          f"{len(images)}/{len(images)} images now captioned")
    return total_captioned


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1.3 entry point
# ══════════════════════════════════════════════════════════════════════════════

def stage_1_3_caption_images(config: Config, stage_1_2_result: dict) -> dict:
    """Caption extracted images, one VLM call per image (NashSU parity).

    Thin wrapper around _stage_1_3_caption_images_batch() for the Stage 1.3
    pipeline checkpoint."""
    images = stage_1_2_result.get("images", [])
    if not images:
        print("[stage 1.3] No images to caption — skipping")
        return {"captioned": 0, "total": 0}
    if not config.caption_api_key:
        media_dir = Path(stage_1_2_result.get("media_dir", "."))
        already = sum(1 for img in images
                      if (media_dir / (img["filename"] + ".caption.txt")).exists())
        _caption_no_key_pause(config, "stage-1.3", media_dir, len(images), already)
        return {"captioned": 0, "total": len(images), "skipped": True, "reason": "no-api-key"}

    media_dir = Path(stage_1_2_result["media_dir"])
    captioned = _stage_1_3_caption_images_batch(images, config, media_dir,
                                source_label="stage-1.3")
    return {"captioned": captioned, "total": len(images)}
