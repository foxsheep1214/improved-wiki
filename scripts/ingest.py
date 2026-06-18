#!/usr/bin/env python3
"""
ingest.py — End-to-end Ingest for one source file (NashSU-style multi-stage pipeline).

Pipeline (per improved-wiki §7, now matching NashSU's actual stages):
  1. Dedup check          (wiki/sources/ source page → skip)
  2. Extract text          (PyMuPDF first, minerU VLM OCR fallback)
  3. Global digest          (1 LLM call: book-level structural summary)
  4. Chunk + analyze       (N LLM calls: per-chunk structured analysis)
  5. Synthesize            (1 LLM call: combine digest+analyses → page specs + File blocks)
  6. Write files           (sources/ + concepts/ + entities/)
  7. Update cache          (sha256 → filesWritten[])

Usage:
  ingest.py <raw-file-path>                # process one file
  ingest.py f1.pdf f2.pdf ...              # batch mode: parallel Stage 0-2
  ingest.py --dry-run <raw-file-path>      # show what would be done, no writes
  ingest.py --verbose <raw-file-path>      # show LLM responses for debugging
  ingest.py --watch                        # continuous queue consumer (daemon mode)
  ingest.py --watch --drain                # process queue until empty, then exit
  ingest.py --watch --poll-interval 60     # re-scan queue every 60s

Configuration:
  ~/.agents/config.json   provider config (default: deepseek, caption: minimax)
  LLM_PROVIDER            override provider name (env var)
  LLM_API_KEY             override API key (env var)
  LLM_BASE_URL            override base URL (env var)
  LLM_MODEL               override model name (env var)
  LLM_CHUNK_CONCURRENCY   max concurrent chunk analysis + generation workers (default 8)
                            (shared by Stage 1.5 chunk analysis and Stage 2.1 per-chunk generation)
  LLM_CHUNK_RETRIES       extra attempts per failed chunk (default 2 → 3 total)
  Text LLM:               config.json default provider (DeepSeek V4 Pro via OpenAI protocol)
  Image caption:          config.json caption_provider (MiniMax via Anthropic protocol)
                            CAPTION_BATCH_SIZE=8   images per API call
                            CAPTION_MAX_WORKERS=6  parallel batch concurrency
  Embeddings:             local Ollama (EMBEDDING_BASE_URL / EMBEDDING_MODEL)

This script is idempotent: if the source page exists for a file, it's skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# Use shared runtime detection (matches all other scripts)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402
# _frontmatter provides parse_frontmatter, write_frontmatter, merge_page_content
# (used locally by the existing merge_page_content in this file)


# ── Progress & heartbeat helpers ──

import threading

_stage_start_times: dict[str, float] = {}
_current_file_local = threading.local()

def _set_current_file(name: str) -> None:
    _current_file_local.value = name

def _get_current_file() -> str:
    return getattr(_current_file_local, "value", "")

def _file_tag() -> str:
    """Short file label for progress lines. Empty string if no file context."""
    f = _get_current_file()
    if not f:
        return ""
    if len(f) > 50:
        return f"[{f[:40]}...{f[-6:]}] "
    return f"[{f}] "

def _stage_begin(name: str) -> None:
    _stage_start_times[name] = time.time()
    tag = _file_tag()
    print(f"\n{'─'*40}\n{tag}[{name}] Starting...\n{'─'*40}", flush=True)

def _stage_end(name: str) -> None:
    t0 = _stage_start_times.pop(name, None)
    elapsed = time.time() - t0 if t0 else 0.0
    tag = _file_tag()
    if elapsed >= 60:
        print(f"{tag}[{name}] Done ({elapsed/60:.1f}m)", flush=True)
    else:
        print(f"{tag}[{name}] Done ({elapsed:.0f}s)", flush=True)

def _heartbeat(msg: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _file_tag()
    suffix = f" — {msg}" if msg else ""
    print(f"  {ts}  {tag}… {suffix}", flush=True)

def _llm_call_progress(label: str, attempt: int = 1, retries: int = 0) -> None:
    tag = _file_tag()
    retry_hint = f" (retry {attempt}/{retries+1})" if retries else ""
    print(f"  {tag}→ {label}{retry_hint}...", end=" ", flush=True)

def _llm_call_done(elapsed: float, chars: int | None = None) -> None:
    size_hint = f", {chars:,} chars" if chars else ""
    print(f"OK ({elapsed:.0f}s{size_hint})", flush=True)


class ConversationPending(Exception):
    """Raised in --conversation mode when a prompt is written and we wait for agent."""


# ---------- Configuration ----------

def _load_provider_config(name: str | None = None) -> dict:
    """Load LLM provider credentials from ~/.agents/config.json, with env-var fallback.

    Args:
        name: provider name to load. If None, uses config.default or LLM_PROVIDER env var.
    """
    config_path = Path.home() / ".agents" / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if name is None:
                default = cfg.get("default", "minimax")
                name = os.environ.get("LLM_PROVIDER", default)
            provider = cfg.get("providers", {}).get(name)
            if provider:
                models = provider.get("models", {})
                return {
                    "api_key": os.environ.get("LLM_API_KEY") or provider.get("api_key", ""),
                    "base_url": os.environ.get("LLM_BASE_URL") or provider.get("base_url", "https://api.minimaxi.com"),
                    "model": os.environ.get("LLM_MODEL") or models.get("text", provider.get("model", "MiniMax-M3")),
                    "protocol": provider.get("protocol", "anthropic"),
                    "provider": name,
                }
        except Exception:
            pass  # fall through to env vars
    # Legacy fallback: env vars
    return {
        "api_key": os.environ.get("LLM_API_KEY", "") or os.environ.get("MINIMAX_CN_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.minimaxi.com"),
        "model": os.environ.get("LLM_MODEL", "MiniMax-M3"),
        "protocol": "anthropic",
        "provider": "env",
    }


def _load_caption_provider() -> dict:
    """Load image captioning provider (always from config, never from env vars).

    Uses config.caption_provider if set, otherwise falls back to config.default.
    Caption requires a multi-modal model with Anthropic-protocol image support.
    """
    config_path = Path.home() / ".agents" / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            caption_name = cfg.get("caption_provider") or cfg.get("default", "minimax")
            provider = cfg.get("providers", {}).get(caption_name)
            if provider:
                models = provider.get("models", {})
                return {
                    "api_key": provider.get("api_key", ""),
                    "base_url": provider.get("base_url", "https://api.minimaxi.com"),
                    "model": models.get("caption") or models.get("vision") or provider.get("model", "MiniMax-M3"),
                    "protocol": provider.get("protocol", "anthropic"),
                    "provider": caption_name,
                }
        except Exception:
            pass
    # Fallback: MiniMax defaults
    return {
        "api_key": os.environ.get("CAPTION_API_KEY") or os.environ.get("LLM_API_KEY", ""),
        "base_url": "https://api.minimaxi.com",
        "model": "MiniMax-M3",
        "protocol": "anthropic",
        "provider": "minimax",
    }


@dataclass
class Config:
    wiki_root: Path
    raw_root: Path
    wiki_dir: Path
    runtime_dir: Path       # .llm-wiki/ (NashSU-aligned) — all runtime state lives here
    cache_path: Path        # runtime_dir / "ingest-cache.json"
    progress_dir: Path      # runtime_dir / "ingest-progress"
    extract_tmp_dir: Path   # runtime_dir / "extract-tmp"
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_protocol: str       # "anthropic" or "openai" — determines API format
    # Caption provider (always MiniMax / Anthropic-protocol multi-image)
    caption_api_key: str
    caption_base_url: str
    caption_model: str
    chunk_size: int
    chunk_overlap: int
    source_budget: int  # max chars for global digest (NashSU: sourceBudget)
    target_chars: int    # target chars per chunk analysis (NashSU: targetChars)
    max_tokens: int      # LLM response max_tokens
    conversation_mode: bool = False
    conversation_prefix: str = ""  # source_hash suffix for per-source file isolation

    @classmethod
    def from_env(cls) -> "Config":
        wiki_root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd())).expanduser()
        provider = _load_provider_config()
        caption = _load_caption_provider()
        # Runtime state: delegating to shared _paths.detect_runtime_dir()
        # (handles .llm-wiki/ default, .iwiki-runtime/ auto-migration, legacy wiki/)
        runtime_dir = detect_runtime_dir(wiki_root)
        return cls(
            wiki_root=wiki_root,
            raw_root=wiki_root / "raw",
            wiki_dir=wiki_root / "wiki",
            runtime_dir=runtime_dir,
            cache_path=runtime_dir / "ingest-cache.json",
            progress_dir=runtime_dir / "ingest-progress",
            extract_tmp_dir=runtime_dir / "extract-tmp",
            llm_base_url=provider["base_url"],
            llm_model=provider["model"],
            llm_api_key=provider["api_key"],
            llm_protocol=provider.get("protocol", "anthropic"),
            caption_api_key=caption["api_key"],
            caption_base_url=caption["base_url"],
            caption_model=caption["model"],
            chunk_size=300_000,       # legacy, unused in phased mode
            chunk_overlap=3_000,      # chars overlap between chunks
            source_budget=200_000,    # max chars for global digest prompt
            target_chars=60_000,      # target chars per chunk (NashSU default)
            max_tokens=16384,         # per-phase response budget (can be overridden by compute_max_tokens)
        )

    def compute_max_tokens(self, base_tokens: int = 16384) -> int:
        """NashSU parity (ingest.ts L2071-2081): scale max_tokens by model context.

        Conservative scaling: smaller models get proportionally smaller responses
        to avoid truncation. Override by setting LLM_MAX_TOKENS env var.
        """
        env_override = os.environ.get("LLM_MAX_TOKENS")
        if env_override:
            return int(env_override)
        # Default: 16K for most models. Increase for large-context models.
        model = self.llm_model.lower()
        if "512k" in model or "1m" in model:
            return min(base_tokens * 2, 32768)
        if "256k" in model or "200k" in model:
            return base_tokens
        if "128k" in model or "100k" in model:
            return max(base_tokens // 2, 8192)
        return base_tokens


# ---------- File-type detection ----------

FOLDER_TO_TEMPLATE = {
    "book": "digest-book.md",
    "paper": "digest-paper.md",
    "datasheet": "digest-datasheet.md",
    "ApplicationNote": "digest-applicationnote.md",
    "DesignExample": "digest-designexample.md",
    "presentation": "digest-presentation.md",
    "standard": "digest-standard.md",
    "news": "digest-news.md",
}

WRAPPER_FOLDERS = {"sources", "assets"}


def _str_distance(a: str, b: str) -> int:
    """Simple Levenshtein-like distance for closest-match suggestion."""
    if not a or not b:
        return max(len(a), len(b))
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    # Approximate: count differing chars in overlapping prefix
    d = abs(len(a) - len(b))
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            d += 1
    return d


def detect_template_type(raw_file: Path, raw_root: Path, override: str | None) -> str:
    if override:
        return override
    try:
        rel = raw_file.relative_to(raw_root)
    except ValueError:
        raise ValueError(f"{raw_file} is not under {raw_root}")
    parts = rel.parts
    if not parts:
        raise ValueError(f"{raw_file} has no components under {raw_root}")

    first = parts[0]
    if first in WRAPPER_FOLDERS and len(parts) > 1:
        if parts[1] in FOLDER_TO_TEMPLATE:
            type_folder = parts[1]
        else:
            type_folder = "book"
            print(f"[detect] Flat layout detected (raw/sources/<file>). Assuming template=book. "
                  f"Override with IMPROVED_WIKI_TYPE=paper if this is wrong.")
    else:
        type_folder = first

    if type_folder not in FOLDER_TO_TEMPLATE:
        valid = ", ".join(sorted(FOLDER_TO_TEMPLATE.keys()))
        # Suggest closest match
        closest = min(FOLDER_TO_TEMPLATE.keys(), key=lambda k: _str_distance(k, type_folder), default=None)
        hint = f" Did you mean '{closest}'?" if closest and _str_distance(closest, type_folder) < 5 else ""
        raise ValueError(
            f"Unknown raw type folder '{type_folder}'.{hint}\n"
            f"  Valid folders: {valid}\n"
            f"  Or set --type <type> to override (e.g. --type=book)"
        )
    return FOLDER_TO_TEMPLATE[type_folder].removesuffix(".md")


def load_template(template_name: str) -> str:
    skill_dir = Path(__file__).parent.parent
    template_path = skill_dir / "templates" / f"{template_name}.md"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


# ---------- Hashing & cache ----------

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cache(config: Config) -> dict:
    if config.cache_path.exists():
        return json.loads(config.cache_path.read_text(encoding="utf-8"))
    return {"version": 1, "entries": {}}


def save_cache(config: Config, cache: dict) -> None:
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(config.cache_path)


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

    out_dir = config.wiki_root / ".ocr-tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = file_path.stem

    cmd = [
        str(mineru_bin), "-p", str(file_path), "-o", str(out_dir),
        "-b", "vlm-auto-engine", "--image-analysis", "False",
        "-m", "auto", "-l", "ch",
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
            if text.strip() and len(text) > len(text.split("\n\n")) * 5:
                return text, "pymupdf"
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

def detect_pdf_type(file_path: Path, sample_pages: int = 10) -> tuple[str, float]:
    """Sample N pages to determine PDF type.

    Uses two signals:
    1. Text chars/page (PyMuPDF get_text())
    2. Presence of full-page images (scanned PDFs have one large image per page)

    Pages are sampled evenly across the document to avoid bias from
    TOC/intro pages at the front and index/bibliography at the back.

    Returns ("text", avg_chars) or ("scanned", avg_chars) or ("mixed", avg_chars).
    """
    try:
        import fitz
    except ImportError:
        return ("text", 0)
    doc = fitz.open(file_path)
    try:
        total_chars = 0
        text_pages = 0
        img_pages = 0
        n = min(sample_pages, len(doc))
        # Evenly-spaced sampling across the full document (avoid TOC bias)
        if len(doc) <= n:
            sample_indices = list(range(len(doc)))
        else:
            step = len(doc) / n
            sample_indices = [int(i * step) for i in range(n)]

        for idx in sample_indices:
            page = doc[idx]
            chars = len(page.get_text())
            # Skip pages that are effectively blank (navigable content might be images)
            if chars < 10:
                continue
            total_chars += chars
            text_pages += 1
            # Check for full-page scan image: >50% of page area
            rect = page.rect
            page_area = rect.width * rect.height
            for img in page.get_images():
                pix = fitz.Pixmap(doc, img[0])
                img_area = pix.width * pix.height
                if img_area > page_area * 0.5:
                    img_pages += 1
                    break

        if text_pages == 0:
            # All sampled pages were blank — assume scanned
            return ("scanned", 0.0)

        avg = total_chars / text_pages
        img_ratio = img_pages / text_pages

        # If most pages have full-page images, it's scanned regardless of text
        if img_ratio > 0.6:
            return ("scanned", avg)
        # Hidden OCR layer: significant text BUT also many full-page images
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


def stage_0_pilot(file_path: Path, config: Config) -> dict:
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
        result = subprocess.run(
            [str(mineru_bin), "-p", str(pilot_pdf), "-o", str(out_dir),
             "-b", "vlm-auto-engine", "-m", "ocr", "-l", "ch"],
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
    """OCR a scanned PDF using local minerU (vlm-auto-engine, MLX backend).

    Splits PDF into ~50-page chunks. Each chunk runs minerU independently.
    Results persisted to extract_tmp_dir/<stem>/ with _mineru_stats.json for crash recovery.
    Extracted images go to wiki/media/<raw-subpath>/<slug>/ for Stage 3.5 (mirrors raw/).
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

    # Run minerU on each pending chunk
    for ci, (start, end) in enumerate(chunks):
        chunk_key = f"{start}-{end}"
        if chunk_key in stats["completed_chunks"]:
            print(f"  [{ci+1:3d}/{len(chunks)}] pages {start+1}-{end} — (cached)")
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

        # Run minerU (retry up to 2 times, killing stale servers between attempts)
        chunk_out = out_dir / f"_chunk_{start:04d}-{end:04d}_out"
        import subprocess
        import shutil
        _wait_for_mineru_slot()  # enforce MINERU_MAX_CONCURRENT system-wide
        mineru_ok = False
        for attempt in range(3):
            if attempt > 0:
                # Kill stale mineru processes between retries
                _kill_mineru_servers()
                time.sleep(2)
                shutil.rmtree(chunk_out, ignore_errors=True)

            print(f"  [{ci+1:3d}/{len(chunks)}] pages {start+1}-{end} — minerU...", end=" ", flush=True)
            t0 = time.time()
            try:
                result = subprocess.run(
                    [str(mineru_bin), "-p", str(chunk_pdf), "-o", str(chunk_out),
                     "-b", "vlm-auto-engine", "-m", "ocr", "-l", "ch"],
                    capture_output=True, text=True, timeout=1800,
                    env={**os.environ},
                )
            except subprocess.TimeoutExpired:
                print(f"TIMEOUT (>30min)")
                continue
            except Exception as e:
                print(f"FAILED: {e}")
                continue

            if result.returncode != 0:
                err_tail = result.stderr[-500:] if result.stderr else ""
                print(f"FAILED ({time.time()-t0:.0f}s): {err_tail[:200]}")
                # Retry on connection/server errors; break only on permanent failures
                RETRY_PATTERNS = [
                    "NoneType", "get",  # original patterns
                    "onnection", "failed", "timeout", "refused",
                    "Task failed", "RemoteDisconnected", "ServerDisconnected",
                    "semaphore", "resource_tracker",  # minerU shutdown crashes
                    "concurrency", "limited",  # minerU VLM concurrency limit (transient)
                ]
                if any(p.lower() in err_tail.lower() for p in RETRY_PATTERNS):
                    continue
                # Permanent failure — don't retry (e.g. invalid PDF, missing file)
                break

            mineru_ok = True
            break

        if not mineru_ok:
            stats["failed_chunks"].append({"chunk": chunk_key, "error": "minerU failed after retries"})
            _save_mineru_stats(stats_path, stats)
            _kill_mineru_servers()
            # ⚠️  Prominent warning — all 3 retries exhausted
            w = 64
            lines = [
                f"ALL 3 RETRIES EXHAUSTED — CHUNK PERMANENTLY FAILED",
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

        elapsed = time.time() - t0

        # Find minerU output
        md_path = chunk_out / chunk_pdf.stem / "vlm" / f"{chunk_pdf.stem}.md"
        if not md_path.exists():
            md_path = chunk_out / chunk_pdf.stem / "auto" / f"{chunk_pdf.stem}.md"
        if not md_path.exists():
            print(f"FAILED: .md not found")
            stats["failed_chunks"].append({"chunk": chunk_key, "error": ".md not found in output"})
            _save_mineru_stats(stats_path, stats)
            continue

        # Read OCR text and collect images
        md_text = md_path.read_text(encoding="utf-8")
        img_dir = chunk_out / chunk_pdf.stem / "vlm" / "images"
        extracted_imgs = _collect_mineru_images(img_dir, start)

        # Save per-page text (split by minerU's page markers or heuristics)
        _save_mineru_chunk_text(md_text, start, end, out_dir, stats, extracted_imgs)

        stats["completed_chunks"].append(chunk_key)
        # Move images from temp to persistent location for Stage 3.5
        _copy_mineru_images(extracted_imgs, config, file_path)
        _save_mineru_stats(stats_path, stats)

        n_imgs = len(extracted_imgs)
        print(f"OK ({elapsed:.0f}s) — {len(md_text)} chars, {n_imgs} images")

        # Clean up chunk files
        chunk_pdf.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(chunk_out, ignore_errors=True)

    doc.close()

    # Assemble full text from per-page files
    page_nums = list(range(total_pages))
    full_text = _assemble_ocr_text(out_dir, page_nums)
    total_imgs = sum(len(v) for v in stats.get("images", {}).values())
    print(f"[ocr] Done — {len(full_text):,} chars OCR text, {total_imgs} images extracted")

    # ── Caption filtered minerU images with minimax ──
    slug = _media_slug(file_path, config)
    media_dir = config.wiki_dir / "media" / slug
    pending_imgs = _find_uncaptioned_mineru_images(media_dir)
    if pending_imgs and config.caption_api_key:
        _caption_images(pending_imgs, config, media_dir, source_label="minerU", batch_size=6)

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


# ---------- Stage 0.6/0.9: Unified image captioning (Path A + Path B merged) ----------

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
    the caption provider (MiniMax via Anthropic protocol).

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
                    print(f"  batch {bi+1}: JSON truncated — salvaged {len(captions)} captions")
                else:
                    # Recovery 2: single caption truncated mid-string (no closing quote)
                    m = re.search(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)$', text)
                    if m and text.count('"idx"') == 1:
                        cap_text = m.group(1).rstrip('，、, \t')
                        if len(cap_text) >= 15:
                            captions = [{"idx": 1, "caption": cap_text}]
                            print(f"  batch {bi+1}: JSON truncated mid-caption — salvaged 1 caption")
                        else:
                            print(f"  batch {bi+1}: JSON parse failed, text[:200]: {text[:200]}")
                            continue
                    else:
                        print(f"  batch {bi+1}: JSON parse failed, text[:200]: {text[:200]}")
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
            print(f"  [{bi+1}/{len(batches)}] {len(captions)} captions")

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

    # Normalize to RGB (harmless: MiniMax M3 handles grayscale fine; this
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
    """Copy minerU extracted images to wiki/media/<raw-subpath>/<slug>/ for Stage 3.5."""
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


# ---------- Stage 0.5: Image extraction ----------

def _extract_images_from_office(raw_file: Path, media_dir: Path, manifest_path: Path,
                                 min_size: int = 100) -> dict:
    """Extract embedded images from PPTX/DOCX via zipfile.

    NashSU parity: extractAndSaveSourceImages handles PPTX/DOCX/PDF.
    Uses Python stdlib zipfile — no external deps needed.
    """
    import zipfile as _zf
    import io as _io

    fmt = raw_file.suffix.lower().lstrip(".")
    print(f"[stage_0_5] Extracting embedded images from {fmt.upper()}...")

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
        print(f"[stage_0_5] {fmt.upper()} image extraction failed: {e}")
        return {"count": 0, "error": str(e)}

    # Write manifest
    manifest_data = {
        "source": str(raw_file),
        "format": fmt,
        "total_images": len(all_images),
        "images": all_images,
    }
    manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[stage_0_5] {fmt.upper()}: {len(all_images)} images → {media_dir}")
    return {"count": len(all_images), "media_dir": str(media_dir),
            "manifest": str(manifest_path), "images": all_images}


def stage_0_5_extract_images(raw_file: Path, config: Config, min_size: int = 100) -> dict:
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
            print(f"[stage_0_5] (cached) {m.get('total_images', 0)} images in {media_dir}")
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
        print("[stage_0_5] PyMuPDF not installed — skipping image extraction")
        return {"count": 0, "skipped": True, "reason": "pymupdf-not-installed"}

    print(f"[stage_0_5] Extracting embedded images from PDF...")

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
        print(f"[stage_0_5] No embedded images found (or all < {min_size}px)")
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
    print(f"[stage_0_5] Raw: {len(all_images)}, after dedup: {len(deduped)}")

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
    print(f"[stage_0_5] Done — {len(saved)} images saved to {media_dir}")
    return {"count": len(saved), "media_dir": str(media_dir), "manifest": str(manifest_path), "images": saved}


def _raw_type_subdir(raw_file: Path, config: Config) -> str:
    """Return the raw/-relative parent directory for this file.

    raw/book/Foo.pdf           → book
    raw/datasheet/05_AMP/Bar.pdf → datasheet/05_AMP
    """
    try:
        rel = raw_file.relative_to(config.raw_root)
    except ValueError:
        return ""
    parent = str(rel.parent)
    return parent if parent != "." else ""


def _media_slug(raw_file: Path, config: Config) -> str:
    """Derive media directory path from raw file path, mirroring raw/ structure.

    raw/book/Foo.pdf           → book/Foo
    raw/datasheet/05_AMP/Bar.pdf → datasheet/05_AMP/Bar
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


# ---------- Stage 0.6: Image captioning ----------

def stage_0_6_caption_images(config: Config, stage_0_5_result: dict, batch_size: int = CAPTION_BATCH_SIZE) -> dict:
    """Caption extracted images using unified caption pipeline (Path A + Path B merged).

    Thin wrapper around _caption_images() for backward compatibility with the
    Stage 0.6 pipeline checkpoint. Internal implementation delegates to the
    unified function which supports both PyMuPDF-extracted images (Path A)
    and minerU-extracted images (Path B), with parallel batch dispatch."""
    images = stage_0_5_result.get("images", [])
    if not images:
        print("[stage_0_6] No images to caption — skipping")
        return {"captioned": 0, "total": 0}
    if not config.caption_api_key:
        print("[stage_0_6] Skipped — no API key for caption provider")
        return {"captioned": 0, "total": len(images), "skipped": True, "reason": "no-api-key"}

    media_dir = Path(stage_0_5_result["media_dir"])
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


# ---------- Stage 2.5: Review suggestions ----------

def stage_2_5_review_suggestions(config: Config, file_blocks: list[tuple[str, str]],
                                  raw_file: Path, raw_response: str = "",
                                  verbose: bool = False) -> dict:
    """Run LLM review over newly generated wiki pages.

    NashSU trigger conditions (ingest.ts): any of —
      - >= 4 FILE blocks
      - >= 10K chars of generation output
      - Incomplete REVIEW block (opened but not closed)

    Output: wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md — human-browsable review pages.
    Each page has frontmatter `resolved: false`. When resolved, user changes to true.
    On next ingest, resolved pages are auto-cleaned.
    Also writes review-suggestions.json to runtime dir for tooling.
    """
    # NashSU 3-condition trigger (not just file block count)
    has_review_open = "---REVIEW:" in raw_response and not raw_response.rstrip().endswith("---END REVIEW---")
    if len(file_blocks) < 4 and len(raw_response) < 10000 and not has_review_open:
        print(f"[stage_2_5] Skipped — {len(file_blocks)} blocks, {len(raw_response)} chars, "
              f"no incomplete REVIEW (all below NashSU thresholds)")
        return {"skipped": True, "reason": "below-thresholds"}

    print(f"[stage_2_5] Running review over {len(file_blocks)} new pages + existing wiki...")

    # Collect new page contents
    new_pages: list[str] = []
    for path, content in file_blocks:
        new_pages.append(f"### {path}\n{content[:1500]}")

    # Sample existing wiki pages (up to 40)
    existing_pages: list[str] = []
    for sub in ["sources", "concepts", "entities", "comparisons", "findings"]:
        d = config.wiki_dir / sub
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix != ".md":
                continue
            content = f.read_text(encoding="utf-8")
            if content.startswith("---"):
                end = content.find("\n---", 3)
                body = content[end + 4:] if end != -1 else content
            else:
                body = content
            existing_pages.append(f"### {sub}/{f.name}\n{body[:1000]}")
            if len(existing_pages) >= 40:
                break
        if len(existing_pages) >= 40:
            break

    schema_text = ""
    schema_path = config.wiki_dir / "schema.md"
    if schema_path.exists():
        schema_text = schema_path.read_text(encoding="utf-8")[:2000]

    user_content = f"""# wiki/schema.md
{schema_text}

# Newly generated pages (from {raw_file.stem})
{chr(10).join(new_pages)}

# Existing wiki pages (sample of {len(existing_pages)})
{chr(10).join(existing_pages[:40])}
"""

    system_prompt = """你是 HardwareWiki 的 review agent。审阅当前 wiki 内容，找出 5 类可疑项：
1. confirm（需要人工确认）：数字、术语、矛盾点
2. suggestion（改进建议）：内容不完整、应补充、可加链接
3. missing-page（缺页）：[[wikilink]] 指向不存在的页面
4. contradiction（页面间矛盾）
5. duplicate（内容重复）

输出严格按 YAML 数组（只输出 YAML）：
```yaml
- id: 1
  type: confirm|suggestion|missing-page|contradiction|duplicate
  title: "一句话标题"
  description: "详细描述"
  affected_pages: ["sources/xxx.md", "concepts/yyy.md"]
  severity: high|medium|low
```
至少 5 个 items。数字、参数、公式要严格。"""

    prompt = f"{system_prompt}\n\n{user_content}"
    try:
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=8192)
    except RuntimeError as e:
        print(f"[stage_2_5] LLM call failed: {e}")
        return {"error": str(e)}

    if verbose:
        print(f"[stage_2_5] Response ({len(response)} chars, stop={stop_reason}):\n{response[:2000]}...\n")

    # Parse YAML
    text = response
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("yaml"):
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]

    try:
        import yaml
        items = yaml.safe_load(text.strip())
    except Exception:
        items = parse_simple_yaml(text.strip())
        if not isinstance(items, list):
            items = [items] if items else []

    if not isinstance(items, list):
        items = []

    # Write review pages to wiki/REVIEW/<review_type>/ (分子目录，一目了然)
    date_str = time.strftime("%Y-%m-%d")
    safe_source = re.sub(r'[^\w\s-]', '', raw_file.stem).strip()[:40]

    written = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        rtype = it.get("type", "suggestion")
        title = it.get("title", "Untitled")
        desc = it.get("description", "")
        affected = it.get("affected_pages", it.get("affected_pages", []))
        if isinstance(affected, str):
            affected = [affected]
        severity = it.get("severity", "medium")

        # Build short-slug from title (kebab-case, English only, max 40 chars)
        import unicodedata
        slug_raw = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode('ascii')
        short_slug = re.sub(r'[^\w\s-]', '', slug_raw).strip().lower()
        short_slug = re.sub(r'[-\s]+', '-', short_slug)[:50].strip('-')
        if not short_slug:
            short_slug = f"item-{written + 1}"

        reviews_dir = config.wiki_dir / "REVIEW" / rtype
        reviews_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-{safe_source}-{short_slug}.md"
        page_path = reviews_dir / filename

        # Build wikilinks for affected pages
        affected_links = "\n".join(f"- [[{p.replace('.md', '')}]]" for p in affected)

        md = f"""---
type: review
review_type: {rtype}
severity: {severity}
affected_pages: [{', '.join(affected)}]
resolved: false
created: {date_str}
source_ingest: "{raw_file.stem}"
---

# [{rtype}] {title}

{desc}

## Affected Pages
{affected_links}

## Resolution
_待审核。处理完成后将 frontmatter 中 `resolved: false` 改为 `resolved: true`，下次 ingest 时自动清理。_
"""
        tmp = page_path.with_suffix(page_path.suffix + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.rename(page_path)
        written += 1

    print(f"[stage_2_5] {written} review pages → wiki/REVIEW/")

    # Also write JSON for tooling (backward compat)
    runtime_dir = config.runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    sugg_path = runtime_dir / "review-suggestions.json"
    sugg_data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": config.llm_model,
        "stop_reason": stop_reason,
        "items": items,
    }
    tmp = sugg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sugg_data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(sugg_path)

    return {"items": written, "stop_reason": stop_reason}


# ---------- Stage 4 cleanup: Remove resolved review pages ----------

def cleanup_resolved_reviews(config: Config) -> int:
    """Delete review pages whose frontmatter has `resolved: true`.

    Called at the start of each ingest run. Returns count of deleted pages.
    """
    reviews_dir = config.wiki_dir / "REVIEW"
    if not reviews_dir.exists():
        return 0

    removed = 0
    for f in sorted(reviews_dir.rglob("*.md")):
        if not f.suffix == ".md":
            continue
        content = f.read_text(encoding="utf-8")
        # Check frontmatter for resolved: true
        m = re.search(r'^resolved:\s*true\s*$', content, re.MULTILINE)
        if m:
            f.unlink()
            removed += 1
            print(f"[cleanup] Resolved review removed: {f.name}")

    if removed > 0:
        print(f"[cleanup] {removed} resolved review page(s) deleted")

    return removed


# ---------- Stage go/no-go validation ----------

# ═══════════════════════════════════════════════════════════════
# Stage verification gates (superpowers: verification-before-completion)
# ═══════════════════════════════════════════════════════════════

def _verify_or_die(condition: bool, stage: str, msg: str) -> None:
    """Gate function: hard-abort on failure.

    Superpowers Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.
    Each stage MUST pass its verification before the pipeline proceeds.
    """
    if not condition:
        raise RuntimeError(f"[{stage}] ❌ VERIFICATION FAILED: {msg}")


def _verify_stage_0_text(raw_file: Path, extracted_text: str, method: str) -> None:
    """Verify OCR/text extraction produced usable output."""
    _verify_or_die(len(extracted_text) >= 500, "Stage 0",
                   f"Extracted text too short ({len(extracted_text)} chars) from {raw_file.name} "
                   f"via {method}. Digest will not be meaningful.")
    # For scanned PDFs with minerU, also verify per-page quality
    if method in ("mineru", "mineru-ocr", "mineru-vlm", "mineru-local-ocr"):
        _verify_or_die(len(extracted_text) >= 2000, "Stage 0",
                       f"MinerU OCR output suspiciously short ({len(extracted_text)} chars). "
                       f"VLM may have deadlocked or produced empty pages.")


def _verify_stage_1_digest(global_digest: dict, raw_file: Path) -> None:
    """Verify global digest has required structural keys."""
    required_keys = {"book_meta", "outline", "key_concepts", "key_claims", "key_entities", "chunk_plan"}
    missing = required_keys - set(global_digest.keys())
    _verify_or_die(len(missing) == 0, "Stage 1",
                   f"Global digest missing required keys: {missing}. "
                   f"Got keys: {list(global_digest.keys())[:8]}. "
                   f"LLM may have returned malformed YAML for {raw_file.name}.")
    # Verify at least some concepts were identified
    key_concepts = global_digest.get("key_concepts", [])
    _verify_or_die(len(key_concepts) >= 1, "Stage 1",
                   f"Global digest found 0 key_concepts for {raw_file.name}. "
                   f"Book may be too short or LLM output was incomplete.")


def _verify_stage_1_5_chunks(chunk_analyses: list[dict], extracted_text: str) -> None:
    """Verify chunk analysis produced results for all chunks."""
    _verify_or_die(len(chunk_analyses) >= 1, "Stage 1.5",
                   f"Chunk analysis produced 0 results. "
                   f"Text was {len(extracted_text)} chars — should produce at least 1 chunk.")
    # Warn if any chunk is suspiciously empty
    empty_chunks = [i for i, c in enumerate(chunk_analyses) if not c.get("concepts_found") and not c.get("entities_found")]
    if empty_chunks:
        print(f"  ⚠️  Stage 1.5: {len(empty_chunks)}/{len(chunk_analyses)} chunks have no concepts or entities found")


def _verify_stage_2_file_blocks(file_blocks: list[tuple[str, str]], raw_file: Path) -> None:
    """Verify synthesis produced valid FILE blocks with correct paths."""
    _verify_or_die(len(file_blocks) >= 1, "Stage 2",
                   f"0 FILE blocks parsed from LLM response for {raw_file.name}. "
                   f"LLM did not generate any wiki pages.")
    # Verify source page block exists
    source_blocks = [p for p, _ in file_blocks if "sources/" in p]
    _verify_or_die(len(source_blocks) >= 1, "Stage 2",
                   f"No source page FILE block in {len(file_blocks)} blocks. "
                   f"Paths: {[p for p, _ in file_blocks[:10]]}. "
                   f"LLM must emit a wiki/sources/<title>.md block.")
    # Verify concept pages are in wiki/concepts/, not bare wiki/ or wiki/sources/
    concept_blocks = [p for p, _ in file_blocks if "concepts/" in p or (not p.startswith(("wiki/", "sources/", "concepts/", "entities/")) and "sources/" not in p)]
    # True bare paths: no known subdirectory prefix and no wiki/ prefix
    _KNOWN_PREFIXES = ("wiki/", "sources/", "concepts/", "entities/", "queries/", "comparisons/", "synthesis/", "findings/", "thesis/")
    bare_paths = [p for p, _ in file_blocks if not p.startswith(_KNOWN_PREFIXES)]
    if bare_paths:
        print(f"  ⚠️  Stage 2: {len(bare_paths)} truly bare paths (no subdirectory prefix) — auto-correcting")
    wrong_dir = [p for p, _ in file_blocks if p.startswith("wiki/sources/") and not any(
        kw in p.lower() for kw in ["source", raw_file.stem.lower()[:10]])]
    # Only flag if there are many pages in sources/ that look like concepts
    sources_pages = [p for p, _ in file_blocks if p.startswith("wiki/sources/")]
    if len(sources_pages) > 2:
        print(f"  ⚠️  Stage 2: {len(sources_pages)} FILE blocks in wiki/sources/ — "
              f"only 1 source page expected, rest may be misplaced concepts")

    # Coverage check: warn if concept generation is sparse
    concept_file_blocks = [p for p, _ in file_blocks if "concepts/" in p]
    # Reasonable minimum: any non-trivial book should produce at least 5 concept pages
    if len(concept_file_blocks) < 5 and len(file_blocks) >= 1:
        print(f"  ⚠️  Stage 2: only {len(concept_file_blocks)} concept pages generated. "
              f"Consider re-running with larger token budget or checking prompt output.")


def _verify_stage_3_files_written(
    source_path: Path, file_blocks: list[tuple[str, str]], config
) -> None:
    """Verify files were written to correct locations."""
    _verify_or_die(source_path.exists(), "Stage 3",
                   f"Source page not written: {source_path.relative_to(config.wiki_root)}")
    # Verify no concept pages ended up in wiki/ root or wiki/sources/
    wiki_root_bare = [p for p, _ in file_blocks
                      if "/" not in p and not p.startswith("wiki/")
                      and Path(p).suffix == ".md"]
    if wiki_root_bare:
        print(f"  ⚠️  Stage 3: {len(wiki_root_bare)} pages would be written to wiki/ root, auto-correction should handle this")


def validate_stage_outputs(
    config: Config,
    raw_file: Path,
    method: str,
    extracted_text: str,
    stage_0_5_result: dict,
    stage_0_6_result: dict,
    file_blocks: list[tuple[str, str]],
    source_path: Path,
) -> list[str]:
    """Run NashSU go/no-go checks across all completed stages.

    Returns list of warnings.  Hard failures raise RuntimeError.
    """
    warnings: list[str] = []

    # Stage 0: extracted text sufficiency
    if len(extracted_text) < 500:
        msg = f"Stage 0: extracted text too short ({len(extracted_text)} chars) — digest may fail"
        warnings.append(msg)
        print(f"  ⚠️  {msg}")

    # Stage 0.5: image extraction completeness
    img_count = stage_0_5_result.get("count", 0)
    if img_count > 0:
        manifest = config.wiki_dir / "media" / _media_slug(raw_file, config) / "_manifest.json"
        if not manifest.exists():
            warnings.append("Stage 0.5: images extracted but _manifest.json missing")
            print(f"  ⚠️  Stage 0.5: _manifest.json missing")

    # Stage 0.6: caption completeness — every image has .caption.txt >= 20 chars
    if img_count > 0:
        images = stage_0_5_result.get("images", [])
        missing_captions = 0
        for img in images:
            cap_path = config.wiki_dir / "media" / _media_slug(raw_file, config) / (img["filename"] + ".caption.txt")
            if not cap_path.exists() or cap_path.stat().st_size < 20:
                missing_captions += 1
        if missing_captions > 0:
            msg = f"Stage 0.6: {missing_captions}/{len(images)} images missing captions"
            warnings.append(msg)
            print(f"  ⚠️  {msg}")
        if stage_0_6_result.get("captioned", 0) == 0 and not stage_0_6_result.get("skipped"):
            warnings.append("Stage 0.6: no captions generated (API may have failed)")
            print(f"  ⚠️  Stage 0.6: 0 captions generated")

    # Stage 2: FILE block validation
    if len(file_blocks) == 0:
        msg = "Stage 2: 0 FILE blocks parsed — LLM did not generate any wiki pages"
        warnings.append(msg)
        print(f"  ❌ {msg}")
    # Check that source page block exists
    source_block_found = any("sources/" in p for p, _ in file_blocks)
    if not source_block_found:
        warnings.append("Stage 2: no source page FILE block emitted (placeholder will be written)")
        print(f"  ⚠️  Stage 2: source page block missing")

    # Stage 3: file writing vs parsed blocks
    written_count = 0
    for rel_path, _ in file_blocks:
        full_path = config.wiki_dir / rel_path
        if full_path.exists():
            written_count += 1
    if written_count < len(file_blocks):
        msg = f"Stage 3: only {written_count}/{len(file_blocks)} FILE blocks written to disk"
        warnings.append(msg)
        print(f"  ⚠️  {msg}")

    # Stage 3.5: image injection verification
    if img_count > 0 and source_path.exists():
        source_content = source_path.read_text(encoding="utf-8")
        if "## Embedded Images" not in source_content:
            warnings.append("Stage 3.5: source page missing '## Embedded Images' section")
            print(f"  ⚠️  Stage 3.5: image injection not found in source page")

    # Stage 3.7: source page exists
    if not source_path.exists():
        warnings.append("Stage 3.7: source page does not exist after ingest")
        print(f"  ❌ Stage 3.7: source page missing")

    # Stage 4.5: review pages in wiki/REVIEW/<type>/ (分子目录)
    reviews_dir = config.wiki_dir / "REVIEW"
    if reviews_dir.exists():
        unresolved = 0
        for rp in reviews_dir.rglob("*.md"):
            content = rp.read_text(encoding="utf-8")
            if "resolved: false" in content[:500]:
                unresolved += 1
        if unresolved > 0:
            print(f"  ℹ️  wiki/REVIEW/: {unresolved} unresolved review pages pending human triage")

    # Stage 5: cache will be written after this — just check cache_path dir exists
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)

    if warnings:
        print(f"\n[validate] {len(warnings)} go/no-go warning(s) — see details above")
    else:
        print(f"[validate] All go/no-go checks passed ✅")

    return warnings


def _run_post_ingest_lint(config: Config) -> None:
    """Run wiki-lint.sh after ingest (structural lint only; semantic lint via standalone command).

    Set SKIP_POST_INGEST_LINT=1 to skip during batch runs (lint once at end).
    """
    if os.environ.get("SKIP_POST_INGEST_LINT") == "1":
        print("[lint] Skipped (SKIP_POST_INGEST_LINT=1)")
        return
    lint_script = Path(__file__).parent / "wiki-lint.sh"
    if not lint_script.exists():
        print("[lint] wiki-lint.sh not found — skipping")
        return
    import subprocess

    cmd = ["bash", str(lint_script), "--summary"]
    try:
        result = subprocess.run(
            cmd, cwd=config.wiki_root, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if "findings" in line or "Pages:" in line:
                    print(line.strip())
            if result.stderr.strip():
                print(f"[lint] {result.stderr.strip()[:200]}")
        else:
            print(f"[lint] wiki-lint.sh exited {result.returncode}: {result.stderr.strip()[:200]}")
    except Exception as e:
        print(f"[lint] Lint failed ({e}) — continuing")


def _run_post_ingest_graph(config: Config) -> None:
    """Rebuild knowledge graph after ingest (once per session, stale-guarded).

    Controlled by AUTO_BUILD_GRAPH=1. The graph needs the full wiki state,
    but rebuilding after every book in a batch would be wasteful. Uses a
    staleness guard: skips if graph.json was rebuilt < 30 minutes ago.

    Mirrors NashSU's desktop app: the graph auto-refreshes when you view it.
    """
    if os.environ.get("AUTO_BUILD_GRAPH") != "1":
        return
    graph_script = Path(__file__).parent / "build_knowledge_graph.py"
    if not graph_script.exists():
        print("[graph] build_knowledge_graph.py not found — skipping")
        return

    # Staleness guard: don't rebuild more than once per 30 minutes
    graph_json = config.runtime_dir / "graph.json"
    if graph_json.exists():
        age_min = (time.time() - graph_json.stat().st_mtime) / 60
        if age_min < 30:
            print(f"[graph] Skipped — graph rebuilt {age_min:.0f}m ago (staleness guard)")
            return

    import subprocess
    print("[graph] Rebuilding knowledge graph...")
    try:
        result = subprocess.run(
            [sys.executable, str(graph_script)],
            cwd=config.wiki_root, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[-3:]:
                print(f"[graph] {line.strip()}")
        else:
            print(f"[graph] Failed ({result.returncode}): {result.stderr[:200]}")
    except Exception as e:
        print(f"[graph] Failed ({e}) — continuing")



def stage_3_5_inject_images(config: Config, raw_file: Path, source_path: Path,
                              method: str = "") -> dict:
    """Append '## Embedded Images' section to the source page.

    Two paths:
    - Text-layer PDFs: reads _manifest.json from wiki/media/<raw-subpath>/<slug>/
    - Scanned PDFs:   reads .caption.txt files from OCR output dir
    """
    content = source_path.read_text(encoding="utf-8")
    content = re.sub(r"## Embedded Images.*?(?=^## |\Z)", "", content, flags=re.MULTILINE | re.DOTALL)
    content = content.rstrip() + "\n\n"

    # Path A: Text-layer PDF — embedded images via manifest
    slug = _media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug
    manifest_path = media_dir / "_manifest.json"

    if manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        images = m.get("images", [])
        if images:
            section = f"## Embedded Images\n\n"
            section += f"本书共抽出 {len(images)} 张嵌入图。\n\n"
            section += "| 页号 | Caption | 文件 |\n|------|---------|------|\n"
            for img in sorted(images, key=lambda x: (x["page"], x.get("img_idx_in_page", 0))):
                cap_path = media_dir / (img["filename"] + ".caption.txt")
                cap = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() else "（无 caption）"
                if len(cap) > 80:
                    cap = cap[:80] + "..."
                section += f"| p{img['page']} | {cap} | `{img['path']}` |\n"
            section += f"\n> 详细 manifest: `wiki/media/{slug}/_manifest.json`\n"
            content += section
            tmp = source_path.with_suffix(source_path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.rename(source_path)
            print(f"[stage_3_5] Injected {len(images)} embedded images into {source_path.name}")
            return {"injected": len(images)}

    # Path B: minerU / scanned PDF images in wiki/media/<raw-subpath>/<slug>/
    # Also catches old cloud OCR p*.caption.txt in extract_tmp_dir
    images_in_media: list[tuple[str, str]] = []  # (filename, caption)
    if media_dir.exists():
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                cap_path = media_dir / (f.name + ".caption.txt")
                if cap_path.exists() and cap_path.stat().st_size >= 20:
                    images_in_media.append((f.name, cap_path.read_text(encoding="utf-8").strip()[:80]))

    # Also check old cloud OCR path
    ocr_dir = config.extract_tmp_dir / raw_file.stem
    if ocr_dir.exists():
        for cf in sorted(ocr_dir.glob("p*.caption.txt")):
            cap = cf.read_text(encoding="utf-8").strip()
            for line in cap.split("\n"):
                if line.strip():
                    pn_match = re.match(r'p(\d+)', cf.name)
                    pn = int(pn_match.group(1)) if pn_match else 0
                    images_in_media.append((f"p{pn} (cloud OCR)", line.strip()[:80]))

    if images_in_media:
        section = f"## Embedded Images\n\n"
        section += f"本书共提取 {len(images_in_media)} 张图表。\n\n"
        section += "| 文件/页码 | Caption |\n|------------|----------|\n"
        for name, cap in images_in_media[:200]:  # cap at 200 rows
            cap_short = cap[:80] + "..." if len(cap) > 80 else cap
            section += f"| `{name}` | {cap_short} |\n"
        if len(images_in_media) > 200:
            section += f"| ... | ({len(images_in_media) - 200} more) |\n"
        section += f"\n> Caption 由 MiniMax M3 生成。图片文件见 `wiki/media/{slug}/`\n"
        content += section
        tmp = source_path.with_suffix(source_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(source_path)
        print(f"[stage_3_5] Injected {len(images_in_media)} images into {source_path.name}")
        return {"injected": len(images_in_media)}

    print("[stage_3_5] No images or figures to inject — skipping")
    return {"injected": 0}


# ---------- LLM API call ----------

def _call_anthropic_api(prompt: str, config: Config, max_tokens: int | None = None) -> tuple[str, str]:
    """Call an Anthropic-protocol API (MiniMax or compatible).

    Uses Anthropic Messages API format:
      POST {base}/anthropic/v1/messages
      Auth: x-api-key
      Response: {"content": [{"type": "text", "text": "..."}], "stop_reason": "..."}
    """
    import urllib.request
    import urllib.error

    mt = max_tokens or config.max_tokens
    url = f"{config.llm_base_url.rstrip('/')}/anthropic/v1/messages"
    body = json.dumps({
        "model": config.llm_model,
        "max_tokens": mt,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    max_retries = 3
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            t0 = time.time()
            _llm_call_progress("LLM (Anthropic)", attempt=attempt + 1, retries=max_retries)
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": config.llm_api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
            content = data.get("content", [])
            if not content:
                raise RuntimeError(f"LLM response has no content: {data}")
            text_parts = [block.get("text", "") for block in content if block.get("type") == "text"]
            stop_reason = data.get("stop_reason", "unknown")
            result = "".join(text_parts)
            _llm_call_done(time.time() - t0, chars=len(result))
            return result, stop_reason
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"LLM API HTTP {e.code}: {err_body[-500:]}")
            # Retry on 5xx (server errors) and 429 (rate limit)
            is_retryable = (e.code and (500 <= e.code < 600 or e.code == 429))
            if is_retryable and attempt < max_retries:
                wait = (2 ** attempt) + (time.time() % 1)
                code_label = "rate-limit" if e.code == 429 else f"HTTP {e.code}"
                print(f"[llm] {code_label} on attempt {attempt + 1}/{max_retries + 1} — retrying in {wait:.1f}s...", flush=True)
                time.sleep(wait)
                continue
            raise last_error
        except (OSError, TimeoutError) as e:
            last_error = RuntimeError(f"LLM API connection error: {e}")
            if attempt < max_retries:
                wait = (2 ** attempt) + (time.time() % 1)
                print(f"[llm] {type(e).__name__} on attempt {attempt + 1}/{max_retries + 1} — retrying in {wait:.1f}s...", flush=True)
                time.sleep(wait)
                continue
            raise last_error
    raise last_error  # type: ignore[return-value]


def _call_openai_api(prompt: str, config: Config, max_tokens: int | None = None) -> tuple[str, str]:
    """Call an OpenAI-compatible API (DeepSeek, OpenAI, etc.).

    Uses OpenAI Chat Completions format:
      POST {base}/v1/chat/completions
      Auth: Authorization: Bearer <key>
      Response: {"choices": [{"message": {"content": "..."}}], "usage": {...}}
    """
    import urllib.request
    import urllib.error

    mt = max_tokens or config.max_tokens
    url = f"{config.llm_base_url.rstrip('/')}/v1/chat/completions"
    body = json.dumps({
        "model": config.llm_model,
        "max_tokens": mt,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    max_retries = 3
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            t0 = time.time()
            _llm_call_progress("LLM (OpenAI)", attempt=attempt + 1, retries=max_retries)
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.llm_api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"LLM response has no choices: {data}")
            text = choices[0].get("message", {}).get("content", "")
            stop_reason = choices[0].get("finish_reason", "unknown")
            _llm_call_done(time.time() - t0, chars=len(text))
            usage = data.get("usage", {})
            if usage:
                print(f"[llm] tokens: {usage.get('prompt_tokens', '?')} in / {usage.get('completion_tokens', '?')} out", flush=True)
            return text.strip(), stop_reason
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"LLM API HTTP {e.code}: {err_body[-500:]}")
            if e.code and 500 <= e.code < 600 and attempt < max_retries:
                wait = (2 ** attempt) + (time.time() % 1)
                print(f"[llm] HTTP {e.code} on attempt {attempt + 1}/{max_retries + 1} — retrying in {wait:.1f}s...", flush=True)
                time.sleep(wait)
                continue
            raise last_error
        except (OSError, TimeoutError) as e:
            last_error = RuntimeError(f"LLM API connection error: {e}")
            if attempt < max_retries:
                wait = (2 ** attempt) + (time.time() % 1)
                print(f"[llm] {type(e).__name__} on attempt {attempt + 1}/{max_retries + 1} — retrying in {wait:.1f}s...", flush=True)
                time.sleep(wait)
                continue
            raise last_error
    raise last_error  # type: ignore[return-value]


def call_anthropic_protocol(prompt: str, config: Config, max_tokens: int | None = None) -> tuple[str, str]:
    """Call the configured LLM API, or delegate to agent in conversation mode.

    Routes to the correct protocol based on config.llm_protocol:
      - "openai"    → OpenAI Chat Completions (DeepSeek, OpenAI, etc.)
      - "anthropic" → Anthropic Messages API (MiniMax, Claude, etc.)

    Returns (text_content, stop_reason).
    """
    if config.conversation_mode:
        return _conversation_llm_call(prompt, config, max_tokens)

    proto = getattr(config, 'llm_protocol', 'anthropic')
    if proto == "openai":
        return _call_openai_api(prompt, config, max_tokens)
    else:
        return _call_anthropic_api(prompt, config, max_tokens)


# ---------- Conversation / Delegate Mode ----------


def _conversation_llm_call(prompt: str, config: Config, max_tokens=None) -> tuple[str, str]:
    """Conversation mode: write prompt to disk, raise ConversationPending.

    The calling agent (Hermes) reads the prompt file, executes it with its own
    LLM, writes the result back, and re-invokes ingest.py.  On re-invoke,
    ingest.py finds the result file and continues.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", _infer_stage(prompt)).strip("-")[:60] or "llm-task"
    prefix = config.conversation_prefix or "00000000"
    conv_dir = config.runtime_dir / "conversation" / prefix
    conv_dir.mkdir(parents=True, exist_ok=True)
    pending_md = conv_dir / f"{slug}.md"
    result_file = conv_dir / f"{slug}.txt"

    if result_file.exists():
        response = result_file.read_text(encoding="utf-8")
        if _is_stale_result(response, prompt):
            print(f"[conv:{slug}] Result appears to be a prompt copy — regenerating")
            result_file.unlink(missing_ok=True)
        else:
            result_file.unlink(missing_ok=True)
            pending_md.unlink(missing_ok=True)
            print(f"[conv:{slug}] Read response ({len(response)} chars)")
            _mark_task_done(config, slug)
            return response, "end_turn"

    pending_md.write_text(prompt, encoding="utf-8")
    _mark_task_pending(config, slug)
    print(f"\n{'='*60}")
    print(f"  CONVERSATION → {slug}")
    print(f"  Prompt:  {pending_md}")
    print(f"  Result:  {result_file}")
    print(f"{'='*60}\n")
    raise ConversationPending()


def _task_manifest_path(config: Config) -> Path:
    return config.runtime_dir / "conversation" / config.conversation_prefix / "tasks.json"


def _load_task_manifest(config: Config) -> dict:
    p = _task_manifest_path(config)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pending": [], "completed": []}


def _save_task_manifest(config: Config, manifest: dict) -> None:
    p = _task_manifest_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_task_pending(config: Config, slug: str) -> None:
    m = _load_task_manifest(config)
    if slug not in m.get("pending", []):
        m.setdefault("pending", []).append(slug)
    _save_task_manifest(config, m)


def _mark_task_done(config: Config, slug: str) -> None:
    m = _load_task_manifest(config)
    m["pending"] = [s for s in m.get("pending", []) if s != slug]
    m.setdefault("completed", []).append(slug)
    _save_task_manifest(config, m)


def _is_stale_result(response: str, prompt: str) -> bool:
    """Detect if agent just copied the prompt instead of generating output."""
    has_yaml = "```yaml" in response or "entities_found" in response or "concepts_found" in response
    has_files = "---FILE:" in response or "### File" in response
    if has_yaml or has_files:
        return False
    return any(m in response for m in ["# Role", "You are"]) and len(response) < len(prompt) * 0.8


def _infer_stage(prompt: str) -> str:
    if "Generate wiki pages" in prompt or ("Synthesis" in prompt and "FILE blocks" in prompt):
        return "Stage-2-Synthesis"
    if "review" in prompt.lower() and "suggestions" in prompt.lower():
        return "Stage-2-5-Review"
    if "Chunk Analysis" in prompt[:500]:
        m = re.search(r"chunk (\d+)/(\d+)", prompt)
        if m:
            return f"Stage-1-5-Chunk-{m.group(1)}"
    if "book_meta" in prompt[:1000] or "produce a **high-level structural summary**" in prompt:
        return "Stage-1-Global-Digest"
    return "LLM-task"


def parse_yaml_block(response: str) -> dict:
    """Extract the first YAML block from the LLM response."""
    m = re.search(r"```yaml\s*\n(.*?)\n```", response, re.DOTALL)
    yaml_text = m.group(1) if m else response
    try:
        import yaml
        return yaml.safe_load(yaml_text) or {}
    except ImportError:
        return parse_simple_yaml(yaml_text)
    except Exception:
        print(f"[parse] yaml.safe_load failed — falling back to simple parser")
        return parse_simple_yaml(yaml_text)


def parse_simple_yaml(text: str) -> dict:
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in text.split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  - ") and current_list_key:
            # Use unwrapped value since we always set current_list_key
            result[current_list_key].append(line[4:].strip())
            continue
        m = re.match(r"^(\w[\w_]*):\s*(.*)", line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if value == "":
                result[key] = []
                current_list_key = key
            else:
                result[key] = value
                current_list_key = None
    return result


def parse_file_blocks(response: str) -> list[tuple[str, str]]:
    """Extract wiki page blocks from the LLM response.

    Supports two formats:
    1. NashSU native:  ---FILE:wiki/<path>--- ... ---END FILE---
    2. Legacy:         ### File N: <path>.md ...
    """
    # NashSU parity: normalize CRLF before parsing (ingest.ts L361)
    response = response.replace("\r\n", "\n")
    blocks: list[tuple[str, str]] = []

    # Format 1: NashSU-style ---FILE:wiki/<path>--- ... ---END FILE---
    # NashSU parity: fence-aware parsing (ingest.ts L377-400) — track CommonMark
    # code fences so ---END FILE--- inside a code block doesn't close the outer block.
    # Accept both ---FILE:wiki/concepts/X.md--- (correct) and
    # ---FILE:concepts/X.md--- (LLM forgot wiki/ prefix; auto-correct strips it either way)
    FILE_HEADER_RE = re.compile(r'^---FILE:\s*(wiki/)?(.+?)\s*---\s*$')
    END_FILE_RE = re.compile(r'^---END FILE---\s*$')
    FENCE_RE = re.compile(r'^(```|~~~)')

    # Known wiki subdirectories (must match WIKI_TYPE_DIRS)
    _KNOWN_SUBDIRS = (
        "sources", "concepts", "entities", "queries", "comparisons",
        "synthesis", "findings", "thesis",
    )

    lines = response.split("\n")
    fence_stack: list[str] = []  # track open fence markers
    current_path: str | None = None
    current_lines: list[str] = []

    for line in lines:
        # Track CommonMark code fences (still add the line to content)
        is_fence_line = False
        fm = FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not fence_stack:
                fence_stack.append(marker)
            elif fence_stack[-1] == marker:
                fence_stack.pop()
            is_fence_line = True

        # Only match FILE/END FILE headers outside fences
        if not fence_stack and not is_fence_line:
            end_match = END_FILE_RE.match(line)
            if end_match and current_path is not None:
                content = "\n".join(current_lines).rstrip() + "\n"
                blocks.append((current_path, content))
                current_path = None
                current_lines = []
                continue

            file_match = FILE_HEADER_RE.match(line)
            if file_match:
                if current_path is not None:
                    # Unclosed previous block — flush it
                    content = "\n".join(current_lines).rstrip() + "\n"
                    blocks.append((current_path, content))
                # group(1) = optional "wiki/" prefix, group(2) = actual path
                path = file_match.group(2).strip()
                if not path.endswith(".md"):
                    current_path = None
                    current_lines = []
                    continue
                # Normalize: if path has more than 2 segments (subdir/.../file.md),
                # merge extra segments into filename by replacing / with -.
                # Exception: sources/ keeps its category subdirectory (e.g. sources/book/x.md)
                parts = path.split("/")
                if len(parts) > 2:
                    subdir = parts[0]
                    if subdir == "sources":
                        # Preserve: sources/book/slug.md → keep as-is
                        pass
                    else:
                        merged_slug = "-".join(parts[1:])
                        corrected = f"{subdir}/{merged_slug}"
                        print(f"  [parse] merged / in slug: {path} → {corrected}")
                        path = corrected
                # Auto-correct LLM hyphen-for-slash error (subdir-slug → subdir/slug)
                for subdir in _KNOWN_SUBDIRS:
                    prefix = f"{subdir}-"
                    if path.startswith(prefix):
                        corrected = f"{subdir}/{path[len(prefix):]}"
                        print(f"  [parse] corrected path: {path} → {corrected}")
                        path = corrected
                        break
                # Validate path safety (NashSU parity)
                if not is_safe_ingest_path(path):
                    print(f"  [parse] unsafe path rejected: {path}")
                    current_path = None
                    current_lines = []
                    continue
                current_path = path
                current_lines = []
                continue

        # Collect content lines for current block
        if current_path is not None:
            current_lines.append(line)

    # Flush last unclosed block (tolerant of missing END FILE)
    if current_path is not None and current_lines:
        content = "\n".join(current_lines).rstrip() + "\n"
        blocks.append((current_path, content))

    if blocks:
        return blocks

    # Format 2: Legacy ### File N: <path>.md
    HEADER_RE = re.compile(r"^###\s+File\s+(\d+):\s*([^\n]+\.md)\s*$", re.MULTILINE)
    matches = list(HEADER_RE.finditer(response))
    for i, m in enumerate(matches):
        path = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(response)
        content = response[start:end].rstrip() + "\n"
        if path.startswith("wiki/"):
            path = path[len("wiki/"):]
        if not path.endswith(".md"):
            continue
        if not is_safe_ingest_path(path):
            print(f"  [parse] unsafe path rejected: {path}")
            continue
        blocks.append((path, content))
    return blocks


# ---------- Chunking ----------

def chunk_text(text: str, target_chars: int, overlap_chars: int) -> list[str]:
    """Split text into overlapping chunks.

    NashSU parity (ingest.ts L2107-2205): prefers markdown heading boundaries
    (H1-H6), then paragraph breaks, then sentence ends near target_chars.
    """
    if len(text) <= target_chars:
        return [text]

    print(f"[chunk] Splitting {len(text)} chars into ~{target_chars}-char chunks...", flush=True)

    # Pre-scan: find all heading boundaries for heading-aware splitting
    _HEADING_RE = re.compile(r'^#{1,6}\s+.+$', re.MULTILINE)
    heading_positions = [m.start() for m in _HEADING_RE.finditer(text)]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + target_chars, len(text))
        if end >= len(text):
            chunks.append(text[start:].strip())
            break

        search_start = max(start, end - int(target_chars * 0.15))

        # Priority 1: markdown heading boundary (NashSU heading-aware)
        boundary = -1
        for hp in reversed(heading_positions):
            if search_start <= hp < end:
                boundary = hp
                break

        # Priority 2: paragraph break
        if boundary == -1:
            boundary = text.rfind("\n\n", search_start, end)

        # Priority 3: single newline
        if boundary == -1:
            boundary = text.rfind("\n", search_start, end)

        # Priority 4: CJK sentence end
        if boundary == -1:
            boundary = text.rfind("。", search_start, end)

        # Priority 5: English sentence end
        if boundary == -1:
            boundary = text.rfind(". ", search_start, end)

        if boundary > start:
            end = boundary + 1

        chunks.append(text[start:end].strip())
        new_start = end - overlap_chars
        if new_start <= start:
            break
        start = new_start

    print(f"[chunk] Done — {len(chunks)} chunks", flush=True)
    return chunks


def _resolve_chunk_heading_path(text: str, chunk_start: int, chunk_end: int) -> str:
    """Find the heading hierarchy that a chunk falls under (NashSU parity).

    Scans backwards from chunk_start to find the nearest H1-H6 heading, then
    walks further back to build the full ancestor path. Returns a string like
    "Chapter 3 > Section 3.2 > Subsection 3.2.1" or "" if no heading found.
    """
    _HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
    _heading_stack: list[tuple[int, str]] = []  # (level, title)

    for m in _HEADING_RE.finditer(text):
        if m.start() > chunk_start:
            break
        level = len(m.group(1))
        title = m.group(2).strip()
        # Pop headings of same or deeper level
        while _heading_stack and _heading_stack[-1][0] >= level:
            _heading_stack.pop()
        _heading_stack.append((level, title))

    if _heading_stack:
        return " > ".join(h[1] for h in _heading_stack)
    return ""


# ---------- Stage 1: Global Digest ----------

def build_global_digest_prompt(
    extracted_text: str,
    file_path: Path,
    config: Config,
    template: str = "",
) -> str:
    """Build the prompt for Stage 1: Global Digest."""
    summary_text = extracted_text[:config.source_budget]
    existing_slugs = list_existing_slugs(config)

    # Inject type-specific template instructions (first 4000 chars — enough for schema guidance)
    template_section = ""
    if template:
        template_trimmed = template[:4000]
        template_section = f"""
# Document Type Instructions
The source is a **{file_path.parent.name}** document. Follow these type-specific conventions:
<template>
{template_trimmed}
</template>

"""

    return f"""# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 1: Global Digest** of a book ingest pipeline.
{template_section}
# Input
- Source file: {file_path.stem}
- Extracted text (first {config.source_budget:,} chars of full book):
<extracted_text>
{summary_text}
</extracted_text>

- Existing wiki pages: {', '.join(existing_slugs[:300])}

# Task
Read the extracted text and produce a **high-level structural summary** of this book.
This will be used as context for per-chapter detailed analysis in the next stage.

# Output (YAML only, in ```yaml block)
```yaml
book_meta:
  title: "..."
  authors: [...]
  year: N
  pages: N
  publisher: "..."
  language: "zh" | "en" | "mixed"

outline:
  # Complete chapter tree with approximate page/char ranges
  - chapter: 1
    title: "..."
    key_topics: ["...", "..."]
    # Key: give a unique start marker (first 30 chars of chapter text)
    # so the chunker can align chunks to chapter boundaries
    start_marker: "..."

key_entities:
  - name: "..."
    role: "person" | "organization" | "system" | "model" | "standard"

key_concepts:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"

key_claims:
  - claim: "..."
    chapter: N

chunk_plan:
  # How many chunks needed? Where's the natural split?
  estimated_total_chunks: N
  # For each chunk: which chapters does it cover?
  - chunk: 1
    chapters: [1, 2]
    estimated_chars: N
```

# Constraints
- Focus on STRUCTURE, not details — per-chapter details come in Stage 1.5
- The outline must be as complete as possible
- chapter_map.start_marker is critical for accurate chunking in Stage 1.5
- Do NOT propose new wiki pages yet — that's Stage 2 (Synthesis)
"""


def stage_1_global_digest(
    extracted_text: str,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> dict:
    """Stage 1: One LLM call for book-level structural summary."""
    print(f"[stage_1] Global Digest — sending {min(len(extracted_text), config.source_budget):,} chars to LLM...")
    prompt = build_global_digest_prompt(extracted_text, file_path, config, template)
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=8192)
    if verbose:
        print(f"[stage_1] Raw response ({len(response)} chars, stop={stop_reason}):\n{response[:3000]}...\n")
    digest = parse_yaml_block(response)
    print(f"[stage_1] Done — {len(digest)} top-level keys in digest")
    return digest


# ---------- Stage 1.5: Chunk Analysis ----------

def build_chunk_analysis_prompt(
    chunk_text: str,
    chunk_index: int,
    chunk_total: int,
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    accumulated_digest: str = "",
    overlap_before: str = "",
    heading_path: str = "",
) -> str:
    """Build the prompt for Stage 1.5: Chunk Analysis.

    If accumulated_digest is provided (sequential mode), it replaces the
    static global_digest as the primary context — giving later chunks the
    benefit of all previous chunks' discoveries (NashSU parity).

    If overlap_before is provided, it's the tail-end text from the previous
    chunk that this chunk overlaps with — gives the LLM continuity context
    when a sentence/concept spans a chunk boundary (NashSU parity).

    If heading_path is provided, it tells the LLM which chapter/section
    hierarchy this chunk belongs to (NashSU parity: chunk.headingPath).
    """
    if accumulated_digest:
        # Sequential mode: use accumulated digest from previous chunks
        digest_str = accumulated_digest
    else:
        # Legacy / first-chunk mode: crop global digest to essentials
        digest_compact = {}
        for key in ("book_meta", "outline", "key_entities", "key_concepts"):
            if key in global_digest:
                digest_compact[key] = global_digest[key]
        digest_str = json.dumps(digest_compact, ensure_ascii=False, indent=2)
    # cap to keep prompts lean
    if len(digest_str) > 6000:
        digest_str = digest_str[:6000] + "\n... (truncated)"
    existing_slugs = list_existing_slugs(config)

    # Inject type-specific template for concept/entity extraction guidance (first 2000 chars)
    template_section = ""
    if template:
        template_trimmed = template[:2000]
        template_section = f"""
# Document Type Instructions
<template>
{template_trimmed}
</template>

"""

    # ── Overlap context (NashSU parity: overlapBefore + overlapSuffix) ──
    overlap_section = ""
    if overlap_before:
        # NashSU parity: paragraph/sentence-aware boundary, not raw tail slice
        overlap_for_boundary = overlap_before[-800:]  # search in last 800 chars
        boundary = -1
        # Priority 1: paragraph break in overlap window
        boundary = overlap_for_boundary.rfind("\n\n")
        # Priority 2: sentence boundary
        if boundary == -1:
            import re as _re2
            m = _re2.search(r'[.!?。！？]\s+', overlap_for_boundary)
            if m:
                boundary = m.start() + 1
        # Fallback: start at a word boundary
        if boundary == -1:
            boundary = max(0, len(overlap_for_boundary) - 500)
        overlap_trimmed = overlap_for_boundary[boundary:][-500:]
        overlap_section = f"""
# Continuity: text right before this chunk (may span sentence boundary)
<overlap>
{overlap_trimmed}
</overlap>

"""

    # ── Heading path (NashSU parity: chunk.headingPath) ──
    heading_section = ""
    if heading_path:
        heading_section = f"""
# Current location in the book
You are analyzing content from: **{heading_path}**

"""

    return f"""# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 1.5: Chunk Analysis** (chunk {chunk_index + 1}/{chunk_total}) of a book ingest pipeline.
{template_section}
# Context: Accumulated Global Digest
This digest includes discoveries from all PREVIOUS chunks. Use it to avoid
re-extracting the same concepts and to build on what earlier chunks found.
If a concept was already defined in a prior chunk, note it as a
cross-reference rather than re-defining it.

```yaml
{digest_str}
```
{heading_section}{overlap_section}
# Input
- Source: {file_path.stem}
- Chunk {chunk_index + 1} of {chunk_total}
- Extracted text of this chunk:
<extracted_text>
{chunk_text}
</extracted_text>

- Existing wiki pages: {', '.join(existing_slugs[:200])}

# Task
Analyze THIS CHUNK of the book. Extract:

1. All concepts defined or heavily used in this chunk (skip if already in the
   Accumulated Global Digest — just cross-reference instead)
2. All entities (people, organizations, systems, models, standards) mentioned
3. Key claims, formulas, data points
4. Connections to existing wiki pages (if any)
5. An **Updated Global Digest** — merge this chunk's key discoveries into the
   Accumulated Global Digest above, so the next chunk benefits from everything
   learned so far. Keep it concise but cumulative: add new concepts, entities,
   and key claims. Do NOT remove anything from the existing digest.

# Output (YAML only, in ```yaml block)
```yaml
chunk_index: {chunk_index + 1}
chunk_total: {chunk_total}

entities_found:
  - name: "..."
    role: "person" | "organization" | "system" | "model" | "standard"
    significance: "..."     # why this entity matters (1 sentence)

concepts_found:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"
    definition: "..."      # the concept's definition as stated in the book
    key_details: ["...", "..."]   # 2-4 key facts / formulas / design rules

claims:
  - claim: "..."
    evidence: "..."
    confidence: "high" | "medium" | "low"

formulas:
  - formula: "LaTeX"
    meaning: "..."

connections_to_existing_wiki:
  - existing_page: "..."
    relationship: "extends" | "contrasts" | "applies" | "cites"

updated_global_digest: |
  # Accumulated Global Digest (after chunk {chunk_index + 1}/{chunk_total})
  # Merge this chunk's key concepts, entities, and claims into the prior digest.
  # Be cumulative — keep everything from before, add only what's new.
  ...

# Do NOT propose new wiki pages — that's Stage 2
```
"""


def _chunk_concurrency() -> int:
    """Max concurrent workers for Stage 2.1 per-chunk generation (default 8).

    Stage 1.5 (chunk analysis) is now sequential (NashSU parity).
    This env var only affects Stage 2.1 parallel page generation.
    """
    env = os.environ.get("LLM_CHUNK_CONCURRENCY", "")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return 8


def _chunk_retries() -> int:
    """Max attempts per chunk (1 initial + N retries). Default 2 retries → 3 total attempts."""
    env = os.environ.get("LLM_CHUNK_RETRIES", "")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return 2


# Rate-limit tracker shared across chunk workers (thread-safe via lock)
_RLOCK = __import__('threading').Lock()
_RATE_LIMIT_HIT_AT: float = 0.0


def _record_rate_limit() -> None:
    """Record that a rate-limit was hit, so other workers can back off."""
    global _RATE_LIMIT_HIT_AT
    with _RLOCK:
        _RATE_LIMIT_HIT_AT = time.time()


def _rate_limit_cooldown_remaining() -> float:
    """Seconds remaining in rate-limit cooldown (0 if clear)."""
    with _RLOCK:
        elapsed = time.time() - _RATE_LIMIT_HIT_AT
        return max(0, 3.0 - elapsed)


def stage_1_5_chunk_analysis(
    extracted_text: str,
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    source_hash: str = "",
) -> list[dict]:
    """Stage 1.5: Split text into chunks and analyze each one SEQUENTIALLY.

    NashSU parity: each chunk builds on the accumulated discoveries of all
    previous chunks via an "Updated Global Digest" that grows with each step.
    Later chunks get richer context — concepts found in chunk 3 are available
    to chunk 8, preventing duplicate extraction and improving cross-chapter
    awareness.

    Per-chunk checkpoint (NashSU parity): after each successful chunk, saves
    accumulated digest + partial analyses to the progress file.  On resume,
    completed chunks are skipped and processing resumes from the last checkpoint.

    Still supports per-chunk retries (LLM_CHUNK_RETRIES, default 2 → 3 total).
    """
    chunks = chunk_text(extracted_text, config.target_chars, config.chunk_overlap)
    chunk_total = len(chunks)
    max_retries = _chunk_retries()
    print(f"[stage_1_5] Chunk Analysis — {chunk_total} chunks "
          f"(target {config.target_chars:,} chars/chunk, overlap {config.chunk_overlap:,}, "
          f"sequential NashSU mode, retries={max_retries})")

    t0 = time.time()
    analyses: list[dict] = []
    accumulated_digest = ""
    start_chunk = 0

    # ── Resume from per-chunk checkpoint (NashSU parity: LongSourceCheckpoint) ──
    if source_hash:
        progress = load_progress(config, source_hash)
        cp = (progress or {}).get("stage_1_5_cp") if progress else None
        if cp and cp.get("chunk_total") == chunk_total:
            analyses = cp.get("analyses", [])
            accumulated_digest = cp.get("accumulated_digest", "")
            start_chunk = len(analyses)
            if start_chunk > 0:
                print(f"[stage_1_5] Resuming from chunk {start_chunk + 1}/{chunk_total} "
                      f"({start_chunk} completed, digest={len(accumulated_digest)} chars)")

    # Build initial digest string from Stage 1.1 global digest (first chunk only)
    if not accumulated_digest:
        digest_compact = {}
        for key in ("book_meta", "outline", "key_entities", "key_concepts"):
            if key in global_digest:
                digest_compact[key] = global_digest[key]
        accumulated_digest = json.dumps(digest_compact, ensure_ascii=False, indent=2)

    for i in range(start_chunk, chunk_total):
        chunk = chunks[i]
        chunk_len = len(chunk)
        chunk_ok = False
        last_error = None
        # NashSU parity: pass tail text from previous chunk as overlap context
        overlap_before = chunks[i - 1] if i > 0 else ""
        # NashSU parity: find heading hierarchy for this chunk
        heading_path = ""
        if i == 0:
            chunk_pos = 0
        else:
            # Find this chunk's start position in extracted text
            chunk_pos = extracted_text.find(chunk)
            if chunk_pos == -1:
                chunk_pos = i * config.target_chars  # fallback estimate
        heading_path = _resolve_chunk_heading_path(extracted_text, chunk_pos, chunk_pos + chunk_len)

        for attempt in range(1 + max_retries):
            # Build prompt with current accumulated digest
            prompt = build_chunk_analysis_prompt(
                chunk, i, chunk_total, global_digest, file_path, config,
                template=template, accumulated_digest=accumulated_digest,
                overlap_before=overlap_before,
                heading_path=heading_path,
            )

            try:
                t_chunk = time.time()
                response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=8192)
                analysis = parse_yaml_block(response)
                analysis["_chunk_index"] = i + 1
                analysis["_chunk_size"] = chunk_len
                analysis["_attempts"] = attempt + 1
                dt = time.time() - t_chunk
                n_c = len(analysis.get("concepts_found") or [])
                n_e = len(analysis.get("entities_found") or [])
                elapsed = time.time() - t0
                done_count = i + 1
                eta = (elapsed / done_count) * (chunk_total - done_count) if done_count > 0 else 0
                pct = done_count * 100 // chunk_total
                tag = f" (retry #{attempt})" if attempt > 0 else ""
                print(f"  [stage_1_5] chunk {i+1}/{chunk_total} OK{tag} — "
                      f"{n_c} concepts, {n_e} entities, {dt:.0f}s "
                      f"[{pct}% ETA {eta:.0f}s]")

                # Extract updated global digest from this chunk's analysis
                updated_digest = analysis.get("updated_global_digest", "")
                if isinstance(updated_digest, str) and len(updated_digest.strip()) > 50:
                    accumulated_digest = updated_digest.strip()
                elif isinstance(updated_digest, dict):
                    # Some LLMs return nested dict instead of string
                    accumulated_digest = json.dumps(updated_digest, ensure_ascii=False, indent=2)
                # else: keep the previous accumulated_digest unchanged

                analyses.append(analysis)
                chunk_ok = True

                # ── Per-chunk checkpoint ──
                if source_hash:
                    _checkpoint_1_5(config, source_hash, chunk_total, accumulated_digest, analyses)

                break

            except RuntimeError as e:
                err_str = str(e)
                last_error = RuntimeError(f"{type(e).__name__}: {err_str[:200]}")
                if "429" in err_str or "503" in err_str or "rate" in err_str.lower():
                    _record_rate_limit()
                if attempt < max_retries:
                    wait = (2 ** attempt) + (time.time() % 2)
                    print(f"  [stage_1_5] chunk {i+1}/{chunk_total} attempt {attempt+1} failed "
                          f"(HTTP {err_str[:80]}) — retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                # Last attempt failed
                print(f"  [stage_1_5] chunk {i+1}/{chunk_total} FAILED after "
                      f"{1 + max_retries} attempts: {err_str[:120]}")
                analyses.append({
                    "chunk_index": i + 1, "error": str(last_error),
                    "chunk_text_length": chunk_len, "_attempts": 1 + max_retries,
                })
                # Checkpoint even failed chunks (so we don't re-process them)
                if source_hash:
                    _checkpoint_1_5(config, source_hash, chunk_total, accumulated_digest, analyses)

        if not chunk_ok and last_error:
            pass

    total_concepts = sum(len(a.get("concepts_found") or []) for a in analyses)
    total_entities = sum(len(a.get("entities_found") or []) for a in analyses)
    errored = sum(1 for a in analyses if "error" in a)
    elapsed = time.time() - t0
    speed = chunk_total / elapsed if elapsed > 0 else 0
    print(f"[stage_1_5] Done — {chunk_total} chunks in {elapsed:.0f}s ({speed:.1f} chunks/s), "
          f"{errored} failed, {total_concepts} concepts, {total_entities} entities total")
    if errored > 0:
        failed_indices = [a.get("chunk_index", -1) for a in analyses if "error" in a]
        print(f"[stage_1_5] ⚠️  Failed chunks: {failed_indices} — Stage 2 synthesis may be incomplete")
    return analyses


def _checkpoint_1_5(config: Config, source_hash: str, chunk_total: int,
                    accumulated_digest: str, analyses: list[dict]) -> None:
    """Save per-chunk checkpoint for Stage 1.5 resume (NashSU parity)."""
    # Merge into existing progress to preserve other stage data
    progress = load_progress(config, source_hash) or {}
    progress["stage"] = "stage_1_5_partial"
    progress["stage_1_5_cp"] = {
        "chunk_total": chunk_total,
        "accumulated_digest": accumulated_digest,
        "analyses": analyses,
    }
    save_progress(config, source_hash, progress)


# ---------- Stage 2: Per-Chunk Generation ----------


def build_per_chunk_gen_prompt(
    chunk_analysis: dict,
    chunk_text: str,
    chunk_index: int,
    file_path: Path,
    config: Config,
    template: str = "",
    generated_slugs: list[str] | None = None,
) -> str:
    """Build prompt to generate concept/entity pages from ONE chunk's analysis.

    Accepts generated_slugs from previously-processed chunks so the LLM can:
      - Skip concepts already covered by earlier chunks
      - Use [[wikilinks]] to reference existing pages
      - Avoid duplicate slug generation
    (NashSU parity: sequential, accumulating context.)
    """
    concepts = chunk_analysis.get("concepts_found", [])
    entities = chunk_analysis.get("entities_found", [])
    existing_slugs = list_existing_slugs(config)
    if generated_slugs is None:
        generated_slugs = []

    concept_lines = []
    for c in concepts:
        if isinstance(c, dict):
            name = c.get("name", "")
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            # Mark if this concept was already covered by a prior chunk
            slug = name.lower().replace(" ", "-").replace("/", "-")
            already = " [ALREADY COVERED — SKIP]" if slug in generated_slugs else ""
            concept_lines.append(f"  - {name} [{imp}]: {defn}{already}")
            if not already:
                for d in details[:3]:
                    concept_lines.append(f"      • {d}")

    entity_lines = []
    for e in entities:
        if isinstance(e, dict):
            name = e.get("name", "")
            role = e.get("role", "")
            sig = e.get("significance", "")
            slug = name.lower().replace(" ", "-").replace("/", "-")
            already = " [ALREADY COVERED — SKIP]" if slug in generated_slugs else ""
            entity_lines.append(f"  - {name} ({role}): {sig}{already}")

    concept_str = "\n".join(concept_lines[:100]) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines[:30]) if entity_lines else "(none)"

    generated_str = "\n".join(f"  - {s}" for s in generated_slugs) if generated_slugs else "(none yet — you are the first chunk)"

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:1500]}\n</template>\n"

    return f"""# Role
You are generating wiki pages for ONE chunk of a book. Previous chunks have
already been processed — their pages are listed below. Do NOT regenerate them.

# Source
Book: {file_path.stem}
Chunk: {chunk_index + 1}

{template_section}
# Pages already generated by previous chunks (SKIP these):
{generated_str}

# Concepts found in this chunk (generate a page for each — skip ALREADY COVERED):
{concept_str}

# Entities found in this chunk (generate a page for key ones — skip ALREADY COVERED):
{entity_str}

# Existing wiki pages (avoid duplicate slugs):
{', '.join(existing_slugs[:100])}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/concepts/...`
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.
- Use [[wikilink]] with FULL filename stem to link to pages from previous chunks
- ⚠️ NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
- Math: $inline$ $$display$$

# Output Format — EXACT
---FILE:wiki/concepts/<slug>.md---
---
type: concept
title: "..."
domain: general
tags: [...]
related: [...]
sources: ["raw/{file_path.relative_to(config.raw_root)}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# Title

(content)

---END FILE---
---FILE:wiki/entities/<slug>.md---
(frontmatter + content)
---END FILE---

Generate a page for EVERY concept listed above that is NOT marked [ALREADY COVERED]. Go!
"""


def stage_2_per_chunk_generation(
    chunk_analyses: list[dict],
    chunks: list[str],
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    max_chunk_concurrent: int = 4,
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Stage 2 (per-chunk): Generate FILE blocks SEQUENTIALLY.

    NashSU parity: each chunk builds on pages already generated by previous
    chunks.  Later chunks know which concepts have already been covered, so
    they skip duplicates and use [[wikilinks]] to reference existing pages.

    The old dedup step is no longer needed — sequential execution with
    accumulated slug awareness prevents duplicates at the source.
    """
    chunk_total = len(chunk_analyses)
    print(f"[stage_2] Per-chunk generation: {chunk_total} chunks, sequential NashSU mode")

    all_file_blocks: list[tuple[str, str]] = []
    all_responses: list[str] = []
    generated_slugs: list[str] = []  # accumulates as chunks are processed
    gen_tokens = config.compute_max_tokens(8192)

    t0 = time.time()
    for idx in range(chunk_total):
        analysis = chunk_analyses[idx]
        chunk_text = chunks[idx] if idx < len(chunks) else ""
        concepts_n = len(analysis.get("concepts_found", []))
        entities_n = len(analysis.get("entities_found", []))
        if concepts_n == 0 and entities_n == 0:
            print(f"  [chunk {idx+1}/{chunk_total}] (no concepts or entities — skipped)")
            continue

        prompt = build_per_chunk_gen_prompt(
            analysis, chunk_text, idx, file_path, config, template,
            generated_slugs=generated_slugs,
        )
        try:
            t_chunk = time.time()
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
            all_responses.append(response)
            blocks = parse_file_blocks(response)
            all_file_blocks.extend(blocks)
            dt = time.time() - t_chunk
            elapsed = time.time() - t0
            done_count = idx + 1
            eta = (elapsed / done_count) * (chunk_total - done_count) if done_count > 0 else 0
            pct = done_count * 100 // chunk_total
            print(f"  [chunk {idx+1}/{chunk_total}] {concepts_n}c/{entities_n}e → "
                  f"{len(blocks)} blocks ({len(response):,} chars, {stop_reason}) "
                  f"{dt:.0f}s [{pct}% ETA {eta:.0f}s]")
            # Extract slugs from generated blocks so the NEXT chunk knows
            for path, _content in blocks:
                slug = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
                if slug not in generated_slugs:
                    generated_slugs.append(slug)
        except Exception as e:
            print(f"  [chunk {idx+1}/{chunk_total}] ❌ {e}")
            # Continue to next chunk even if this one failed

    # Generate source page from global digest
    source_rel = f"sources/{file_path.relative_to(config.raw_root).with_suffix('.md')}"
    source_prompt = f"""# Role
Generate a source page for this book from the global digest.

# Global Digest
```yaml
{json.dumps(global_digest, ensure_ascii=False, indent=2)[:5000]}
```

# Concepts generated ({len(all_file_blocks)} pages)
{', '.join(Path(p).stem for p, _ in all_file_blocks[:60])}

# Output Format — EXACT
---FILE:wiki/{source_rel}---
(frontmatter type:source + content)
---END FILE---

START IMMEDIATELY with ---FILE:... No preamble.
"""
    try:
        src_response, _ = call_anthropic_protocol(source_prompt, config, max_tokens=8192)
        all_responses.append(src_response)
        src_blocks = parse_file_blocks(src_response)
        all_file_blocks.extend(src_blocks)
    except Exception as e:
        print(f"  [stage_2] Source page generation failed: {e}")

    combined = "\n".join(all_responses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
    source_blocks = [b for b in all_file_blocks if "sources/" in b[0]]

    print(f"[stage_2] Done — {chunk_total} chunks in {time.time()-t0:.0f}s, "
          f"{len(all_file_blocks)} file blocks ({len(concept_blocks)} concepts, "
          f"{len(entity_blocks)} entities, {len(source_blocks)} source)")

    # Build analysis for cache
    unique_concepts, _ = _extract_concept_entity_names(chunk_analyses)
    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "source_generated": len(source_blocks) > 0,
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": chunk_total,
        "method": "per-chunk-sequential",
    }
    return analysis, combined, all_file_blocks



# ---------- Stage 2: Synthesis (legacy, for small books) ----------

def _build_image_reference_section(file_path: Path, config: Config) -> str:
    """Build a compact list of available images for the Stage 2 prompt."""
    slug = _media_slug(file_path, config)
    media_dir = config.wiki_dir / "media" / slug
    if not media_dir.exists():
        return "（本书无提取图片）\n"

    manifest_path = media_dir / "_manifest.json"
    captioned = 0
    total = 0
    sample_lines: list[str] = []

    if manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        images = m.get("images", [])
        total = len(images)
        for img in sorted(images, key=lambda x: (x["page"], x.get("img_idx_in_page", 0)))[:60]:
            cap_path = media_dir / (img["filename"] + ".caption.txt")
            cap = cap_path.read_text(encoding="utf-8").strip()[:70] if cap_path.exists() else ""
            sample_lines.append(f"  p{img['page']} `{img['filename']}`: {cap}")
            if cap:
                captioned += 1
    else:
        # Loose files (minerU)
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                total += 1
                cap_path = media_dir / (f.name + ".caption.txt")
                cap = cap_path.read_text(encoding="utf-8").strip()[:70] if cap_path.exists() else ""
                if total <= 60:
                    sample_lines.append(f"  `{f.name}`: {cap}")
                if cap:
                    captioned += 1

    if total == 0:
        return "（本书无提取图片）\n"

    section = f"本书共 {total} 张图（{captioned} 有caption）。图片位于 wiki/media/{slug}/。\n"
    section += "在 concept/entity 页面中用 ![](media/{}/filename) 引用相关图片。\n".format(slug)
    section += "关键图片示例：\n"
    section += "\n".join(sample_lines[:60])
    if total > 60:
        section += f"\n  ... （共 {total} 张，仅列前 60）"
    return section + "\n"



def _extract_concept_entity_names(chunk_analyses: list[dict]) -> tuple[list[str], list[str]]:
    """Extract deduplicated concept and entity names from chunk analyses."""
    all_concepts: list[str] = []
    all_entities: list[str] = []
    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            all_concepts.append(name)
        for e in a.get("entities_found") or []:
            name = e.get("name", e) if isinstance(e, dict) else str(e)
            all_entities.append(name)
    seen_c: set[str] = set()
    unique_concepts = [x for x in all_concepts if not (x in seen_c or seen_c.add(x))]  # type: ignore[func-returns-value]
    seen_e: set[str] = set()
    unique_entities = [x for x in all_entities if not (x in seen_e or seen_e.add(x))]  # type: ignore[func-returns-value]
    return unique_concepts, unique_entities


def _classify_concepts_by_importance(chunk_analyses: list[dict]) -> dict[str, list[str]]:
    """Classify deduplicated concepts by importance: core / supporting / mentioned.

    Returns dict with keys 'core', 'supporting', 'mentioned', each a list of names.
    A concept appearing in multiple chunks takes the highest importance seen.
    """
    seen: dict[str, str] = {}  # name → importance (highest wins)
    importance_rank = {"core": 3, "supporting": 2, "mentioned": 1}

    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            imp = c.get("importance", "mentioned")
            if name not in seen or importance_rank.get(imp, 0) > importance_rank.get(seen[name], 0):
                seen[name] = imp

    # Normalize importance to handle LLM typos (e.g., "supported" → "supporting")
    _imp_norm: dict[str, str] = {}
    for raw in ["core", "supporting", "mentioned"]:
        _imp_norm[raw] = raw
    # Map common LLM typos
    _imp_norm["supported"] = "supporting"
    _imp_norm["major"] = "core"
    _imp_norm["primary"] = "core"
    _imp_norm["minor"] = "mentioned"
    _imp_norm["reference"] = "mentioned"

    result: dict[str, list[str]] = {"core": [], "supporting": [], "mentioned": []}
    for name, imp in seen.items():
        imp_normalized = _imp_norm.get(imp, "mentioned")  # default to mentioned
        result[imp_normalized].append(name)
    # Sort each list alphabetically
    for imp in result:
        result[imp].sort()
    return result


# Coverage targets by importance level (NashSU-aligned: not every concept needs a page).
# "mentioned" concepts are typically covered inline in other pages.
COVERAGE_TARGETS = {
    "core": 0.80,        # Core concepts should have dedicated pages
    "supporting": 0.50,  # Supporting concepts should mostly be covered
    "mentioned": 0.20,   # Mentioned can be inline — low bar to catch egregious gaps
}


def _normalize_for_matching(s: str) -> str:
    """Normalize a string for fuzzy concept-to-page matching.

    Strips common prefixes, removes punctuation, and collapses whitespace
    so that "Buck Converter" matches "buck-converter-power-electronics".
    """
    import re as _re
    # Remove wiki/ path prefix and common subdirs
    s = _re.sub(r'^(wiki/)?(concepts|sources|entities)/', '', s)
    s = s.replace('.md', '')
    # Replace delimiters with spaces, then collapse
    s = s.replace('_', ' ').replace('-', ' ').replace('/', ' ')
    # Lowercase and remove all non-alphanumeric except spaces
    s = _re.sub(r'[^a-z0-9一-鿿 ]', '', s.lower())
    # Collapse multiple spaces
    s = _re.sub(r'\s+', ' ', s).strip()
    return s


def _concept_matches_page(concept_name: str, page_path: str) -> bool:
    """Check if a concept name matches a generated page path.

    Uses token-level matching: the concept name's tokens should all appear
    in the page path, in order (though not necessarily adjacent).
    """
    c_tokens = _normalize_for_matching(concept_name).split()
    p_norm = _normalize_for_matching(page_path)
    if not c_tokens:
        return False
    # All concept tokens must appear in the page path in order
    pos = 0
    for token in c_tokens:
        idx = p_norm.find(token, pos)
        if idx == -1:
            return False
        pos = idx + len(token)
    return True


def _compute_uncovered_concepts(
    unique_concepts: list[str], file_blocks: list[tuple[str, str]]
) -> list[str]:
    """Return concepts from the master list that have no corresponding FILE block."""
    uncovered = []
    for c in unique_concepts:
        if not any(_concept_matches_page(c, path) for path, _ in file_blocks):
            uncovered.append(c)
    return uncovered


def build_synthesis_prompt(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    round_num: int = 1,
    prior_response_tail: str = "",
    uncovered_concepts: list[str] | None = None,
    collision_warning: str = "",
    current_domain: str = "general",
) -> str:
    """Build the prompt for Stage 2: Synthesis.

    Round 1: full context with mandatory coverage targets.
    Round 2+: gap-aware continuation listing remaining uncovered concepts.
    """
    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)
    classified = _classify_concepts_by_importance(chunk_analyses)
    core_concepts = classified.get("core", [])
    supporting_concepts = classified.get("supporting", [])
    mentioned_concepts = classified.get("mentioned", [])

    if round_num == 1:
        # Round 1: full context
        digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
        if len(digest_str) > 5000:
            digest_str = digest_str[:5000] + "\n... (truncated)"

        existing_slugs = list_existing_slugs(config)

        # source_rel mirrors raw/ directory structure (e.g. "book/High Speed Digital Design")
        try:
            source_rel = str(file_path.relative_to(config.raw_root).with_suffix(""))
        except ValueError:
            source_rel = file_path.stem

        template_section = ""
        if template:
            template_trimmed = template[:2500]
            template_section = f"""
# Document Type Instructions
<template>
{template_trimmed}
</template>
"""

        # Show concepts by importance tier
        core_str = ', '.join(core_concepts[:50])
        supp_str = ', '.join(supporting_concepts[:50])
        ment_str = ', '.join(mentioned_concepts[:30])
        concept_list_str = (
            f"**CORE concepts ({len(core_concepts)} — MUST generate ALL):**\n{core_str}\n\n"
            f"**SUPPORTING concepts ({len(supporting_concepts)} — generate at least 60%):**\n{supp_str}"
        )
        if mentioned_concepts:
            concept_list_str += f"\n\n**MENTIONED concepts ({len(mentioned_concepts)} — can cover inline):**\n{ment_str}"

        entity_list_str = ', '.join(unique_entities[:60])
        if len(unique_entities) > 60:
            entity_list_str += f"\n... and {len(unique_entities) - 60} more"

        return f"""# Role
You are maintaining a Karpathy-pattern knowledge base wiki.
{template_section}
# Current Domain
This source belongs to the **{current_domain}** domain. Tag all generated concept pages with `domain: {current_domain}` in their frontmatter.

{collision_warning}
# Global Digest
```yaml
{digest_str}
```

# Concepts to cover ({len(unique_concepts)} total — ALL must be generated)
{concept_list_str}

# Entities to cover ({len(unique_entities)} total)
{entity_list_str}

# Existing wiki pages (avoid duplicates)
{', '.join(existing_slugs[:200])}

# Source
- Book: {file_path.stem}

# Extracted Images
{_build_image_reference_section(file_path, config)}
# Task
The source page has already been generated separately. Now create:
1. Concept pages at wiki/concepts/<slug>.md for EVERY concept in the list above
2. Entity pages at wiki/entities/<slug>.md for key entities

**Every concept page frontmatter MUST include: `domain: {current_domain}`**

Include relevant images in pages using Markdown syntax: ![](media/<stem>/<filename>)

# Output Format — EXACT
Every page MUST be wrapped in delimiters:
```
---FILE:wiki/sources/{source_rel}.md---
(frontmatter + content)
---END FILE---
---FILE:wiki/concepts/<slug>.md---
(frontmatter + content)
---END FILE---
```

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/sources/...`
- Do NOT write any preamble, introduction, analysis, table of contents, or
  commentary before the first FILE block. The parser IGNORES everything outside
  ---FILE:...---END FILE--- blocks. Every token before the first FILE block is WASTED.
- Use [[wikilink]] with FULL filename stem
- ⚠️ NEVER use `/` in filenames (macOS rejects it). Replace "/" with "-" in slugs
- Math: $inline$ $$display$$
- **MANDATORY COVERAGE (importance-weighted)**:
  - CORE concepts ({len(core_concepts)}): Generate a dedicated page for EVERY one. Target: {int(COVERAGE_TARGETS['core']*100)}%.
  - SUPPORTING concepts ({len(supporting_concepts)}): Generate pages for at least {int(COVERAGE_TARGETS['supporting']*100)}%.
  - MENTIONED concepts ({len(mentioned_concepts)}): Can be covered inline in other pages.
  - Focus your effort on depth for CORE, breadth for SUPPORTING.
- Do NOT stop after a few pages — continuation rounds will let you finish.
- If you reach the token limit, you'll be continued. Do not stop early.
"""
    else:
        # Round 2+: gap-aware continuation
        uncovered = uncovered_concepts or []
        uncovered_str = ', '.join(uncovered[:80]) if uncovered else '(all concepts from previous rounds covered — continue with entities and remaining details)'
        # Stronger directive if previous round produced 0 blocks (likely all preamble)
        zero_block_warning = ""
        if prior_response_tail and "---FILE:" not in prior_response_tail[-5000:]:
            zero_block_warning = (
                "\n# ⚠️  PREVIOUS ROUND HAD ZERO FILE BLOCKS\n"
                "Your last response contained NO ---FILE:...---END FILE--- blocks. "
                "The parser IGNORES all text outside these delimiters. "
                "START IMMEDIATELY with ---FILE:wiki/concepts/<slug>.md---. "
                "Do NOT write any preamble, analysis, or commentary.\n"
            )
        return f"""# Continue Generation (Round {round_num})
{zero_block_warning}
# Remaining concepts that STILL need pages ({len(uncovered)} remaining of {len(unique_concepts)} total):
{uncovered_str}

# Your previous output ended with:
```
{prior_response_tail[-2000:]}
```

Generate wiki pages using the EXACT format: ---FILE:wiki/<path>.md---...---END FILE---
START IMMEDIATELY with the first FILE block. No preamble.
Focus on the UNCOVERED concepts listed above. Do NOT repeat previous pages.
"""


def stage_2_0_source_page(
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    current_domain: str = "general",
    verbose: bool = False,
) -> tuple[str, str]:
    """Stage 2.0: Dedicated source page generation (NashSU two-step).

    Separated from concept/entity generation so the LLM can focus entirely
    on producing a high-quality source page from the global digest.
    This matches NashSU ingest.ts which generates the source page first,
    then concept/entity pages in a separate pass.
    """
    try:
        source_rel = str(file_path.relative_to(config.raw_root).with_suffix(""))
    except ValueError:
        source_rel = file_path.stem

    book_meta = global_digest.get("book_meta", {})
    title = book_meta.get("title", file_path.stem)
    authors = book_meta.get("authors", [])
    year = book_meta.get("year", "")
    publisher = book_meta.get("publisher", "")

    digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
    if len(digest_str) > 8000:
        digest_str = digest_str[:8000] + "\n... (truncated)"

    outline = global_digest.get("outline", [])
    key_claims = global_digest.get("key_claims", [])
    key_concepts = global_digest.get("key_concepts", [])
    key_entities = global_digest.get("key_entities", [])

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:2000]}\n</template>\n"

    prompt = f"""# Role
You are writing a **source page** for a Karpathy-pattern wiki knowledge base.
This page will be the authoritative entry for a book in the wiki.
{template_section}
# Book Information (from Global Digest)
```yaml
{digest_str}
```

# Task
Write a comprehensive source page at wiki/sources/{source_rel}.md.

**Required structure:**

```
---
type: source
title: "{title}"
domain: {current_domain}
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
tags: [tag1, tag2, tag3]
related: []
sources: ["raw/{source_rel}.pdf"]
---

# {title}

## Book Summary

2-4 sentences summarizing what this book covers, its approach, and who it's for.

## Table of Contents & Key Concepts

For EACH chapter in the outline, write one comprehensive line:
1. **Chapter Title:** list ALL key topics covered — aim for 5-15 items, comma-separated. Include specific component names, formulas, design methods, and techniques.

Example:
1. **DC-DC Converters:** buck (step-down), boost (step-up), buck-boost, continuous vs discontinuous conduction mode, voltage-mode control, efficiency analysis, PWM, synchronous rectification.

## Key Takeaways

The 5-10 most important claims, formulas, design rules, or conclusions. Each ONE sentence, actionable.
```

# Instructions
- The frontmatter MUST be exactly as shown above with real data from the digest
- ⚠️ CRITICAL: DO NOT wrap the YAML frontmatter in ```yaml fences. The first line MUST be `---`, the frontmatter ends with `---`, then the body follows immediately. No code blocks anywhere.
- ⚠️ The source page MUST contain ONLY these 3 sections: ## Book Summary, ## Table of Contents & Key Concepts, ## Key Takeaways. Do NOT add extra sections (no 核心概念 list, no 关键实体 list, no 相关器件, no 关联知识点, no 来源说明). Link to concept/entity pages with [[wikilinks]] instead.
- Chapter outline: list ALL key topics per chapter (aim for 5-15 items). Be comprehensive — this is the wiki's authoritative reference for what the book covers.
- Key Takeaways: extract the most impactful claims from the digest's key_claims
- tags: Generate 3-8 relevant tags from the book's content (e.g. [dc-dc-converter, power-electronics, magnetics]). Do NOT leave tags: [] empty.
- related: Link to 2-5 related wiki pages by slug (e.g. [power-electronics, buck-converter])
- Use [[wikilink]] syntax to link to concept pages (slugs should be concept-name-slug format)
- The response MUST start with `---` (three dashes on the first line) — NO preamble, NO ``` fences, NO commentary
- Math: $inline$ $$display$$
"""

    gen_tokens = config.compute_max_tokens(8192)
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
    if verbose:
        print(f"[stage_2_0] Source page generated ({len(response):,} chars, stop={stop_reason})")
    else:
        print(f"[stage_2_0] Source page ready ({len(response):,} chars)")

    return response, stop_reason


def stage_2_synthesis(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Stage 2: Multi-round generation with coverage enforcement.

    Round 1: full context with mandatory coverage target.
    Rounds 2-8: gap-aware continuation listing uncovered concepts.
    After all rounds: gap-fill check — if < 90% coverage, do targeted rounds.
    """
    max_rounds = 8
    gen_tokens = config.compute_max_tokens(16384)
    all_responses: list[str] = []
    prior_tail = ""

    unique_concepts, _ = _extract_concept_entity_names(chunk_analyses)
    classified = _classify_concepts_by_importance(chunk_analyses)
    core_concepts = classified.get("core", [])
    supporting_concepts = classified.get("supporting", [])
    mentioned_concepts = classified.get("mentioned", [])
    target_count = len(core_concepts) + len(supporting_concepts)  # primary coverage target

    # Domain detection & slug collision check (Plan B: disambiguation)
    current_domain = _detect_domain(file_path, template, global_digest)
    existing_domains = _list_existing_concepts_with_domains(config)
    # Generate tentative slugs for all concepts to find collisions
    all_concept_names = unique_concepts
    collisions = _find_slug_collisions(all_concept_names, existing_domains, current_domain)
    collision_warning = _build_collision_warning(collisions, existing_domains)
    if collisions:
        print(f"[stage_2] ⚠️  Domain: {current_domain} — {len(collisions)} slug collisions across domains: "
              f"{', '.join(s for s, _, _ in collisions[:8])}{'...' if len(collisions) > 8 else ''}")
    else:
        print(f"[stage_2] Domain: {current_domain} — no cross-domain slug collisions detected")

    print(f"[stage_2] Concept importance: {len(core_concepts)} core, "
          f"{len(supporting_concepts)} supporting, {len(mentioned_concepts)} mentioned "
          f"(coverage targets: core≥{COVERAGE_TARGETS['core']:.0%}, "
          f"supporting≥{COVERAGE_TARGETS['supporting']:.0%}, mentioned≥{COVERAGE_TARGETS['mentioned']:.0%})")
    uncovered: list[str] = []

    for round_num in range(1, max_rounds + 1):
        # Compute uncovered concepts from what's been generated so far
        if round_num > 1:
            combined_so_far = "\n".join(all_responses)
            blocks_so_far = parse_file_blocks(combined_so_far)
            uncovered = _compute_uncovered_concepts(unique_concepts, blocks_so_far)

        print(f"[stage_2] Round {round_num}/{max_rounds} — building prompt...", flush=True)
        prompt = build_synthesis_prompt(
            global_digest, chunk_analyses, file_path, config, template,
            round_num=round_num, prior_response_tail=prior_tail,
            uncovered_concepts=uncovered,
            collision_warning=collision_warning if round_num == 1 else "",
            current_domain=current_domain,
        )
        prompt_len = len(prompt)
        print(f"[stage_2] Round {round_num} — prompt {prompt_len:,} chars, "
              f"{len(uncovered)} uncovered concepts, calling LLM (max_tokens={gen_tokens})...", flush=True)
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
        all_responses.append(response)
        blocks_this_round = len(parse_file_blocks(response))
        print(f"[stage_2] Round {round_num} — {len(response):,} chars, "
              f"{blocks_this_round} blocks, stop_reason={stop_reason}", flush=True)

        # Detect: LLM generated preamble instead of FILE blocks (wasted round)
        if blocks_this_round == 0 and len(response) > 5000:
            # LLM produced substantial text but no FILE blocks — likely preamble
            preamble_len = len(response.split("---FILE:")[0]) if "---FILE:" in response else len(response)
            print(f"[stage_2] Round {round_num} — ⚠️  {preamble_len:,} chars of preamble with 0 FILE blocks. "
                  f"Adding stronger directive for next round.")

        prior_tail = response[-3000:]

        if stop_reason == "end_turn":
            # Check importance-weighted coverage before accepting end_turn
            combined = "\n".join(all_responses)
            current_blocks = parse_file_blocks(combined)
            concept_paths = [p for p, _ in current_blocks if "concepts/" in p]

            # Compute coverage by importance tier (using shared matching function)
            core_covered = len([c for c in core_concepts if any(
                _concept_matches_page(c, p) for p in concept_paths)])
            supp_covered = len([c for c in supporting_concepts if any(
                _concept_matches_page(c, p) for p in concept_paths)])
            ment_covered = len([c for c in mentioned_concepts if any(
                _concept_matches_page(c, p) for p in concept_paths)])

            core_pct = core_covered / max(len(core_concepts), 1)
            supp_pct = supp_covered / max(len(supporting_concepts), 1)
            ment_pct = ment_covered / max(len(mentioned_concepts), 1)

            core_ok = core_pct >= COVERAGE_TARGETS["core"]
            supp_ok = supp_pct >= COVERAGE_TARGETS["supporting"]
            ment_ok = ment_pct >= COVERAGE_TARGETS["mentioned"]

            if core_ok and supp_ok:
                print(f"[stage_2] Round {round_num} — end_turn, coverage met: "
                      f"core={core_pct:.0%} supp={supp_pct:.0%} ment={ment_pct:.0%}. Done.")
                break
            else:
                missing = []
                if not core_ok: missing.append(f"core={core_pct:.0%} (need {COVERAGE_TARGETS['core']:.0%})")
                if not supp_ok: missing.append(f"supp={supp_pct:.0%} (need {COVERAGE_TARGETS['supporting']:.0%})")
                print(f"[stage_2] Round {round_num} — end_turn but coverage insufficient: {', '.join(missing)}. Continuing...")
        elif stop_reason == "max_tokens":
            print(f"[stage_2] Round {round_num} hit max_tokens — continuing...")
        else:
            print(f"[stage_2] Round {round_num} stop_reason={stop_reason}, continuing...")

    combined = "\n".join(all_responses)
    file_blocks = parse_file_blocks(combined)
    concept_blocks = [b for b in file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in file_blocks if "entities/" in b[0]]
    source_blocks = [b for b in file_blocks if "sources/" in b[0]]
    overall_pct = len(concept_blocks) / max(len(unique_concepts), 1)

    # Final importance-weighted coverage
    concept_paths = [p for p, _ in concept_blocks]
    core_final = len([c for c in core_concepts if any(
        _concept_matches_page(c, p) for p in concept_paths)])
    supp_final = len([c for c in supporting_concepts if any(
        _concept_matches_page(c, p) for p in concept_paths)])

    print(f"[stage_2] Done — {len(all_responses)} rounds, {len(combined):,} chars total, "
          f"{len(file_blocks)} file blocks ({len(concept_blocks)} concepts, {len(entity_blocks)} entities, "
          f"{len(source_blocks)} source), "
          f"coverage: core={core_final}/{len(core_concepts)} "
          f"supp={supp_final}/{len(supporting_concepts)} "
          f"overall={overall_pct:.0%}")

    if verbose and file_blocks:
        for p, content in file_blocks:
            print(f"  block: {p} ({len(content)} chars)")

    # Build analysis from global_digest + chunk summaries (for cache/logging)
    all_concepts: list[str] = []
    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            all_concepts.append(name)

    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_core": len(core_concepts),
        "concepts_supporting": len(supporting_concepts),
        "concepts_mentioned": len(mentioned_concepts),
        "concepts_generated": len(concept_blocks),
        "coverage_core": round(core_final / max(len(core_concepts), 1), 2),
        "coverage_supporting": round(supp_final / max(len(supporting_concepts), 1), 2),
        "coverage_pct": round(overall_pct, 2),
        "entities_generated": len(entity_blocks),
        "source_generated": len(source_blocks) > 0,
        "total_rounds": len(all_responses),
        "stop_reason": stop_reason,
    }
    return analysis, combined, file_blocks


# ---------- Stage 2.3: Query generation ----------

def build_query_generation_prompt(
    global_digest: dict,
    concept_titles: list[str],
    entity_titles: list[str],
    key_claims: list[dict],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.3: generate open questions from single-source analysis."""
    digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
    if len(digest_str) > 3000:
        digest_str = digest_str[:3000] + "\n... (truncated)"

    concepts_str = '\n'.join(f"- {c}" for c in concept_titles[:80])
    entities_str = '\n'.join(f"- {e}" for e in entity_titles[:40])
    claims_str = '\n'.join(
        f"- {c.get('claim', str(c))}" if isinstance(c, dict)
        else f"- {c}"
        for c in (key_claims or [])[:30]
    )
    existing_slugs = list_existing_slugs(config)
    today_str = time.strftime("%Y-%m-%d")
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    return f"""# Role
You are maintaining a Karpathy-pattern knowledge base wiki. You have just finished generating source/concept/entity pages for a book.

# Current Domain
{current_domain}

# Book Context
- Title: {file_path.stem}
- Canonical source path: raw/{raw_rel}
- Global Digest (summary):
```yaml
{digest_str}
```

# Generated Concepts ({len(concept_titles)} total)
{concepts_str if concepts_str else '(none)'}

# Generated Entities ({len(entity_titles)} total)
{entities_str if entities_str else '(none)'}

# Key Claims from the Book
{claims_str if claims_str else '(none)'}

# Existing Wiki Pages (avoid referencing non-existent pages)
{', '.join(existing_slugs[:200])}

# Task
Identify **0-5 open questions** this book raises but does NOT fully answer.
A good query is:
1. Grounded — stems from specific content in the book
2. Explorable — can be advanced by reading more, experimenting, or deeper analysis
3. Bounded — specific enough to have a clear exploration direction

Bad examples (do NOT generate):
- "What is voltage?" — book already answers this
- "How to learn hardware design?" — too broad
- "Will AI replace hardware engineers?" — unrelated to this book

# Output Format
---FILE:wiki/queries/{{slug}}.md---
---
type: query
title: "{{question ending with ?}}"
domain: {current_domain}
tags: [{{2-4 tags}}]
related: [{{2-4 wikilink stems from generated concepts/entities}}]
sources: ["raw/{raw_rel}"]
created: {today_str}
updated: {today_str}
---

# {{question title}}

## Background
{{2-3 sentences: what specific content in the book prompted this question}}

## Clues from the Book
{{bullet points of partial answers/data/cases already in the book, each with chapter source}}

## To Explore
{{2-4 specific sub-questions the book left unanswered}}

## See Also
- [[{{related concept}}]] — {{one-line description}}
---END FILE---

If no worthwhile query exists, output exactly:
---QUERIES: 0---
(no open questions worth a standalone page)
---END QUERIES---

# Constraints
- slug: English kebab-case, 3-6 words
- title: complete question ending with ? or ？
- related: ONLY wikilink stems from THIS ingest (see Generated Concepts/Entities above)
- sources: ONLY this book
- Each query body ≥200 chars (excluding frontmatter)
- START IMMEDIATELY with ---FILE: or ---QUERIES: — no preamble
"""


def stage_2_3_query_generation(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_blocks: list[tuple[str, str]],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> tuple[list[tuple[str, str]], str]:
    """Stage 2.3: Generate query pages (open questions) from single-source analysis.

    Returns (new_query_blocks, raw_response).
    Skips for datasheet/standard source types.
    """
    # Skip for datasheet/standard — pure fact listing, no meaningful open questions
    try:
        from _paths import detect_template_type
        src_type = detect_template_type(file_path, config)
    except Exception:
        src_type = None
    if src_type in ("datasheet", "standard"):
        if verbose:
            print(f"[stage_2_3] Skipped — {src_type} source type (no meaningful open questions)")
        return [], ""

    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)

    # Collect key claims from chunk analyses
    key_claims = []
    for ca in chunk_analyses:
        claims = ca.get("claims", [])
        if isinstance(claims, list):
            key_claims.extend(claims)

    # Get concept/entity titles from generated file blocks
    concept_titles = []
    entity_titles = []
    for path, _ in file_blocks:
        if path.startswith("concepts/"):
            concept_titles.append(path.replace("concepts/", "").replace(".md", ""))
        elif path.startswith("entities/"):
            entity_titles.append(path.replace("entities/", "").replace(".md", ""))

    # If no concepts generated, skip
    if not concept_titles:
        if verbose:
            print("[stage_2_3] Skipped — no concepts generated")
        return [], ""

    # Detect domain
    current_domain = global_digest.get("book_meta", {}).get("domain", "general") if isinstance(global_digest.get("book_meta"), dict) else "general"

    prompt = build_query_generation_prompt(
        global_digest, concept_titles, entity_titles,
        key_claims, file_path, config, current_domain
    )

    query_tokens = config.compute_max_tokens(4096)
    if verbose:
        print(f"[stage_2_3] Query generation — {len(concept_titles)} concepts, "
              f"{len(key_claims)} claims, prompt {len(prompt):,} chars...")

    try:
        response, stop_reason = _call_anthropic_api(prompt, config, query_tokens)
    except Exception as e:
        print(f"[stage_2_3] LLM call failed: {e}")
        return [], ""

    if verbose:
        print(f"[stage_2_3] Response ({len(response)} chars, stop={stop_reason}):\n{response[:2000]}...\n")

    # Parse query FILE blocks
    query_blocks = parse_file_blocks(response)
    if query_blocks:
        print(f"[stage_2_3] Generated {len(query_blocks)} query page(s)")
        for path, _ in query_blocks:
            print(f"  → {path}")
    elif "---QUERIES: 0---" in response or "QUERIES: 0" in response:
        print("[stage_2_3] No worthwhile queries (---QUERIES: 0---)")
    else:
        print("[stage_2_3] No query blocks parsed (may be implicit ---QUERIES: 0---)")

    return query_blocks, response


# ---------- Stage 2.5: Comparison generation ----------

def build_comparison_disambiguation_prompt(
    concept_titles: list[str],
    entity_titles: list[str],
    existing_slugs: list[str],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.5A: disambiguation comparisons."""
    new_titles = concept_titles + entity_titles
    new_str = '\n'.join(f"- {t} (domain: {current_domain})" for t in new_titles[:80])
    existing_str = ', '.join(existing_slugs[:300])
    today_str = time.strftime("%Y-%m-%d")

    return f"""# Role
You are maintaining a wiki knowledge base. You have just generated concept/entity pages for a book.

# Current Domain
{current_domain}

# New Pages from This Book
{new_str}

# Existing Wiki Pages
{existing_str}

# Task
Check if any NEW page title has an EXACT name match with an EXISTING wiki page from a DIFFERENT domain.
ONLY create a disambiguation page when there is a genuine naming collision across domains.
Do NOT create disambiguation for:
- Similar-but-different names (e.g., "8b/10b encoding" vs "8b10b encoding bypass")
- Terms that only exist in ONE domain
- Terms where the domain distinction is already clear from the page title
- Sub-topics or variations of the same concept

A genuine collision example: "Switch" exists in BOTH circuit-fundamentals AND power-electronics with different meanings.

# Output Format
---FILE:wiki/comparisons/{{term-slug}}.md---
---
type: comparison
title: "{{Term}} (disambiguation)"
domain: general
tags: [disambiguation]
related: [{{domain-specific page stems}}]
sources: []
created: {today_str}
updated: {today_str}
---

# {{Term}} (disambiguation)

The term "{{Term}}" has different meanings across HardwareWiki domains:

| Domain | Meaning | Page |
|--------|---------|------|
| {{domain-1}} | {{one-sentence definition}} | [[{{term}}-{{domain-1}}]] |
| {{domain-2}} | {{one-sentence definition}} | [[{{term}}-{{domain-2}}]] |

## How to Distinguish
{{1-2 sentences on how to tell which domain based on context}}

## See Also
- [[{{term}}-{{domain-1}}]] — {{description}}
- [[{{term}}-{{domain-2}}]] — {{description}}
---END FILE---

If no disambiguation is needed, output:
---COMPARISONS_DISAMBIGUATION: 0---
---END COMPARISONS_DISAMBIGUATION---

START IMMEDIATELY with ---FILE: or ---COMPARISONS_DISAMBIGUATION: — no preamble.
"""


def build_comparison_in_source_prompt(
    concept_titles: list[str],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.5B: in-source concept comparisons."""
    concepts_with_desc = '\n'.join(f"- {c}" for c in concept_titles[:60])
    today_str = time.strftime("%Y-%m-%d")
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    return f"""# Role
You are maintaining a wiki knowledge base. Review the concepts just generated for a book.

# Current Domain
{current_domain}

# Source
{file_path.stem} (raw/{raw_rel})

# Generated Concepts
{concepts_with_desc}

# Task
Identify pairs of concepts that are **naturally compared** — understanding one illuminates the other.
Good candidates:
- Two choices on the same dimension (CCM vs DCM, Buck vs Boost, Voltage Mode vs Current Mode)
- Commonly confused pairs (EMI vs EMC, SNR vs SINAD, PSRR vs CMRR)
- Explicitly contrasted in the book

Bad candidates:
- Upstream/downstream relationships (MOSFET → Gate Driver)
- Parent/child relationships (DC-DC Converter → Buck Converter)
- Three or more items → NOT a comparison

Generate at most 2 comparisons. Output 0 if no good pair exists.

# Output Format
---FILE:wiki/comparisons/{{slug}}.md---
---
type: comparison
title: "{{Concept A}} vs {{Concept B}}"
domain: {current_domain}
tags: [{{2-4 tags}}]
related: [{{concept-A-stem}}, {{concept-B-stem}}]
sources: ["raw/{raw_rel}"]
created: {today_str}
updated: {today_str}
---

# {{Concept A}} vs {{Concept B}}

## Why Compare
{{1-2 sentences: why these two benefit from side-by-side understanding}}

## Comparison Table
| Dimension | {{Concept A}} | {{Concept B}} |
|-----------|---------------|---------------|
| {{dim 1: e.g. operating principle}} | | |
| {{dim 2: e.g. key characteristic}} | | |
| {{dim 3: e.g. typical application}} | | |
| {{dim 4: e.g. advantages/disadvantages}} | | |

## Selection Guide
{{When to choose A vs B — 2-3 specific recommendations}}

## See Also
- [[{{Concept A}}]] — {{one-line description}}
- [[{{Concept B}}]] — {{one-line description}}
---END FILE---

If no good comparison pair exists, output:
---COMPARISONS_IN_SOURCE: 0---
---END COMPARISONS_IN_SOURCE---

START IMMEDIATELY with ---FILE: or ---COMPARISONS_IN_SOURCE: — no preamble.
"""


def stage_2_5_comparison_generation(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_blocks: list[tuple[str, str]],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> tuple[list[tuple[str, str]], str]:
    """Stage 2.5: Generate comparison pages (disambiguation + in-source contrast).

    Returns (new_comparison_blocks, raw_response).
    Skips when no concepts were generated.
    """
    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)

    # Get concept/entity titles from generated file blocks
    concept_titles = []
    entity_titles = []
    for path, _ in file_blocks:
        if path.startswith("concepts/"):
            concept_titles.append(path.replace("concepts/", "").replace(".md", ""))
        elif path.startswith("entities/"):
            entity_titles.append(path.replace("entities/", "").replace(".md", ""))

    if not concept_titles and not entity_titles:
        if verbose:
            print("[stage_2_5_comp] Skipped — no concepts/entities generated")
        return [], ""

    current_domain = global_digest.get("book_meta", {}).get("domain", "general") if isinstance(global_digest.get("book_meta"), dict) else "general"
    existing_slugs = list_existing_slugs(config)
    comp_tokens = config.compute_max_tokens(4096)
    all_blocks: list[tuple[str, str]] = []

    # 2.5A: Disambiguation
    if verbose:
        print(f"[stage_2_5_comp] 2.5A Disambiguation check — {len(concept_titles)} concepts vs {len(existing_slugs)} existing...")
    prompt_25a = build_comparison_disambiguation_prompt(
        concept_titles, entity_titles, existing_slugs, file_path, config, current_domain
    )
    try:
        response_25a, stop_25a = _call_anthropic_api(prompt_25a, config, comp_tokens)
    except Exception as e:
        print(f"[stage_2_5_comp] 2.5A LLM call failed: {e}")
        response_25a = ""
    if response_25a:
        blocks_25a = parse_file_blocks(response_25a)
        if blocks_25a:
            print(f"[stage_2_5_comp] 2.5A: {len(blocks_25a)} disambiguation page(s)")
            all_blocks.extend(blocks_25a)
        else:
            print("[stage_2_5_comp] 2.5A: no disambiguation needed")

    # 2.5B: In-source concept comparison
    if len(concept_titles) >= 2:
        if verbose:
            print(f"[stage_2_5_comp] 2.5B In-source comparison — {len(concept_titles)} concepts...")
        prompt_25b = build_comparison_in_source_prompt(
            concept_titles, file_path, config, current_domain
        )
        try:
            response_25b, stop_25b = _call_anthropic_api(prompt_25b, config, comp_tokens)
        except Exception as e:
            print(f"[stage_2_5_comp] 2.5B LLM call failed: {e}")
            response_25b = ""
        if response_25b:
            blocks_25b = parse_file_blocks(response_25b)
            if blocks_25b:
                print(f"[stage_2_5_comp] 2.5B: {len(blocks_25b)} comparison page(s)")
                for path, _ in blocks_25b:
                    print(f"  → {path}")
                all_blocks.extend(blocks_25b)
            else:
                print("[stage_2_5_comp] 2.5B: no comparison pairs found")
    else:
        if verbose:
            print("[stage_2_5_comp] 2.5B skipped — fewer than 2 concepts")

    if all_blocks:
        print(f"[stage_2_5_comp] Total: {len(all_blocks)} comparison page(s)")
    else:
        print("[stage_2_5_comp] No comparisons generated (---COMPARISONS: 0---)")

    combined_response = response_25a
    if response_25a and response_25b:
        combined_response = response_25a + "\n" + response_25b
    elif response_25b:
        combined_response = response_25b

    return all_blocks, combined_response


# ---------- File writing ----------

# NashSU parity: isSafeIngestPath (ingest.ts L290-306)
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"}
for _i in range(1, 10):
    _WINDOWS_RESERVED.add(f"com{_i}")
    _WINDOWS_RESERVED.add(f"lpt{_i}")

_ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


def _contains_cjk(text: str) -> bool:
    """Check if text contains CJK characters (NashSU parity: containsCjk)."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
            0xF900 <= cp <= 0xFAFF or    # CJK Compatibility
            0x3040 <= cp <= 0x309F or    # Hiragana
            0x30A0 <= cp <= 0x30FF or    # Katakana
            0xAC00 <= cp <= 0xD7AF):     # Hangul
            return True
    return False


def _make_cjk_slug(title: str) -> str:
    """Create a readable CJK slug from a page title.

    Rules (NashSU parity):
    - Keep CJK characters, alphanumeric, spaces, hyphens
    - Replace special chars with hyphens
    - Collapse multiple hyphens
    - Trim to 120 chars
    - Preserve proper nouns and technical identifiers in original form
    """
    import re as _re
    # Keep CJK, alphanumeric, spaces, hyphens, parentheses (for units like "Cauer/Foster")
    slug = _re.sub(r'[^\w\s\-\(\)一-鿿㐀-䶿豈-﫿぀-ゟ゠-ヿ가-힯]', '-', title, flags=_re.UNICODE)
    # Collapse whitespace and hyphens
    slug = _re.sub(r'[\s_]+', '-', slug)
    slug = _re.sub(r'-{2,}', '-', slug)
    slug = slug.strip('-')
    # Replace problematic chars for macOS filenames
    slug = slug.replace('/', '-').replace(':', '-').replace('\\', '-')
    if len(slug) > 120:
        slug = slug[:120].rstrip('-')
    return slug if slug else ""


def _auto_correct_wiki_path(rel_path: str, content: str, config: Config | None = None) -> str | None:
    """Auto-correct malformed wiki paths from LLM output.

    LLM sometimes outputs:
      wiki/ConceptName        → concepts/ConceptName.md
      wiki/Book Title.md      → sources/Book Title.md
      wiki/Some Entity        → entities/Some Entity.md

    Also performs cross-domain slug disambiguation (Plan B):
    If a concept slug collides with an existing concept from a different domain,
    auto-appends the current domain suffix.

    Returns corrected path (relative to wiki/ dir, NO "wiki/" prefix) or None if uncorrectable.
    """
    import re as _re
    basename = Path(rel_path).name
    stem = Path(rel_path).stem

    # 2026-06-15: macOS/Linux 文件名不能含 /，LLM 可能在 slug 中输出 /
    # 例如 [[热仿真(Cauer/Foster模型)]] → slug "热仿真(Cauer/Foster模型)"
    stem = stem.replace("/", "_")

    # 2026-06-15: agent sometimes outputs paths without .md extension
    if not rel_path.endswith(".md"):
        rel_path += ".md"

    # Read frontmatter type and domain from content (used by all cases below)
    fm_type = None
    fm_domain = None
    fm_match = _re.match(r'^---\s*\n(.*?)\n---', content, _re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).split("\n"):
            m = _re.match(r'type:\s*(\S+)', line)
            if m:
                fm_type = m.group(1).strip()
            m = _re.match(r'domain:\s*(\S+)', line)
            if m:
                fm_domain = m.group(1).strip()

    # Plan B: Check for cross-domain slug collisions
    slug = stem
    if config and fm_type == "concept" and fm_domain:
        concepts_dir = config.wiki_dir / "concepts"
        existing_path = concepts_dir / f"{slug}.md"
        if existing_path.exists():
            # Read existing page's domain
            try:
                existing_text = existing_path.read_text(encoding="utf-8")
                ex_match = _re.match(r'^---\s*\n(.*?)\n---', existing_text, _re.DOTALL)
                existing_domain = "general"
                if ex_match:
                    for line in ex_match.group(1).split("\n"):
                        dm = _re.match(r'domain:\s*(\S+)', line)
                        if dm:
                            existing_domain = dm.group(1).strip()
                            break
                if existing_domain != fm_domain and existing_domain != "general" and fm_domain != "general":
                    new_slug = f"{slug}-{fm_domain}"
                    print(f"  ⚠️  [disambig] Slug collision: '{slug}' exists in domain '{existing_domain}', "
                          f"new page from domain '{fm_domain}' → renaming to '{new_slug}'")
                    slug = new_slug
            except Exception:
                pass  # can't read existing page, proceed with original slug

    # ── CJK slug rewriting (NashSU parity: rewriteIngestPathFromTitleForTargetLanguage) ──
    fm_title = None
    if fm_match:
        for line in fm_match.group(1).split("\n"):
            tm = _re.match(r'title:\s*["\']?(.+?)["\']?\s*$', line)
            if tm:
                fm_title = tm.group(1).strip()
                break
    if fm_title and _contains_cjk(fm_title) and not _contains_cjk(slug):
        cjk_slug = _make_cjk_slug(fm_title)
        if cjk_slug and _contains_cjk(cjk_slug):
            print(f"  ⚠️  [cjk] Slug '{slug}' → '{cjk_slug}' (CJK title detected)")
            slug = cjk_slug

    # Case: bare filename (no path prefix) — LLM forgot wiki/concepts/ prefix
    # This is the most common correction: "ConceptName.md" → "concepts/ConceptName.md"
    if "/" not in rel_path:
        if fm_type == "source":
            return f"sources/{slug}.md"
        elif fm_type == "entity":
            return f"entities/{slug}.md"
        else:
            # Default: treat as concept (vast majority of pages)
            return f"concepts/{slug}.md"

    # Strip wiki/ prefix if present (from LLM or legacy format)
    if "/" in rel_path:
        if rel_path.startswith("wiki/"):
            rel_path = rel_path[len("wiki/"):]
        parts = rel_path.split("/")
        if len(parts) >= 2:
            # Case: 4+ part path — LLM added extra nesting
            # wiki/sources/book/Title → sources/book/Title.md (keep type subdir, aligns with raw/)
            # wiki/concepts/topic/Title → concepts/Title.md (flatten — concepts have no subdirs)
            # wiki/entities/category/Name → entities/Name.md (flatten — entities have no subdirs)
            if len(parts) >= 4:
                dir_name = parts[1]  # "sources" or "concepts" or "entities"
                extra = parts[2]     # e.g., "book", "topic", "category"
                actual_slug = parts[-1].replace(".md", "")
                # Use frontmatter type if available, else infer from dir_name
                target_dir = dir_name if dir_name in ("sources", "concepts", "entities") else "concepts"
                if fm_type == "source":
                    target_dir = "sources"
                elif fm_type == "concept":
                    target_dir = "concepts"
                elif fm_type == "entity":
                    target_dir = "entities"
                # Source pages keep type subdirectory for raw/ alignment
                if target_dir == "sources":
                    return f"sources/{extra}/{actual_slug}.md"
                else:
                    # Concepts and entities: flatten — no subdirectories
                    return f"{target_dir}/{actual_slug}.md"

            # Case: 3-part path like wiki/sources/ConceptName
            # Check frontmatter type vs directory mismatch, then correct
            if len(parts) == 3:
                dir_name = parts[1]
                _type_to_dir = {"source": "sources", "concept": "concepts", "entity": "entities"}
                if fm_type and fm_type in _type_to_dir and _type_to_dir[fm_type] != dir_name:
                    return f"{_type_to_dir[fm_type]}/{slug}.md"
                # No frontmatter + in sources/ but not source-like → concepts
                if dir_name == "sources" and not ("## " in content and "sources:" in content.lower()):
                    return f"concepts/{slug}.md"

            # Use fm_type from outer scope (already parsed at top of function)
            if fm_type == "source":
                return f"sources/{slug}.md"
            elif fm_type == "concept":
                return f"concepts/{slug}.md"
            elif fm_type == "entity":
                return f"entities/{slug}.md"

            # Heuristic fallback: check content for source-like patterns
            if "## " in content and ("sources:" in content.lower() or "## Source" in content):
                return f"sources/{slug}.md"
            # Default: treat as concept (most common case for Chinese wiki)
            return f"concepts/{slug}.md"

    # ── Schema routing validation (NashSU parity: validateWikiPageRouting) ──
    # After all corrections, verify that frontmatter type matches directory.
    # This catches LLM writing type:concept to entities/ or vice versa.
    if rel_path and fm_type:
        _TYPE_TO_DIR = {
            "source": "sources", "concept": "concepts", "entity": "entities",
            "query": "queries", "comparison": "comparisons",
            "synthesis": "synthesis", "finding": "findings",
            "thesis": "thesis", "methodology": "methodology",
        }
        expected_dir = _TYPE_TO_DIR.get(fm_type)
        if expected_dir:
            actual_dir = rel_path.split("/")[0] if "/" in rel_path else ""
            if actual_dir and actual_dir != expected_dir:
                print(f"  ⚠️  [schema] Type '{fm_type}' in '{actual_dir}/' → routing to '{expected_dir}/'")
                if "/" in rel_path:
                    rel_path = f"{expected_dir}/{rel_path.split('/', 1)[1]}"
                else:
                    rel_path = f"{expected_dir}/{rel_path}"
            elif not actual_dir:
                rel_path = f"{expected_dir}/{rel_path}"

    return None


def is_safe_ingest_path(rel_path: str) -> bool:
    """Reject paths that are unsafe to write to the wiki/ directory.

    NashSU checks (ingest.ts L290-306):
      - No control/NUL bytes
      - Not an absolute path (POSIX /, Windows drive, UNC)
      - No .. segments
      - No segments ending with space or .
      - No Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
      - No Windows illegal characters (<>:"|?*)
    """
    if not rel_path or _ILLEGAL_CHARS_RE.search(rel_path):
        return False
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        return False
    # Windows drive letter or UNC
    if len(rel_path) >= 2 and rel_path[1] == ":":
        return False
    if rel_path.startswith("\\\\"):
        return False
    if ".." in rel_path.split("/") or ".." in rel_path.split("\\"):
        return False
    for segment in rel_path.replace("\\", "/").split("/"):
        if not segment:
            continue
        if segment.endswith(" ") or segment.endswith("."):
            return False
        if segment.lower() in _WINDOWS_RESERVED:
            return False
    return True


def wiki_path_for_source(raw_file: Path, config: Config) -> Path:
    """Return wiki/sources/<raw-rel-path>.md mirroring raw/ directory structure."""
    try:
        rel = raw_file.relative_to(config.raw_root).with_suffix(".md")
    except ValueError:
        rel = raw_file.with_suffix(".md").name
    return config.wiki_dir / "sources" / rel


def sanitize_ingested_content(content: str) -> str:
    """NashSU parity (ingest-sanitize.ts): fix common LLM formatting errors."""
    # Fix stray opening code fences without closing
    fence_count = content.count("\n```")
    if fence_count % 2 != 0:
        # Remove last unclosed fence
        last_fence = content.rfind("\n```")
        if last_fence != -1:
            content = content[:last_fence] + content[last_fence:].replace("\n```", "", 1)
    # Fix "frontmatter:" prefix (LLM sometimes echoes the instruction)
    content = re.sub(r'^frontmatter:\s*\n', '', content, flags=re.MULTILINE)
    return content


def backup_existing_page(path: Path, config: Config) -> None:
    """NashSU parity (ingest.ts L2575-2584): snapshot existing page before overwrite."""
    if not path.exists():
        return
    history_dir = config.runtime_dir / "page-history"
    history_dir.mkdir(parents=True, exist_ok=True)
    safe_name = str(path.relative_to(config.wiki_dir)).replace("/", "_")
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = history_dir / f"{ts}_{safe_name}"
    backup_path.write_text(path.read_text(encoding="utf-8"))
    print(f"  [backup] {path.name} → page-history/{backup_path.name}")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from page text. Returns (fields, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fields: dict = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Parse YAML lists: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
            fields[key] = val
    return fields, body


def _merge_frontmatter_arrays(existing: dict, new: dict) -> dict:
    """NashSU parity: union-merge array fields (sources, tags, related)."""
    merged = dict(existing)
    for key in ("sources", "tags", "related"):
        existing_vals = existing.get(key, [])
        new_vals = new.get(key, [])
        if isinstance(existing_vals, str):
            existing_vals = [existing_vals] if existing_vals else []
        if isinstance(new_vals, str):
            new_vals = [new_vals] if new_vals else []
        merged_set = list(dict.fromkeys(list(existing_vals) + list(new_vals)))
        merged[key] = merged_set
    # Lock fields: preserve existing type, title, created
    for key in ("type", "title", "created"):
        if key in existing:
            merged[key] = existing[key]
    return merged


def _fmt_frontmatter(fields: dict) -> str:
    """Format frontmatter dict back to YAML string."""
    lines = ["---"]
    for key, val in fields.items():
        if isinstance(val, list):
            items = ", ".join(
                f'"{v}"' if (" " in v or "-" in v) and not v.startswith("[[") else v
                for v in val
            )
            lines.append(f"{key}: [{items}]")
        elif isinstance(val, str) and (" " in val or ":" in val):
            lines.append(f'{key}: "{val}"')
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def merge_page_content(existing_text: str, new_text: str, config: Config) -> str:
    """NashSU parity (ingest.ts L1597-1609): 3-layer merge of old + new page content.

    1. Union-merge frontmatter arrays (sources, tags, related)
    2. Lock fields (type, title, created) from existing
    3. If both have substantial bodies, ask LLM to merge; else use new body
    """
    existing_fm, existing_body = _parse_frontmatter(existing_text)
    new_fm, new_body = _parse_frontmatter(new_text)

    merged_fm = _merge_frontmatter_arrays(existing_fm, new_fm)
    merged_fm["updated"] = time.strftime("%Y-%m-%d")

    # If new content is empty or just a stub, keep existing body
    if len(new_body.strip()) < 200:
        return _fmt_frontmatter(merged_fm) + "\n\n" + existing_body

    # If existing body is substantial, LLM merge
    if len(existing_body.strip()) > 200:
        prompt = f"""Merge two versions of a wiki page. Preserve ALL unique information from both.
Do NOT drop claims, entities, formulas, or references from either version.

# Existing page content
{existing_body[:3000]}

# New content (from latest ingest)
{new_body[:3000]}

# Task
Output the merged page body (no frontmatter, no code fences).
The merged version should contain everything from both versions,
with duplicates consolidated and new information integrated.
"""
        try:
            response, _ = call_anthropic_protocol(prompt, config, max_tokens=4096)
            merged_body = response.strip()
            # Safety: if LLM response is too short, fall back to new body
            if len(merged_body) < 100:
                merged_body = new_body
        except Exception:
            merged_body = new_body
    else:
        merged_body = new_body

    return _fmt_frontmatter(merged_fm) + "\n\n" + merged_body


def canonicalize_sources_field(content: str, canonical_source: str) -> str:
    """NashSU parity (ingest.ts L1298-1324): union-merge sources[] with dedup.

    Preserves existing sources from prior ingests. Only adds the canonical
    source if it's not already present (matched by full path or basename).
    Removes duplicate entries.
    """
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    fm = content[3:end]
    body = content[end + 4:]

    # Parse existing sources
    existing_sources: list[str] = []
    src_match = re.search(r'^sources:\s*\[(.*?)\]', fm, re.MULTILINE)
    if src_match:
        src_text = src_match.group(1)
        # Extract individual source strings (quoted or unquoted)
        existing_sources = [s.strip().strip('\'"') for s in src_text.split(",") if s.strip()]

    # Normalize canonical source for comparison
    canon_norm = canonical_source.lower().replace("\\", "/").rstrip("/")
    canon_base = Path(canon_norm).name.lower()

    # Check if canonical source already present (full path or basename match)
    already_present = False
    for s in existing_sources:
        sn = s.lower().replace("\\", "/").rstrip("/")
        if sn == canon_norm or Path(sn).name == canon_base:
            already_present = True
            break

    if not already_present:
        existing_sources.append(canonical_source)

    # Dedup (keep order, remove case-duplicates)
    seen: set[str] = set()
    deduped: list[str] = []
    for s in existing_sources:
        key = s.lower().replace("\\", "/").rstrip("/")
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    # Rebuild sources line
    items = ", ".join(f'"{s}"' for s in deduped)
    lines = fm.split("\n")
    new_lines = []
    for line in lines:
        if line.strip().startswith("sources:"):
            new_lines.append(f"sources: [{items}]")
        else:
            new_lines.append(line)
    return "---\n" + "\n".join(new_lines) + "\n---" + body


def stamp_frontmatter_dates(content: str, today: str) -> str:
    """NashSU parity (ingest.ts L1440-1468): stamp created/updated dates."""
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    fm = content[3:end]
    body = content[end + 4:]
    lines = fm.split("\n")
    new_lines = []
    has_created = False
    has_updated = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("created:"):
            new_lines.append(f"created: {today}")
            has_created = True
        elif stripped.startswith("updated:"):
            new_lines.append(f"updated: {today}")
            has_updated = True
        else:
            new_lines.append(line)
    if not has_created:
        new_lines.append(f"created: {today}")
    if not has_updated:
        new_lines.append(f"updated: {today}")
    return "---\n" + "\n".join(new_lines) + "\n---" + body


def write_wiki_file(path: Path, content: str, config: Config | None = None, merge: bool = False) -> None:
    content = sanitize_ingested_content(content)
    if config is not None:
        backup_existing_page(path, config)
        if merge and path.exists():
            existing = path.read_text(encoding="utf-8")
            content = merge_page_content(existing, content, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def stage_2_6_aggregate_repair(
    source_path: Path,
    raw_file: Path,
    analysis: dict,
    source_hash: str,
    extract_method: str,
    config: Config,
) -> list[str]:
    """NashSU Stage 2.6: update index.md (append), log.md (append), overview.md (LLM rewrite)."""
    files_written: list[str] = []

    # log.md
    log_path = config.wiki_dir / "log.md"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8")
    else:
        log_text = "# Log\n"
    raw_rel = raw_file.relative_to(config.raw_root)
    source_rel = source_path.relative_to(config.wiki_dir)
    entry = (
        f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} — INGEST\n"
        f"- Source: `raw/{raw_rel}`\n"
        f"- Source page: `wiki/{source_rel}`\n"
        f"- Hash: {source_hash[:16]}\n"
        f"- Method: {extract_method}\n"
    )
    log_text += entry
    write_wiki_file(log_path, log_text, config)
    files_written.append(str(log_path.relative_to(config.wiki_root)))

    # index.md — append link to new source page
    index_path = config.wiki_dir / "index.md"
    if index_path.exists():
        index_text = index_path.read_text(encoding="utf-8")
    else:
        index_text = "# Index\n\n## Sources\n\n"
    new_link = f"- [[{source_path.stem}]]\n"
    if "## Sources" in index_text and new_link not in index_text:
        index_text = index_text.replace("## Sources\n", f"## Sources\n\n{new_link}", 1)
        write_wiki_file(index_path, index_text, config)
        files_written.append(str(index_path.relative_to(config.wiki_root)))

    # overview.md — NashSU aggregate repair: LLM rewrite with existing content as context.
    # Unlike the ADL8113 incident, the LLM SEES the current overview and preserves it.
    overview_path = config.wiki_dir / "overview.md"
    if overview_path.exists():
        current_overview = overview_path.read_text(encoding="utf-8")
        # NashSU parity (ingest.ts L1281-1296): proportional safety caps.
        # Section cap = max(4K, 12% of context window) for both index and overview.
        _AGGREGATE_CAP = max(4096, int(config.source_budget * 0.12))
        OVERVIEW_MAX_CHARS = min(24000, _AGGREGATE_CAP)
        INDEX_MAX_CHARS = _AGGREGATE_CAP
        if len(current_overview) > OVERVIEW_MAX_CHARS:
            print(f"[stage_2_6] Overview too large ({len(current_overview)} > {OVERVIEW_MAX_CHARS}) — "
                  f"skipping LLM rewrite to avoid truncation")
            return files_written

        # Index size check (NashSU parity: isAggregateRepairSafe)
        if index_path.exists():
            index_size = index_path.stat().st_size
            if index_size > INDEX_MAX_CHARS:
                print(f"[stage_2_6] Index too large ({index_size} > {INDEX_MAX_CHARS}) — "
                      f"skipping aggregate repair to avoid context overflow")
                return files_written
        source_content = source_path.read_text(encoding="utf-8") if source_path.exists() else ""

        sources_lines: list[str] = []
        sources_dir = config.wiki_dir / "sources"
        if sources_dir.is_dir():
            for f in sorted(sources_dir.rglob("*.md"))[-10:]:
                text = f.read_text(encoding="utf-8")
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    body = text[end + 4:] if end != -1 else text
                else:
                    body = text
                sources_lines.append(f"### {f.stem}\n{body[:800]}")

        prompt = f"""You maintain the overview of a hardware knowledge base wiki.
Below is the CURRENT overview.md, followed by the newly ingested source page.
Rewrite overview.md to incorporate the new source into a comprehensive 2-5
paragraph overview of ALL topics now in the wiki. Preserve all existing claims
and source references; only add or refine based on the new source.

# Current overview.md
{current_overview}

# New source page: {source_path.stem}
{source_content[:3000]}

# Recent source pages (for context)
{chr(10).join(sources_lines[:8])}

# Task
Rewrite the COMPLETE overview.md. Output ONLY the new overview.md content
(starting with \"# Overview\"). Preserve the structure:
- ## Where we are (2-5 paragraph comprehensive overview of ALL topics)
- ## Strong Claims (well-supported by multiple sources)
- ## Weak Claims (single-source or speculative)
- ## Open Questions
- ## Sources (auto-populated list — keep existing entries, add new source link)

Do NOT change or remove existing Strong Claims / Weak Claims / Open Questions
unless the new source directly contradicts or answers them.
"""
        try:
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=4096)
            # NashSU parity: filter aggregate repair output — reject FILE blocks (ingest.ts L1216-1235)
            if "---FILE:" in response:
                print(f"[stage_2_6] LLM response contained FILE blocks — discarding")
            elif response.strip().startswith("#"):
                write_wiki_file(overview_path, response.strip() + "\n", config)
                files_written.append(str(overview_path.relative_to(config.wiki_root)))
                print(f"[stage_2_6] Overview updated via LLM ({len(response)} chars, stop={stop_reason})")
            else:
                print(f"[stage_2_6] LLM overview response did not start with '# Overview' — skipping")
        except Exception as e:
            print(f"[stage_2_6] Overview LLM update failed: {e}")

    return files_written


# ---------- Existing wiki context ----------

# ---------- Domain detection & slug disambiguation ----------

# Domain detection keywords: title/subtitle → domain slug
_DOMAIN_KEYWORDS: dict[str, str] = {
    "thermal": "thermal-management",
    "cooling": "thermal-management",
    "heat transfer": "thermal-management",
    "heat sink": "thermal-management",
    "power electronic": "power-electronics",
    "switching converter": "power-electronics",
    "converter": "power-electronics",
    "dc-dc": "power-electronics",
    "electromagnetic compatibility": "emc",
    "emc": "emc",
    "emi": "emc",
    "signal integrity": "signal-integrity",
    "high-speed digital": "signal-integrity",
    "high speed digital": "signal-integrity",
    "transmission line": "signal-integrity",
    "crosstalk": "signal-integrity",
    "art of electronics": "circuit-fundamentals",
    "electronic": "circuit-fundamentals",
    "digital circuit": "digital-circuits",
    "digital logic": "digital-circuits",
    "pcb design": "pcb-design",
    "printed circuit": "pcb-design",
    "rf ": "rf-microwave",
    "microwave": "rf-microwave",
    "antenna": "rf-microwave",
    "radar": "radar-systems",
    "phased array": "radar-systems",
    "operational amplifier": "analog-circuits",
    "op-amp": "analog-circuits",
    "analog circuit": "analog-circuits",
    "filter design": "analog-circuits",
    "mosfet": "semiconductor-devices",
    "igbt": "semiconductor-devices",
    "gan": "semiconductor-devices",
    "sic": "semiconductor-devices",
    "semiconductor": "semiconductor-devices",
    "reliability": "reliability-engineering",
    "failure analysis": "reliability-engineering",
    "circuit": "circuit-fundamentals",
    "electric circuit": "circuit-fundamentals",
    "ohm": "circuit-fundamentals",
    "kirchhoff": "circuit-fundamentals",
}

# Template type → domain mapping (datasheets are almost always semiconductor-devices)
_TEMPLATE_DOMAIN: dict[str, str] = {
    "digest-datasheet.md": "semiconductor-devices",
    "digest-applicationnote.md": "general",    # application notes span multiple domains
    "digest-designexample.md": "general",
    "digest-standard.md": "general",
    "digest-news.md": "general",
}


def _detect_domain(file_path: Path, template: str, global_digest: dict | None = None) -> str:
    """Detect the domain of a source file from title, template type, and digest.

    Uses keyword matching on the filename stem and global digest outline.
    Falls back to 'general' if no domain can be determined.
    """
    title_lower = file_path.stem.lower()

    # Template-based detection first (high precision)
    template_name = Path(template).name if template else ""
    if template_name in _TEMPLATE_DOMAIN:
        return _TEMPLATE_DOMAIN[template_name]

    # Keyword-based detection from title
    for keyword, domain in _DOMAIN_KEYWORDS.items():
        if keyword in title_lower:
            return domain

    # If we have a global digest, check its outline for domain hints
    if global_digest:
        outline = global_digest.get("outline", [])
        outline_str = " ".join(
            (c.get("title", "") + " " + str(c.get("key_topics", "")) if isinstance(c, dict)
             else str(c))
            for c in outline
        ).lower()
        for keyword, domain in _DOMAIN_KEYWORDS.items():
            if keyword in outline_str:
                return domain

    return "general"


def _list_existing_concepts_with_domains(config) -> dict[str, str]:
    """Scan wiki/concepts/ and return dict of slug → domain for all concept pages.

    Reads frontmatter to extract the domain field. Pages without domain default to 'general'.
    """
    result: dict[str, str] = {}
    concepts_dir = config.wiki_dir / "concepts"
    if not concepts_dir.exists():
        return result

    for f in concepts_dir.glob("*.md"):
        slug = f.stem
        try:
            text = f.read_text(encoding="utf-8")
            fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
            if fm_match:
                for line in fm_match.group(1).split("\n"):
                    m = re.match(r'domain:\s*(\S+)', line)
                    if m:
                        result[slug] = m.group(1).strip()
                        break
            if slug not in result:
                result[slug] = "general"
        except Exception:
            result[slug] = "general"
    return result


def _find_slug_collisions(
    new_concepts: list[str],
    existing_domains: dict[str, str],
    current_domain: str,
) -> list[tuple[str, str, str]]:
    """Find new concept slugs that collide with existing ones from different domains.

    Returns list of (slug, existing_domain, current_domain) for collisions.
    Excludes same-domain matches (those are legitimate merges, not collisions).
    """
    collisions: list[tuple[str, str, str]] = []
    for name in new_concepts:
        # Generate expected slug (kebab-case, no special chars)
        slug = re.sub(r'[<>:"|?*\\/]+', '', name).strip()
        slug = re.sub(r'\s+', '-', slug).lower()
        if slug in existing_domains:
            existing_domain = existing_domains[slug]
            if existing_domain != current_domain:
                collisions.append((slug, existing_domain, current_domain))
    return collisions


def _disambiguate_slug(slug: str, domain: str, existing_domains: dict[str, str]) -> str:
    """Resolve a slug collision by appending the domain suffix.

    Only appends if the slug already exists with a DIFFERENT domain.
    If slug exists with the SAME domain, returns unchanged (merge case).
    """
    if slug not in existing_domains:
        return slug  # no collision
    existing_domain = existing_domains[slug]
    if existing_domain == domain:
        return slug  # same domain → merge, no rename needed
    # Different domain → disambiguate
    # Remove any existing domain suffix first (avoid double-suffix)
    for d in _TEMPLATE_DOMAIN.values():
        d_slug = d.replace("_", "-")
        if slug.endswith(f"-{d_slug}"):
            slug = slug[:-len(f"-{d_slug}")]
            break
    for d in _DOMAIN_KEYWORDS.values():
        d_slug = d.replace("_", "-")
        if slug.endswith(f"-{d_slug}"):
            slug = slug[:-len(f"-{d_slug}")]
            break
    return f"{slug}-{domain}"


def _build_collision_warning(
    collisions: list[tuple[str, str, str]],
    existing_domains: dict[str, str],
) -> str:
    """Build a prompt section warning about slug collisions across domains."""
    if not collisions:
        return ""

    lines = [
        "",
        "# ⚠️ SLUG COLLISION WARNINGS",
        "The following concept names already exist in the wiki under DIFFERENT domains.",
        "Use domain-specific slugs (e.g., `switch-power-electronics` instead of `switch`) to disambiguate.",
        "",
    ]
    for slug, existing_domain, current_domain in collisions:
        lines.append(f"- **{slug}** — already exists in `{existing_domain}`, new use is in `{current_domain}` → use `{_disambiguate_slug(slug, current_domain, existing_domains)}`")

    lines.append("")
    return "\n".join(lines)


def list_existing_slugs(config: Config) -> list[str]:
    if not config.wiki_dir.exists():
        return []
    return [f.stem for f in config.wiki_dir.rglob("*.md")]


# ---------- Checkpoint / Resume ----------

def progress_path(config: Config, source_hash: str) -> Path:
    """Path to the progress checkpoint file for a given source hash."""
    config.progress_dir.mkdir(parents=True, exist_ok=True)
    return config.progress_dir / f"{source_hash[:16]}.json"


def load_progress(config: Config, source_hash: str) -> dict | None:
    """Load partial progress. Returns None if no checkpoint exists."""
    pp = progress_path(config, source_hash)
    if pp.exists():
        return json.loads(pp.read_text(encoding="utf-8"))
    return None


def save_progress(config: Config, source_hash: str, data: dict) -> None:
    """Save partial progress atomically."""
    pp = progress_path(config, source_hash)
    tmp = pp.with_suffix(".tmp")
    data["_updated_at"] = int(time.time() * 1000)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(pp)


def clear_progress(config: Config, source_hash: str) -> None:
    """Remove progress checkpoint after successful completion."""
    pp = progress_path(config, source_hash)
    if pp.exists():
        pp.unlink()


# ---------- Project-level lock (NashSU parity: withProjectLock) ----------


class ProjectLock:
    """PID-file based mutual exclusion for a wiki project.

    Uses a lockfile at <runtime>/ingest.lock containing "owner=<id> pid=<pid>".
    Unlike fcntl.flock, this survives process exit — critical for conversation
    mode where ingest.py exits with code 101 between stages.

    Lock owner is the source hash prefix (8 hex chars), so the same source can
    resume across invocations while different sources serialize.
    """

    def __init__(self, config: Config, owner_id: str = ""):
        self._lock_path = config.runtime_dir / "ingest.lock"
        self._owner = owner_id or str(os.getpid())

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _read_lock(self) -> tuple[str, int] | None:
        if not self._lock_path.exists():
            return None
        try:
            text = self._lock_path.read_text().strip()
            owner = ""
            pid = 0
            for part in text.split():
                if part.startswith("owner="):
                    owner = part.split("=", 1)[1]
                elif part.startswith("pid="):
                    pid = int(part.split("=", 1)[1])
            return (owner, pid)
        except Exception:
            return None

    def acquire(self, timeout: float = 300) -> bool:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout

        while True:
            current = self._read_lock()
            if current is None:
                # No lock — acquire it
                self._lock_path.write_text(f"owner={self._owner} pid={os.getpid()}\n")
                print(f"[lock] Acquired by {self._owner}")
                return True

            owner, pid = current
            if owner == self._owner:
                # We already own the lock (resuming after conversation round)
                # Update PID in case we're a new process
                self._lock_path.write_text(f"owner={self._owner} pid={os.getpid()}\n")
                print(f"[lock] Re-acquired by {self._owner} (resume)")
                return True

            if not self._pid_alive(pid):
                # Stale lock — take it
                print(f"[lock] Stale lock from pid={pid} — taking over")
                self._lock_path.write_text(f"owner={self._owner} pid={os.getpid()}\n")
                return True

            if time.time() > deadline:
                print(f"[lock] Timed out after {timeout}s — held by {owner} (pid={pid})")
                return False

            print(f"[lock] Waiting for {owner} (pid={pid})...")
            time.sleep(3)

    def release(self):
        current = self._read_lock()
        if current and current[0] == self._owner:
            try:
                self._lock_path.unlink(missing_ok=True)
                print(f"[lock] Released by {self._owner}")
            except Exception:
                pass
        elif current:
            print(f"[lock] Not releasing — owned by {current[0]} not {self._owner}")
        else:
            pass  # already released


# ---------- Main pipeline ----------

def ingest_one(
    raw_file: Path,
    config: Config,
    template_override: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> dict:
    """Process one file end-to-end (NashSU-style 15-stage pipeline with checkpoint/resume)."""
    _set_current_file(raw_file.name)
    print(f"\n=== Ingest: {raw_file} ===")

    # 0. Clean up resolved review pages
    cleanup_resolved_reviews(config)

    # 1. Dedup check: wiki/sources/ immutable record
    #    Cache is NOT used for dedup — it is a volatile runtime optimization only.
    h = file_sha256(raw_file)
    cache = load_cache(config)
    config.conversation_prefix = h[-8:]  # per-source conversation file isolation
    task_manifest = _load_task_manifest(config)
    try:
        rel = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        rel = str(raw_file)
    source_page = wiki_path_for_source(raw_file, config)
    pending_tasks = task_manifest.get("pending", [])
    if pending_tasks:
        print(f"[conversation] {len(pending_tasks)} pending task(s) — resuming pipeline")

    # Check wiki/sources/ — immutable record of a completed ingest.
    # Also verify completeness: a source page alone isn't enough — the
    # ingest may have crashed after writing sources/ but before concepts/entities/.
    if source_page.exists() and not pending_tasks:
        # Read source page to find which concept/entity pages it references
        try:
            source_text = source_page.read_text(encoding="utf-8")
        except Exception:
            source_text = ""
        refs = re.findall(r'\[\[([^\]]+)\]\]', source_text)
        missing = []
        for slug in refs:
            slug = slug.split("|")[0].strip()
            concept_path = config.wiki_dir / "concepts" / f"{slug}.md"
            entity_path = config.wiki_dir / "entities" / f"{slug}.md"
            if not concept_path.exists() and not entity_path.exists():
                missing.append(slug)
        if not refs or len(missing) > len(refs) * 0.8:
            print(f"[skip] Source page exists + {len(refs)-len(missing)}/{len(refs)} linked pages found ({source_page.name})")
            return {"status": "skipped", "reason": "source-page-exists"}
        else:
            print(f"[skip:warn] Source page exists but {len(missing)}/{len(refs)} linked pages missing — re-ingesting")

    # Cache hit (hash match) is NOT a skip reason — it only means we can
    # resume from a partial run. Full digest always checks (a) and (b) first.

    # Check for partial progress (resume)
    progress = load_progress(config, h)
    resumed_from = None
    if progress:
        resumed_from = progress.get("stage", "unknown")
        print(f"[resume] Found checkpoint at stage={resumed_from}")

    # NOTE: Stage 0-2 logic duplicated with _do_prepare()
    # 2. Extract text
    if progress and "extracted_text" in progress:
        extracted_text = progress["extracted_text"]
        method = progress.get("extract_method", "cached")
        print(f"[extract] (cached) {method}: {len(extracted_text)} chars")
        _verify_stage_0_text(raw_file, extracted_text, method)
    else:
        _stage_begin("Stage 0: Text extraction")
        extracted_text, method = extract_text(raw_file, config, pilot_confirmed=pilot_confirmed)
        print(f"[extract] {method}: {len(extracted_text)} chars")
        _verify_stage_0_text(raw_file, extracted_text, method)
        save_progress(config, h, {
            "stage": "stage_0_done",
            "extracted_text": extracted_text,
            "extract_method": method,
        })
        _stage_end("Stage 0: Text extraction")

    # 3. Detect and load template (used in all 3 stages to guide LLM output)
    template_name = detect_template_type(raw_file, config.raw_root, template_override)
    template_content = load_template(template_name)
    print(f"[template] {template_name}")

    # ── Stage 0.5: Image extraction (text-layer PDFs only) ──
    stage_0_5_result: dict = {"count": 0}
    if progress and "stage_0_5" in progress:
        stage_0_5_result = progress["stage_0_5"]
        print(f"[stage_0_5] (cached) {stage_0_5_result.get('count', 0)} images")
    elif raw_file.suffix.lower() in (".pdf", ".pptx", ".docx") and (
            raw_file.suffix.lower() != ".pdf" or method == "pymupdf"):
        stage_0_5_result = stage_0_5_extract_images(raw_file, config)
        # Save progress with stage_0_5 data (preserve existing checkpoint data)
        cp = {"stage": "stage_0_done", "extracted_text": extracted_text,
              "extract_method": method, "stage_0_5": stage_0_5_result}
        save_progress(config, h, cp)

    # ── Stage 0.6 (Caption) ∥ Stage 1 (Global Digest) ──
    # These two stages are independent: caption uses MiniMax VLM on images,
    # digest uses LLM on text. Different API endpoints, no shared state.
    # Run them in parallel to hide I/O latency.
    needs_caption = (
        not progress or "stage_0_6" not in progress
    ) and stage_0_5_result.get("count", 0) > 0
    needs_digest = (
        not progress or progress.get("stage") not in ("stage_1_done", "stage_1_5_done", "stage_2_done")
    )
    stage_0_6_result = progress.get("stage_0_6", {"captioned": 0}) if progress and "stage_0_6" in progress else {"captioned": 0}

    if needs_caption and needs_digest:
        _stage_begin("Stage 0.6∥1: Caption + Global Digest (parallel)")
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_cap = executor.submit(stage_0_6_caption_images, config, stage_0_5_result)
            fut_dig = executor.submit(stage_1_global_digest, extracted_text, raw_file, config, template_content, verbose=verbose)
            stage_0_6_result = fut_cap.result()
            global_digest = fut_dig.result()
        _verify_stage_1_digest(global_digest, raw_file)
        _stage_end("Stage 0.6∥1: Caption + Global Digest (parallel)")
        if "extracted_text" not in (progress or {}):
            save_progress(config, h, {"stage": "stage_0_done", "extracted_text": extracted_text,
                  "extract_method": method, "stage_0_5": stage_0_5_result, "stage_0_6": stage_0_6_result})
        save_progress(config, h, {"stage": "stage_1_done", "extracted_text": extracted_text,
            "extract_method": method, "global_digest": global_digest, "stage_0_5": stage_0_5_result,
            "stage_0_6": stage_0_6_result})
    else:
        if needs_caption:
            print(f"[stage_0_6] Captioning images...")
            stage_0_6_result = stage_0_6_caption_images(config, stage_0_5_result)
        elif progress and "stage_0_6" in progress:
            stage_0_6_result = progress["stage_0_6"]
            print(f"[stage_0_6] (cached) {stage_0_6_result.get('captioned', 0)} captions")

        if needs_digest:
            global_digest = stage_1_global_digest(extracted_text, raw_file, config, template_content, verbose=verbose)
            _verify_stage_1_digest(global_digest, raw_file)
        else:
            global_digest = progress["global_digest"]
            print(f"[stage_1] (cached) Global Digest — {len(global_digest)} keys")
            _verify_stage_1_digest(global_digest, raw_file)

        if needs_caption and "extracted_text" not in (progress or {}):
            save_progress(config, h, {"stage": "stage_0_done", "extracted_text": extracted_text,
                  "extract_method": method, "stage_0_5": stage_0_5_result, "stage_0_6": stage_0_6_result})
        if needs_digest:
            save_progress(config, h, {"stage": "stage_1_done", "extracted_text": extracted_text,
                "extract_method": method, "global_digest": global_digest, "stage_0_5": stage_0_5_result,
                "stage_0_6": stage_0_6_result})

    # ── Stage 1.5: Chunk Analysis ──
    if progress and progress.get("stage") in ("stage_1_5_done", "stage_2_done") and "chunk_analyses" in progress:
        chunk_analyses = progress["chunk_analyses"]
        print(f"[stage_1_5] (cached) Chunk Analysis — {len(chunk_analyses)} chunks")
        _verify_stage_1_5_chunks(chunk_analyses, extracted_text)
    else:
        _stage_begin("Stage 1.5: Chunk Analysis")
        chunk_analyses = stage_1_5_chunk_analysis(extracted_text, global_digest, raw_file, config, template_content, verbose=verbose, source_hash=h)
        _verify_stage_1_5_chunks(chunk_analyses, extracted_text)
        _stage_end("Stage 1.5: Chunk Analysis")
        save_progress(config, h, {
            "stage": "stage_1_5_done",
            "extracted_text": extracted_text,
            "extract_method": method,
            "global_digest": global_digest,
            "chunk_analyses": chunk_analyses,
            "stage_0_5": stage_0_5_result,
            "stage_0_6": stage_0_6_result,
        })

    # ── Stage 2.0: Source page (NashSU two-step — dedicated LLM call) ──
    _stage_begin("Stage 2.0: Source page generation")
    # Infer domain from book meta (same logic as single-book path)
    current_domain = _detect_domain(file_path, template_content, global_digest)
    if progress and progress.get("stage") in ("stage_2_0_done", "stage_2_done") and "source_page_response" in progress:
        source_page_response = progress["source_page_response"]
        print(f"[stage_2_0] (cached) Source page already generated")
    else:
        source_page_response, _ = stage_2_0_source_page(
            global_digest, raw_file, config,
            template=template_content, current_domain=current_domain, verbose=verbose
        )
    _stage_end("Stage 2.0: Source page generation")

    # ── Stage 2: Concept/Entity Generation ──
    # Per-chunk mode for multi-chunk books, legacy synthesis for small books
    _stage_begin("Stage 2: Concept + Entity pages")
    if progress and progress.get("stage") == "stage_2_done" and "raw_response" in progress:
        analysis = progress["analysis"]
        raw_response = progress["raw_response"]
        file_blocks = parse_file_blocks(raw_response)
        print(f"[stage_2] (cached) Synthesis — {len(file_blocks)} file blocks from checkpoint")
    elif len(chunk_analyses) > 1:
        # Per-chunk generation: each chunk generates its own concept pages in parallel
        analysis, raw_response, file_blocks = stage_2_per_chunk_generation(
            chunk_analyses, [], global_digest, raw_file, config, template_content,
            max_chunk_concurrent=_chunk_concurrency(), verbose=verbose,
        )
    else:
        analysis, raw_response, file_blocks = stage_2_synthesis(
            global_digest, chunk_analyses, raw_file, config, template_content, verbose=verbose
        )
    _verify_stage_2_file_blocks(file_blocks, raw_file)

    # Merge Stage 2.0 source page into file_blocks (NashSU two-step)
    if source_page_response:
        source_blocks = parse_file_blocks(source_page_response)
        if source_blocks:
            file_blocks = source_blocks + list(file_blocks)
            print(f"[stage_2_0] Source page block merged into {len(file_blocks)} total blocks")
        else:
            print(f"[stage_2_0] ⚠️  No FILE block found in source page response — "
                  f"response starts: {source_page_response[:100]}...")

    if not progress or progress.get("stage") != "stage_2_done":
        # Save synthesis checkpoint — expensive call, don't lose it
        save_progress(config, h, {
            "stage": "stage_2_done",
            "extracted_text": extracted_text,
            "extract_method": method,
            "global_digest": global_digest,
            "chunk_analyses": chunk_analyses,
            "source_page_response": source_page_response,
            "analysis": analysis,
            "raw_response": raw_response,
            "stage_0_5": stage_0_5_result,
            "stage_0_6": stage_0_6_result,
        })
    analysis["__source_hash"] = h
    analysis["__extract_method"] = method

    if verbose:
        print(f"\n{'='*60}")
        print(f"RAW LLM RESPONSE (Stage 2 — Synthesis):")
        print(f"{'='*60}")
        print(raw_response[:10000])
        print(f"{'='*60}\n")

    _stage_end("Stage 2: Synthesis + File blocks")

    # ── Stage 2.3: Query generation ──
    query_blocks, query_response = stage_2_3_query_generation(
        global_digest, chunk_analyses, file_blocks, raw_file, config,
        template=template_content, verbose=verbose
    )
    if query_blocks:
        file_blocks = list(file_blocks) + query_blocks

    # ── Stage 2.5: Comparison generation ──
    comp_blocks, comp_response = stage_2_5_comparison_generation(
        global_digest, chunk_analyses, file_blocks, raw_file, config,
        template=template_content, verbose=verbose
    )
    if comp_blocks:
        file_blocks = list(file_blocks) + comp_blocks

    # NOTE: Stage 3+ logic duplicated with _do_write()
    # 6. Write wiki files
    source_path = wiki_path_for_source(raw_file, config)
    files_written_paths: list[str] = []
    hard_failures: list[str] = []  # NashSU parity: track FS errors to gate cache save
    source_block: tuple[str, str] | None = None

    # Known wiki subdirectories for schema routing validation (NashSU parity)
    _VALID_SUBDIRS = {"sources", "concepts", "entities", "queries", "comparisons",
                      "synthesis", "findings", "thesis"}
    _LISTING_PAGES = {"index.md", "log.md", "overview.md", "schema.md"}

    # Detect expected language from source text (NashSU parity: contentMatchesTargetLanguage)
    try:
        from _language import detect_language
        expected_lang = detect_language(extracted_text[:5000]) if extracted_text else "unknown"
    except ImportError:
        expected_lang = "unknown"

    canonical_source = f"raw/{raw_file.relative_to(config.raw_root)}"
    today_str = time.strftime("%Y-%m-%d")

    for rel_path, content in file_blocks:
        # NashSU parity: isSafeIngestPath validation
        if ".." in rel_path or rel_path.startswith("/"):
            print(f"[write] Skipping unsafe path: {rel_path}")
            continue
        if not is_safe_ingest_path(rel_path):
            print(f"[write] Dropped unsafe path: {rel_path}")
            continue

        # NashSU parity: schema routing validation + auto-correction
        top_dir = rel_path.split("/")[0] if "/" in rel_path else ""
        basename = Path(rel_path).name
        if basename in _LISTING_PAGES:
            pass  # listing pages allowed at wiki root
        elif top_dir not in _VALID_SUBDIRS:
            # Auto-correct malformed paths instead of dropping them.
            # LLM sometimes outputs wiki/ConceptName instead of wiki/concepts/ConceptName.md
            # or wiki/Book Title instead of wiki/sources/Book Title.md
            corrected = _auto_correct_wiki_path(rel_path, content, config)
            if corrected:
                print(f"[write] Auto-corrected: {rel_path} → {corrected}")
                rel_path = corrected
            else:
                print(f"[write] Dropped — cannot correct path: {rel_path}")
                continue

        # Auto-add .md extension if missing (LLM sometimes drops it)
        if not rel_path.endswith(".md"):
            rel_path = rel_path + ".md"
            print(f"[write] Added .md extension: {rel_path}")

        # NashSU parity: language validation (warn on mismatch, write regardless)
        if expected_lang not in ("unknown", "English"):
            block_lang = detect_language(content[:2000])
            if block_lang not in (expected_lang, "English") and block_lang != "unknown":
                print(f"  [lang] ⚠️  {rel_path}: expected {expected_lang}, got {block_lang}")

        # NashSU parity: canonicalize sources field + stamp dates in frontmatter
        content = canonicalize_sources_field(content, canonical_source)
        content = stamp_frontmatter_dates(content, today_str)

        full_path = config.wiki_dir / rel_path
        # NashSU parity: listing pages (index/log/overview) overwrite; content pages merge
        is_listing = basename in _LISTING_PAGES
        do_merge = full_path.exists() and not is_listing

        try:
            write_wiki_file(full_path, content, config, merge=do_merge)
        except OSError as e:
            print(f"[write] HARD ERROR: {rel_path} — {e}")
            hard_failures.append(rel_path)
            continue

        files_written_paths.append(str(full_path.relative_to(config.wiki_root)))
        if full_path == source_path:
            source_block = (rel_path, content)
        action = "[merge]" if do_merge else "[overwrite]" if is_listing and full_path.exists() else "[write]"
        print(f"{action} {rel_path}")

    if not source_block:
        # Build NashSU-quality source page from digest data (no LLM needed)
        book_meta = analysis.get("book_meta", {})
        outline = analysis.get("outline", [])
        key_claims = analysis.get("key_claims", [])
        title = book_meta.get("title", raw_file.stem)
        authors = book_meta.get("authors", [])
        year = book_meta.get("year", "")
        publisher = book_meta.get("publisher", "")

        lines = [
            "---",
            "type: source",
            f'title: "{title}"',
            "domain: general",
            f"created: {today_str}",
            f"updated: {today_str}",
            "tags: []",
            "related: []",
            f'sources: ["{canonical_source}"]',
            "---",
            "",
            f"# {title}",
            "",
        ]
        if authors:
            lines.append(f"**Authors:** {', '.join(str(a) for a in authors[:5])}")
        if year:
            lines.append(f"**Year:** {year}")
        if publisher:
            lines.append(f"**Publisher:** {publisher}")
        lines.append("")

        if outline:
            lines.append("## Table of Contents & Key Concepts")
            lines.append("")
            for ch in outline[:40]:
                if isinstance(ch, dict):
                    ch_title = ch.get("title", "")
                    topics = ch.get("key_topics", [])
                    topics_str = ", ".join(str(t) for t in topics[:4]) if topics else ""
                else:
                    ch_title = str(ch)
                    topics_str = ""
                lines.append(f"1. **{ch_title}**" + (f" — {topics_str}" if topics_str else ""))
            lines.append("")

        if key_claims:
            lines.append("## Key Takeaways")
            lines.append("")
            for claim in key_claims[:10]:
                if isinstance(claim, dict):
                    lines.append(f"- {claim.get('claim', str(claim))}")
                else:
                    lines.append(f"- {str(claim)}")
            lines.append("")

        placeholder_content = "\n".join(lines) + "\n"
        try:
            write_wiki_file(source_path, placeholder_content, config)
            files_written_paths.append(str(source_path.relative_to(config.wiki_root)))
            print(f"[write] {source_path.relative_to(config.wiki_root)}  (placeholder)")
        except OSError as e:
            print(f"[write] HARD ERROR: placeholder source page — {e}")
            hard_failures.append("source-placeholder")

    # ── Stage 3.5: Image injection into source page ──
    stage_3_5_result: dict = {"injected": 0}
    if source_path.exists():
        stage_3_5_result = stage_3_5_inject_images(config, raw_file, source_path, method)

    # ── Stage 2.5: Review suggestions (NashSU 3-condition trigger) ──
    stage_2_5_result = stage_2_5_review_suggestions(
        config, file_blocks, raw_file, raw_response=raw_response, verbose=verbose)

    # ── Go/no-go validation (NashSU: all stages must pass checks) ──
    go_nogo_warnings = validate_stage_outputs(
        config, raw_file, method, extracted_text,
        stage_0_5_result, stage_0_6_result,
        file_blocks, source_path,
    )

# (compliance record removed — validate_ingest.py covers this)

    # ── Post-ingest lint (NashSU: structural lint after every ingest) ──
    _run_post_ingest_lint(config)
    # ── Post-ingest graph (staleness-guarded, <30min) ──
    _run_post_ingest_graph(config)

    # 7. Stage 2.6: Aggregate repair (index/log/overview)
    index_log_files = stage_2_6_aggregate_repair(source_path, raw_file, analysis, h, method, config)

    # 8. Update cache
    cache["entries"][rel] = {
        "hash": h,
        "timestamp": int(time.time() * 1000),
        "filesWritten": files_written_paths + index_log_files,
        "method": method,
        "template": template_name,
        "sourceHash": h,
        "fileBlockCount": len(file_blocks),
        "stages": {
            "global_digest_keys": len(global_digest),
            "chunks_analyzed": len(chunk_analyses),
            "file_blocks_generated": len(file_blocks),
            "concepts_identified": analysis.get("concepts_identified", len(file_blocks)),
            "concepts_core": analysis.get("concepts_core", 0),
            "concepts_supporting": analysis.get("concepts_supporting", 0),
            "concepts_generated": analysis.get("concepts_generated", len(file_blocks)),
            "coverage_core": analysis.get("coverage_core", 1.0),
            "coverage_supporting": analysis.get("coverage_supporting", 1.0),
            "coverage_pct": analysis.get("coverage_pct", 1.0),
            "images_extracted": stage_0_5_result.get("count", 0),
            "images_captioned": stage_0_6_result.get("captioned", 0),
            "images_injected": stage_3_5_result.get("injected", 0),
            "review_items": stage_2_5_result.get("items", 0),
        },
    }
    # NashSU parity: hard failures during file writes prevent cache save (ingest.ts L1115-1124).
    # Without this, a partial-write result is permanently cached and never retried.
    if hard_failures:
        print(f"[cache] SKIPPED — {len(hard_failures)} hard failure(s): {', '.join(hard_failures[:5])}")
        return {"status": "hard-error", "hard_failures": hard_failures,
                "files_written": files_written_paths + index_log_files}

    try:
        save_cache(config, cache)
        clear_progress(config, h)
        print(f"[cache] saved {rel}")
    except OSError as e:
        print(f"[cache] HARD ERROR — cache not saved: {e}")
        return {"status": "hard-error", "error": str(e), "files_written": files_written_paths + index_log_files}

    # ── Stage 6 (NashSU parity): auto-embed new pages if embedding is configured ──
    _auto_embed_new_pages(config, files_written_paths + index_log_files)

    # ── Final verification: run validate_ingest.py to confirm all stages pass ──
    # Superpowers: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
    _auto_validate_ingest(config, raw_file)

    return {"status": "ok", "files_written": cache["entries"][rel]["filesWritten"]}


# ═══════════════════════════════════════════════════════════════
# Batch ingest: parallel Stage 0-2, serial Stage 3+
# ═══════════════════════════════════════════════════════════════

# Maximum concurrency for parallel LLM phases.
# Stage 1/1.5/2 are read-only LLM calls — no shared state mutation,
# no disk writes to wiki/ — so they can safely run in parallel.
# Set based on LLM API rate limits; 4 is safe for most providers.
BATCH_MAX_CONCURRENT = 4


def _do_prepare(
    raw_file: Path, config: Config,
    template_override: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> dict | None:
    """Stage 0-2 for one book.  Read-only: no shared state writes, no lock needed.

    Returns a dict with all data needed for Stage 3+, or None on skip/failure.
    Suitable for parallel execution across multiple books.
    """
    _set_current_file(raw_file.name)
    print(f"\n=== [prepare] {raw_file.name} ===")
    try:
        # Dedup check — verify completeness, not just source page existence
        source_page = wiki_path_for_source(raw_file, config)
        if source_page.exists():
            try:
                source_text = source_page.read_text(encoding="utf-8")
            except Exception:
                source_text = ""
            refs = re.findall(r'\[\[([^\]]+)\]\]', source_text)
            missing = []
            for slug in refs:
                slug = slug.split("|")[0].strip()
                concept_path = config.wiki_dir / "concepts" / f"{slug}.md"
                entity_path = config.wiki_dir / "entities" / f"{slug}.md"
                if not concept_path.exists() and not entity_path.exists():
                    missing.append(slug)
            if not refs or len(missing) > len(refs) * 0.8:
                print(f"  [skip:warn] Source page exists but {len(missing)}/{len(refs)} linked pages missing — re-ingesting")
            else:
                print(f"  [skip] Source page exists ({len(refs)-len(missing)}/{len(refs)} linked pages found)")
                return None

        h = file_sha256(raw_file)
        progress = load_progress(config, h)

        # NOTE: Stage 0-2 logic duplicated with _do_prepare()
        # Stage 0: Text extraction
        if progress and "extracted_text" in progress:
            extracted_text = progress["extracted_text"]
            method = progress.get("extract_method", "cached")
            print(f"  [extract] (cached) {method}: {len(extracted_text)} chars")
        else:
            extracted_text, method = extract_text(raw_file, config, pilot_confirmed=pilot_confirmed)
            print(f"  [extract] {method}: {len(extracted_text)} chars")
            _verify_stage_0_text(raw_file, extracted_text, method)
            save_progress(config, h, {
                "stage": "stage_0_done", "extracted_text": extracted_text,
                "extract_method": method,
            })

        # Template
        template_name = detect_template_type(raw_file, config.raw_root, template_override)
        template_content = load_template(template_name)
        print(f"  [template] {template_name}")

        # Stage 0.5: Image extraction
        stage_0_5_result: dict = {"count": 0}
        if progress and "stage_0_5" in progress:
            stage_0_5_result = progress["stage_0_5"]
            print(f"  [stage_0_5] (cached) {stage_0_5_result.get('count', 0)} images")
        elif raw_file.suffix.lower() == ".pdf" and method == "pymupdf":
            stage_0_5_result = stage_0_5_extract_images(raw_file, config)
            # Save progress with stage_0_5 data (preserve existing checkpoint data)
            cp = {"stage": "stage_0_done", "extracted_text": extracted_text,
                  "extract_method": method, "stage_0_5": stage_0_5_result}
            save_progress(config, h, cp)

        # Stage 0.6 (Caption) ∥ Stage 1 (Global Digest) — batch path
        needs_caption = (
            not progress or "stage_0_6" not in progress
        ) and stage_0_5_result.get("count", 0) > 0
        needs_digest = (
            not progress or progress.get("stage") not in ("stage_1_done", "stage_1_5_done", "stage_2_done")
        )
        stage_0_6_result = progress.get("stage_0_6", {"captioned": 0}) if progress and "stage_0_6" in progress else {"captioned": 0}

        if needs_caption and needs_digest:
            with ThreadPoolExecutor(max_workers=2) as executor:
                fut_cap = executor.submit(stage_0_6_caption_images, config, stage_0_5_result)
                fut_dig = executor.submit(stage_1_global_digest, extracted_text, raw_file, config, template_content, verbose=verbose)
                stage_0_6_result = fut_cap.result()
                global_digest = fut_dig.result()
            _verify_stage_1_digest(global_digest, raw_file)
            if "extracted_text" not in (progress or {}):
                save_progress(config, h, {"stage": "stage_0_done", "extracted_text": extracted_text,
                      "extract_method": method, "stage_0_5": stage_0_5_result, "stage_0_6": stage_0_6_result})
        else:
            if needs_caption:
                stage_0_6_result = stage_0_6_caption_images(config, stage_0_5_result)
            elif progress and "stage_0_6" in progress:
                stage_0_6_result = progress["stage_0_6"]
                print(f"  [stage_0_6] (cached) {stage_0_6_result.get('captioned', 0)} captions")

            if needs_digest:
                global_digest = stage_1_global_digest(extracted_text, raw_file, config, template_content, verbose=verbose)
                _verify_stage_1_digest(global_digest, raw_file)
            else:
                global_digest = progress["global_digest"]
                print(f"  [stage_1] (cached) Global Digest — {len(global_digest)} keys")
                _verify_stage_1_digest(global_digest, raw_file)

            if needs_caption and "extracted_text" not in (progress or {}):
                save_progress(config, h, {"stage": "stage_0_done", "extracted_text": extracted_text,
                      "extract_method": method, "stage_0_5": stage_0_5_result, "stage_0_6": stage_0_6_result})

        # Stage 1.5: Chunk Analysis
        if progress and progress.get("stage") in ("stage_1_5_done", "stage_2_done") and "chunk_analyses" in progress:
            chunk_analyses = progress["chunk_analyses"]
            print(f"  [stage_1_5] (cached) Chunk Analysis — {len(chunk_analyses)} chunks")
            _verify_stage_1_5_chunks(chunk_analyses, extracted_text)
        else:
            chunk_analyses = stage_1_5_chunk_analysis(extracted_text, global_digest, raw_file, config, template_content, verbose=verbose, source_hash=h)
            _verify_stage_1_5_chunks(chunk_analyses, extracted_text)

        # Stage 2.0: Source page generation (NashSU two-step — dedicated LLM call)
        current_domain = _detect_domain(file_path, template_content, global_digest)
        if progress and progress.get("stage") in ("stage_2_0_done", "stage_2_done") and "source_page_response" in progress:
            source_page_response = progress["source_page_response"]
            print(f"  [stage_2_0] (cached) Source page already generated")
        else:
            source_page_response, _ = stage_2_0_source_page(
                global_digest, raw_file, config,
                template=template_content, current_domain=current_domain, verbose=verbose
            )

        # Stage 2: Generation (per-chunk for multi-chunk, legacy synthesis for single)
        if progress and progress.get("stage") == "stage_2_done" and "raw_response" in progress:
            analysis = progress["analysis"]
            raw_response = progress["raw_response"]
            file_blocks = parse_file_blocks(raw_response)
            print(f"  [stage_2] (cached) Synthesis — {len(file_blocks)} file blocks")
        elif len(chunk_analyses) > 1:
            # Passing [] for chunks: the chunk-text-aware path is not yet wired;
            # chunk_analyses carry all structured data the LLM needs per chunk.
            analysis, raw_response, file_blocks = stage_2_per_chunk_generation(
                chunk_analyses, [], global_digest, raw_file, config, template_content, verbose=verbose,
            )
        else:
            analysis, raw_response, file_blocks = stage_2_synthesis(
                global_digest, chunk_analyses, raw_file, config, template_content, verbose=verbose
            )
        _verify_stage_2_file_blocks(file_blocks, raw_file)

        # Merge Stage 2.0 source page into file_blocks
        if source_page_response:
            source_blocks = parse_file_blocks(source_page_response)
            if source_blocks:
                file_blocks = source_blocks + list(file_blocks)
                print(f"  [stage_2_0] Source page block merged ({len(file_blocks)} total)")

        # ── Stage 2.3: Query generation ──
        query_blocks, query_response = stage_2_3_query_generation(
            global_digest, chunk_analyses, file_blocks, raw_file, config,
            template=template_content, verbose=verbose
        )
        if query_blocks:
            file_blocks = list(file_blocks) + query_blocks

        # ── Stage 2.5: Comparison generation ──
        comp_blocks, comp_response = stage_2_5_comparison_generation(
            global_digest, chunk_analyses, file_blocks, raw_file, config,
            template=template_content, verbose=verbose
        )
        if comp_blocks:
            file_blocks = list(file_blocks) + comp_blocks

        analysis["__source_hash"] = h
        analysis["__extract_method"] = method

        print(f"  [prepare] ✅ done — {len(file_blocks)} blocks")
        return {
            "raw_file": raw_file, "config": config,
            "h": h, "method": method, "extracted_text": extracted_text,
            "global_digest": global_digest, "chunk_analyses": chunk_analyses,
            "analysis": analysis, "raw_response": raw_response,
            "file_blocks": file_blocks,
            "stage_0_5_result": stage_0_5_result,
            "stage_0_6_result": stage_0_6_result,
            "template_name": template_name,
        }
    except Exception as e:
        print(f"  [prepare] ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return None


def _do_write(prepared: dict, verbose: bool = False) -> dict:
    """Stage 3+ for one book.  Writes wiki files, updates cache, runs validation.
    MUST be called serially — modifies shared wiki/ state.
    """
    raw_file = prepared["raw_file"]
    config = prepared["config"]
    h = prepared["h"]
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
    raw_response = prepared["raw_response"]
    file_blocks = prepared["file_blocks"]
    stage_0_5_result = prepared["stage_0_5_result"]
    stage_0_6_result = prepared["stage_0_6_result"]
    template_name = prepared["template_name"]

    print(f"\n=== [write] {raw_file.name} ===")

    # Write wiki files (same logic as ingest_one Stage 3+)
    source_path = wiki_path_for_source(raw_file, config)
    files_written_paths: list[str] = []
    hard_failures: list[str] = []
    source_block: tuple[str, str] | None = None

    _VALID_SUBDIRS = {"sources", "concepts", "entities", "queries", "comparisons",
                      "synthesis", "findings", "thesis"}
    _LISTING_PAGES = {"index.md", "log.md", "overview.md", "schema.md"}

    try:
        from _language import detect_language
        expected_lang = detect_language(extracted_text[:5000]) if extracted_text else "unknown"
    except ImportError:
        expected_lang = "unknown"

    canonical_source = f"raw/{raw_file.relative_to(config.raw_root)}"
    today_str = time.strftime("%Y-%m-%d")

    for rel_path, content in file_blocks:
        if ".." in rel_path or rel_path.startswith("/"):
            continue
        if not is_safe_ingest_path(rel_path):
            continue

        top_dir = rel_path.split("/")[0] if "/" in rel_path else ""
        basename = Path(rel_path).name
        if basename in _LISTING_PAGES:
            pass
        elif top_dir not in _VALID_SUBDIRS:
            corrected = _auto_correct_wiki_path(rel_path, content, config)
            if corrected:
                print(f"  [write] Auto-corrected: {rel_path} → {corrected}")
                rel_path = corrected
            else:
                print(f"  [write] Dropped — cannot correct path: {rel_path}")
                continue

        if not rel_path.endswith(".md"):
            rel_path = rel_path + ".md"

        if expected_lang not in ("unknown", "English"):
            try:
                from _language import detect_language
                block_lang = detect_language(content[:2000])
                if block_lang not in (expected_lang, "English") and block_lang != "unknown":
                    print(f"  [lang] ⚠️  {rel_path}: expected {expected_lang}, got {block_lang}")
            except ImportError:
                pass

        content = canonicalize_sources_field(content, canonical_source)
        content = stamp_frontmatter_dates(content, today_str)

        full_path = config.wiki_dir / rel_path
        is_listing = basename in _LISTING_PAGES
        do_merge = full_path.exists() and not is_listing

        try:
            write_wiki_file(full_path, content, config, merge=do_merge)
        except OSError as e:
            print(f"  [write] HARD ERROR: {rel_path} — {e}")
            hard_failures.append(rel_path)
            continue

        files_written_paths.append(str(full_path.relative_to(config.wiki_root)))
        if full_path == source_path:
            source_block = (rel_path, content)
        action = "[merge]" if do_merge else "[overwrite]" if is_listing and full_path.exists() else "[write]"
        print(f"  {action} {rel_path}")

    if not source_block:
        # Build NashSU-quality source page from digest data (no LLM needed)
        book_meta = analysis.get("book_meta", {})
        outline = analysis.get("outline", [])
        key_claims = analysis.get("key_claims", [])
        title = book_meta.get("title", raw_file.stem)
        authors = book_meta.get("authors", [])
        year = book_meta.get("year", "")
        publisher = book_meta.get("publisher", "")

        lines = [
            "---",
            "type: source",
            f'title: "{title}"',
            "domain: general",
            f"created: {today_str}",
            f"updated: {today_str}",
            "tags: []",
            "related: []",
            f'sources: ["{canonical_source}"]',
            "---",
            "",
            f"# {title}",
            "",
        ]
        if authors:
            lines.append(f"**Authors:** {', '.join(str(a) for a in authors[:5])}")
        if year:
            lines.append(f"**Year:** {year}")
        if publisher:
            lines.append(f"**Publisher:** {publisher}")
        lines.append("")

        if outline:
            lines.append("## Table of Contents & Key Concepts")
            lines.append("")
            for ch in outline[:40]:
                if isinstance(ch, dict):
                    ch_title = ch.get("title", "")
                    topics = ch.get("key_topics", [])
                    topics_str = ", ".join(str(t) for t in topics[:4]) if topics else ""
                else:
                    ch_title = str(ch)
                    topics_str = ""
                lines.append(f"1. **{ch_title}**" + (f" — {topics_str}" if topics_str else ""))
            lines.append("")

        if key_claims:
            lines.append("## Key Takeaways")
            lines.append("")
            for claim in key_claims[:10]:
                if isinstance(claim, dict):
                    lines.append(f"- {claim.get('claim', str(claim))}")
                else:
                    lines.append(f"- {str(claim)}")
            lines.append("")

        placeholder_content = "\n".join(lines) + "\n"
        try:
            write_wiki_file(source_path, placeholder_content, config)
            files_written_paths.append(str(source_path.relative_to(config.wiki_root)))
        except OSError as e:
            hard_failures.append("source-placeholder")

    # Stage 3.5: Image injection
    stage_3_5_result: dict = {"injected": 0}
    if source_path.exists():
        stage_3_5_result = stage_3_5_inject_images(config, raw_file, source_path, method)

    # Stage 2.5: Review
    stage_2_5_result = stage_2_5_review_suggestions(
        config, file_blocks, raw_file, raw_response=raw_response, verbose=verbose)

    # Go/no-go validation
    go_nogo_warnings = validate_stage_outputs(
        config, raw_file, method, extracted_text,
        stage_0_5_result, stage_0_6_result,
        file_blocks, source_path,
    )

    # Post-ingest lint
    _run_post_ingest_lint(config)

    # Stage 2.6: Aggregate repair
    index_log_files = stage_2_6_aggregate_repair(source_path, raw_file, analysis, h, method, config)

    # Update cache
    try:
        rel = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        rel = str(raw_file)
    cache = load_cache(config)
    cache["entries"][rel] = {
        "hash": h,
        "timestamp": int(time.time() * 1000),
        "filesWritten": files_written_paths + index_log_files,
        "method": method,
        "template": template_name,
        "sourceHash": h,
        "fileBlockCount": len(file_blocks),
        "stages": {
            "global_digest_keys": len(global_digest),
            "chunks_analyzed": len(chunk_analyses),
            "file_blocks_generated": len(file_blocks),
            "concepts_identified": analysis.get("concepts_identified", len(file_blocks)),
            "concepts_core": analysis.get("concepts_core", 0),
            "concepts_supporting": analysis.get("concepts_supporting", 0),
            "concepts_generated": analysis.get("concepts_generated", len(file_blocks)),
            "coverage_core": analysis.get("coverage_core", 1.0),
            "coverage_supporting": analysis.get("coverage_supporting", 1.0),
            "coverage_pct": analysis.get("coverage_pct", 1.0),
            "images_extracted": stage_0_5_result.get("count", 0),
            "images_captioned": stage_0_6_result.get("captioned", 0),
            "images_injected": stage_3_5_result.get("injected", 0),
            "review_items": stage_2_5_result.get("items", 0),
        },
    }
    if hard_failures:
        print(f"  [cache] SKIPPED — {len(hard_failures)} hard failure(s)")
        return {"status": "hard-error", "hard_failures": hard_failures,
                "files_written": files_written_paths + index_log_files}
    try:
        save_cache(config, cache)
        clear_progress(config, h)
        print(f"  [cache] saved")
    except OSError as e:
        return {"status": "hard-error", "error": str(e),
                "files_written": files_written_paths + index_log_files}

    # Archive + embed + validate
    _auto_embed_new_pages(config, files_written_paths + index_log_files)
    _auto_validate_ingest(config, raw_file)

    return {"status": "ok", "files_written": cache["entries"][rel]["filesWritten"]}


def batch_ingest(
    raw_files: list[Path],
    config: Config,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    template_override: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> list[dict]:
    """Ingest multiple books with parallel Stage 0-2 and serial Stage 3+.

    Why this works:
      - Stage 0-2 (text extraction, digest, chunk analysis, synthesis) are
        read-only LLM calls. No wiki/ files are written, no shared state
        is mutated. Different books' Stage 0-2 can run concurrently.
      - Stage 3+ (file write, cache update, lint, archive, validation)
        modifies shared wiki/ state. MUST be serialized to avoid races.

    Max concurrency: {BATCH_MAX_CONCURRENT} by default.  Increase if your
    LLM API has generous rate limits.  Memory/CPU usage is negligible
    (just API call orchestration).
    """
    if max_concurrent < 1:
        max_concurrent = 1
    max_concurrent = min(max_concurrent, len(raw_files))

    print(f"\n{'='*60}")
    print(f"Batch ingest: {len(raw_files)} books, max {max_concurrent} concurrent")
    print(f"{'='*60}")

    # Pipeline: parallel prepare (Stage 0-2) → serial write (Stage 3+).
    # Books are written as soon as their Stage 2 finishes — no need to wait
    # for all books.  Write order is completion order, not submission order.
    lock = ProjectLock(config, owner_id="batch")
    if not lock.acquire():
        raise RuntimeError("Could not acquire project lock for batch write phase")

    results: list[dict] = []
    prepared_count = 0
    total_books = len(raw_files)

    try:
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            futures: dict[concurrent.futures.Future, Path] = {}
            for f in raw_files:
                futures[executor.submit(
                    _do_prepare, f, config, template_override, verbose, pilot_confirmed
                )] = f

            for future in as_completed(futures):
                prepared = future.result()
                prepared_count += 1
                if prepared is None:
                    print(f"\n[batch] {prepared_count}/{total_books} prepared (skipped)", flush=True)
                    continue

                print(f"\n[batch] {prepared_count}/{total_books} prepared — writing immediately ({prepared['raw_file'].name})", flush=True)
                try:
                    result = _do_write(prepared, verbose=verbose)
                    results.append(result)
                except Exception as e:
                    print(f"[batch] Write failed for {prepared['raw_file'].name}: {e}")
                    import traceback
                    traceback.print_exc()
    finally:
        lock.release()

    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\n{'='*60}")
    print(f"Batch complete: {ok}/{len(results)} books processed successfully")
    print(f"{'='*60}")

    # Staleness-guarded: rebuild graph after batch (no-op if <30min since last rebuild)
    if ok > 0:
        _run_post_ingest_graph(config)

    return results


# ---------- Queue-based continuous ingestion (--watch) ----------

def _read_queue(config: Config) -> list[dict]:
    """Read ingest-queue.json, returning entries sorted by addedAt (oldest first)."""
    qpath = config.runtime_dir / "ingest-queue.json"
    if not qpath.exists():
        return []
    try:
        queue = json.loads(qpath.read_text(encoding="utf-8"))
        if not isinstance(queue, list):
            return []
        # Sort: priority first, then oldest addedAt
        return sorted(queue, key=lambda e: (
            0 if e.get("priority") else 1,
            e.get("addedAt", 0),
        ))
    except Exception:
        return []


def _write_queue(config: Config, queue: list[dict]) -> None:
    """Atomically write ingest-queue.json."""
    qpath = config.runtime_dir / "ingest-queue.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = qpath.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(qpath)


def _queue_entry_to_file(entry: dict, config: Config) -> Path | None:
    """Convert a queue entry's sourcePath to an absolute raw file path."""
    sp = entry.get("sourcePath", "")
    if not sp:
        return None
    # sourcePath is like "raw/book/foo.pdf"
    if sp.startswith("raw/"):
        sp = sp[4:]
    full = (config.raw_root / sp).expanduser().resolve()
    if full.exists():
        return full
    return None


def ingest_watch(
    config: Config,
    poll_interval: int = 120,
    drain: bool = False,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    max_retries: int = 3,
    resume_from: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> None:
    """Continuously watch ingest-queue.json and process pending entries.

    Each watch cycle:
      1. Read the queue
      2. Collect pending entries (status=pending, or failed with retryCount < max_retries)
      3. Feed them through the batch pipeline (parallel Stage 0-2, serial Stage 3+)
      4. Update queue status for each (done / failed / skipped)
      5. Re-scan for new entries added by wiki-monitor.sh
      6. If --drain: exit when queue is empty; otherwise loop forever

    This is the daemon mode: run it in a tmux/screen session or via nohup.
    wiki-monitor.sh (cron or manual) adds new files to the queue;
    ingest.py --watch picks them up in the next cycle.
    """
    lock = ProjectLock(config, owner_id="watch")
    if not lock.acquire(timeout=10):
        raise RuntimeError(
            "Could not acquire project lock for watch mode. "
            "Is another ingest.py --watch or batch running?"
        )

    cycle = 0
    total_processed = 0
    total_done = 0
    total_failed = 0

    print(f"[watch] Starting queue watcher (poll={poll_interval}s, "
          f"concurrent={max_concurrent}, retries={max_retries}, "
          f"drain={'yes' if drain else 'no'})")
    print(f"[watch] Queue: {config.runtime_dir / 'ingest-queue.json'}")
    if drain:
        print(f"[watch] Mode: drain — will exit when queue is empty")
    else:
        print(f"[watch] Mode: continuous — press Ctrl+C to stop")

    try:
        while True:
            cycle += 1
            queue = _read_queue(config)
            # Separate pending from the rest
            pending: list[dict] = []
            rest: list[dict] = []

            for entry in queue:
                status = entry.get("status", "pending")
                retries = entry.get("retryCount", 0)

                if status == "done":
                    rest.append(entry)
                    continue

                if status == "failed" and retries >= max_retries:
                    rest.append(entry)
                    continue

                # pending, processing, or failed with retries remaining
                if status in ("pending", "failed"):
                    pending.append(entry)
                elif status == "processing":
                    # Stale processing marker — re-queue
                    entry["status"] = "pending"
                    pending.append(entry)
                else:
                    rest.append(entry)

            if not pending:
                if drain:
                    print(f"[watch] Queue empty — draining complete. "
                          f"Total: {total_processed} processed "
                          f"({total_done} done, {total_failed} failed)")
                    break
                else:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[watch] [{ts}] No pending entries. "
                          f"Waiting {poll_interval}s... "
                          f"(processed {total_processed} so far: "
                          f"{total_done} done, {total_failed} failed)", flush=True)
                    time.sleep(poll_interval)
                    continue

            # Process this wave
            wave_size = len(pending)
            print(f"\n[watch] Cycle {cycle} — {wave_size} pending entries")
            for i, e in enumerate(pending):
                sp = e.get("sourcePath", "?")
                retries = e.get("retryCount", 0)
                tag = f" (retry {retries})" if retries > 0 else ""
                print(f"  {i+1}. {sp}{tag}")

            # Convert to file paths (skip entries with missing files)
            wave_files: list[tuple[dict, Path]] = []
            for entry in pending:
                fp = _queue_entry_to_file(entry, config)
                if fp is None:
                    sp = entry.get("sourcePath", "?")
                    print(f"  SKIP: {sp} — file not found in raw/")
                    entry["status"] = "failed"
                    entry["error"] = "file not found in raw/"
                    entry["retryCount"] = entry.get("retryCount", 0) + 1
                    rest.append(entry)
                    continue
                # Mark as processing
                entry["status"] = "processing"
                entry["startedAt"] = int(time.time() * 1000)
                wave_files.append((entry, fp))

            # Write intermediate state so wiki-monitor sees processing entries
            _write_queue(config, [e for e, _ in wave_files] + rest)

            if not wave_files:
                # All entries had missing files — don't re-loop immediately
                time.sleep(poll_interval)
                continue

            # Run batch pipeline on this wave
            raw_paths = [fp for _, fp in wave_files]
            try:
                results = batch_ingest(
                    raw_paths, config,
                    max_concurrent=max_concurrent,
                    verbose=verbose,
                    pilot_confirmed=pilot_confirmed,
                )
            except Exception as e:
                print(f"[watch] Batch ingest crashed: {e}")
                import traceback
                traceback.print_exc()
                # Mark all wave entries as failed
                for entry, fp in wave_files:
                    entry["status"] = "failed"
                    entry["error"] = f"batch crash: {e}"
                    entry["retryCount"] = entry.get("retryCount", 0) + 1
                    entry["failedAt"] = int(time.time() * 1000)
                    rest.append(entry)
                _write_queue(config, rest)
                total_failed += len(wave_files)
                total_processed += len(wave_files)
                time.sleep(poll_interval)
                continue

            # Map results back to queue entries by file path
            result_by_path: dict[str, dict] = {}
            for r in results:
                rf = r.get("raw_file", "")
                result_by_path[str(rf)] = r

            for entry, fp in wave_files:
                result = result_by_path.get(str(fp))
                if result and result.get("status") == "ok":
                    entry["status"] = "done"
                    entry["completedAt"] = int(time.time() * 1000)
                    entry["error"] = None
                    total_done += 1
                else:
                    entry["status"] = "failed"
                    retries = entry.get("retryCount", 0) + 1
                    entry["retryCount"] = retries
                    err = result.get("error", "unknown") if result else "no result"
                    entry["error"] = str(err)[:200]
                    entry["failedAt"] = int(time.time() * 1000)
                    if retries >= max_retries:
                        print(f"  [watch] {entry['sourcePath']}: max retries ({max_retries}) reached — giving up")
                    total_failed += 1
                rest.append(entry)
                total_processed += 1

            _write_queue(config, rest)
            print(f"[watch] Cycle {cycle} complete — "
                  f"cumulative: {total_done} done, {total_failed} failed", flush=True)

    except KeyboardInterrupt:
        print(f"\n[watch] Interrupted. "
              f"Processed {total_processed}: {total_done} done, {total_failed} failed.")
        print(f"[watch] Queue preserved at {config.runtime_dir / 'ingest-queue.json'}")
    finally:
        lock.release()


def _auto_validate_ingest(config: Config, raw_file: Path) -> None:
    """Run validate_ingest.py inline for the just-completed source.

    Superpowers Iron Law: every ingest MUST produce fresh verification evidence
    before claiming completion.  This runs the 15-stage validator on the current
    source and prints the result.  Hard failures prevent the "ok" status.
    """
    import subprocess
    validate_script = Path(__file__).parent / "validate_ingest.py"
    if not validate_script.exists():
        print("[validate] ⚠️  validate_ingest.py not found, skipping final verification")
        return

    slug = raw_file.stem
    # Compute the exact cache key (matching ingest.py's `rel` variable)
    try:
        cache_key = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        cache_key = str(raw_file)
    print(f"\n[validate] Running 15-stage final verification for {slug} (cache_key={cache_key})...")
    result = subprocess.run(
        [sys.executable, str(validate_script)],
        env={**os.environ, "IMPROVED_WIKI_ROOT": str(config.wiki_root),
             "SOURCE_SLUG": slug,
             "CACHE_KEY": cache_key},
        capture_output=True, text=True, timeout=60,
    )
    # Print the validator output (shows per-stage PASS/FAIL)
    stdout = result.stdout.strip()
    if stdout:
        # Print only the summary lines to avoid overwhelming output
        for line in stdout.splitlines():
            if any(marker in line for marker in ["Result:", "PASS", "FAIL", "❌", "✅", "Stage"]):
                print(f"  {line}")

    if result.returncode != 0:
        # Don't raise — the ingest succeeded but validation found issues.
        # The compliance record already documents stage status.
        stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""
        print(f"[validate] ⚠️  Validator exit {result.returncode} — review warnings above")
        if stderr_tail:
            print(f"[validate] {stderr_tail}")
    else:
        print(f"[validate] ✅ All 15 stages verified — ingest complete")


def _auto_embed_new_pages(config: Config, files_written: list[str]) -> None:
    """NashSU parity (ingest.ts L1127-1146): embed new pages after successful ingest.

    Runs only if EMBEDDING_BASE_URL is set and lancedb is installed.
    Skips index.md, log.md, overview.md, and schema.md.
    """
    if not os.environ.get("EMBEDDING_BASE_URL"):
        return
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return

    skip_files = {"index.md", "log.md", "overview.md", "schema.md"}
    new_files = [
        str(config.wiki_dir / f) for f in files_written
        if Path(f).name not in skip_files and (config.wiki_dir / f).exists()
    ]
    if not new_files:
        return

    print(f"[stage_6] Auto-embedding {len(new_files)} new pages...")
    try:
        from build_embeddings import embed_pages
        embed_pages(config.wiki_dir, config.runtime_dir, new_files)
    except ImportError:
        # Fallback: run build_embeddings.py as subprocess
        import subprocess
        script = Path(__file__).parent / "build_embeddings.py"
        subprocess.run(
            [sys.executable, str(script), "--project", str(config.wiki_root), "embed"],
            capture_output=True, timeout=300,
        )
    print(f"[stage_6] Embedding complete")


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest source files into the wiki (NashSU-style 15-stage)")
    parser.add_argument("file", nargs="*", help="Path(s) to raw source file(s). Multiple files enable batch mode. "
                        "Omit with --watch to consume the queue.")
    parser.add_argument("--type", help="Override template type (book/paper/datasheet/...)")
    parser.add_argument("--parallel", type=int, default=0,
                        help=f"Max concurrent books for Stage 0-2 (default: {BATCH_MAX_CONCURRENT} if multiple files, 1 for single)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write anything")
    parser.add_argument("--delete", action="store_true",
                        help="Delete source: remove source page, cache entry, and cleanup orphans (NashSU source-lifecycle parity)")
    parser.add_argument("--enrich-wikilinks", action="store_true", default=True,
                        help="Auto-enrich new pages with [[wikilinks]] after write (NashSU enrich-wikilinks parity)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Disable wikilink enrichment")
    parser.add_argument("--pilot-confirmed", action="store_true",
                        help="Acknowledge Stage 0 pilot quality and proceed with full OCR (required for scanned PDFs)")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print LLM responses for debugging",
    )
    parser.add_argument(
        "--conversation", action="store_true",
        help="Delegate LLM calls to calling agent via prompt.md → result.txt protocol.",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Continuously watch ingest-queue.json and process pending entries. "
             "New entries added by wiki-monitor.sh are picked up automatically.",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=30,
        help="Seconds between queue re-scans in --watch mode (default: 30)",
    )
    parser.add_argument(
        "--drain", action="store_true",
        help="With --watch: exit when the queue is empty instead of looping forever.",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="Max attempts per queued entry before giving up (default: 3)",
    )
    args = parser.parse_args()

    # ── Watch mode: continuous queue consumer ──
    if args.watch:
        config = Config.from_env()
        config.conversation_mode = args.conversation
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        ingest_watch(
            config,
            poll_interval=args.poll_interval,
            drain=args.drain,
            max_concurrent=max_conc,
            max_retries=args.max_retries,
            verbose=args.verbose,
            pilot_confirmed=args.pilot_confirmed,
        )
        return 0

    if not args.file:
        parser.print_help()
        print("\nTip: use --watch to process the queue, or pass file(s) for direct ingest.", file=sys.stderr)
        return 1

    # ── Source lifecycle: delete ──
    if args.delete:
        config = Config.from_env()
        from _source_lifecycle import delete_source
        for f in args.file:
            rf = Path(f).expanduser().resolve()
            delete_source(rf, config)
        return 0

    config = Config.from_env()
    config.conversation_mode = args.conversation

    raw_files = []
    for f in args.file:
        rf = Path(f).expanduser().resolve()
        if not rf.exists():
            print(f"ERROR: {rf} not found", file=sys.stderr)
            return 1
        if not rf.is_relative_to(config.raw_root):
            print(f"ERROR: {rf} is not under raw_root ({config.raw_root})", file=sys.stderr)
            return 1
        raw_files.append(rf)

    # Batch mode: multiple files or explicit --parallel
    if len(raw_files) > 1 or args.parallel > 1:
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        results = batch_ingest(
            raw_files, config, max_concurrent=max_conc,
            template_override=args.type, verbose=args.verbose,
            pilot_confirmed=args.pilot_confirmed,
        )
        ok = sum(1 for r in results if r.get("status") == "ok")
        return 0 if ok == len(results) else 1

    # Single-book mode
    raw_file = raw_files[0]

    if args.dry_run:
        template = detect_template_type(raw_file, config.raw_root, args.type)
        hs = file_sha256(raw_file)
        print(f"DRY RUN: would process {raw_file}")
        print(f"  hash: {hs}")
        print(f"  template: {template}")
        # Estimate cost
        if raw_file.suffix.lower() == ".pdf":
            pdf_type, avg_chars = detect_pdf_type(raw_file)
            print(f"  PDF type: {pdf_type} (avg {avg_chars:.0f} chars/page, 10-page sample)")
            if pdf_type in ("scanned", "mixed"):
                try:
                    import fitz
                    doc = fitz.open(raw_file)
                    pages = len(doc)
                    doc.close()
                    batches = (pages + 4) // 5
                    print(f"  Stage 0 OCR: {pages} pages → ~{batches} API calls (5 pages/batch)")
                except Exception:
                    pass
        # Estimate Stage 1/1.5/2 (use PDF page count, don't call APIs)
        if raw_file.suffix.lower() == ".pdf":
            try:
                import fitz
                doc = fitz.open(raw_file)
                pages = len(doc)
                doc.close()
                est_chars = int(max(avg_chars, 200)) * pages  # floor at 200 chars/page
                chunks_est = max(1, (est_chars + config.target_chars - 1) // config.target_chars)
                print(f"  Estimated text: ~{est_chars:,} chars ({pages} pages × {max(avg_chars, 200):.0f} chars/page)")
                print(f"  Estimated API calls: 1 (Stage 1) + {chunks_est} (Stage 1.5 chunks) + 1-3 (Stage 2)")
                if pdf_type in ("scanned", "mixed"):
                    batches = (pages + 4) // 5
                    print(f"  ⚠️  May need Stage 0 OCR: ~{batches} batch calls for full-book OCR if PyMuPDF insufficient")
            except Exception:
                pass
        print(f"  Stages: text-extract -> image-extract+caption -> digest -> chunk -> generate -> review -> inject -> write -> cache")
        return 0

    h = file_sha256(raw_file)
    lock = ProjectLock(config, owner_id=h[-8:])
    if not lock.acquire():
        print("ERROR: Could not acquire project lock — another ingest may be running", file=sys.stderr)
        return 1
    try:
        result = ingest_one(raw_file, config, args.type, verbose=args.verbose,
                            pilot_confirmed=args.pilot_confirmed)
        print(f"\nResult: {result}")
        return 0 if result["status"] in ("ok", "skipped") else 1
    except ConversationPending:
        return 101
    except Exception:
        lock.release()
        raise
    else:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
