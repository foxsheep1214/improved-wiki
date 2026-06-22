# Batch Digest

When you have dozens of pending books and want to process them all without
manual intervention per book.

## Why not Claude Code?

- Claude `-p` (print) mode is **single-turn** — it processes ONE request and exits.
  It cannot run a multi-book batch in a single invocation.
- Claude Code interactive mode works but requires a visible Terminal window.
  When the display is off/locked, you can't inject text into a running Terminal
  session (see `macos-app-automation` skill pitfall #27).
- `ingest.py` hands each LLM step to the calling agent (current
  model); for unattended batches you run an agent loop that answers each
  conversation prompt (see `references/delegate-mode.md`). No external LLM API
  key is needed for text generation — only image captioning calls MiniMax.

## Recommended: Built-in Batch Modes

`ingest.py` has two built-in batch modes. Prefer these over the legacy
subprocess loop (section below).

### Mode 1: `--watch --drain` (Queue-Driven, Fire-and-Forget)

Best for production / unattended runs. An external script (e.g. `wiki-monitor.sh`)
drops entries into `ingest-queue.json`. `ingest.py` watches the queue, processes
each entry, and exits when the queue is empty.

```bash
# Start the watcher — picks up entries from ingest-queue.json, exits when empty
nohup python3 ~/.agents/skills/improved-wiki/scripts/ingest.py \
  --watch --drain \
  --parallel 4 \
  > /tmp/ingest_watch.log 2>&1 &

# Monitor
tail -f /tmp/ingest_watch.log
```

Key options:
- `--watch` — continuously re-scans `ingest-queue.json` (every 30s by default)
- `--drain` — exit when the queue is empty (omit to loop forever)
- `--parallel N` — max concurrent books for Stage 1.1-2 (default: 4 in batch)
- `--poll-interval SECS` — override the 30s queue re-scan interval
- `--max-retries N` — max attempts per queued entry before giving up (default: 3)

### Mode 2: `--parallel N` (Multi-File, One-Shot)

Best for ad-hoc batches when you know the file list upfront. Pass multiple
PDF paths and a parallelism limit directly on the command line.

```bash
python3 ~/.agents/skills/improved-wiki/scripts/ingest.py \
  ~/Documents/知识库/HardwareWiki/raw/Book/*.pdf \
  --parallel 4
```

This processes all matching PDFs concurrently (up to `--parallel` at once)
and exits when done. Dedup is automatic — `ingest.py` skips books that
already have a source page in `wiki/sources/`.

## Legacy: Subprocess Loop (Advanced / Custom Logic)

If you need custom dedup logic, per-book logging, or integration with an
external scheduler that the built-in modes don't cover, you can still use the
subprocess loop below. **Use with caution** — you lose built-in concurrency
limiting and the watcher's automatic retry/queue management.

```python
#!/usr/bin/env python3
"""Legacy batch digest — serial subprocess loop. Prefer --watch --drain instead."""
import os, subprocess, time
from pathlib import Path

RAW_DIR = Path.home() / "Documents/知识库/HardwareWiki/raw/book"
WIKI_SRC = Path.home() / "Documents/知识库/HardwareWiki/wiki/sources"
INGEST = Path.home() / ".agents/skills/improved-wiki/scripts/ingest.py"
PROJECT_ROOT = RAW_DIR.parent.parent

os.environ["IMPROVED_WIKI_ROOT"] = str(PROJECT_ROOT)
# Text generation runs in conversation mode (calling agent's model) — no LLM
# API key needed. MINIMAX_CN_API_KEY is only for image captioning, if used.
os.environ["MINIMAX_CN_API_KEY"] = os.environ.get("MINIMAX_CN_API_KEY", "")

# Collect pending: books with PDF in raw/ but no source page in wiki/
pdfs = []
for pdf in sorted(RAW_DIR.rglob("*.pdf")):
    if not (WIKI_SRC / f"{pdf.stem}.md").exists():
        pdfs.append(pdf)

total = len(pdfs)
print(f"Pending: {total} books")

success = failed = 0
for i, pdf in enumerate(pdfs, 1):
    print(f"[{i}/{total}] {pdf.stem}", flush=True)
    try:
        r = subprocess.run(
            ["python3", str(INGEST), str(pdf)],
            capture_output=True, text=True, timeout=3600,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "IMPROVED_WIKI_ROOT": str(PROJECT_ROOT)}
        )
        if r.returncode == 0:
            success += 1
        else:
            failed += 1
    except subprocess.TimeoutExpired:
        failed += 1
    except Exception as e:
        failed += 1

print(f"DONE: {success} OK, {failed} failed, {total} total")
```

## Key Points (All Modes)

- **Dedup via source page**: `ingest.py` checks `wiki/sources/<pdf-stem>.md` before
  processing. This is the authoritative "already ingested" signal (per the
  improved-wiki skill). Do NOT use `ingest-cache.json` or memory.
- **Serial only for minerU-intensive stages**: minerU has built-in concurrency
  limiting (max 2 instances). Running too many books in parallel will SIGABRT on
  16GB Macs. `--parallel 4` is a safe default — it batches Stage 1.1-2 but keeps
  minerU instances bounded.
- **Timeout**: 3600s per book (1 hour). Most books complete in 10-30 minutes.
- **LLM model**: Text generation runs in conversation mode — the calling agent
  answers each LLM step with the current model. No `LLM_API_KEY` is needed for
  text gen. `MINIMAX_CN_API_KEY` is only required for image captioning (Stage 1.3).
- **Project root**: `IMPROVED_WIKI_ROOT` must point to the project root
  (e.g., `~/Documents/知识库/HardwareWiki`), not `raw/Book/`.

## Running

```bash
# Recommended: queue-driven
nohup python3 ~/.agents/skills/improved-wiki/scripts/ingest.py \
  --watch --drain --parallel 4 \
  > /tmp/ingest_watch.log 2>&1 &

# Legacy: subprocess loop
nohup python3 /tmp/hw_batch.py > /tmp/hw_batch.log 2>&1 &

# Monitor either mode
tail -f /tmp/ingest_watch.log
```

## When Display is Off

All modes run entirely headless — no GUI, no Terminal window needed.
The LLM calls go through the calling agent (conversation mode). This is the right tool when:
- Display is off/locked (cua-driver returns 0x0 captures)
- Batch is large (10+ books, multiple hours)
- You want fire-and-forget with a log to check later
