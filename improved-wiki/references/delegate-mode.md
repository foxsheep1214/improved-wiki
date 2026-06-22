# Delegate Mode — Agent Orchestration

Invoking improved-wiki from an agent (Claude Code, Hermes, etc.) always uses **conversation mode** — there is no flag, and no direct-API alternative. Every text-generation LLM step (including wikilink enrichment, batched once per ingest) is handled by the calling agent with the current conversation's model. The one external API dependency outside text generation is image captioning (Stage 1.3, MiniMax VLM) — vision content can't flow through the prompt-file handoff, so it always calls its configured HTTP API directly.

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
