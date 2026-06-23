"""Extract, caption, and validate raw source files (Phase 1).

Pipeline stages:
  Phase 1 Stage 1.1: Extract text from PDF/PPTX/DOCX (minerU pipeline for text PDFs, minerU VLM for scanned)
  Phase 1 Stage 1.2: Extract embedded images from PDF
  Phase 1 Stage 1.3: Generate image captions via VLM

Extracted from ingest.py on 2026-06-18. Refactored 2026-06-21 for explicit stage naming.
Imports shared infrastructure from _core.
"""
from __future__ import annotations

import base64
import fcntl
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

MINERU_MAX_CONCURRENT = 1
MINERU_API_PORT = int(os.environ.get("MINERU_API_PORT", "19999"))
CAPTION_BATCH_SIZE = int(os.environ.get("CAPTION_BATCH_SIZE", "8"))
MINERU_LOCK_FILE = Path.home() / ".cache" / "improved-wiki" / ".mineru.lock"
MINERU_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

# Minimum image dimensions for mineru-extracted figures. Images below this are
# treated as noise (1x1/2x2 artifacts, stray pixels) and dropped. Threshold is
# deliberately very conservative: tiny formula strips (29-70px tall) are
# valuable because MiniMax-M3 transcribes them to LaTeX/Unicode ~81% of the
# time, so we must NOT filter them out. See image-caption-strategy.md.
MINERU_IMG_MIN_WIDTH = int(os.environ.get("MINERU_IMG_MIN_WIDTH", "20"))
MINERU_IMG_MIN_HEIGHT = int(os.environ.get("MINERU_IMG_MIN_HEIGHT", "20"))

# parse_method override for the current minerU run ("ocr" | None). Set by
# stage_1_1_extract_text when fitz sampling detects a garbled-font PDF (text
# layer exists but is custom-encoded junk — auto would read it via txt and
# produce garbage, so force OCR). Read by _stage_1_1_scanned_build_parse_body
# to emit a parse_method Form field. Safe as module-level state because
# _stage_1_1_acquire_mineru_lock serializes all minerU runs — only one ingest
# touches this at a time. Reset to None after each run.
_PARSE_METHOD_OVERRIDE: str | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Utility Functions
# ══════════════════════════════════════════════════════════════════════════════

def _is_image_too_small(width: int, height: int) -> bool:
    """Check if image is too small to keep.

    Filters only true noise (stray 1x1/2x2 pixel artifacts). Does NOT filter
    formula strips — tiny formula images (29-70px tall) are valuable because
    MiniMax-M3 transcribes them to LaTeX/Unicode ~81% of the time. The
    threshold is intentionally very low (default 20px) to avoid throwing away
    recoverable formula content.
    """
    return width < MINERU_IMG_MIN_WIDTH or height < MINERU_IMG_MIN_HEIGHT


# ---------- Text extraction ----------

def _stage_1_1_mineru_find_md(out_dir: Path, stem: str) -> Path:
    """Locate the .md file produced by minerU under out_dir.

    minerU's output path varies by version and backend (vlm/auto/txt subdirs,
    stem-prefixed filenames). Preference order: vlm > auto > latest modified.
    Raises RuntimeError with a directory listing if no .md is found, so the
    caller can see what minerU actually produced for debugging.
    """
    candidates = sorted(out_dir.rglob(f"{stem}*.md"))
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
        all_output = sorted(out_dir.rglob("*"))
        found = [str(p.relative_to(out_dir)) for p in all_output if p.is_file()]
        raise RuntimeError(
            f"minerU finished but no .md output found for stem '{stem}'. "
            f"Actual output under {out_dir}: {found[:20]}"
        )
    print(f"[mineru] Found output: {md_out.relative_to(out_dir)}")
    return md_out



def _clean_mineru_latex(text: str) -> str:
    """Clean minerU's noisy LaTeX formula output (font-dependent noise).

    minerU wraps individual chars in \\mathsf{}/\\mathrm{}/\\mathcal{} and
    inserts spurious spaces between digits/letters. Operates ONLY inside
    $...$ / $$...$$ formula spans to avoid touching prose. Conservative:
    skips ambiguous cases (e.g. \\nu which may be Greek nu or voltage v).

    Measured on AFE439A2 (TI datasheet font): 84% -> 1% noise.
    See ~/Desktop/formula-test/公式识别调研结论.md.
    """
    import re

    def _join_single_chars(content: str) -> str:
        """Join space-separated single-char tokens: 'D D' -> 'DD'."""
        parts = content.split()
        if len(parts) > 1 and all(len(part) == 1 for part in parts):
            return "".join(parts)
        return content

    def _clean_formula(s: str) -> str:
        # \mathsf { ... } -> strip wrapper, join single-char tokens inside
        s = re.sub(r"\\mathsf\s*\{\s*([^{}]+?)\s*\}",
                   lambda m: _join_single_chars(m.group(1)), s)
        # \mathrm { X } single char -> X  (keep multi-char \mathrm{system})
        s = re.sub(r"\\mathrm\s*\{\s*(\S)\s*\}", r"\1", s)
        # \mathcal { X } single char -> X
        s = re.sub(r"\\mathcal\s*\{\s*(\S)\s*\}", r"\1", s)
        # \vee / \bigvee -> V  (voltage variable; logic-or rare in eng docs)
        s = re.sub(r"\\bigvee\b", r"V", s)
        s = re.sub(r"\\vee\b", r"V", s)
        # \sf followed by space -> remove
        s = re.sub(r"\\sf\s+", r"", s)
        # Join digits/dots separated by spaces: "0 . 0 0 1 6" -> "0.0016"
        s = re.sub(r"(?<=[\d.])\s+(?=[\d.])", r"", s)
        # Inside _{...} / ^{...}: join single-char tokens
        def _join_braces(m):
            inner = m.group(2)
            parts = inner.split()
            if len(parts) > 1 and all(len(part) == 1 for part in parts):
                return m.group(1) + "".join(parts) + "}"
            return m.group(0)
        s = re.sub(r"([_\^])\{\s*([^{}]+?)\s*\}", _join_braces, s)
        # Collapse multiple spaces
        s = re.sub(r"  +", r" ", s)
        return s

    # Apply only inside $$...$$ and $...$ spans (preserve prose).
    text = re.sub(r"\$\$[^\$]+\$\$",
                  lambda m: "$$" + _clean_formula(m.group(0)[2:-2]) + "$$", text)
    text = re.sub(r"\$[^\$\n]+\$",
                  lambda m: "$" + _clean_formula(m.group(0)[1:-1]) + "$", text)
    return text


def _stage_1_1_extract_text_mineru_pipeline_impl(file_path: Path, config: Config) -> str:
    """Extract text from a text-based PDF via minerU pipeline backend (no OCR).

    Uses `-b pipeline -m txt`: layout-aware text extraction that preserves
    tables (HTML <table> with rowspan/colspan), formulas (LaTeX), and figure
    regions (image blocks with captions). Replaces the former PyMuPDF
    get_text() path which lost all table/formula/figure structure.

    Comparison (AFE439A2 datasheet, 69 pages): PyMuPDF -> 0 tables / 0 formulas
    / 2 images; minerU pipeline -> 73 tables / 7 formulas / 157 images.
    Trade-off: ~180s vs 0.17s per file (model loading dominates; page
    processing is ~3-4s). See ~/Desktop/afe439a2-compare/对比报告.md.
    """
    mineru_bin = Path.home() / ".venv" / "bin" / "mineru"
    if not mineru_bin.exists():
        raise RuntimeError(f"minerU CLI not found at {mineru_bin}")

    # Per-stem output dir (same convention as the scanned/VLM path) so Stage
    # 1.2 image extraction can locate minerU's images via the shared
    # `extract_tmp_dir/<stem>` lookup in ingest.py. minerU nests `<stem>/txt/`
    # under out_dir automatically.
    out_dir = config.extract_tmp_dir / file_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = file_path.stem

    cmd = [
        str(mineru_bin), "-p", str(file_path), "-o", str(out_dir),
        "-b", "pipeline", "-m", "txt", "-l", "ch",
    ]
    print(f"[pipeline] Running minerU: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"minerU pipeline failed: {result.stderr[-500:]}")

    md_out = _stage_1_1_mineru_find_md(out_dir, stem)
    return _clean_mineru_latex(md_out.read_text(encoding="utf-8"))


def _stage_1_1_extract_text_mineru_pipeline(file_path: Path, config: Config) -> str:
    """Lock-wrapped entry point for minerU pipeline extraction.

    minerU loads multi-GB models into memory on each invocation; concurrent
    runs cause OOM. The file lock serializes all minerU backends (pipeline +
    VLM share the same lock, see _stage_1_1_extract_text_scanned_locked).
    """
    lock_fd = _stage_1_1_acquire_mineru_lock()
    try:
        return _stage_1_1_extract_text_mineru_pipeline_impl(file_path, config)
    finally:
        _stage_1_1_release_mineru_lock(lock_fd)


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
    API server (mineru.cli.fast_api) + /file_parse per 50-page chunk, backend
    hybrid-engine (server default), parse_method auto — hybrid-engine routes
    per-page (text layer present → txt, no OCR; absent → VLM OCR). The former
    text/scanned/mixed branching is retired: hybrid-engine/auto does that
    routing internally, so an external fitz classification would be redundant.

    The ONE fitz-based override: a garbled-font PDF (text layer exists but is
    custom-encoded garbage, e.g. the Fuqua book) would be misread by auto — it
    sees a text layer and reads it via txt, producing junk. _stage_1_1_sample_pdf
    detects this (C0 control-char ratio > 1%) and the caller forces
    parse_method=ocr so hybrid-engine OCRs instead of reading the garbage layer.
    This is the sole reason fitz sampling is retained (detection only, NOT
    extraction).

    txt/md/pptx/docx bypass minerU entirely.

    Returns (text, method_label). method_label always contains "mineru" for PDFs
    (the validator keys on that): "mineru-api" (auto), "mineru-api-ocr"
    (garbled-forced OCR), suffixed "-low-quality" when <2000 chars extracted.

    Backend choice rationale (verified 2026-06-23 on Wu text PDF + Huang scanned
    PDF): hybrid-engine matches or beats pipeline/vlm-engine on both text and
    scanned — identical CJK text, equal block-formula capture, 2.5x more inline
    formulas on scanned vs pipeline. The `mineru -b pipeline` CLI also still
    hits a 502 bug in 3.4.0; set IMPROVED_WIKI_PIPELINE_CLI=1 to retry it
    (non-garbled only — garbled must use the API path to force parse_method=ocr).
    """
    if file_path.suffix.lower() in {".txt", ".md"}:
        return file_path.read_text(encoding="utf-8"), "plain-text"
    if file_path.suffix.lower() in {".pptx", ".docx"}:
        return _stage_1_1_extract_text_office(file_path), f"zipfile-{file_path.suffix.lower().lstrip('.')}"
    if file_path.suffix.lower() != ".pdf":
        raise ValueError(f"Unsupported file type: {file_path.suffix}")

    # fitz sampling: garbled-font detection + diagnostics. NOT content extraction.
    avg_chars, is_garbled, img_ratio = _stage_1_1_sample_pdf(file_path)
    print(f"[extract] PDF sample: avg {avg_chars:.0f} chars/page, img_ratio={img_ratio:.2f}, garbled={is_garbled}")

    # Garbled text layer → force OCR (auto would read the garbage layer via txt).
    # Otherwise auto lets hybrid-engine route per-page (txt vs VLM OCR).
    parse_method = "ocr" if is_garbled else "auto"
    base_label = "mineru-api-ocr" if is_garbled else "mineru-api"

    # Set the per-run override read by _stage_1_1_scanned_build_parse_body.
    # Safe as module-level state: _stage_1_1_acquire_mineru_lock serializes runs.
    global _PARSE_METHOD_OVERRIDE
    _PARSE_METHOD_OVERRIDE = parse_method
    try:
        if os.environ.get("IMPROVED_WIKI_PIPELINE_CLI") and not is_garbled:
            # Opt-in (broken) pipeline CLI — non-garbled only; garbled needs the
            # API path to force parse_method=ocr.
            try:
                text = _stage_1_1_extract_text_mineru_pipeline(file_path, config)
                method = "mineru-pipeline"
            except Exception as e:
                print(f"[extract] minerU pipeline CLI failed ({e}) -- falling back to API path")
                text = _stage_1_1_extract_text_scanned(file_path, config)
                method = base_label
        else:
            text = _stage_1_1_extract_text_scanned(file_path, config)
            method = base_label
    finally:
        _PARSE_METHOD_OVERRIDE = None

    # Universal quality guard (formerly scanned/mixed-only).
    if len(text) < 2000:
        print(f"[extract] ⚠️  Only {len(text)} chars extracted -- quality may be poor")
        method = f"{method}-low-quality"
    return text, method


# ---------- Stage 0: PDF type detection ----------

def _stage_1_1_sample_pdf(file_path: Path, sample_pages: int = 15) -> tuple[float, bool, float]:
    """Sample N pages (skipping first+last) via fitz — detection only, NOT extraction.

    Returns (avg_chars, is_garbled, img_ratio):
      - avg_chars: mean chars/page over sampled pages with ≥10 chars (0 if none).
      - is_garbled: True if >1% of sampled chars are C0 control chars (0x00-0x1F),
        indicating custom font encoding PyMuPDF cannot decode (e.g. Fuqua book:
        500+ chars/page but all garbage). Such PDFs have a text layer that is
        junk — hybrid-engine/auto would read it via txt and produce garbage, so
        the caller must force parse_method=ocr.
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
    text/scanned/mixed: hybrid-engine/auto routes per-page internally, and the
    only fitz-based override is the garbled-font check (is_garbled). This
    classifier is kept so `--dry-run` can still print a human-readable type.
    Delegates to _stage_1_1_sample_pdf.
    """
    avg, is_garbled, img_ratio = _stage_1_1_sample_pdf(file_path, sample_pages)
    if is_garbled:
        return ("scanned", avg)  # garbled → needs OCR
    if avg > 500:
        return ("mixed" if img_ratio > 0.3 else "text", avg)
    if avg < 50:
        return ("scanned", avg)
    return ("mixed", avg)


MINERU_CHUNK_SIZE = 50  # pages per minerU invocation


def _stage_1_1_acquire_mineru_lock(timeout: int = 3600) -> int:
    """Acquire exclusive file lock for minerU execution (race-condition prevention).

    Returns file descriptor (lock holder). Blocks until available or timeout.
    Call _stage_1_1_release_mineru_lock(fd) when done.

    Rationale: pgrep-based counting is unreliable under concurrent stress (multiple
    conversations/cron jobs). File lock is atomic and system-wide.
    """
    try:
        # Touch lock file if not exists
        if not MINERU_LOCK_FILE.exists():
            MINERU_LOCK_FILE.touch(mode=0o644)

        fd = os.open(str(MINERU_LOCK_FILE), os.O_RDWR)
        start = time.time()
        last_print_minute = -1
        while True:
            try:
                # Non-blocking attempt
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print(f"[mineru] Lock acquired")
                return fd
            except OSError:
                # Lock busy, wait and retry
                elapsed = time.time() - start
                if elapsed > timeout:
                    raise RuntimeError(f"minerU lock timeout after {elapsed:.0f}s")
                # Print once per minute boundary crossed — `% 60 == 0` drifts
                # past exact multiples due to the 5s sleep + work-time jitter
                # and can silently stop firing for many minutes.
                minute = int(elapsed // 60)
                if minute != last_print_minute:
                    last_print_minute = minute
                    print(f"[mineru] Waiting for lock... ({elapsed:.0f}s elapsed)")
                time.sleep(5)
    except Exception as e:
        raise RuntimeError(f"Failed to acquire minerU lock: {e}")


def _stage_1_1_release_mineru_lock(fd: int) -> None:
    """Release file lock acquired by _stage_1_1_acquire_mineru_lock()."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        print(f"[mineru] Lock released")
    except Exception as e:
        print(f"[mineru] Warning: Failed to release lock: {e}")


def _stage_1_1_kill_mineru_servers() -> None:
    """Kill lingering mineru-api processes to ensure clean state."""
    import subprocess
    try:
        subprocess.run(
            ["pkill", "-f", "mineru-api"], capture_output=True, timeout=5,
        )
    except Exception:
        pass  # best-effort


def _stage_1_1_extract_text_scanned_locked(file_path: Path, config: Config) -> str:
    """Wrapper around _stage_1_1_extract_text_scanned_impl() with file lock management."""
    lock_fd = _stage_1_1_acquire_mineru_lock()
    try:
        text = _stage_1_1_extract_text_scanned_impl(file_path, config)
        return _clean_mineru_latex(text)
    finally:
        _stage_1_1_release_mineru_lock(lock_fd)


def _stage_1_1_extract_text_scanned(file_path: Path, config: Config) -> str:
    """Alias for _stage_1_1_extract_text_scanned_locked (entry point for OCR)."""
    return _stage_1_1_extract_text_scanned_locked(file_path, config)


_log_file: Path | None = None


def log_event(event_type: str, **kwargs) -> None:
    """Append a structured JSONL event to _log_file (best-effort)."""
    if _log_file is None:
        return
    try:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event_type": event_type,
            **kwargs,
        }
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break OCR


def _stage_1_1_scanned_load_stats(out_dir: Path) -> tuple[dict, Path]:
    """Load _mineru_stats.json for crash-recovery, or init empty stats."""
    stats_path = out_dir / "_mineru_stats.json"
    stats: dict = {"completed_chunks": [], "failed_chunks": [], "images": {}}
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    return stats, stats_path


def _stage_1_1_scanned_start_api_server() -> tuple["object", Path]:
    """Start a persistent minerU API server (one per book) and wait for health.

    Returns (api_proc, venv_python). Raises RuntimeError if the API never
    becomes healthy (caller must close any open fitz doc on failure).
    """
    import subprocess as _sp
    venv_python = Path.home() / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        venv_python = Path(sys.executable)
    api_proc = _sp.Popen(
        [str(venv_python), "-m", "mineru.cli.fast_api",
         "--host", "127.0.0.1", "--port", str(MINERU_API_PORT)],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    for _ in range(30):
        time.sleep(2)
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{MINERU_API_PORT}/health", timeout=3)
            if json.loads(r.read()).get("status") == "healthy":
                print(f"[ocr] minerU API ready on port {MINERU_API_PORT}")
                return api_proc, venv_python
        except Exception:
            pass
    api_proc.terminate()
    api_proc.wait()
    raise RuntimeError(f"minerU API failed to start on port {MINERU_API_PORT}")


def _stage_1_1_scanned_restart_server(venv_python: Path):
    """Spawn a fresh minerU API server (after a crash / 5xx)."""
    import subprocess as _sp
    return _sp.Popen(
        [str(venv_python), "-m", "mineru.cli.fast_api",
         "--host", "127.0.0.1", "--port", str(MINERU_API_PORT)],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )


def _stage_1_1_scanned_warmup(doc, out_dir: Path) -> None:
    """1-page warmup to initialize the model and avoid cold-start delay.

    First chunk typically takes 134s; warmup reduces to ~74s (60s savings).
    Non-critical: failures are logged and skipped.
    """
    try:
        import fitz
    except ImportError:
        return
    print("[ocr] Warmup: initializing model...", end=" ", flush=True)
    warmup_start = time.time()
    warmup_pdf = out_dir / "_warmup.pdf"
    try:
        warmup_doc = fitz.open()
        warmup_doc.insert_pdf(doc, from_page=0, to_page=0)
        warmup_doc.save(warmup_pdf)
        warmup_doc.close()
        body, boundary = _stage_1_1_scanned_build_parse_body(
            warmup_pdf, "warmup.pdf", with_images=False)
        req = urllib.request.Request(
            f"http://127.0.0.1:{MINERU_API_PORT}/file_parse",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(req, timeout=120).read()
        warmup_pdf.unlink(missing_ok=True)
        print(f"OK ({time.time() - warmup_start:.0f}s) — model ready")
    except Exception as e:
        warmup_pdf.unlink(missing_ok=True)
        print(f"skipped ({time.time() - warmup_start:.0f}s, {type(e).__name__})")


def _stage_1_1_scanned_build_parse_body(
    pdf_path: Path, upload_filename: str, *, with_images: bool = False
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for minerU /file_parse.

    upload_filename is the filename declared in the Content-Disposition (the
    minerU API keys its results by this name — historically hardcoded, NOT
    pdf_path.name). with_images requests return_images + return_content_list
    so figures can be harvested and mapped to source pages.
    """
    boundary = "----FormBoundary" + os.urandom(8).hex()
    parts: list[bytes] = []
    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="files"; filename="{upload_filename}"'.encode())
    parts.append(b"Content-Type: application/pdf")
    parts.append(b"")
    parts.append(pdf_path.read_bytes())
    parts.append(f"--{boundary}".encode())
    parts.append(b'Content-Disposition: form-data; name="data"')
    parts.append(b"")
    parts.append(json.dumps({"lang": "ch"}).encode())
    # parse_method override (set by stage_1_1_extract_text for garbled-font
    # PDFs to force OCR). Omitting it lets the server default to "auto".
    # NOTE: the "data" field above is actually ignored by the API (it reads
    # lang_list/backend/parse_method as separate Form fields with defaults);
    # parse_method here is the one that takes effect.
    if _PARSE_METHOD_OVERRIDE:
        parts.append(f"--{boundary}".encode())
        parts.append(b'Content-Disposition: form-data; name="parse_method"')
        parts.append(b"")
        parts.append(_PARSE_METHOD_OVERRIDE.encode())
    if with_images:
        for field in ("return_images", "return_content_list"):
            parts.append(f"--{boundary}".encode())
            parts.append(
                f'Content-Disposition: form-data; name="{field}"'.encode())
            parts.append(b"")
            parts.append(b"true")
    parts.append(f"--{boundary}--".encode())
    return b"\r\n".join(parts), boundary


def _stage_1_1_scanned_extract_md(
    results: dict, chunk_pdf: Path, out_dir: Path, start: int, end: int,
    file_path: Path, config,
) -> tuple[str, "Path | None"]:
    """Extract md_content from API results, write it, and harvest figures.

    Returns (md, md_path); md is "" and md_path is None when no content found.
    """
    md = ""
    for rk in (chunk_pdf.name, chunk_pdf.stem):
        if rk in results and isinstance(results[rk], dict):
            md = results[rk].get("md_content", "")
            if md:
                break
    if not md:
        for rv in results.values():
            if isinstance(rv, dict):
                md = rv.get("md_content", "")
                if md:
                    break
    if not md:
        return "", None
    chunk_out = out_dir / f"_chunk_{start:04d}-{end:04d}"
    chunk_out.mkdir(parents=True, exist_ok=True)
    md_path = chunk_out / f"{chunk_pdf.stem}.md"
    md_path.write_text(md, encoding="utf-8")
    _stage_1_2_harvest_images(results, start, file_path, config, chunk_out)
    return md, md_path


def _stage_1_1_scanned_poll_task(
    task_id: str, chunk_pdf: Path, out_dir: Path, start: int, end: int,
    file_path: Path, config, t0: float,
) -> tuple["Path | None", bool]:
    """Poll a minerU async task until completion. Returns (md_path, ok)."""
    for _ in range(60):
        time.sleep(5)
        tr = urllib.request.urlopen(
            f"http://127.0.0.1:{MINERU_API_PORT}/tasks/{task_id}")
        td = json.loads(tr.read())
        if td.get("status") == "completed":
            tdr = td.get("results", {})
            md, md_path = _stage_1_1_scanned_extract_md(
                tdr, chunk_pdf, out_dir, start, end, file_path, config)
            chunk_time = time.time() - t0
            if md:
                print(f"OK ({chunk_time:.0f}s, {len(md)} chars)")
            else:
                md_path = None
                print(f"EMPTY ({chunk_time:.0f}s)")
            return md_path, True
        if td.get("status") == "failed":
            print(f"TASK FAILED: {td.get('error_message', str(td)[:200])}")
            return None, False
    return None, False  # poll timeout (5 min)


def _stage_1_1_scanned_submit_chunk_with_retries(
    chunk_pdf: Path, start: int, end: int, out_dir: Path, file_path: Path,
    config, api_proc, venv_python: Path, ci: int, total_chunks: int,
):
    """Submit one chunk to minerU /file_parse with up to 3 retries + server restart.

    Returns (md_path, chunk_time, ok, api_proc). chunk_time is None on failure.
    api_proc may be replaced if the server is restarted mid-retry.
    """
    t0 = time.time()
    md_path = None
    for attempt in range(3):
        if attempt > 0:
            time.sleep(2)
        try:
            body, boundary = _stage_1_1_scanned_build_parse_body(
                chunk_pdf, "chunk.pdf", with_images=True)
            req = urllib.request.Request(
                f"http://127.0.0.1:{MINERU_API_PORT}/file_parse",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            r = urllib.request.urlopen(req, timeout=1200)
            resp = json.loads(r.read())
            if resp.get("status") == "completed":
                results = resp.get("results", {})
                md, md_path = _stage_1_1_scanned_extract_md(
                    results, chunk_pdf, out_dir, start, end, file_path, config)
                chunk_time = time.time() - t0
                if md:
                    print(f"OK ({chunk_time:.0f}s, {len(md)} chars)")
                    log_event("chunk_complete", chunk=ci + 1, total=total_chunks,
                              elapsed_sec=round(chunk_time, 2), chars=len(md),
                              attempt=attempt + 1)
                else:
                    md_path = None
                    print(f"EMPTY ({chunk_time:.0f}s)")
                    log_event("chunk_complete", chunk=ci + 1, total=total_chunks,
                              elapsed_sec=round(chunk_time, 2), chars=0,
                              attempt=attempt + 1)
                return md_path, chunk_time, True, api_proc
            elif resp.get("status") == "failed":
                err = resp.get("error_message", resp.get("error", "unknown"))
                if attempt < 2:
                    print(f"API FAILED (retry {attempt+1}/3): {err[:100]}")
                    continue
                print(f"API FAILED (final): {err[:200]}")
            else:
                task_id = resp.get("task_id", "")
                if task_id:
                    md_path, ok = _stage_1_1_scanned_poll_task(
                        task_id, chunk_pdf, out_dir, start, end, file_path,
                        config, t0)
                    if ok:
                        return md_path, time.time() - t0, True, api_proc
                else:
                    print("NO TASK ID")
                    continue
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            if attempt < 2:
                if e.code >= 500:
                    print(f"HTTP {e.code} (retry {attempt+1}/3, restarting server)...")
                    api_proc.terminate()
                    try:
                        api_proc.wait(timeout=5)
                    except Exception:
                        api_proc.kill()
                    time.sleep(3)
                    api_proc = _stage_1_1_scanned_restart_server(venv_python)
                    time.sleep(5)
                    continue
                print(f"HTTP {e.code} (retry {attempt+1}/3): {err_body[:100]}")
                continue
            print(f"HTTP {e.code} (final): {err_body[:200]}")
        except Exception as e:
            if attempt < 2:
                if "Connection refused" in str(e):
                    print(f"Connection failed (retry {attempt+1}/3, restarting server)...")
                    time.sleep(3)
                    api_proc = _stage_1_1_scanned_restart_server(venv_python)
                    time.sleep(8)
                    continue
                print(f"Error (retry {attempt+1}/3): {str(e)[:100]}")
                continue
            print(f"FAILED (final): {str(e)[:200]}")
        break
    return None, None, False, api_proc


def _stage_1_1_scanned_print_failure_banner(start: int, end: int, chunk_pdf: Path) -> None:
    """Print a visible banner when a chunk exhausts all retries."""
    w = 64
    lines = [
        "ALL RETRIES EXHAUSTED — CHUNK PERMANENTLY FAILED",
        "",
        f"Chunk:  pages {start+1}-{end}",
        f"File:   {chunk_pdf.name}",
        "",
        "Action: re-run ingest to retry this chunk, or check",
        "        _mineru_stats.json for error details",
    ]
    print("")
    print(f"  ╔{'═'*w}╗")
    for i, line in enumerate(lines):
        if i == 0:
            print(f"  ║  ⚠️  {line:<{w-5}} ║")
        else:
            print(f"  ║     {line:<{w-4}} ║")
    print(f"  ╚{'═'*w}╝")
    print("")


def _stage_1_1_scanned_process_chunk(
    ci: int, start: int, end: int, chunks, doc, out_dir: Path, stats: dict,
    stats_path: Path, chunk_times: list, api_proc, venv_python: Path,
    file_path: Path, config,
):
    """Process one chunk: create chunk PDF, submit with retries, persist stats.

    Returns api_proc (may change on server restart). Raises RuntimeError if
    cumulative failure rate exceeds 30% (fatal abort).
    """
    chunk_key = f"{start}-{end}"
    if chunk_key in stats["completed_chunks"]:
        percent = (ci + 1) * 100 // len(chunks)
        print(f"  [{ci+1:3d}/{len(chunks)}] [{percent:3d}%] pages {start+1}-{end} — (cached)")
        return api_proc

    # Create chunk PDF
    try:
        import fitz
    except ImportError:
        raise RuntimeError("Scanned PDF OCR requires PyMuPDF")
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
        return api_proc

    # Progress + ETA
    percent = (ci + 1) * 100 // len(chunks)
    if chunk_times:
        avg_time = sum(chunk_times) / len(chunk_times)
        remaining = len(chunks) - ci - 1
        eta_sec = remaining * avg_time
        eta_str = f"ETA: {int(eta_sec)}s" if eta_sec < 60 else f"ETA: {int(eta_sec/60):.1f}m"
    else:
        eta_str = "computing ETA..."
    print(f"  [{ci+1:3d}/{len(chunks)}] [{percent:3d}%] pages {start+1}-{end} — minerU API ({eta_str})...",
          end=" ", flush=True)

    md_path, chunk_time, ok, api_proc = _stage_1_1_scanned_submit_chunk_with_retries(
        chunk_pdf, start, end, out_dir, file_path, config, api_proc, venv_python,
        ci, len(chunks))
    if chunk_time is not None:
        chunk_times.append(chunk_time)

    if not ok:
        stats["failed_chunks"].append({"chunk": chunk_key, "error": "minerU API failed after retries"})
        _stage_1_1_save_mineru_stats(stats_path, stats)
        log_event("chunk_error", chunk=ci + 1, total=len(chunks),
                  error="max retries exceeded")
        _stage_1_1_scanned_print_failure_banner(start, end, chunk_pdf)
        if len(stats["failed_chunks"]) > len(chunks) * 0.3:
            _stage_1_1_kill_mineru_servers()
            raise RuntimeError(
                f"minerU OCR: {len(stats['failed_chunks'])}/{len(chunks)} chunks failed. "
                f"Aborting. Check _mineru_stats.json in extract_tmp_dir.")
        return api_proc

    # API wrote .md — read it (EMPTY → md_path None → record as failed, no fatal check)
    if md_path is None or not md_path.exists():
        print(f"  [{ci+1:3d}/{len(chunks)}] FAILED — no output file")
        stats["failed_chunks"].append({"chunk": chunk_key, "error": "no .md output from API"})
        _stage_1_1_save_mineru_stats(stats_path, stats)
        return api_proc

    md_text = md_path.read_text(encoding="utf-8")
    _media_slug = _stage_1_2_media_slug(file_path, config)
    media_dir = config.wiki_dir / "media" / _media_slug
    media_dir.mkdir(parents=True, exist_ok=True)
    _stage_1_1_save_mineru_chunk_text(md_text, start, end, out_dir, stats, [])
    stats["completed_chunks"].append(chunk_key)
    _stage_1_1_save_mineru_stats(stats_path, stats)
    print(f"  [{ci+1:3d}/{len(chunks)}] done — {len(md_text)} chars")
    chunk_pdf.unlink(missing_ok=True)
    return api_proc


def _stage_1_1_scanned_assemble_manifest(
    out_dir: Path, stats: dict, file_path: Path, config, total_pages: int,
) -> str:
    """Assemble per-page OCR text into full text and write _manifest.json."""
    page_nums = list(range(total_pages))
    full_text = _stage_1_1_assemble_ocr_text(out_dir, page_nums)
    total_imgs = sum(len(v) for v in stats.get("images", {}).values())
    print(f"[ocr] Done — {len(full_text):,} chars OCR text, {total_imgs} images extracted")

    slug = _stage_1_2_media_slug(file_path, config)
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
        _stage_1_2_write_manifest(manifest_path, "mineru-ocr", file_path, extracted_figures)
        print(f"[ocr] {len(extracted_figures)} extracted figures → _manifest.json")
        pending = _stage_1_2_find_uncaptioned_images(media_dir)
        if pending and config.caption_api_key:
            _stage_1_3_caption_images_batch(
                pending, config, media_dir, source_label="mineru-extracted", batch_size=6)
    else:
        _stage_1_2_write_manifest(manifest_path, "mineru-ocr", file_path, [])
        print("[ocr] No extracted figures — empty manifest written")
    return full_text


def _stage_1_1_extract_text_scanned_impl(file_path: Path, config: Config) -> str:
    """Extract a PDF (any type) via the local minerU API server (hybrid-engine).

    Despite the legacy "_scanned" name, this is the shared extraction path for
    text / scanned / mixed PDFs (see stage_1_1_extract_text routing). It starts
    a persistent mineru.cli.fast_api server and calls /file_parse per ~50-page
    chunk. The server defaults to hybrid-engine with parse_method=auto, which
    auto-routes: text layer present -> txt (no OCR); absent -> VLM OCR. The
    /file_parse endpoint accepts a per-request `backend` Form field, so pipeline
    or vlm-engine could be forced, but hybrid-engine is the verified default
    (see stage_1_1_extract_text docstring for the rationale).

    Splits PDF into ~50-page chunks. Each chunk runs minerU independently.
    Results persisted to extract_tmp_dir/<stem>/ with _mineru_stats.json for crash recovery.
    Extracted images go to wiki/media/<raw-subpath>/<slug>/ for Stage 3.2 (mirrors raw/).

    Note: File-based lock managed by wrapper function _stage_1_1_extract_text_scanned_locked().
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

    # Route the minerU API server's output root into the runtime temp dir
    # (.llm-wiki/). minerU defaults to "./output" relative to the server's
    # cwd, which previously polluted the wiki root with uuid-named dirs.
    # The server reads MINERU_API_OUTPUT_ROOT at startup; both start and
    # restart Popen calls inherit the parent env, so set it once here.
    api_output_root = config.runtime_dir / "mineru-api-out"
    api_output_root.mkdir(parents=True, exist_ok=True)
    os.environ["MINERU_API_OUTPUT_ROOT"] = str(api_output_root)

    # Build chunks: 50 pages each
    chunks = []
    for start in range(0, total_pages, MINERU_CHUNK_SIZE):
        end = min(start + MINERU_CHUNK_SIZE, total_pages)
        chunks.append((start, end))  # 0-indexed, [start, end)

    print(f"[ocr] Local minerU: {total_pages} pages → {len(chunks)} chunks "
          f"({MINERU_CHUNK_SIZE} pages/chunk)")

    stats, stats_path = _stage_1_1_scanned_load_stats(out_dir)

    # Initialize structured logging (JSON Lines format)
    global _log_file
    _log_file = out_dir / "ocr_log.jsonl"

    # Early exit if all chunks already cached
    pending = [c for c in chunks if f"{c[0]}-{c[1]}" not in stats["completed_chunks"]]
    if not pending:
        doc.close()
        return _stage_1_1_assemble_ocr_text(out_dir, [end for _, end in chunks])

    api_proc = None
    try:
        api_proc, venv_python = _stage_1_1_scanned_start_api_server()
        _stage_1_1_scanned_warmup(doc, out_dir)

        # Run minerU on each pending chunk (with progress tracking)
        chunk_times: list[float] = []  # completion times for ETA estimation
        for ci, (start, end) in enumerate(chunks):
            api_proc = _stage_1_1_scanned_process_chunk(
                ci, start, end, chunks, doc, out_dir, stats, stats_path,
                chunk_times, api_proc, venv_python, file_path, config)
    finally:
        doc.close()
        if api_proc is not None:
            api_proc.terminate()
            try:
                api_proc.wait(timeout=10)
            except Exception:
                api_proc.kill()

    return _stage_1_1_scanned_assemble_manifest(out_dir, stats, file_path, config, total_pages)

def _stage_1_1_save_mineru_stats(stats_path: Path, stats: dict) -> None:
    """Atomically persist minerU stats for crash recovery."""
    tmp = stats_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(stats_path)


def _stage_1_2_find_uncaptioned_images(media_dir: Path) -> list[dict]:
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


def _stage_1_3_is_caption_failed(text: str) -> bool:
    """Detect VLM failure responses that shouldn't be treated as valid captions."""
    if not text or len(text) < 15:
        return True
    failure_markers = ["解析失败", "无法识别", "unable to", "cannot describe",
                       "抱歉", "sorry", "I can't", "not clear", "无法描述"]
    text_lower = text.lower()
    return any(m.lower() in text_lower for m in failure_markers)


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
            # 改进3：Fallback 降级方案
            print(f"[caption] Fallback (attempt {attempt+1}): {type(e).__name__}")
            fallback_text = "\n".join(f"[图{i+1}] 技术图表" for i in range(len(batch)))
            return fallback_text, None
    return None, "max-retries"



def _stage_1_2_harvest_images(results: dict, page_offset: int, raw_file: Path,
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
    slug = _stage_1_2_media_slug(raw_file, config)
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

            # 改进5：用 MD5 ID 替代位置索引
            raw_bytes = _b64.b64decode(data)
            img_id = hashlib.md5(raw_bytes).hexdigest()[:8]

            # Determine extension
            if img_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                ext = img_name.rsplit(".", 1)[-1].lower()
            else:
                ext = "png"
            filename = f"p{page_num:04d}-mineru_{img_id}.{ext}"
            out_path = media_dir / filename

            if not out_path.exists():
                try:
                    out_path.write_bytes(raw_bytes)
                except Exception:
                    continue

            # Get dimensions if possible; drop true noise (1x1/2x2 artifacts).
            # PIL open is wrapped (can fail on corrupt bytes), but the size
            # check is outside the try so a NameError in _is_image_too_small
            # surfaces instead of silently keeping every image.
            w, h = 0, 0
            pil_ok = False
            try:
                from PIL import Image
                im = Image.open(out_path)
                w, h = im.size
                im.close()
                pil_ok = True
            except Exception:
                pass  # PIL couldn't read; keep image with unknown dims
            if pil_ok and _is_image_too_small(w, h):
                out_path.unlink(missing_ok=True)
                continue

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


def _stage_1_1_save_mineru_chunk_text(md_text: str, start: int, end: int, out_dir: Path,
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


def _stage_1_1_assemble_ocr_text(out_dir: Path, page_nums: list[int]) -> str:
    """Concatenate per-page OCR text files in page order."""
    parts = []
    for pn in page_nums:
        page_path = out_dir / f"p{pn+1:04d}.txt"
        if page_path.exists():
            parts.append(page_path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


# ---------- Stage 1.2: Image extraction ----------

def _stage_1_2_extract_images_office(raw_file: Path, media_dir: Path, manifest_path: Path,
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


def _stage_1_2_extract_from_mineru(out_dir: Path, config: Config, raw_file: Path) -> dict:
    """Extract images from minerU output (pipeline txt / vlm / auto backends).

    minerU writes images to <out_dir>/<stem>/<method>/images/ where <method>
    is txt (pipeline -m txt), vlm (vlm-engine), or auto. Also reads
    content_list.json to harvest minerU's own image_caption (the PDF figure
    caption) and sub_type (flowchart/curve/text_image) so downstream Stage 1.3
    can skip re-captioning figures minerU already described.
    """
    media_dir = config.wiki_dir / "media" / _stage_1_2_media_slug(raw_file, config)
    media_dir.mkdir(parents=True, exist_ok=True)

    # Locate image source dir across backends: txt (pipeline), vlm, auto.
    # minerU nests output as <out_dir>/<stem>/<method>/images/.
    stem = raw_file.stem
    img_source_dir = None
    for method in ("txt", "vlm", "auto"):
        cand = out_dir / stem / method / "images"
        if cand.exists():
            img_source_dir = cand
            break
    # Fallback: older flat layout <out_dir>/vlm/images or auto/images
    if img_source_dir is None:
        for method in ("vlm", "auto"):
            cand = out_dir / method / "images"
            if cand.exists():
                img_source_dir = cand
                break

    # Harvest minerU image_caption + sub_type from content_list.json.
    # Keyed by image basename so we can attach during copy.
    caption_map: dict[str, dict] = {}
    cl_files = sorted(out_dir.rglob("*content_list.json"))
    for clf in cl_files:
        try:
            blocks = json.loads(clf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for b in blocks:
            if b.get("type") in ("image", "chart"):
                ip = b.get("img_path", "")
                bn = os.path.basename(ip)
                if bn:
                    caps = b.get("image_caption", [])
                    caption_map[bn] = {
                        "caption": caps[0] if caps else "",
                        "sub_type": b.get("sub_type", ""),
                        "page": b.get("page_idx", 0),
                    }
        if caption_map:
            break  # use first content_list that yields images

    images = []
    mineru_captioned = 0
    if img_source_dir:
        for img_path in sorted(img_source_dir.glob("*")):
            if not img_path.is_file():
                continue
            dest = media_dir / img_path.name
            shutil.copy2(img_path, dest)
            meta = caption_map.get(img_path.name, {})
            # Write minerU caption as sidecar so Stage 1.3 skips re-captioning.
            # Combine image_caption (figure label) + content (Mermaid for flowcharts).
            cap_parts = []
            if meta.get("caption"):
                cap_parts.append(meta["caption"])
            if meta.get("content"):
                cap_parts.append(meta["content"])
            if cap_parts:
                sidecar = media_dir / (img_path.name + ".caption.txt")
                sidecar.write_text("\n".join(cap_parts), encoding="utf-8")
                mineru_captioned += 1
            images.append({
                "filename": img_path.name,
                "path": str(dest.relative_to(config.wiki_root)),
                "page": meta.get("page", 0),
                "caption": meta.get("caption", ""),
                "sub_type": meta.get("sub_type", ""),
                "width": 0,
                "height": 0,
            })

    manifest_path = media_dir / "_manifest.json"
    _stage_1_2_write_manifest(manifest_path, "mineru-ocr", raw_file, images)
    print(f"[stage 1.2] minerU: {len(images)} images from {img_source_dir} "
          f"({mineru_captioned} pre-captioned by minerU, Stage 1.3 will skip)")
    return {
        "count": len(images),
        "media_dir": str(media_dir),
        "manifest": str(manifest_path),
        "images": images,
        "mineru": True,
    }


def stage_1_2_extract_images(raw_file: Path, config: Config, min_size: int = 100) -> dict:
    """Extract embedded images from PPTX / DOCX via their internal zipfile media/ directory
    (NashSU parity: extractAndSaveSourceImages).

    PDF images are extracted separately by _stage_1_2_extract_from_mineru(), since all PDF
    text extraction routes through minerU (pipeline or VLM), which extracts images as part
    of the same pass.

    Returns: {"count": int, "media_dir": str, "manifest": str, "images": list}
    """
    slug = _stage_1_2_media_slug(raw_file, config)
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
    return _stage_1_2_extract_images_office(raw_file, media_dir, manifest_path, min_size)


def _stage_1_2_raw_type_subdir(raw_file: Path, config: Config) -> str:
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


def _stage_1_2_media_slug(raw_file: Path, config: Config) -> str:
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


def _stage_1_2_write_manifest(manifest_path: Path, source: str, raw_file: Path, images: list[dict]) -> None:
    """改进4：manifest 版本控制和提取配置记录。"""
    manifest = {
        "manifest_version": 2,  # 版本控制
        "extraction_time": time.strftime("%Y-%m-%d %H:%M:%S"),  # 时间戳
        "extraction_config": {"min_size": 100},  # 配置记录
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

    Thin wrapper around _stage_1_3_caption_images_batch() for backward compatibility with the
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
    captioned = _stage_1_3_caption_images_batch(images, config, media_dir,
                                source_label="pyMuPDF",
                                batch_size=batch_size)
    return {"captioned": captioned, "total": len(images)}


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


def _stage_1_1_check_text_quality(text: str, source_name: str = "") -> dict:
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

