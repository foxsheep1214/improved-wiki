# Delegate Mode — Agent Orchestration

When invoking improved-wiki from an agent (Claude Code, Hermes, etc.), use **delegate mode** to let the agent handle LLM calls with its own model and API key.

---

## Normal vs Delegate Mode

| Mode | Who calls LLM? | API key needed? |
|------|----------------|-----------------|
| Normal | ingest.py directly | ✅ Yes (LLM_API_KEY) |
| Delegate | Calling agent | ❌ No (agent uses its own) |

---

## Delegate Mode Workflow

### Step 1: Start with `--delegate`

```bash
cd /path/to/wiki/project
scripts/ingest.py raw/book/Book.pdf --delegate
```

**Output:** JSON with delegate task + checkpoint path

```json
{
  "status": "awaiting_delegate",
  "stage": "stage_1",
  "prompt": "...",
  "checkpoint_path": "/path/to/.ingest-checkpoints/abc123.json",
  "instructions": "Execute this LLM task..."
}
```

Exit code: `101` (special code for "awaiting delegate")

---

### Step 2: Agent executes the task

The agent reads the prompt, uses its LLM to generate a response, and saves:

```json
{
  "response": "LLM-generated YAML...",
  "model": "claude-opus-4-8",
  "timestamp": "..."
}
```

---

### Step 3: Continue with result

```bash
scripts/ingest.py \
  --continue-from <checkpoint_path> \
  --result <result.json>
```

**If more work is needed:** Returns next delegate task (exit code 101 again)

**If done:** Returns `{"status": "ok", "files_written": [...]}` (exit code 0)

---

## Full Example

```bash
# Agent starts
ingest.py raw/book/Book.pdf --delegate
# → exits 101, outputs phase1 task

# Agent executes Phase 1
# ... generates result ...

# Agent continues
ingest.py --continue-from .ingest-checkpoints/abc123.json --result phase1-result.json
# → exits 101, outputs phase2_chunk_1 task

# Agent executes Phase 2 Chunk 1
# ... generates result ...

# Agent continues (repeat for each chunk)
ingest.py --continue-from .ingest-checkpoints/abc123_chunk1.json --result chunk1-result.json
# → exits 101, outputs phase2_chunk_2 task
# ...

# After all chunks done
# → exits 101, outputs phase3 task

# Agent executes Phase 3
# ... generates result ...

# Agent continues (final step)
ingest.py --continue-from .ingest-checkpoints/abc123_phase3.json --result phase3-result.json
# → exits 0, files written
```

---

## Checkpoint Structure

Each checkpoint contains:

```json
{
  "phase": "phase1" | "phase2" | "chunked" | "phase3",
  "extracted_text": "...",
  "extract_method": "pymupdf",
  "global_digest": {...},
  "chunk_analyses": [...],
  "raw_file": "...",
  "_source_hash": "...",
  "_updated_at": 1234567890
}
```

---

## Agent Integration Pattern

Pseudo-code for an agent:

```python
def ingest_with_delegate(pdf_path, project_path):
    checkpoint = None
    result = None

    while True:
        if checkpoint is None:
            # First call
            cmd = ["ingest.py", pdf_path, "--delegate"]
        else:
            # Continue with result
            cmd = ["ingest.py", "--continue-from", checkpoint, "--result", result]

        proc = subprocess.run(cmd, cwd=project_path)
        exit_code = proc.returncode

        if exit_code == 0:
            # Done!
            return parse_final_output()

        elif exit_code == 101:
            # Read delegate task
            task = read_delegate_output(project_path)

            # Execute with my LLM
            result_text = my_llm_call(task["prompt"])

            # Save result
            result_path = Path(task["checkpoint_path"]).with_suffix(".result.json")
            result_path.write_text(json.dumps({"response": result_text}))

            checkpoint = task["checkpoint_path"]
            result = result_path

        else:
            raise Exception(f"Ingest failed with code {exit_code}")
```

---

## Backward Compatibility

- **Normal mode still works:** Without `--delegate`, uses `LLM_API_KEY` env var
- **Environment variables ignored in delegate mode:** `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` not used
