"""Stage 1.1 scanned-PDF / minerU API OCR pipeline.

Extracted from _stage_1_extract.py on 2026-06-24. Owns the persistent local
minerU API server, per-chunk /file_parse submission, crash-recovery stats,
figure harvesting orchestration, and the structured JSONL event log. Despite
the legacy "_scanned" name this is the shared extraction path for text /
scanned / mixed PDFs (hybrid-engine/auto routes per-page internally).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Shared infrastructure
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import Config  # noqa: E402

from _stage_1_2_images import (  # noqa: E402
    _stage_1_2_harvest_images,
    _stage_1_2_write_manifest,
    _stage_1_2_find_uncaptioned_images,
)
from _paths import media_slug  # noqa: E402
from _stage_1_3_caption import (  # noqa: E402
    _stage_1_3_caption_images_batch,
    _caption_no_key_pause,
    CAPTION_BATCH_SIZE,
)

# ══════════════════════════════════════════════════════════════════════════════
# Constants & Concurrency Control
# ══════════════════════════════════════════════════════════════════════════════

MINERU_MAX_CONCURRENT = 1
MINERU_API_PORT = int(os.environ.get("MINERU_API_PORT", "19999"))
MINERU_LOCK_FILE = Path.home() / ".cache" / "improved-wiki" / ".mineru.lock"
MINERU_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

MINERU_CHUNK_SIZE = 50  # pages per minerU invocation


# ══════════════════════════════════════════════════════════════════════════════
# minerU LaTeX cleanup
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# minerU file lock
# ══════════════════════════════════════════════════════════════════════════════

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
    # Late import: _PARSE_METHOD_OVERRIDE lives in the facade (_stage_1_extract)
    # and is set by stage_1_1_extract_text before calling down into this path.
    # Late import avoids a load-time circular dependency (facade imports this
    # module at top level).
    from _stage_1_extract import _PARSE_METHOD_OVERRIDE

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
    _media_slug = media_slug(file_path, config)
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

    slug = media_slug(file_path, config)
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
                pending, config, media_dir, source_label="mineru-extracted",
                batch_size=CAPTION_BATCH_SIZE)
        elif pending and not config.caption_api_key:
            # No caption API key — VLM main path cannot run. Pause (no fallback).
            already = len(extracted_figures) - len(pending)
            _caption_no_key_pause(config, "mineru-extracted", media_dir,
                                  len(extracted_figures), already)
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
        # BUGFIX 2026-06-24: was [end for _, end in chunks] — only chunk-end pages
        # (16 of 794), so cache-resume fed Stage 2.1 ~1% of the text. Assemble ALL pages.
        return _stage_1_1_assemble_ocr_text(out_dir, list(range(total_pages)))

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
