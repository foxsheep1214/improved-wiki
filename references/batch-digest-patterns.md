# Batch Digest Patterns and Pitfalls

## ❌ `claude -p` cannot run 17-stage ingest pipeline

Using `claude -p "digest this book"` for batch wiki ingestion fails (exit=1) for every book. The `-p` (print/non-interactive) mode:
- Cannot handle the multi-step `ingest.py` conversation-mode handoff (exit 101 prompt-file pattern)
- Has no access to `--allowedTools` that match the pipeline's needs
- Times out before OCR completes on scanned PDFs

**✅ Correct batch pattern**: Call `ingest.py` directly via Python, not through `claude -p`:

```python
import subprocess, os
os.environ["MINIMAX_CN_API_KEY"] = "..."  # for caption API
result = subprocess.run(
    ["python3", str(INGEST_PY), str(pdf_path)],
    capture_output=True, text=True, timeout=3600,
    cwd=str(project_root),
    env={**os.environ, "IMPROVED_WIKI_ROOT": str(project_root)}
)
```

See `/tmp/hw_batch_v4.py` for a working batch loop script.

## Common ingest.py failure modes

| Failure | Exit code | Cause | Fix |
|---------|-----------|-------|-----|
| Stage 2 verification | 1 | LLM didn't emit `wiki/sources/<title>.md` FILE block | Retry; check LLM model supports the prompt format |
| minerU OCR timeout | -15 (SIGTERM) | Scanned PDF too large, OCR > 3600s | Increase timeout or skip large scanned books |
| Stale lock | 1 (recovered) | Previous ingest crashed, `.ingest-progress/` lock file remains | `ingest.py` auto-recovers: "Stale lock from pid=XXX — taking over" |
| minerU hybrid OCR routing | 0 (normal) | 文本层薄/图表密集的 PDF | hybrid-engine `parse_method=auto` 按页自动判 txt vs VLM OCR，所有 PDF 统一走 minerU |

## Source page dedup check

`ingest.py` is idempotent: if `wiki/sources/<stem>.md` exists, it skips the file.
The batch script should also pre-check to avoid spawning processes for already-digested books:

```python
source_page = WIKI_SRC / f"{pdf.stem}.md"
if source_page.exists():
    continue  # already digested
```

**Note**: Source pages may be in subdirectories (`wiki/sources/book/`, `wiki/sources/datasheet/`) matching the `raw/` layout. Check all subdirs.

## Concurrency

- `ingest.py` uses a file lock (`.ingest-progress/<hash>.lock`) per project
- Multiple `ingest.py` processes on the same project serialize automatically
- minerU OCR is strictly serialized system-wide by a cross-process file lock (`fcntl.flock` on `~/.cache/improved-wiki/.mineru.lock`), not a process counter
- Batch: only the wiki-independent PREFETCH (Phase 0/1 + Stage 2.1/2.2) runs across books in parallel; the wiki-dependent spine (Stage 2.3→write) runs one book at a time so each book's 2.3 dedup/linking sees prior books' pages
- LLM calls within a single book are serial (conversation mode — one prompt at a time)
