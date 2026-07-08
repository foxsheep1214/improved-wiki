"""Extract, caption, and validate raw source files (Phase 1).

FACADE module (refactored 2026-06-24): keeps the Stage 1.1 text-extraction
entry points and re-exports the Stage 1.1 scanned-OCR / 1.2 image / 1.3 caption
implementations from their split sibling modules so existing importers are
unchanged:

  - _stage_1_1_scanned.py  — minerU API OCR pipeline (text/scanned/mixed PDFs)
  - _stage_1_2_images.py   — PPTX/DOCX image extraction + minerU figure harvest
  - _stage_1_3_caption.py  — VLM image captioning

Pipeline stages:
  Phase 1 Stage 1.1: Extract text from PDF/PPTX/DOCX (minerU pipeline for text PDFs, minerU VLM for scanned)
  Phase 1 Stage 1.2: Extract embedded images from PDF
  Phase 1 Stage 1.3: Generate image captions via VLM

Extracted from ingest.py on 2026-06-18. Refactored 2026-06-21 for explicit stage naming.
Split into facade + sub-modules 2026-06-24. Imports shared infrastructure from _core.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import sys
import time
import zipfile
from pathlib import Path

# Shared infrastructure
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import (
    Config,
    set_current_file as _set_current_file,
    get_current_file as _get_current_file,
    file_tag as _file_tag,
    stage_begin as _stage_begin,
    stage_end as _stage_end,
    heartbeat as _heartbeat,
    llm_call_progress as _llm_call_progress,
    llm_call_done as _llm_call_done,
    record_rate_limit as _record_rate_limit,
    file_sha256,
    load_cache,
    save_cache,
    detect_template_type,
    load_template,
)

# ── Re-exports from split sub-stage modules (facade back-compat) ──────────────
# External importers (ingest.py, _ingest_prepare.py, _stage_3_2_inject_images.py,
# _stage_validators.py, _stage_2_base.py) import these names from this facade;
# do not remove. Dependency direction is one-way at load time:
#   _stage_1_extract → {scanned, images, caption}
#   scanned → {images, caption}
#   caption → {}  (log_event reached via late import)
#   images → {}
from _stage_1_1_scanned import (  # noqa: F401
    _stage_1_1_extract_text_scanned,
    _clean_mineru_latex,
    log_event,
)
from _stage_1_2_images import (  # noqa: F401
    stage_1_2_extract_images,
    _stage_1_2_extract_from_mineru,
    _stage_1_2_write_manifest,
    _stage_1_2_find_uncaptioned_images,
    _stage_1_2_harvest_images,
    _stage_1_2_extract_images_office,
    _is_image_too_small,
    MINERU_IMG_MIN_WIDTH,
    MINERU_IMG_MIN_HEIGHT,
)
from _stage_1_3_caption import (  # noqa: F401
    stage_1_3_caption_images,
    CAPTION_MAX_WORKERS,
)
# Back-compat aliases: media_slug / raw_type_subdir now live in _paths.py.
# Kept under the old `_stage_1_2_*` names so existing
# `from _stage_1_extract import _stage_1_2_media_slug` callers keep working.
from _paths import (  # noqa: F401
    media_slug as _stage_1_2_media_slug,
    raw_type_subdir as _stage_1_2_raw_type_subdir,
)

# Public API: only export stage entry points
# Internal helpers (prefixed with _) are imported directly when needed
__all__ = [
    "stage_1_1_extract_text",   # Phase 1.1: Text extraction
    "stage_1_2_extract_images", # Phase 1.2: Image extraction
    "stage_1_3_caption_images", # Phase 1.3: Image captioning
]

# ══════════════════════════════════════════════════════════════════════════════
# Constants & Concurrency Control
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Stage 1.1: Text extraction
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_1_extract_text_office(file_path: Path) -> str:
    """Extract readable text from PPTX/DOCX via zipfile + XML parsing.

    NashSU parity: read non-PDF sources. Uses stdlib only — no external deps.
    PPTX: parses <a:t> text runs from ppt/slides/slide*.xml.
    DOCX: parses <w:t> text runs from word/document.xml (plus headers/footers/notes).
    """
    import zipfile as _zf
    import xml.etree.ElementTree as _ET

    suffix = file_path.suffix.lower()
    chunks: list[str] = []

    try:
        with _zf.ZipFile(file_path, "r") as zf:
            if suffix == ".pptx":
                # Extract text from each slide
                slides = sorted(
                    [n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")],
                    key=lambda n: int("".join(c for c in n if c.isdigit()) or "0")
                )
                ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
                for slide_name in slides:
                    try:
                        root = _ET.fromstring(zf.read(slide_name))
                        slide_text: list[str] = []
                        for t_elem in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
                            if t_elem.text:
                                slide_text.append(t_elem.text)
                        if slide_text:
                            slide_num = "".join(c for c in slide_name if c.isdigit()) or "?"
                            chunks.append(f"\n## Slide {slide_num}\n" + " ".join(slide_text))
                    except Exception:
                        continue

            elif suffix == ".docx":
                # Extract from document.xml, headers, footers, endnotes, footnotes
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                xml_files = ["word/document.xml"]

                # Add headers/footers if present
                for n in zf.namelist():
                    if n.startswith("word/header") or n.startswith("word/footer") or \
                       n.startswith("word/endnote") or n.startswith("word/footnote"):
                        if n.endswith(".xml"):
                            xml_files.append(n)

                for xml_file in xml_files:
                    try:
                        root = _ET.fromstring(zf.read(xml_file))
                        parts: list[str] = []
                        for p_elem in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                            para_parts = []
                            for t_elem in p_elem.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
                                if t_elem.text:
                                    para_parts.append(t_elem.text)
                            if para_parts:
                                parts.append("".join(para_parts))
                        if parts:
                            label = xml_file.split("/")[-1].replace(".xml", "") if xml_file != "word/document.xml" else "Body"
                            chunks.append(f"\n## {label}\n" + "\n".join(parts))
                    except Exception:
                        continue

    except Exception as e:
        raise RuntimeError(f"Failed to extract text from {file_path.name}: {e}")

    text = "\n".join(chunks)
    if not text.strip():
        raise RuntimeError(f"No extractable text found in {file_path.name}")
    print(f"[extract] {suffix.upper()}: {len(text):,} chars from {len(chunks)} sections")
    return text


def stage_1_1_extract_text(file_path: Path, config: Config) -> tuple[str, str]:
    """Extract text from a source file via the minerU API server (hybrid-engine).

    All PDFs (text / scanned / mixed) take ONE path: a persistent local minerU
    API server (mineru.cli.fast_api) + /file_parse per N-page chunk, backend
    hybrid-engine (server default), parse_method auto — hybrid-engine routes
    per-page (text layer present → txt, no OCR; absent → VLM OCR).

    NashSU parity note: NashSU uses the minerU **cloud** API (mineru.net, needs
    a token, pipeline/vlm model, 200-page cap). improved-wiki uses the **local**
    free server (hybrid-engine/auto, no token, no page cap) — an intentional
    offset. No garbled-font pre-detection and no extraction quality gate: NashSU
    has neither, and on minerU 3.4.0 the OCR impact of dropping them is limited,
    so both were removed 2026-07-08 for NashSU alignment. (If minerU is upgraded,
    reconsider whether auto still handles garbled text layers acceptably.)

    txt/md/pptx/docx bypass minerU entirely.

    Returns (text, method_label). method_label is "mineru-api" for PDFs — the
    Stage-1.2 image path keys on the "mineru" prefix.
    """
    if file_path.suffix.lower() in {".txt", ".md"}:
        return file_path.read_text(encoding="utf-8"), "plain-text"
    if file_path.suffix.lower() in {".pptx", ".docx"}:
        return _stage_1_1_extract_text_office(file_path), f"zipfile-{file_path.suffix.lower().lstrip('.')}"
    if file_path.suffix.lower() != ".pdf":
        raise ValueError(f"Unsupported file type: {file_path.suffix}")

    # parse_method=auto lets hybrid-engine route per-page (txt vs VLM OCR).
    text = _stage_1_1_extract_text_scanned(file_path, config)
    return text, "mineru-api"


# ---------- Stage 0: PDF type detection ----------

def _stage_1_1_sample_pdf(file_path: Path, sample_pages: int = 15) -> tuple[float, bool, float]:
    """Sample N pages (skipping first+last) via fitz — detection only, NOT extraction.

    Returns (avg_chars, is_garbled, img_ratio):
      - avg_chars: mean chars/page over sampled pages with ≥10 chars (0 if none).
      - is_garbled: True if >1% of sampled chars are C0 control chars (0x00-0x1F),
        indicating custom font encoding PyMuPDF cannot decode (e.g. Fuqua book:
        500+ chars/page but all garbage). Informational only since 2026-07-08
        (garbled pre-detection removed from the extraction path for NashSU
        alignment) — now consumed only by the --dry-run type estimate.
      - img_ratio: fraction of sampled text-pages with a >50%-page image.

    Sampling is deterministic per file (seeded by file path) so the same PDF
    always samples the same way across runs (uses hashlib, not built-in hash(),
    which is randomized per process). Page 0 (cover) and last page are skipped;
    N pages are picked from the middle. Returns (0.0, False, 0.0) if fitz is
    unavailable or no sampled page has ≥10 chars.
    """
    try:
        import fitz
    except ImportError:
        return (0.0, False, 0.0)

    _C0_RE = __import__('re').compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    _seed = int(hashlib.md5(str(file_path).encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(_seed)

    doc = fitz.open(file_path)
    try:
        total_chars = 0
        text_pages = 0
        img_pages = 0
        garbled_chars = 0
        middle_pages = max(0, len(doc) - 2)  # skip page 0 and last page
        n = min(sample_pages, middle_pages)
        if middle_pages <= 0:
            sample_indices = list(range(len(doc)))  # too short, take all
        else:
            pool = list(range(1, len(doc) - 1))    # pages between first and last
            sample_indices = rng.sample(pool, n) if n < len(pool) else pool

        for idx in sample_indices:
            page = doc[idx]
            text = page.get_text()
            chars = len(text)
            if chars < 10:
                continue
            total_chars += chars
            text_pages += 1
            garbled_chars += len(_C0_RE.findall(text))
            rect = page.rect
            page_area = rect.width * rect.height
            for img in page.get_images():
                pix = fitz.Pixmap(doc, img[0])
                img_area = pix.width * pix.height
                if img_area > page_area * 0.5:
                    img_pages += 1
                    break

        if text_pages == 0:
            return (0.0, False, 0.0)
        avg = total_chars / text_pages
        img_ratio = img_pages / text_pages
        is_garbled = (garbled_chars / max(total_chars, 1)) > 0.01
        return (avg, is_garbled, img_ratio)
    finally:
        doc.close()


def _stage_1_1_detect_pdf_type(file_path: Path, sample_pages: int = 15) -> tuple[str, float]:
    """Backward-compat text/scanned/mixed classifier — used ONLY by the --dry-run
    cost estimate in ingest.py.

    The active extraction path (stage_1_1_extract_text) no longer branches on
    any fitz detection: hybrid-engine/auto routes per-page internally (garbled
    pre-detection removed 2026-07-08). This classifier is kept only so
    `--dry-run` can print a human-readable type. Delegates to
    _stage_1_1_sample_pdf.
    """
    avg, is_garbled, img_ratio = _stage_1_1_sample_pdf(file_path, sample_pages)
    if is_garbled:
        return ("scanned", avg)  # garbled → needs OCR
    if avg > 500:
        return ("mixed" if img_ratio > 0.3 else "text", avg)
    if avg < 50:
        return ("scanned", avg)
    return ("mixed", avg)

