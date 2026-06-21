"""Stage 0.3: Pilot OCR quality validation for scanned PDFs.

Part of the pregate (Stage 0) pre-processing gates, run before Stage 1
extraction. Extracted from _stage_1_extract.py on 2026-06-21.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from _core import Config


def stage_0_3_pilot(file_path: Path, config: Config) -> dict:
    """Run 5-page pilot OCR for scanned PDF validation using local minerU.

    Extracts 5 pages into a temp PDF, runs local minerU CLI, and displays
    the OCR output for quality review.  No API key required.

    Stage 0.3 quality checks:
    - Text extraction: ≥200 chars/page (minimum digestible content)
    - Image extraction: ≥0.5 images/page (expect diagrams in scanned PDFs)
    - Overall: quality_ok if (chars_per_page > 200 AND img_per_page >= 0.5) OR chars_per_page > 600
    """
    try:
        import fitz
    except ImportError:
        return {"status": "error", "error": "PyMuPDF not installed"}

    mineru_bin = Path.home() / ".venv" / "bin" / "mineru"
    if not mineru_bin.exists():
        return {"status": "error", "error": "缺失minerU工具"}

    pilot_pages = min(5, len(fitz.open(file_path)))
    pilot_dir = config.extract_tmp_dir / ".pilot"
    pilot_dir.mkdir(parents=True, exist_ok=True)

    # Create a small 5-page pilot PDF
    doc = fitz.open(file_path)
    pilot_pdf = pilot_dir / f"{file_path.stem}-pilot.pdf"
    pilot_pdf.unlink(missing_ok=True)
    try:
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=0, to_page=pilot_pages - 1)
        new_doc.save(pilot_pdf)
        new_doc.close()
    finally:
        doc.close()

    print(f"[pilot] Extracted {pilot_pages} pages → {pilot_pdf}")
    print(f"[pilot] Running local minerU OCR...")

    out_dir = pilot_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        backend = os.environ.get("MINERU_BACKEND", "vlm-engine")
        result = subprocess.run(
            [str(mineru_bin), "-p", str(pilot_pdf), "-o", str(out_dir),
             "-b", backend, "-l", "ch"],
            capture_output=True, text=True, timeout=600,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Pilot OCR timed out (>10 min)"}
    except Exception as e:
        return {"status": "error", "error": f"minerU failed: {e}"}

    if result.returncode != 0:
        return {"status": "error", "error": f"minerU exit {result.returncode}: {result.stderr[-300:]}"}

    # Read OCR output (minerU v3.x writes to vlm/ subdirectory)
    stem = pilot_pdf.stem
    md_path = out_dir / stem / "vlm" / f"{stem}.md"
    if not md_path.exists():
        # Fallback: try auto/ subdirectory (older minerU)
        md_path = out_dir / stem / "auto" / f"{stem}.md"
    if not md_path.exists():
        return {"status": "error", "error": f"minerU finished but .md not found at {md_path}"}

    text = md_path.read_text(encoding="utf-8")
    # Count extracted images
    img_dir = out_dir / stem / "vlm" / "images"
    img_count = len(list(img_dir.glob("*"))) if img_dir.exists() else 0

    print(f"\n{'='*60}")
    print(f"PILOT OCR OUTPUT ({pilot_pages} pages, {len(text)} chars, {img_count} images):")
    print(f"{'='*60}")
    print(text[:3000])
    if len(text) > 3000:
        print(f"... ({len(text) - 3000} more chars)")
    print(f"{'='*60}\n")

    # Two-signal quality check (Stage 0.3)
    chars_per_page = len(text) / max(1, pilot_pages)
    img_per_page = img_count / max(1, pilot_pages)

    # Quality pass conditions:
    # 1. Text-heavy (>600 chars/page) — acceptable even with few images
    # 2. Balanced (>200 chars/page AND >0.5 images/page) — typical textbook
    quality_ok = (chars_per_page > 600) or (chars_per_page > 200 and img_per_page >= 0.5)

    return {
        "status": "ok" if quality_ok else "quality-low",
        "pilot_pages": pilot_pages,
        "ocr_chars": len(text),
        "stop_reason": "end_turn" if result.returncode == 0 else "error",
        "quality_ok": quality_ok,
        "chars_per_page": chars_per_page,
        "img_per_page": img_per_page,
        "text": text,
        "images_extracted": img_count,
    }
