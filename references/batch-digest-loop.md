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

### Mode 1: `--watch --drain` (Queue-Driven)

Best for a persistent queue coordinator. An external script (e.g. `wiki-monitor.sh`)
drops entries into `ingest-queue.json`. `ingest.py` watches the queue, processes
each entry, and exits when the queue is empty. Conversation-mode exit 101
handoffs still require the calling agent; this is not a standalone text-LLM daemon.

```bash
# Start the watcher — picks up entries from ingest-queue.json, exits when empty
mkdir -p /tmp/codex-work/improved-wiki-batch
nohup python3 "$SKILL_DIR/scripts/ingest.py" \
  --watch --drain \
  --parallel 4 \
  > /tmp/codex-work/improved-wiki-batch/ingest_watch.log 2>&1 &

# Monitor
tail -f /tmp/codex-work/improved-wiki-batch/ingest_watch.log
```

Key options:

- `--watch` — continuously re-scans `ingest-queue.json` (every 30s by default)
- `--drain` — exit when the queue is empty (omit to loop forever)
- `--parallel N` — batch concurrency ceiling. `N=1` uses one Phase-1 worker;
  `N>=2` enables the two-stage OCR/caption prefetch. Stage 2.3→write remains one
  book at a time. The same value is enforced as the Stage 2.4 prompt-wave ceiling
  (`10 chunks, N=4 → 4+4+2`); Stage 2.2 remains serial.
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

This runs up to two ordered detached Phase-1 workers: one book may use minerU
while another uses the cross-process-limited caption slot. The main conversation
advances Stage 2.2 and the Stage 2.3+ spine one book at a time. Dedup is
automatic and uses the authoritative `ingested` marker.

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
  minerU instance, regardless of `--parallel`. Caption rounds also have a
  cross-process per-user flock; the round itself still uses four image-call threads.
- **Per-project write lock**: `.llm-wiki/ingest.lock` is held only for the current
  book's active Stage 2.3+ invocation. OCR, caption, Stage 2.2 and watch idle time do
  not hold it. `.llm-wiki/spine-reservation.json` retains the same source as logical
  owner across exit-101 handoffs, when the kernel lock necessarily drops.
- **Batch parallelism rule**: automatic cross-book concurrency covers Phase 1.
  Ordinary batch Stage 2.2 remains current-book serial because its chunks use a
  rolling digest. Stage 2.4 is the parallel exception: independent chunk prompts
  are emitted in bounded waves. Explicit next-book 2.2 prefetch is an advanced
  separate flow.
  See `references/batch-parallel-prefetch.md`.
- **Timeouts**: worker health is heartbeat-driven (60s stale window by default);
  there is no fixed two-hour worker deadline. A hard wall limit is optional via
  `IMPROVED_WIKI_BG_EXTRACT_MAX_SECONDS`. minerU lock acquisition still times out
  after 3600s; each chunk HTTP request times out at 1200s with retries.
- **LLM model**: Text generation runs in conversation mode — the calling agent
  answers each LLM step with the current model. No `LLM_API_KEY` is needed for
  text gen. Image captioning (Stage 1.3) needs a `caption_provider` configured
  in `~/.agents/config.json` — no env-var alternative.
- **Project root**: `IMPROVED_WIKI_ROOT` must point to the project root
  (e.g., `~/Documents/知识库/HardwareWiki`), not `raw/Book/`.

## Running

```bash
# Recommended: queue-driven
mkdir -p /tmp/codex-work/improved-wiki-batch
nohup python3 "$SKILL_DIR/scripts/ingest.py" \
  --watch --drain --parallel 4 \
  > /tmp/codex-work/improved-wiki-batch/ingest_watch.log 2>&1 &

# Monitor
tail -f /tmp/codex-work/improved-wiki-batch/ingest_watch.log
```

Pause/resume a direct batch from the project root:

```bash
python3 "$SKILL_DIR/scripts/ingest.py" --pause-batch
python3 "$SKILL_DIR/scripts/ingest.py" --pause-prefetch   # OCR/caption only
python3 "$SKILL_DIR/scripts/ingest.py" --batch-status
python3 "$SKILL_DIR/scripts/ingest.py" --resume-prefetch
python3 "$SKILL_DIR/scripts/ingest.py" --resume-batch \
  "raw/Book/A.pdf" "raw/Book/B.pdf"
```

Resume requires the complete, previously confirmed file list. Multi-file
`--stop-after-stage` is rejected before OCR to prevent accidental OCR-only batches.

## Common ingest.py failure modes

| Failure | Exit code | Cause | Fix |
|---------|-----------|-------|-----|
| Stage 2 verification | 1 | LLM didn't emit `wiki/sources/<title>.md` FILE block | Retry; check LLM model supports the prompt format |
| minerU timeout | 1 | mineru lock wait > 3600s (another book's OCR holding the lock), or a chunk exhausted its 3×1200s HTTP retries | Re-run — completed chunks are cached (`scanned-pdf-ocr-pipeline.md`); don't kill the running OCR |
| Project write lock busy | 1 | Another Stage 2.3+ spine currently holds `.llm-wiki/ingest.lock` | Wait for that writer; do not delete the advisory lock file |
| Full project ingest paused | 75 | `.llm-wiki/batch.pause` exists (also blocks a stale driver that resumes books one-by-one) | Re-run the confirmed full list with `--resume-batch` |
| OCR/caption prefetch paused | 76 | `.llm-wiki/batch-prefetch.pause` exists and the next book still needs Phase 1 | Keep it paused, or clear with `--resume-prefetch` |
| Logical spine reserved | 77 | Another source owns `spine-reservation.json` across a handoff/failure | Use `--batch-status`; resume the owner. Abandon only after checking partial writes |
| Coordinator busy | 78 | Another live batch/watch invocation owns `batch-coordinator.lock` | Do not start a duplicate scheduler; wait for the active invocation to yield |
| Background worker stalled | foreground cache recovery | PID died, terminal failure, or heartbeat expired | Inspect the recorded `log_file`; rerun resumes completed OCR/caption artifacts |
| minerU hybrid OCR routing | 0 (normal) | 文本层薄/图表密集的 PDF | hybrid-engine `parse_method=auto` 按页自动判 txt vs VLM OCR，所有 PDF 统一走 minerU |

## When Display is Off

All modes run entirely headless — no GUI, no Terminal window needed.
The LLM calls go through the calling agent (conversation mode). This is the right tool when:
- Display is off/locked (cua-driver returns 0x0 captures)
- Batch is large (10+ books, multiple hours)
- You want fire-and-forget with a log to check later
