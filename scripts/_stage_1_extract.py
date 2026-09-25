"""Extract, caption, and validate raw source files (Phase 1).

This facade owns Office text extraction and PDF routing, and re-exports the
public Stage 1.2 image and Stage 1.3 caption entry points:

  - _stage_1_1_scanned.py  — minerU API OCR pipeline (text/scanned/mixed PDFs)
  - _stage_1_1_documents.py — XLSX/ODT/EPUB/RTF text extraction (stdlib only)
  - _stage_1_2_images.py   — PPTX/DOCX image extraction + minerU figure harvest
  - _stage_1_3_caption.py  — VLM image captioning

Pipeline stages:
  Phase 1 Stage 1.1: Extract text from PDF/PPTX/DOCX/XLSX/ODT/EPUB/RTF (all PDFs use minerU hybrid-engine/auto)
  Phase 1 Stage 1.2: Extract embedded images from PDF
  Phase 1 Stage 1.3: Generate image captions via VLM

"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

from _config import Config

from _stage_1_1_documents import (
    SUPPORTED_DOCUMENT_SUFFIXES,
    extract_document_text,
)
from _stage_1_1_scanned import _stage_1_1_extract_text_scanned
from _stage_1_2_images import stage_1_2_extract_images
from _stage_1_3_caption import stage_1_3_caption_images

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

# Office (PPTX/DOCX) extraction: individual slide/XML parse failures are
# counted and reported; if more than this fraction of parts fail, the whole
# extraction raises instead of silently handing off partial text.
OFFICE_XML_SKIP_RAISE_RATIO = 0.3

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
                skipped = 0
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
                    except Exception as slide_err:
                        skipped += 1
                        print(f"[extract] PPTX slide skipped ({slide_name}): {slide_err}")
                        continue
                if slides and skipped / len(slides) > OFFICE_XML_SKIP_RAISE_RATIO:
                    raise RuntimeError(
                        f"PPTX extraction: {skipped}/{len(slides)} slides failed to parse "
                        f"in {file_path.name} — refusing to hand off partial text")

            elif suffix == ".docx":
                # Extract from document.xml, headers, footers, endnotes, footnotes
                xml_files = ["word/document.xml"]

                # Add headers/footers if present
                for n in zf.namelist():
                    if n.startswith("word/header") or n.startswith("word/footer") or \
                       n.startswith("word/endnote") or n.startswith("word/footnote"):
                        if n.endswith(".xml"):
                            xml_files.append(n)

                skipped = 0
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
                    except Exception as xml_err:
                        skipped += 1
                        print(f"[extract] DOCX part skipped ({xml_file}): {xml_err}")
                        continue
                if xml_files and skipped / len(xml_files) > OFFICE_XML_SKIP_RAISE_RATIO:
                    raise RuntimeError(
                        f"DOCX extraction: {skipped}/{len(xml_files)} XML parts failed to "
                        f"parse in {file_path.name} — refusing to hand off partial text")

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

    txt/md/pptx/docx and xlsx/odt/epub/rtf bypass minerU entirely.

    Returns (text, method_label). method_label is "mineru-api" for PDFs — the
    Stage-1.2 image path keys on the "mineru" prefix.
    """
    if file_path.suffix.lower() in {".txt", ".md"}:
        return file_path.read_text(encoding="utf-8"), "plain-text"
    if file_path.suffix.lower() in {".pptx", ".docx"}:
        return _stage_1_1_extract_text_office(file_path), f"zipfile-{file_path.suffix.lower().lstrip('.')}"
    if file_path.suffix.lower() in SUPPORTED_DOCUMENT_SUFFIXES:
        suffix = file_path.suffix.lower()
        return extract_document_text(file_path), f"document-{suffix.lstrip('.')}"
    if file_path.suffix.lower() != ".pdf":
        raise ValueError(f"Unsupported file type: {file_path.suffix}")

    # parse_method=auto lets hybrid-engine route per-page (txt vs VLM OCR).
    text = _stage_1_1_extract_text_scanned(file_path, config)
    return text, "mineru-api"


def sample_pdf_text_density(file_path: Path, sample_pages: int = 15) -> float:
    """Deterministic lightweight chars/page estimate for dry-run only."""
    try:
        import fitz
    except ImportError:
        return 0.0
    seed = int(hashlib.md5(str(file_path).encode()).hexdigest()[:8], 16)
    with fitz.open(file_path) as doc:
        pool = list(range(1, len(doc) - 1)) if len(doc) > 2 else list(range(len(doc)))
        indices = random.Random(seed).sample(pool, min(sample_pages, len(pool)))
        lengths = [len(doc[i].get_text()) for i in indices]
        readable = [n for n in lengths if n >= 10]
        return sum(readable) / len(readable) if readable else 0.0
