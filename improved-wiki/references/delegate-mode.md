# Delegate Mode — Agent Orchestration

Invoking improved-wiki from an agent (Claude Code, Hermes, etc.) always uses **conversation mode** — there is no flag, and no direct-API alternative. Every text-generation LLM step (including wikilink enrichment, batched once per ingest) is handled by the calling agent with the current conversation's model. Two external dependencies outside text generation: **image captioning** (Stage 1.3, MiniMax VLM — vision content can't flow through the prompt-file handoff) and **embeddings** (Stage 3.7, local Ollama bge-m3). Both are **no-fallback**: if the caption key is missing/batch fails or the Ollama stack is down, `ingest.py` raises `RuntimeError` and pauses — the calling agent must surface this to the user rather than silently continuing. Extraction/page writes are cached, so re-running after the user fixes the dependency resumes from the failed stage.

---

## Conversation Mode

| Who calls LLM? | API key needed? |
|----------------|-----------------|
| Calling agent, via prompt files (current model) | No for text gen (agent uses its own model). MiniMax key only for image captioning. |

---

## Conversation Mode Workflow

### Step 1: Start the ingest

```bash
cd /path/to/wiki/project
scripts/ingest.py raw/Book/Book.pdf
```

At each LLM call point, `ingest.py` writes a prompt file and raises `ConversationPending` (exit code `101`).

### Step 2: Agent reads prompt and generates response

Prompt files are written to:
```
<llm-wiki>/conversation/<sha256_prefix>/<stage-slug>.md
```

The agent reads the `.md` file, executes the LLM task, and writes the result to:
```
<llm-wiki>/conversation/<sha256_prefix>/<stage-slug>.txt
```

### Step 3: Re-invoke to continue

```bash
scripts/ingest.py raw/Book/Book.pdf
```

`ingest.py` finds the result file, reads it, continues to the next stage, and repeats until completion.

### Task manifest

Pipelines with multiple LLM calls (chunk analysis, per-chunk generation) use a `tasks.json` manifest in the conversation directory to track pending/completed tasks.

---

## Agent Integration Pattern

```python
def ingest_via_conversation(pdf_path, project_path):
    while True:
        proc = subprocess.run(
            ["scripts/ingest.py", pdf_path],
            cwd=project_path,
            env={**os.environ, "IMPROVED_WIKI_ROOT": project_path},
        )

        if proc.returncode == 0:
            return  # Done

        if proc.returncode == 101:
            # Read the pending prompt
            conv_dir = find_conversation_dir(project_path)
            prompt_file = find_pending_prompt(conv_dir)
            prompt = prompt_file.read_text()

            # Execute with agent's own LLM
            result = call_llm(prompt)

            # Write result back
            result_file = prompt_file.with_suffix(".txt")
            result_file.write_text(result)
            continue

        raise RuntimeError(f"Ingest failed: {proc.returncode}")
```

---

## Implementation Notes

- `conversation_prefix` = last 8 hex chars of the raw file's SHA-256 hash (per-source isolation)
- Multiple simultaneous ingests are safe — each has a unique conversation directory
- Task files use simple markdown (no JSON serialization needed)
- `ConversationPending` exception is defined in `_core.py`

## Operational pitfalls

### Must use venv Python — system Python 3.9 will crash

`ingest.py` uses PEP 604 union syntax (`str | None`). macOS system `/usr/bin/python3`
is 3.9 and will throw `TypeError: unsupported operand type(s) for |`. **Always** invoke
with `~/.venv/bin/python3`:

```bash
IMPROVED_WIKI_ROOT="$(pwd)" ~/.venv/bin/python3 ~/.agents/skills/improved-wiki/scripts/ingest.py "raw/Book/Book.pdf"
```

This is also documented as Pitfall 4 in `references/scripting-pitfalls.md` but remains
the #1 first-run failure.

### minerU OCR can take 10+ minutes — use `--stop-after-stage 0`

A 272-page book takes ~10 min for minerU OCR (6 chunks × ~100s/chunk). This will
exceed foreground terminal timeouts. Split the run:

```bash
# Phase 1: OCR only (may timeout, re-run resumes from cache)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/Book.pdf" --stop-after-stage 0

# Phase 2: LLM stages (conversation mode, multiple exit-101 cycles)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/Book.pdf"
```

`--stop-after-stage 0` halts **cleanly (exit 0, `{"status":"ok","stopped_after":"0"}`)**
after Stage 1.1–1.3 (text + image + caption) complete and **before** Stage 2.1 —
the 2.1 global digest is not even submitted. Re-running without the flag resumes
from the cached extraction (stage_1_x_done markers). If the OCR phase times out
mid-chunk, re-running the same command resumes from the last completed chunk
(minerU caches per-chunk results in `.llm-wiki/extract-tmp/`).

> **Behavior note (2026-06-25 fix):** previously `--stop-after-stage 0` was
> effectively dead on a fresh run — the stop check sat *after* `_do_prepare`,
> which runs all of Stage 0-2 (pausing at the 2.1/2.2/2.4 LLM handoffs) before
> that check, so the process entered 2.1 and exited 101 instead of halting after
> OCR. The check now raises `PrepareStopAfter` at the in-prepare boundary. The
> same fix makes `1` (after global digest) and `2` (after generation) halt
> cleanly. `1.5`/`2.3` (inside the chunk pipeline, no clean resume marker) remain
> best-effort — they are not intercepted on a fresh run.

### Wikilink enrichment generates many merge tasks

After Stage 3.1 (write files), the pipeline enters wikilink enrichment which generates
multiple `LLM-task-*.md` merge prompts in the conversation directory. Each prompt asks
to merge an existing wiki page with a new version from the current ingest. There can be
5-15 such tasks for a single book ingest.

**Pattern for handling merge tasks efficiently:**
1. Check for pending `LLM-task-*.md` files (no corresponding `.txt`)
2. For source-page merges that appear identical to a previous merge (same existing +
   new content), copy the previous `.txt` result instead of re-generating
3. For concept/entity page merges where the "existing" page was just written by this
   ingest (no prior version), the new content IS the merged content — output it as-is
4. Use `delegate_task` to batch-process merge tasks in the background while you continue
   other work

**Merge task identification:**
```bash
# List pending merge tasks
for f in .llm-wiki/conversation/<conv_prefix>/LLM-task-*.md; do
  t="${f%.md}.txt"
  [ ! -f "$t" ] && echo "PENDING: $f"
done
```

### Re-ingest pattern: `--delete` first

`--delete` removes the source page, orphaned concept/entity pages (those whose
only source was the deleted book), media directory, and cache entry, then a
fresh run re-ingests cleanly. **Authoritative flow (backup → delete → re-ingest
→ compare): `references/re-ingest-comparison.md`.**

### Source page may be merged multiple times

The pipeline can generate 2-3 redundant source-page merge `LLM-task` prompts during
a single ingest (Stage 2.6 writes it, then enrichment re-merges it). If you see the
same source page appearing in multiple merge tasks, reuse your first merge result —
the content doesn't change between merges.
