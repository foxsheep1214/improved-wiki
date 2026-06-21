"""Stage 0: Text extraction, PDF type detection, OCR, image extraction & captioning.

Extracted from ingest.py on 2026-06-18.
Imports shared infrastructure from _core and _llm_api.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Re-export for ingest.py
__all__ = [
    "extract_text",
    "extract_text_pymupdf",
    "extract_text_mineru",
    "extract_text_scanned_pdf",
    "detect_pdf_type",
    "stage_0_3_pilot",
    "stage_1_2_extract_images",
    "stage_1_3_caption_images",
    "_is_caption_failed",
    "_caption_images",
    "_caption_one_batch",
    "_preprocess_image_for_caption",
]

# ── Stage 0 constants ──

MINERU_MAX_CONCURRENT = 1  # max parallel minerU OCR jobs system-wide
MINERU_API_PORT = int(os.environ.get("MINERU_API_PORT", "19999"))  # fixed API port
CAPTION_BATCH_SIZE = int(os.environ.get("CAPTION_BATCH_SIZE", "8"))

# ── Module-level structured logging (shared by extract_text_scanned_pdf and _caption_images) ──

import datetime as _dt

_log_file: Path | None = None  # set by extract_text_scanned_pdf(); _caption_images uses media_dir fallback

def log_event(event_type: str, **kwargs):
    """Write structured event to JSON Lines log.

    Module-level so both extract_text_scanned_pdf() and _caption_images() can call it.
    If _log_file is not set (caption path), writes to a fallback path derived from kwargs.
    """
    target = _log_file
    entry = {
        "timestamp": _dt.datetime.now().isoformat(),
        "event_type": event_type,
        **kwargs
    }
    if target is None:
        return  # no log file configured yet — skip silently
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

# ---------- Text extraction ----------

def _find_pymupdf_python() -> Path | None:
    """Find a Python that has PyMuPDF. Check current interpreter, then venv."""
    try:
        import fitz  # noqa: F401
        return Path(sys.executable)
    except ImportError:
        pass
    venv_python = Path.home() / ".venv" / "bin" / "python3"
    if venv_python.exists():
        import subprocess
        r = subprocess.run(
            [str(venv_python), "-c", "import fitz"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return venv_python
    return None


def _ensure_pymupdf() -> Path:
    """Find or install PyMuPDF. Prefer current Python, then venv, then install."""
    python = _find_pymupdf_python()
    if python is not None:
        return python
    # Install into venv if it exists, otherwise current Python
    python = Path.home() / ".venv" / "bin" / "python3"
    if not python.exists():
        python = Path(sys.executable)
    print(f"[extract] Installing PyMuPDF into {python.parent}...")
    import subprocess
    r = subprocess.run(
        [str(python), "-m", "pip", "install", "pymupdf", "-q"],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Failed to install PyMuPDF: {r.stderr.decode()[:300]}")
    return python


def _pymupdf_page_count(file_path: Path) -> int:
    """Get page count via PyMuPDF. Returns 0 on failure."""
    try:
        python = _find_pymupdf_python()
        if python is None:
            return 0
        if python == Path(sys.executable):
            import fitz
            doc = fitz.open(file_path)
            try:
                return len(doc)
            finally:
                doc.close()
        else:
            import subprocess
            r = subprocess.run(
                [str(python), "-c",
                 f"import fitz; doc=fitz.open({file_path!r}); print(len(doc)); doc.close()"],
                capture_output=True, text=True, timeout=30,
            )
            return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
    except Exception:
        return 0


def extract_text_pymupdf(file_path: Path) -> str:
    python = _ensure_pymupdf()
    if python == Path(sys.executable):
        import fitz
        doc = fitz.open(file_path)
        try:
            text_parts = [page.get_text() for page in doc]
            return "\n\n".join(text_parts)
        finally:
            doc.close()
    else:
        import subprocess
        script = f"""
import fitz, sys
doc = fitz.open(sys.argv[1])
for page in doc:
    print(page.get_text())
    print('\\n\\n')
doc.close()
"""
        r = subprocess.run(
            [str(python), "-c", script, str(file_path)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(f"PyMuPDF subprocess failed: {r.stderr[:200]}")
        return r.stdout.strip()


def extract_text_mineru(file_path: Path, config: Config) -> str:
    import subprocess
    env = os.environ.copy()
    mineru_bin = Path.home() / ".venv" / "bin" / "mineru"
    if not mineru_bin.exists():
        raise RuntimeError(f"mineru CLI not found at {mineru_bin}")

    out_dir = config.extract_tmp_dir / "ocr"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = file_path.stem

    # minerU v3 backend: defaults to hybrid-engine; override via MINERU_BACKEND env
    backend = os.environ.get("MINERU_BACKEND", "vlm-engine")
    cmd = [
        str(mineru_bin), "-p", str(file_path), "-o", str(out_dir),
        "-b", backend, "-l", "ch",
    ]
    print(f"[ocr] Running minerU: {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"minerU failed: {result.stderr[-500:]}")

    # minerU output path varies by version — search for the actual .md output
    candidates = sorted(out_dir.rglob(f"{stem}*.md"))
    # Prefer VLM output, then auto, then any
    md_out = None
    for c in candidates:
        if "vlm" in str(c):
            md_out = c
            break
    if md_out is None:
        for c in candidates:
            if "auto" in str(c):
                md_out = c
                break
    if md_out is None and candidates:
        md_out = candidates[-1]  # last resort: latest modified
    if md_out is None or not md_out.exists():
        # Show what minerU actually produced for debugging
        all_output = sorted(out_dir.rglob("*"))
        found = [str(p.relative_to(out_dir)) for p in all_output if p.is_file()]
        raise RuntimeError(
            f"minerU finished but no .md output found for stem '{stem}'. "
            f"Actual output under {out_dir}: {found[:20]}"
        )
    print(f"[ocr] Found output: {md_out.relative_to(out_dir)}")
    return md_out.read_text(encoding="utf-8")


def _extract_text_from_office(file_path: Path) -> str:
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


def extract_text(file_path: Path, config: Config, pilot_confirmed: bool = False) -> tuple[str, str]:
    if file_path.suffix.lower() in {".txt", ".md"}:
        return file_path.read_text(encoding="utf-8"), "plain-text"
    if file_path.suffix.lower() in {".pptx", ".docx"}:
        return _extract_text_from_office(file_path), f"zipfile-{file_path.suffix.lower().lstrip('.')}"
    if file_path.suffix.lower() != ".pdf":
        raise ValueError(f"Unsupported file type: {file_path.suffix}")

    # Stage 0: Detect PDF type
    pdf_type, avg_chars = detect_pdf_type(file_path)
    print(f"[extract] PDF type: {pdf_type} (avg {avg_chars:.0f} chars/page from 10-page sample)")

    if pdf_type == "text":
        try:
            text = extract_text_pymupdf(file_path)
            # Check 1: not empty, not sparse
            ok1 = text.strip() and len(text) > len(text.split("\n\n")) * 5
            # Check 2: per-page char threshold (>50 chars/page minimum for real text docs).
            # Misclassified scanned PDFs detected as "text" will have near-zero per-page yield.
            ok2 = True
            if ok1:
                pages = _pymupdf_page_count(file_path)
                if pages and len(text) < pages * 50:
                    ok2 = False
                    print(f"[extract] PyMuPDF returned only {len(text)} chars over {pages} pages "
                          f"({len(text)/max(pages,1):.0f} chars/page) — likely scanned, trying minerU fallback")
            if ok1 and ok2:
                return text, "pymupdf"
            if ok1 and not ok2:
                print(f"[extract] PyMuPDF text looks sparse per-page — trying minerU fallback")
            elif not ok1:
                print(f"[extract] PyMuPDF returned sparse text — trying minerU fallback")
        except Exception as e:
            print(f"[extract] PyMuPDF failed ({e}) — trying minerU fallback")
        try:
            text = extract_text_mineru(file_path, config)
            return text, "mineru"
        except Exception:
            raise RuntimeError(f"Both PyMuPDF and minerU failed for {file_path.name}")

    elif pdf_type == "scanned":
        # Path B: VLM OCR required. Auto-fallback without interactive pilot gate.
        # pilot_confirmed=True (rare, interactive only): run full OCR directly.
        # pilot_confirmed=False (normal / batch): auto-fallback, don't block.
        if pilot_confirmed:
            print(f"[extract] Running local minerU OCR on scanned PDF (pilot confirmed)...")
            text = extract_text_scanned_pdf(file_path, config)
            return text, "mineru-local-ocr"
        else:
            print(f"[extract] Scanned PDF: auto-fallback to minerU OCR...")
            try:
                text = extract_text_scanned_pdf(file_path, config)
                if len(text) > 2000:
                    return text, "mineru-local-ocr"
                print(f"[extract] ⚠️  Scanned PDF OCR returned only {len(text)} chars — quality may be poor")
                return text, "mineru-local-ocr-low-quality"
            except Exception as e:
                raise RuntimeError(
                    f"Scanned PDF minerU OCR failed ({e}). "
                    f"Re-run interactively with --pilot-confirmed to review."
                )

    elif pdf_type == "mixed":
        # Mixed: try PyMuPDF first. If text layer is usable, take it.
        # Otherwise auto-fallback to minerU OCR without blocking interactive pilot.
        try:
            text = extract_text_pymupdf(file_path)
            if text.strip() and len(text) > 2000:
                print(f"[extract] Mixed PDF: PyMuPDF returned {len(text):,} chars — using text layer")
                return text, "pymupdf-mixed"
        except Exception as e:
            print(f"[extract] Mixed PDF: PyMuPDF failed ({e})")
        # Sparse text — auto-fallback to minerU OCR.
        # Previously required --pilot-confirmed which blocked batch ingest.
        print(f"[extract] Mixed PDF: auto-fallback to minerU OCR (no interactive pilot)...")
        try:
            text = extract_text_scanned_pdf(file_path, config)
            if len(text) > 2000:
                return text, "mineru-local-ocr"
            print(f"[extract] ⚠️  Mixed PDF OCR returned only {len(text)} chars — quality may be poor")
            return text, "mineru-local-ocr-low-quality"
        except Exception as e:
            raise RuntimeError(
                f"Mixed PDF minerU OCR failed ({e}). "
                f"Re-run interactively with --pilot-confirmed to review."
            )

    else:
        raise RuntimeError(f"Unknown PDF type: {pdf_type}")


# ---------- Stage 0 pilot: PDF type detection + pilot OCR ----------

def detect_pdf_type(file_path: Path, sample_pages: int = 5) -> tuple[str, float]:
    """Sample N pages (skipping first+last) to determine PDF type.

    Uses three signals:
    1. Text chars/page (PyMuPDF get_text())
    2. Presence of full-page images (scanned PDFs have one large image per page)
    3. Garbled text ratio — C0 control chars (0x00-0x1F) indicate custom font
       encoding that PyMuPDF cannot decode. >1% garbled → force OCR path.
       (2026-06-18: Fuqua book had 500+ chars/page but all garbled.)

    Page 0 (cover/title) and the last page (index/back-cover) are always
    skipped. N pages are randomly picked from the remaining middle pages.
    Short PDFs (< N+2 pages) sample all available middle pages.

    Returns ("text", avg_chars) or ("scanned", avg_chars) or ("mixed", avg_chars).
    """
    try:
        import fitz
    except ImportError:
        return ("text", 0)

    _C0_RE = __import__('re').compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

    doc = fitz.open(file_path)
    try:
        total_chars = 0
        text_pages = 0
        img_pages = 0
        garbled_chars = 0
        n = min(sample_pages, len(doc) - 2)  # skip page 0 and last page
        middle_pages = max(0, len(doc) - 2)
        n = min(sample_pages, middle_pages)
        if middle_pages <= 0:
            sample_indices = list(range(len(doc)))  # too short, take all
        else:
            pool = list(range(1, len(doc) - 1))    # pages between first and last
            sample_indices = random.sample(pool, n) if n < len(pool) else pool

        for idx in sample_indices:
            page = doc[idx]
            text = page.get_text()
            chars = len(text)
            if chars < 10:
                continue
            total_chars += chars
            text_pages += 1
            # Signal 3: garbled text (custom font encoding → force OCR)
            garbled_chars += len(_C0_RE.findall(text))
            # Signal 2: full-page scan image
            rect = page.rect
            page_area = rect.width * rect.height
            for img in page.get_images():
                pix = fitz.Pixmap(doc, img[0])
                img_area = pix.width * pix.height
                if img_area > page_area * 0.5:
                    img_pages += 1
                    break

        if text_pages == 0:
            return ("scanned", 0.0)

        avg = total_chars / text_pages
        img_ratio = img_pages / text_pages
        garbled_ratio = garbled_chars / max(total_chars, 1)

        # Signal 3: garbled text trumps all — force OCR
        if garbled_ratio > 0.01:
            return ("scanned", avg)
        if img_ratio > 0.6:
            return ("scanned", avg)
        if avg > 500 and img_ratio > 0.3:
            return ("mixed", avg)
        if avg > 500:
            return ("text", avg)
        elif avg < 50:
            return ("scanned", avg)
        else:
            return ("mixed", avg)
    finally:
        doc.close()


def stage_0_3_pilot(file_path: Path, config: Config) -> dict:
    """Run 5-page pilot OCR for scanned PDF validation using local minerU.

    Extracts 5 pages into a temp PDF, runs local minerU CLI, and displays
    the OCR output for quality review.  No API key required.
    """
    try:
        import fitz
    except ImportError:
        return {"status": "error", "error": "PyMuPDF not installed"}

    mineru_bin = Path.home() / ".venv" / "bin" / "mineru"
    if not mineru_bin.exists():
        return {"status": "error", "error": f"minerU not found at {mineru_bin}"}

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
    import subprocess
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

    quality_ok = len(text) > pilot_pages * 100

    return {
        "status": "ok" if quality_ok else "quality-low",
        "pilot_pages": pilot_pages,
        "ocr_chars": len(text),
        "stop_reason": "end_turn" if result.returncode == 0 else "error",
        "quality_ok": quality_ok,
        "text": text,
        "images_extracted": img_count,
    }


# ---------- Stage 0 path B: Scanned PDF OCR via local minerU ----------

MINERU_CHUNK_SIZE = 50  # pages per minerU invocation
MINERU_MAX_CONCURRENT = 1  # max parallel minerU OCR jobs system-wide (串行执行，避免 VLM 内存竞争)


def _count_running_mineru() -> int:
    """Count minerU OCR jobs currently running SYSTEM-WIDE.

    Each active minerU OCR job consists of a CLI process and a Fast API server.
    We count both to detect jobs started by ANY process (other conversations,
    cron jobs, manual CLI).  Returns the number of OCCUPIED slots (job pairs).
    """
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "mineru"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return 0
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        alive = 0
        for pid in pids:
            try:
                state = subprocess.run(
                    ["ps", "-p", pid, "-o", "state="],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
                if state and "Z" not in state:
                    alive += 1
            except Exception:
                pass
        # Each OCR job has 2 processes (CLI + Fast API server) → jobs = alive / 2
        return alive // 2
    except Exception:
        return 0


def _wait_for_mineru_slot(poll_interval: int = 120) -> None:
    """Block until a minerU concurrency slot is available. Prints prominent status updates.

    When all slots are busy, identifies which file(s) currently occupy them and
    shows accumulated wait time.  Designed for MINERU_MAX_CONCURRENT=1 (serial OCR)
    where wait times can be 5-30 minutes per chunk.
    """
    import subprocess
    wait_start = time.time()
    first_cycle = True
    while True:
        running = _count_running_mineru()
        if running < MINERU_MAX_CONCURRENT:
            if not first_cycle:
                waited = time.time() - wait_start
                if waited >= 60:
                    print(f"[mineru] ✅ slot freed after {waited/60:.1f}min — proceeding")
                else:
                    print(f"[mineru] ✅ slot freed after {waited:.0f}s — proceeding")
            return

        # Build informative wait message
        waited = time.time() - wait_start
        # Try to identify which file(s) are occupying the slot(s)
        busy_desc = ""
        try:
            proc = subprocess.run(
                ["pgrep", "-fla", "mineru"],
                capture_output=True, text=True, timeout=3,
            )
            if proc.stdout.strip():
                import re
                for line in proc.stdout.strip().split("\n"):
                    m = re.search(r'-p\s+(/[^\s]+\.pdf)', line)
                    if m and "fast_api" not in line.lower():
                        fname = Path(m.group(1)).name
                        if fname not in busy_desc:
                            busy_desc += f"「{fname}」"
        except Exception:
            pass

        # Format elapsed time
        if waited >= 60:
            elapsed = f"{waited/60:.1f} 分钟"
        else:
            elapsed = f"{waited:.0f} 秒"

        print(f"[mineru] ⏳ 并发槽已满 ({running}/{MINERU_MAX_CONCURRENT}){busy_desc} — 已等待 {elapsed}，{poll_interval}s 后重试...")
        time.sleep(poll_interval)


def _kill_mineru_servers() -> None:
    """Kill lingering mineru-api processes to ensure clean state."""
    import subprocess
    try:
        subprocess.run(
            ["pkill", "-f", "mineru-api"], capture_output=True, timeout=5,
        )
    except Exception:
        pass  # best-effort


def extract_text_scanned_pdf(file_path: Path, config: Config) -> str:
    """OCR a scanned PDF using local minerU (vlm-engine backend, configurable via MINERU_BACKEND env).

    Splits PDF into ~50-page chunks. Each chunk runs minerU independently.
    Results persisted to extract_tmp_dir/<stem>/ with _mineru_stats.json for crash recovery.
    Extracted images go to wiki/media/<raw-subpath>/<slug>/ for Stage 3.2 (mirrors raw/).
    """
    try:
        import fitz
    except ImportError:
        raise RuntimeError("Scanned PDF OCR requires PyMuPDF")

    mineru_bin = Path.home() / ".venv" / "bin" / "mineru"
    if not mineru_bin.exists():
        raise RuntimeError(f"minerU not found at {mineru_bin}")

    doc = fitz.open(file_path)
    total_pages = len(doc)

    out_dir = config.extract_tmp_dir / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build chunks: 50 pages each
    chunks = []
    for start in range(0, total_pages, MINERU_CHUNK_SIZE):
        end = min(start + MINERU_CHUNK_SIZE, total_pages)
        chunks.append((start, end))  # 0-indexed, [start, end)

    print(f"[ocr] Local minerU: {total_pages} pages → {len(chunks)} chunks ({MINERU_CHUNK_SIZE} pages/chunk)")

    # Load or init stats
    stats_path = out_dir / "_mineru_stats.json"
    stats: dict = {"completed_chunks": [], "failed_chunks": [], "images": {}}
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))

    # Initialize structured logging (JSON Lines format)
    global _log_file
    _log_file = out_dir / "ocr_log.jsonl"

    # Start a persistent minerU API server (one per book, shared across chunks)
    pending = [c for c in chunks if f"{c[0]}-{c[1]}" not in stats["completed_chunks"]]
    if not pending:
        doc.close()
        return _assemble_ocr_text(out_dir, [end for _, end in chunks])

    import subprocess as _sp
    # Use venv python (minerU is only installed in venv, not system python)
    venv_python = Path.home() / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        venv_python = Path(sys.executable)
    api_proc = _sp.Popen(
        [str(venv_python), "-m", "mineru.cli.fast_api",
         "--host", "127.0.0.1", "--port", str(MINERU_API_PORT)],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    # Wait for API to become healthy
    for _ in range(30):
        time.sleep(2)
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{MINERU_API_PORT}/health", timeout=3)
            if json.loads(r.read()).get("status") == "healthy":
                print(f"[ocr] minerU API ready on port {MINERU_API_PORT}")
                break
        except Exception:
            pass
    else:
        api_proc.terminate()
        api_proc.wait()
        doc.close()
        raise RuntimeError(f"minerU API failed to start on port {MINERU_API_PORT}")

    # Warmup: initialize model with a small 1-page test to avoid cold-start delay
    # First chunk typically takes 134s; warmup reduces this to ~74s (60s savings)
    print(f"[ocr] Warmup: initializing model...", end=" ", flush=True)
    warmup_start = time.time()
    try:
        # Create a 1-page warmup PDF
        warmup_pdf = out_dir / "_warmup.pdf"
        warmup_doc = fitz.open()
        warmup_doc.insert_pdf(doc, from_page=0, to_page=0)  # First page only
        warmup_doc.save(warmup_pdf)
        warmup_doc.close()

        # Send warmup request (same format as real chunks)
        boundary = "----FormBoundary" + os.urandom(8).hex()
        body_parts = []
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="files"; filename="warmup.pdf"')
        body_parts.append(b"Content-Type: application/pdf")
        body_parts.append(b"")
        body_parts.append(warmup_pdf.read_bytes())
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="data"')
        body_parts.append(b"")
        body_parts.append(json.dumps({"lang": "ch"}).encode())
        body_parts.append(f"--{boundary}--".encode())
        body = b"\r\n".join(body_parts)

        req = urllib.request.Request(
            f"http://127.0.0.1:{MINERU_API_PORT}/file_parse",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        r = urllib.request.urlopen(req, timeout=120)
        resp = json.loads(r.read())
        warmup_pdf.unlink(missing_ok=True)
        warmup_time = time.time() - warmup_start
        print(f"OK ({warmup_time:.0f}s) — model ready")
    except Exception as e:
        warmup_time = time.time() - warmup_start
        warmup_pdf.unlink(missing_ok=True)
        print(f"skipped ({warmup_time:.0f}s, {type(e).__name__})")
        # Continue even if warmup fails — not critical

    # Run minerU on each pending chunk (with progress tracking)
    # Caller: extract_text_mineru() which is called by ingest.py during Stage 0
    # Data schema: chunk_times (list of float) tracks completed chunk times for ETA estimation
    chunk_times = []  # Track completion times for ETA estimation
    total_start = time.time()

    for ci, (start, end) in enumerate(chunks):
        chunk_key = f"{start}-{end}"
        if chunk_key in stats["completed_chunks"]:
            percent = (ci + 1) * 100 // len(chunks)
            print(f"  [{ci+1:3d}/{len(chunks)}] [{percent:3d}%] pages {start+1}-{end} — (cached)")
            continue

        # Create chunk PDF
        chunk_pdf = out_dir / f"_chunk_{start:04d}-{end:04d}.pdf"
        chunk_pdf.unlink(missing_ok=True)
        try:
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
            new_doc.save(chunk_pdf)
            new_doc.close()
        except Exception as e:
            print(f"  [{ci+1:3d}/{len(chunks)}] pages {start+1}-{end} — FAILED to create chunk: {e}")
            stats["failed_chunks"].append({"chunk": chunk_key, "error": str(e)})
            continue

        # Submit chunk to minerU API (direct HTTP) with progress tracking
        percent = (ci + 1) * 100 // len(chunks)
        # Calculate ETA based on completed chunks
        if chunk_times:
            avg_time = sum(chunk_times) / len(chunk_times)
            remaining_chunks = len(chunks) - ci - 1
            eta_sec = remaining_chunks * avg_time
            eta_str = f"ETA: {int(eta_sec)}s" if eta_sec < 60 else f"ETA: {int(eta_sec/60):.1f}m"
        else:
            eta_str = "computing ETA..."
        print(f"  [{ci+1:3d}/{len(chunks)}] [{percent:3d}%] pages {start+1}-{end} — minerU API ({eta_str})...", end=" ", flush=True)
        t0 = time.time()
        mineru_ok = False
        md_path: Path | None = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2)
            try:
                boundary = "----FormBoundary" + os.urandom(8).hex()
                body_parts = []
                body_parts.append(f"--{boundary}".encode())
                body_parts.append(b'Content-Disposition: form-data; name="files"; filename="chunk.pdf"')
                body_parts.append(b"Content-Type: application/pdf")
                body_parts.append(b"")
                body_parts.append(chunk_pdf.read_bytes())
                data_json = json.dumps({"lang": "ch"})
                body_parts.append(f"--{boundary}".encode())
                body_parts.append(b'Content-Disposition: form-data; name="data"')
                body_parts.append(b"")
                body_parts.append(data_json.encode())
                # Request minerU-extracted figures (not just full-page renders).
                # return_images → base64-encoded images; return_content_list → per-block
                # page_idx so we can map each figure to its source page.
                for field in ("return_images", "return_content_list"):
                    body_parts.append(f"--{boundary}".encode())
                    body_parts.append(f'Content-Disposition: form-data; name="{field}"'.encode())
                    body_parts.append(b"")
                    body_parts.append(b"true")
                body_parts.append(f"--{boundary}--".encode())
                body = b"\r\n".join(body_parts)

                req = urllib.request.Request(
                    f"http://127.0.0.1:{MINERU_API_PORT}/file_parse",
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
                r = urllib.request.urlopen(req, timeout=1200)
                resp = json.loads(r.read())
                if resp.get("status") == "completed":
                    results = resp.get("results", {})
                    # API returns key as original filename (may be stem without .pdf)
                    md = ""
                    for rk in (chunk_pdf.name, chunk_pdf.stem):
                        if rk in results and isinstance(results[rk], dict):
                            md = results[rk].get("md_content", "")
                            if md:
                                break
                    # Fallback: take first result
                    if not md:
                        for rk, rv in results.items():
                            if isinstance(rv, dict):
                                md = rv.get("md_content", "")
                                if md:
                                    break
                    if md:
                        chunk_out = out_dir / f"_chunk_{start:04d}-{end:04d}"
                        chunk_out.mkdir(parents=True, exist_ok=True)
                        md_path = chunk_out / f"{chunk_pdf.stem}.md"
                        md_path.write_text(md, encoding="utf-8")
                        # Harvest minerU-extracted figures (not just full-page renders).
                        # results[rk]["images"] = {basename: "data:image/...;base64,..."}
                        # results[rk]["content_list"] = [{type, img_path, page_idx}, ...]
                        _harvest_mineru_figures(results, start, file_path, config, chunk_out)
                        chunk_time = time.time() - t0
                        chunk_times.append(chunk_time)  # Track for ETA calculation
                        print(f"OK ({chunk_time:.0f}s, {len(md)} chars)")
                        log_event("chunk_complete", chunk=ci+1, total=len(chunks),
                                 elapsed_sec=round(chunk_time, 2), chars=len(md), attempt=attempt+1)
                        mineru_ok = True
                    else:
                        chunk_time = time.time() - t0
                        chunk_times.append(chunk_time)  # Track for ETA calculation
                        print(f"EMPTY ({chunk_time:.0f}s)")
                        log_event("chunk_complete", chunk=ci+1, total=len(chunks),
                                 elapsed_sec=round(chunk_time, 2), chars=0, attempt=attempt+1)
                        mineru_ok = True
                    break
                elif resp.get("status") == "failed":
                    err = resp.get("error_message", resp.get("error", "unknown"))
                    if attempt < 2:
                        print(f"API FAILED (retry {attempt+1}/3): {err[:100]}")
                        continue
                    else:
                        print(f"API FAILED (final): {err[:200]}")
                else:
                    # Poll for completion
                    task_id = resp.get("task_id", "")
                    if task_id:
                        for pi in range(60):
                            time.sleep(5)
                            tr = urllib.request.urlopen(f"http://127.0.0.1:{MINERU_API_PORT}/tasks/{task_id}")
                            td = json.loads(tr.read())
                            if td.get("status") == "completed":
                                tdr = td.get("results", {})
                                md = ""
                                for rk in (chunk_pdf.name, chunk_pdf.stem):
                                    if rk in tdr and isinstance(tdr[rk], dict):
                                        md = tdr[rk].get("md_content", "")
                                        if md: break
                                if not md:
                                    for rk, rv in tdr.items():
                                        if isinstance(rv, dict):
                                            md = rv.get("md_content", "")
                                            if md: break
                                if md:
                                    chunk_out = out_dir / f"_chunk_{start:04d}-{end:04d}"
                                    chunk_out.mkdir(parents=True, exist_ok=True)
                                    md_path = chunk_out / f"{chunk_pdf.stem}.md"
                                    md_path.write_text(md, encoding="utf-8")
                                    _harvest_mineru_figures(tdr, start, file_path, config, chunk_out)
                                    chunk_time = time.time() - t0
                                    chunk_times.append(chunk_time)  # Track for ETA calculation
                                    print(f"OK ({chunk_time:.0f}s, {len(md)} chars)")
                                    mineru_ok = True
                                else:
                                    chunk_time = time.time() - t0
                                    chunk_times.append(chunk_time)  # Track for ETA calculation
                                    print(f"EMPTY ({chunk_time:.0f}s)")
                                    mineru_ok = True
                                break
                            elif td.get("status") == "failed":
                                print(f"TASK FAILED: {td.get('error_message', str(td)[:200])}")
                                break
                        if mineru_ok:
                            break
                    else:
                        print(f"NO TASK ID")
                        continue
            except urllib.error.HTTPError as e:
                err_body = e.read().decode() if e.fp else ""
                if attempt < 2:
                    if e.code >= 500:
                        # Server may need restart
                        print(f"HTTP {e.code} (retry {attempt+1}/3, restarting server)...")
                        api_proc.terminate()
                        try: api_proc.wait(timeout=5)
                        except Exception: api_proc.kill()
                        time.sleep(3)
                        api_proc = _sp.Popen(
                            [str(venv_python), "-m", "mineru.cli.fast_api",
                             "--host", "127.0.0.1", "--port", str(MINERU_API_PORT)],
                            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        )
                        time.sleep(5)
                        continue
                    else:
                        print(f"HTTP {e.code} (retry {attempt+1}/3): {err_body[:100]}")
                        continue
                else:
                    print(f"HTTP {e.code} (final): {err_body[:200]}")
            except Exception as e:
                if attempt < 2:
                    if "Connection refused" in str(e):
                        # Server crashed — restart and retry
                        print(f"Connection failed (retry {attempt+1}/3, restarting server)...")
                        time.sleep(3)
                        api_proc = _sp.Popen(
                            [str(venv_python), "-m", "mineru.cli.fast_api",
                             "--host", "127.0.0.1", "--port", str(MINERU_API_PORT)],
                            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        )
                        time.sleep(8)
                        continue
                    else:
                        print(f"Error (retry {attempt+1}/3): {str(e)[:100]}")
                        continue
                else:
                    print(f"FAILED (final): {str(e)[:200]}")
            break

        if not mineru_ok:
            stats["failed_chunks"].append({"chunk": chunk_key, "error": "minerU API failed after retries"})
            _save_mineru_stats(stats_path, stats)
            log_event("chunk_error", chunk=ci+1, total=len(chunks), error="max retries exceeded")
            w = 64
            lines = [
                f"ALL RETRIES EXHAUSTED — CHUNK PERMANENTLY FAILED",
                f"",
                f"Chunk:  pages {start+1}-{end}",
                f"File:   {chunk_pdf.name}",
                f"",
                f"Action: re-run ingest to retry this chunk, or check",
                f"        _mineru_stats.json for error details",
            ]
            print(f"")
            print(f"  ╔{'═'*w}╗")
            for i, line in enumerate(lines):
                if i == 0:
                    print(f"  ║  ⚠️  {line:<{w-5}} ║")
                else:
                    print(f"  ║     {line:<{w-4}} ║")
            print(f"  ╚{'═'*w}╝")
            print(f"")
            if len(stats["failed_chunks"]) > len(chunks) * 0.3:
                doc.close()
                _kill_mineru_servers()
                raise RuntimeError(
                    f"minerU OCR: {len(stats['failed_chunks'])}/{len(chunks)} chunks failed. "
                    f"Aborting. Check _mineru_stats.json in extract_tmp_dir."
                )
            continue

        # API already wrote .md — read it
        if md_path is None or not md_path.exists():
            print(f"  [{ci+1:3d}/{len(chunks)}] FAILED — no output file")
            stats["failed_chunks"].append({"chunk": chunk_key, "error": "no .md output from API"})
            _save_mineru_stats(stats_path, stats)
            continue
        md_text = md_path.read_text(encoding="utf-8")

        # minerU-extracted figures are already saved by _harvest_mineru_figures()
        # (called immediately after the API response).  No full-page renders needed —
        # minerU VLM already provides OCR text + independently extracted figures.
        slug = _media_slug(file_path, config)
        media_dir = config.wiki_dir / "media" / slug
        media_dir.mkdir(parents=True, exist_ok=True)

        _save_mineru_chunk_text(md_text, start, end, out_dir, stats, [])
        stats["completed_chunks"].append(chunk_key)
        _save_mineru_stats(stats_path, stats)

        print(f"  [{ci+1:3d}/{len(chunks)}] done — {len(md_text)} chars")
        chunk_pdf.unlink(missing_ok=True)

    doc.close()

    # Stop API server
    api_proc.terminate()
    try:
        api_proc.wait(timeout=10)
    except Exception:
        api_proc.kill()

    # Assemble full text from per-page files
    page_nums = list(range(total_pages))
    full_text = _assemble_ocr_text(out_dir, page_nums)
    total_imgs = sum(len(v) for v in stats.get("images", {}).values())
    print(f"[ocr] Done — {len(full_text):,} chars OCR text, {total_imgs} images extracted")

    # Write _manifest.json with minerU-extracted figures.
    # No full-page renders — they were OCR intermediates and have been removed.
    slug = _media_slug(file_path, config)
    media_dir = config.wiki_dir / "media" / slug
    manifest_path = media_dir / "_manifest.json"

    extracted_figures: list[dict] = []
    for f in sorted(media_dir.glob("p*-mineru_*.*")):
        page_num = 0
        m = re.match(r"p(\d+)-mineru_", f.stem)
        if m:
            page_num = int(m.group(1))
        extracted_figures.append({
            "filename": f.name, "page": page_num,
            "path": str(f.relative_to(config.wiki_root)),
        })

    if extracted_figures:
        _write_manifest(manifest_path, "mineru-ocr", file_path, extracted_figures)
        print(f"[ocr] {len(extracted_figures)} extracted figures → _manifest.json")

        pending = _find_uncaptioned_mineru_images(media_dir)
        if pending and config.caption_api_key:
            _caption_images(pending, config, media_dir, source_label="minerU-extracted", batch_size=6)
    else:
        _write_manifest(manifest_path, "mineru-ocr", file_path, [])
        print("[ocr] No extracted figures — empty manifest written")

    return full_text


def _save_mineru_stats(stats_path: Path, stats: dict) -> None:
    """Atomically persist minerU stats for crash recovery."""
    tmp = stats_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(stats_path)


def _find_uncaptioned_mineru_images(media_dir: Path) -> list[dict]:
    """Find minerU images in wiki/media/<subpath>/ that need captions."""
    if not media_dir.exists():
        return []
    imgs = []
    for f in sorted(media_dir.iterdir()):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
            cap_path = media_dir / (f.name + ".caption.txt")
            if not cap_path.exists() or cap_path.stat().st_size < 20:
                imgs.append({"filename": f.name, "path": str(f)})
    return imgs


# ---------- Stage 1.3: Unified image captioning (Path A + Path B merged) ----------

CAPTION_BATCH_SIZE = int(os.environ.get("CAPTION_BATCH_SIZE", "8"))
CAPTION_MAX_WORKERS = int(os.environ.get("CAPTION_MAX_WORKERS", "6"))


def _is_caption_failed(text: str) -> bool:
    """Detect VLM failure responses that shouldn't be treated as valid captions."""
    if not text or len(text) < 15:
        return True
    failure_markers = ["解析失败", "无法识别", "unable to", "cannot describe",
                       "抱歉", "sorry", "I can't", "not clear", "无法描述"]
    text_lower = text.lower()
    return any(m.lower() in text_lower for m in failure_markers)


def _caption_images(images: list[dict], config: Config, media_dir: Path,
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
    if not images:
        return 0
    if not config.caption_api_key:
        print(f"[caption] Skipped — no API key for caption provider")
        return 0

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
                if _is_caption_failed(existing):
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
            executor.submit(_caption_one_batch, b, i, len(batches), config, media_dir): i
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
                    if _is_caption_failed(caption_text):
                        caption_text = f"[待重试] 图片 {batch[idx]['filename']}，尺寸 {batch[idx].get('width','?')}×{batch[idx].get('height','?')}"
                    cap_path = media_dir / (batch[idx]["filename"] + ".caption.txt")
                    cap_path.write_text(caption_text, encoding="utf-8")
                    captioned += 1
            print(f"  [{bi+1}/{len(batches)}] {len(captions)}/{len(batch)} captions ✓")

    print(f"[caption] Done — {captioned} captions written")
    return captioned


def _preprocess_image_for_caption(img_path: Path, max_dim: int = 1568) -> str:
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


def _caption_one_batch(batch: list[dict], batch_idx: int, total_batches: int,
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
        img_data = _preprocess_image_for_caption(img_path)
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
            return None, f"{type(e).__name__}: {e}"
    return None, "max-retries"


MINERU_IMG_MIN_WIDTH = 200
MINERU_IMG_MIN_HEIGHT = 150


def _harvest_mineru_figures(results: dict, page_offset: int, raw_file: Path,
                             config: Config, chunk_out: Path) -> list[dict]:
    """Harvest minerU-extracted figures from API response and save to wiki/media.

    minerU VLM extracts individual figures/charts/tables from within scanned pages.
    These are the ONLY images produced for scanned PDFs (no full-page renders).
    This function:
    1. Reads {basename: base64-data-uri} from results[*]["images"]
    2. Reads content_list from results[*]["content_list"] to map images → pages
    3. Saves each figure to wiki/media/<slug>/ as p{page:04d}-mineru_{n}.{ext}
    4. Returns metadata list for _figures.json

    Called immediately after a successful minerU API response for each chunk.
    """
    import base64 as _b64
    slug = _media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug
    media_dir.mkdir(parents=True, exist_ok=True)

    # Collect all images and content_list items across result entries
    all_images: dict[str, str] = {}        # basename → data URI
    all_content: list[dict] = []

    for _rk, rv in (results or {}).items():
        if not isinstance(rv, dict):
            continue
        imgs = rv.get("images")
        if isinstance(imgs, dict):
            all_images.update(imgs)
        cl = rv.get("content_list")
        if isinstance(cl, list):
            all_content.extend(cl)

    if not all_images:
        return []

    # Build page→[image_basename] mapping from content_list.
    # Each IMAGE block has: type="image", img_path="images/hash.png", page_idx=N
    page_figs: dict[int, list[str]] = {}
    for block in all_content:
        block_type = block.get("type", "")
        if block_type not in ("image", "chart"):
            continue
        img_path = block.get("img_path", "")
        if not img_path:
            continue
        img_basename = os.path.basename(img_path)
        page_idx = block.get("page_idx", 0)
        abs_page = page_offset + int(page_idx)
        page_figs.setdefault(abs_page, []).append(img_basename)

    # If content_list mapping produced nothing, fall back: assign all images
    # to the chunk-start page so they aren't lost.
    if not page_figs and all_images:
        page_figs[page_offset] = list(all_images.keys())

    # Save images and build metadata
    saved: list[dict] = []
    img_counter: dict[int, int] = {}

    for page_num in sorted(page_figs):
        for img_name in page_figs[page_num]:
            b64_uri = all_images.get(img_name)
            if not b64_uri:
                continue
            # Parse data URI: "data:image/png;base64,XXXX"
            if "," in b64_uri:
                _header, data = b64_uri.split(",", 1)
            else:
                data = b64_uri

            idx = img_counter.get(page_num, 0) + 1
            img_counter[page_num] = idx
            # Determine extension from the original image name or data URI header
            if img_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                ext = img_name.rsplit(".", 1)[-1].lower()
            else:
                ext = "png"
            filename = f"p{page_num:04d}-mineru_{idx}.{ext}"
            out_path = media_dir / filename

            if not out_path.exists():
                try:
                    out_path.write_bytes(_b64.b64decode(data))
                except Exception:
                    continue

            # Get dimensions if possible; skip tiny fragments (formulas, icons)
            w, h = 0, 0
            try:
                from PIL import Image
                im = Image.open(out_path)
                w, h = im.size
                im.close()
                if w < MINERU_IMG_MIN_WIDTH or h < MINERU_IMG_MIN_HEIGHT:
                    out_path.unlink(missing_ok=True)
                    continue
            except Exception:
                pass

            saved.append({
                "filename": filename,
                "page": page_num,
                "path": str(out_path.relative_to(config.wiki_root)),
                "width": w, "height": h,
                "source": "mineru-extracted",
            })

    if saved:
        # Persist to chunk_out so the per-chunk stats can reference them
        harvest_path = chunk_out / "_mineru_figures.json"
        harvest_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"[mineru-figures] {len(saved)} extracted figures saved to {media_dir.name}")

    return saved


def _collect_mineru_images(img_dir: Path, page_offset: int) -> list[dict]:
    """Collect extracted images from minerU output. Filters small fragments (formulas)."""
    if not img_dir.exists():
        return []
    try:
        from PIL import Image
    except ImportError:
        return []  # can't verify sizes, skip
    imgs = []
    for img_file in sorted(img_dir.iterdir()):
        if img_file.suffix.lower() in (".jpg", ".jpeg", ".png"):
            try:
                im = Image.open(img_file)
                w, h = im.size
                im.close()
                if w >= MINERU_IMG_MIN_WIDTH and h >= MINERU_IMG_MIN_HEIGHT:
                    imgs.append({
                        "filename": img_file.name,
                        "source_path": str(img_file),
                        "width": w, "height": h,
                        "page_hint": page_offset,
                    })
            except Exception:
                pass
    return imgs


def _save_mineru_chunk_text(md_text: str, start: int, end: int, out_dir: Path,
                             stats: dict, images: list[dict]) -> None:
    """Save minerU chunk output as per-page text files.

    minerU outputs continuous markdown. We heuristically split by markdown headers
    and assign pages proportionally across the chunk range.
    """
    # Simplify: save the entire chunk as a single text block for page range
    # Strip image references from text (they're in separate files)
    clean = re.sub(r'!\[.*?\]\(images/.*?\)', '', md_text)
    clean = re.sub(r'<details>.*?</details>', '', clean, flags=re.DOTALL)
    clean = re.sub(r'\n{3,}', '\n\n', clean)

    # Split by # headers as rough page boundaries
    sections = re.split(r'\n(?=# )', clean)
    pages_in_chunk = end - start
    if len(sections) >= pages_in_chunk:
        # Assign one section per page
        for i in range(pages_in_chunk):
            pn = start + i
            text = sections[i].strip() if i < len(sections) else ""
            if text:
                page_path = out_dir / f"p{pn+1:04d}.txt"
                page_path.write_text(text, encoding="utf-8")
                stats.setdefault("completed_pages", {})[str(pn)] = len(text)
    else:
        # Fewer sections than pages — distribute
        chars_per_page = len(clean) // max(pages_in_chunk, 1)
        for i in range(pages_in_chunk):
            pn = start + i
            seg_start = i * chars_per_page
            seg_end = seg_start + chars_per_page if i < pages_in_chunk - 1 else len(clean)
            text = clean[seg_start:seg_end].strip()
            if text:
                page_path = out_dir / f"p{pn+1:04d}.txt"
                page_path.write_text(text, encoding="utf-8")
                stats.setdefault("completed_pages", {})[str(pn)] = len(text)

    # Record images
    if images:
        if "images" not in stats:
            stats["images"] = {}
        for img in images:
            pn = img.get("page_hint", start)
            stats["images"].setdefault(str(pn), []).append(img["filename"])


def _copy_mineru_images(images: list[dict], config: Config, raw_file: Path) -> None:
    """Copy minerU extracted images to wiki/media/<raw-subpath>/<slug>/ for Stage 3.2."""
    if not images:
        return
    import shutil
    slug = _media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug
    media_dir.mkdir(parents=True, exist_ok=True)
    for img in images:
        src = Path(img["source_path"])
        if src.exists():
            dst = media_dir / img["filename"]
            if not dst.exists():
                shutil.copy2(src, dst)


def _assemble_ocr_text(out_dir: Path, page_nums: list[int]) -> str:
    """Concatenate per-page OCR text files in page order."""
    parts = []
    for pn in page_nums:
        page_path = out_dir / f"p{pn+1:04d}.txt"
        if page_path.exists():
            parts.append(page_path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


# ---------- Stage 1.2: Image extraction ----------

def _extract_images_from_office(raw_file: Path, media_dir: Path, manifest_path: Path,
                                 min_size: int = 100) -> dict:
    """Extract embedded images from PPTX/DOCX via zipfile.

    NashSU parity: extractAndSaveSourceImages handles PPTX/DOCX/PDF.
    Uses Python stdlib zipfile — no external deps needed.
    """
    import zipfile as _zf
    import io as _io

    fmt = raw_file.suffix.lower().lstrip(".")
    print(f"[stage 1.2] Extracting embedded images from {fmt.upper()}...")

    # Image dir inside the ZIP
    media_prefix = "ppt/media/" if fmt == "pptx" else "word/media/"
    img_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".svg"}

    all_images: list[dict] = []
    seen_hashes: set[str] = set()

    try:
        with _zf.ZipFile(raw_file, "r") as zf:
            for name in zf.namelist():
                if not name.startswith(media_prefix):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in img_exts:
                    continue

                data = zf.read(name)
                if len(data) < min_size:
                    continue

                # Dedup by SHA-256
                fhash = hashlib.sha256(data).hexdigest()
                if fhash in seen_hashes:
                    continue
                seen_hashes.add(fhash)

                # Determine page context if available (from slide/word numbering)
                # PPTX: ppt/slides/slideN.xml → N; DOCX: no direct page mapping
                page = 0
                rel_parts = name.split("/")
                # For PPTX, try to extract slide number from parent dir structure
                if fmt == "pptx":
                    # Images are in ppt/media/, referenced from ppt/slides/slideN.xml
                    # We can't easily map back without parsing XML, so use 0
                    pass

                filename = Path(name).name
                out_path = media_dir / filename
                # Avoid overwriting: append hash prefix if collision
                if out_path.exists():
                    stem, ext2 = out_path.stem, out_path.suffix
                    out_path = media_dir / f"{stem}_{fhash[:6]}{ext2}"

                out_path.write_bytes(data)

                all_images.append({
                    "filename": out_path.name,
                    "page": page,
                    "size": len(data),
                    "sha256": fhash,
                    "format": ext.lstrip("."),
                })

    except Exception as e:
        print(f"[stage 1.2] {fmt.upper()} image extraction failed: {e}")
        return {"count": 0, "error": str(e)}

    # Write manifest
    manifest_data = {
        "source": str(raw_file),
        "format": fmt,
        "total_images": len(all_images),
        "images": all_images,
    }
    manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[stage 1.2] {fmt.upper()}: {len(all_images)} images → {media_dir}")
    return {"count": len(all_images), "media_dir": str(media_dir),
            "manifest": str(manifest_path), "images": all_images}


def stage_1_2_extract_images(raw_file: Path, config: Config, min_size: int = 100) -> dict:
    """Extract embedded images from PDF / PPTX / DOCX.

    PDF: PyMuPDF get_images().  PPTX/DOCX: zipfile internal media/ directory
    (NashSU parity: extractAndSaveSourceImages covers all three formats).

    Returns: {"count": int, "media_dir": str, "manifest": str, "images": list}
    """
    suffix = raw_file.suffix.lower()
    slug = _media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug

    # Skip if already done
    manifest_path = media_dir / "_manifest.json"
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"[stage 1.2] (cached) {m.get('total_images', 0)} images in {media_dir}")
            return {"count": m.get("total_images", 0), "cached": True, "media_dir": str(media_dir),
                    "manifest": str(manifest_path), "images": m.get("images", [])}
        except Exception:
            pass  # corrupt manifest, re-extract

    media_dir.mkdir(parents=True, exist_ok=True)

    # ── PPTX / DOCX extraction (NashSU parity) ──
    if suffix in (".pptx", ".docx"):
        return _extract_images_from_office(raw_file, media_dir, manifest_path, min_size)

    # ── PDF extraction ──
    try:
        import fitz
    except ImportError:
        print("[stage 1.2] PyMuPDF not installed — skipping image extraction")
        return {"count": 0, "skipped": True, "reason": "pymupdf-not-installed"}

    print(f"[stage 1.2] Extracting embedded images from PDF...")

    doc = fitz.open(raw_file)
    all_images: list[dict] = []
    page_images: list[list[dict]] = []

    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            img_list = page.get_images(full=True)
            imgs = []
            for img in img_list:
                xref = img[0]
                bi = doc.extract_image(xref)
                if bi["width"] >= min_size and bi["height"] >= min_size:
                    # Use Pixmap to get correctly-oriented image (respects PDF transform matrix).
                    # doc.extract_image() gives raw bytes which may be rotated/flipped.
                    try:
                        pix = fitz.Pixmap(doc, xref)
                        if pix.n - pix.alpha > 3:  # CMYK or other colorspace → convert to RGB
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        img_bytes = pix.tobytes("png")
                        imgs.append({"xref": xref, "ext": "png", "bytes": img_bytes,
                                     "width": pix.width, "height": pix.height})
                    except Exception:
                        # Fallback to raw bytes if Pixmap fails (e.g., JBIG2, JPEG2000)
                        imgs.append({"xref": xref, "ext": bi["ext"], "bytes": bi["image"],
                                     "width": bi["width"], "height": bi["height"]})
            page_images.append(imgs)
            if imgs:
                all_images.extend(imgs)
                print(f"  page {page_num}: {len(imgs)} image(s)")
    finally:
        doc.close()

    if not all_images:
        print(f"[stage 1.2] No embedded images found (or all < {min_size}px)")
        _write_manifest(manifest_path, str(raw_file), raw_file, [])
        return {"count": 0, "media_dir": str(media_dir), "manifest": str(manifest_path), "images": []}

    # Deduplicate by sha256
    seen: dict[str, dict] = {}
    for img in all_images:
        sha = hashlib.sha256(img["bytes"]).hexdigest()
        if sha not in seen:
            img["sha256"] = sha
            seen[sha] = img

    deduped = list(seen.values())
    print(f"[stage 1.2] Raw: {len(all_images)}, after dedup: {len(deduped)}")

    # Save files and build metadata
    xref_to_page: dict[int, int] = {}
    for pn, imgs in enumerate(page_images):
        for img in imgs:
            xref_to_page[img["xref"]] = pn

    saved: list[dict] = []
    img_idx_per_page: dict[int, int] = {}
    for img in deduped:
        pn = xref_to_page.get(img["xref"], 0)
        fig_idx = img_idx_per_page.get(pn, 0) + 1
        img_idx_per_page[pn] = fig_idx
        filename = f"p{pn}-fig{fig_idx}.{img['ext']}"
        out_path = media_dir / filename
        out_path.write_bytes(img["bytes"])
        rel_path = str(out_path.relative_to(config.wiki_root))
        saved.append({
            "page": pn, "img_idx_in_page": fig_idx, "filename": filename,
            "path": rel_path, "width": img["width"], "height": img["height"],
            "sha256": img["sha256"], "xref": img["xref"],
        })

    _write_manifest(manifest_path, str(raw_file), raw_file, saved)
    print(f"[stage 1.2] Done — {len(saved)} images saved to {media_dir}")
    return {"count": len(saved), "media_dir": str(media_dir), "manifest": str(manifest_path), "images": saved}


def _raw_type_subdir(raw_file: Path, config: Config) -> str:
    """Return the raw/-relative parent directory for this file.

    raw/Book/Foo.pdf           → book
    raw/Datasheet/05_AMP/Bar.pdf → datasheet/05_AMP
    """
    try:
        rel = raw_file.relative_to(config.raw_root)
    except ValueError:
        return ""
    parent = str(rel.parent)
    return parent if parent != "." else ""


def _media_slug(raw_file: Path, config: Config) -> str:
    """Derive media directory path from raw file path, mirroring raw/ structure.

    raw/Book/Foo.pdf           → book/Foo
    raw/Datasheet/05_AMP/Bar.pdf → datasheet/05_AMP/Bar
    """
    try:
        rel = raw_file.relative_to(config.raw_root)
    except ValueError:
        return raw_file.stem
    parent = rel.parent
    stem = rel.stem
    return str(parent / stem) if str(parent) != "." else stem


def _write_manifest(manifest_path: Path, source: str, raw_file: Path, images: list[dict]) -> None:
    manifest = {
        "source": source,
        "source_sha256": file_sha256(raw_file),
        "total_images": len(images),
        "images": images,
    }
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(manifest_path)


# ---------- Stage 1.3: Image captioning ----------

def stage_1_3_caption_images(config: Config, stage_1_2_result: dict, batch_size: int = CAPTION_BATCH_SIZE) -> dict:
    """Caption extracted images using unified caption pipeline (Path A + Path B merged).

    Thin wrapper around _caption_images() for backward compatibility with the
    Stage 1.3 pipeline checkpoint. Internal implementation delegates to the
    unified function which supports both PyMuPDF-extracted images (Path A)
    and minerU-extracted images (Path B), with parallel batch dispatch."""
    images = stage_1_2_result.get("images", [])
    if not images:
        print("[stage 1.3] No images to caption — skipping")
        return {"captioned": 0, "total": 0}
    if not config.caption_api_key:
        print("[stage 1.3] Skipped — no API key for caption provider")
        return {"captioned": 0, "total": len(images), "skipped": True, "reason": "no-api-key"}

    media_dir = Path(stage_1_2_result["media_dir"])
    captioned = _caption_images(images, config, media_dir,
                                source_label="pyMuPDF",
                                batch_size=batch_size)
    return {"captioned": captioned, "total": len(images)}


CAPTION_SYSTEM_PROMPT = (
    "你是硬件知识库的图像解读专家。每次给你若干张图，按图顺序逐张描述："
    "1-3 句中文，不超过 100 字。聚焦：图类型（电路/波形/框图/PCB/曲线/参数表/公式/实物/示意等）"
    "+ 关键内容 + 关键参数/标注。"
    "\n\n输出格式：严格按以下 JSON 数组：\n```json\n[\n  {\"idx\": 1, \"caption\": \"...\"},\n"
    "  {\"idx\": 2, \"caption\": \"...\"},\n  ...\n]\n```\n\n"
    "每个对象都要有，idx 与图顺序一致。即使图不清楚也尽量给个最合理的简短描述。"
)


def check_text_quality(text: str, source_name: str = "") -> dict:
    """Pre-ingest text quality gate (pre-Stage 1.1).

    Detects garbled text from custom font encoding (e.g. Fuqua book:
    500+ chars/page but all unreadable). Returns a quality report dict.
    Caller should warn if quality is poor; ingest pipeline may choose to
    abort or re-route to OCR.

    Checks:
      - C0 control character ratio (>1% → likely garbled)
      - Printable ASCII ratio (<80% → unusual for English technical books)
      - CJK ratio vs expected language (if source_expected_lang is set)
    """
    if not text:
        return {"status": "empty", "c0_ratio": 0, "printable_ratio": 0}

    _C0_RE = __import__('re').compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    _PRINTABLE_RE = __import__('re').compile(r'[A-Za-z0-9\s.,;:!?()[\]{}/\\@#$%^&*+=_\-~`\'"<>|]')
    _CJK_RE = __import__('re').compile(r'[一-鿿㐀-䶿]')

    sample = text[:50000]  # first 50K chars is sufficient
    n = len(sample)
    c0_count = len(_C0_RE.findall(sample))
    printable_count = len(_PRINTABLE_RE.findall(sample))
    cjk_count = len(_CJK_RE.findall(sample))

    c0_ratio = c0_count / n
    printable_ratio = printable_count / n
    cjk_ratio = cjk_count / n

    # Heuristic: classify quality
    issues = []
    if c0_ratio > 0.01:
        issues.append(f"garbled: {c0_ratio:.1%} C0 control chars (font encoding failure?)")
    if printable_ratio < 0.5 and cjk_ratio < 0.1:
        issues.append(f"low-readability: only {printable_ratio:.1%} printable ASCII and {cjk_ratio:.1%} CJK")
    if c0_ratio > 0.05:
        issues.append("SEVERE: text appears to be corrupted — LLM digest will be useless")

    status = "ok" if not issues else ("severe" if c0_ratio > 0.05 else "warning")

    report = {
        "status": status,
        "c0_ratio": round(c0_ratio, 4),
        "printable_ratio": round(printable_ratio, 4),
        "cjk_ratio": round(cjk_ratio, 4),
        "sample_size": n,
        "issues": issues,
    }

    if issues:
        print(f"[quality] {source_name}: {status.upper()} — {'; '.join(issues)}")

    return report

