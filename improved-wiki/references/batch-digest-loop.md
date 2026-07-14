# Batch Digest

When you have dozens of pending books and want to process them all without
manual intervention per book.

## Why not one-shot headless agents?

`ingest.py` hands each LLM step to the calling agent (current model); for
unattended batches you run an agent loop that answers each conversation prompt
(see `references/delegate-mode.md`). Batch via a one-shot, prompt-only agent is not supported —
the pipeline requires conversation-mode handoffs (exit 101 prompt-file pattern).
No external LLM API key is needed for text generation — only image captioning
calls a configured VLM provider.

## Recommended: Built-in Batch Modes

`ingest.py` has two built-in batch modes.

### Mode 1: `--watch --drain` (Queue-Driven, Fire-and-Forget)

Best for production / unattended runs. An external script (e.g. `wiki-monitor.sh`)
drops entries into `ingest-queue.json`. `ingest.py` watches the queue, processes
each entry, and exits when the queue is empty.

```bash
# Start the watcher — picks up entries from ingest-queue.json, exits when empty
nohup python3 "$SKILL_DIR/scripts/ingest.py" \
  --watch --drain \
  --parallel 4 \
  > /tmp/ingest_watch.log 2>&1 &

# Monitor
tail -f /tmp/ingest_watch.log
```

Key options:
- `--watch` — continuously re-scans `ingest-queue.json` (every 30s by default)
- `--drain` — exit when the queue is empty (omit to loop forever)
- `--parallel N` — max concurrent books for the wiki-independent PREFETCH only (Phase 0/1 + Stage 2.2; default: 4). The wiki-dependent spine (Stage 2.3→write) always runs one book at a time regardless of N.
- `--poll-interval SECS` — override the 30s queue re-scan interval
- `--max-retries N` — max attempts per queued entry before giving up (default: 3)

### Mode 2: `--parallel N` (Multi-File, One-Shot)

Best for ad-hoc batches when you know the file list upfront. Pass multiple
PDF paths and a parallelism limit directly on the command line.

```bash
python3 "$SKILL_DIR/scripts/ingest.py" \
  ~/Documents/知识库/HardwareWiki/raw/Book/*.pdf \
  --parallel 4
```

This prefetches the wiki-independent stages of all matching PDFs concurrently
(up to `--parallel` at once), then writes them one at a time, and exits when
done. Dedup is automatic — `ingest.py` skips books that
already have a source page in `wiki/sources/`.

## Key Points (All Modes)

- **Dedup signals**: before selecting files for a batch, the agent-side pre-check is
  source-page existence (`wiki/sources/<raw-rel-path>.md`). The code's Stage 0.2
  adjudication then uses the `ingested` completion marker as the primary signal, with
  source-page existence as the auxiliary check (see `ingest-stages-mandatory.md`
  Stage 0.2). Do NOT use `ingest-cache.json` or memory. Note: source pages may live
  in subdirectories (`wiki/sources/Book/`, `wiki/sources/Datasheet/…`) mirroring the
  `raw/` layout — check all subdirs.
- **minerU is strictly serialized system-wide** — a cross-process file lock
  (`fcntl.flock` on `~/.cache/improved-wiki/.mineru.lock`) allows at most ONE
  minerU instance, regardless of `--parallel` (2026-06-23; replaced the old
  process-counter approach). `--parallel 4` is safe — it batches the
  wiki-independent prefetch while minerU work still runs one book at a time.
- **Per-project lock**: `ingest.py` uses a file lock (`.ingest-progress/<hash>.lock`);
  multiple processes on the same project serialize automatically, and a stale
  lock from a crashed run is auto-recovered ("Stale lock from pid=XXX — taking over").
- **Batch parallelism rule**: only the wiki-independent PREFETCH (Phase 0/1 +
  Stage 2.2) runs across books in parallel; the wiki-dependent spine
  (Stage 2.3→write) runs one book at a time. Concurrency caps and the in-book
  Stage 2.4 parallel exception (2026-07-09): `references/batch-parallel-prefetch.md`
  (authoritative).
- **Timeouts**: there is no per-book timeout. 3600s is the minerU **lock-acquisition**
  timeout (`fcntl.flock`, waiting behind another book's OCR); each chunk's HTTP request
  times out at 1200s with 3 retries (see `references/scanned-pdf-ocr-pipeline.md`).
  Most books complete in 10-30 minutes.
- **LLM model**: Text generation runs in conversation mode — the calling agent
  answers each LLM step with the current model. No `LLM_API_KEY` is needed for
  text gen. Image captioning (Stage 1.3) needs a `caption_provider` configured
  in `~/.agents/config.json` — no env-var alternative.
- **Project root**: `IMPROVED_WIKI_ROOT` must point to the project root
  (e.g., `~/Documents/知识库/HardwareWiki`), not `raw/Book/`.

## Running

```bash
# Recommended: queue-driven
nohup python3 "$SKILL_DIR/scripts/ingest.py" \
  --watch --drain --parallel 4 \
  > /tmp/ingest_watch.log 2>&1 &

# Monitor
tail -f /tmp/ingest_watch.log
```

## Common ingest.py failure modes

| Failure | Exit code | Cause | Fix |
|---------|-----------|-------|-----|
| Stage 2 verification | 1 | LLM didn't emit `wiki/sources/<title>.md` FILE block | Retry; check LLM model supports the prompt format |
| minerU timeout | 1 | mineru lock wait > 3600s (another book's OCR holding the lock), or a chunk exhausted its 3×1200s HTTP retries | Re-run — completed chunks are cached (`scanned-pdf-ocr-pipeline.md`); don't kill the running OCR |
| Stale lock | 1 (recovered) | Previous ingest crashed, `.ingest-progress/` lock file remains | `ingest.py` auto-recovers: "Stale lock from pid=XXX — taking over" |
| minerU hybrid OCR routing | 0 (normal) | 文本层薄/图表密集的 PDF | hybrid-engine `parse_method=auto` 按页自动判 txt vs VLM OCR，所有 PDF 统一走 minerU |

## When Display is Off

All modes run entirely headless — no GUI, no Terminal window needed.
The LLM calls go through the calling agent (conversation mode). This is the right tool when:
- Display is off/locked (cua-driver returns 0x0 captures)
- Batch is large (10+ books, multiple hours)
- You want fire-and-forget with a log to check later
