# Conversation Handoff Policy

This file defines the agent-orchestration contract. Per-stage response formats
and validation checks are in `conversation-mode-agent-workflow.md`; batch
scheduling is in `batch-parallel-prefetch.md`.

## Lifecycle

Start or resume with the same command:

```bash
IMPROVED_WIKI_ROOT="$(pwd)" \
python3 "$SKILL_DIR/scripts/ingest.py" raw/Book/example.pdf
```

The process either:

- exits `0`: the requested source is complete or authoritatively skipped;
- exits `101`: conversation work is pending;
- returns another documented exit code or raises: inspect and repair the
  corresponding pause, coordination, or dependency condition.

Exit `101` is never a terminal result. The driver reads the pending task
manifest under `.llm-wiki/conversation/<source-prefix>/`, answers the prompt,
publishes the result, and immediately re-invokes the exact command.

## One-handoff isolation

Except for the tiny context-window probe:

1. Dispatch one fresh worker/subagent per handoff.
2. Give it exactly one prompt and forbid further delegation or background
   fan-out.
3. Require one complete response, then terminate that worker.
4. Keep the main conversation as orchestrator; it must not answer prompts.

This applies to chunked and single-shot stages: 2.2, 2.4, 2.6, 3.4,
dedup confirmation, page merge, and wikilink enrichment. Rationale and incident
history are recorded in `architecture-decisions.md` ADR-001.

## Atomic publication

Never stream into the final result path. The answering worker writes:

```text
<stage-slug>.txt.tmp
```

The driver then:

1. verifies the worker actually stopped;
2. validates format/schema and stage-specific quality;
3. atomically renames the temporary file to `<stage-slug>.txt`;
4. re-runs `ingest.py`.

A worker saying “completed” is not evidence that the result exists or is
valid. The final `.txt` file is the pipeline's publication boundary.

For Stage 2.2, run:

```bash
python3 "$SKILL_DIR/scripts/qc_stage22.py" \
  --file <current-Stage-2-2-result.txt.tmp>
```

Use `--conv` only for a historical whole-source audit; current-handoff release
must not be blocked by stale results for obsolete prompt hashes.

## Scheduling

- Stage 2.2 answers strictly one at a time because the rolling digest is an
  input to the next prompt.
- Stage 2.4 answers may run concurrently in the wave emitted by `ingest.py`.
  One fresh worker still owns each prompt; do not exceed `--parallel`.
- Validate and publish every answer in a Stage 2.4 wave before re-invoking.
- Never run two cross-book Stage 2.3+ write spines.

See `architecture-decisions.md` ADR-002 and
`batch-parallel-prefetch.md` for the complete model.

## Completion invariant

Once the user confirms an ingest or batch, the driver continues the
dispatch→validate→publish→re-invoke loop until:

- every confirmed source exits `0`;
- the user explicitly asks to pause; or
- an external dependency or authorization genuinely blocks progress and is
  reported.

Do not send a completion response while a prompt is pending, a published
answer has not been consumed, a source is waiting behind the spine, or a
requested source lacks its `ingested` completion marker.

## Task and progress evidence

The source-bound task manifest records source identity, pipeline contract,
chunk plan, page refs, and completion prerequisites. Artifact checkpoint files
store data; stage-marker files drive control flow. Do not infer completion from
console text or a source page alone.

User-facing progress should distinguish:

- Phase 1 extraction/caption status;
- Stage 2.2 serial chunk progress;
- Stage 2.4 wave progress;
- active or reserved write spine;
- post-write review/aggregate/embed completion.

Use `ingest.py --batch-status` for a read-only snapshot.

## Recovery

- Long OCR: use `--stop-after-stage 0`; re-running resumes completed minerU
  chunks and media artifacts.
- Project lock busy: inspect the real flock holder; do not delete the lock
  file.
- Spine reservation: resume the recorded owner first. Use
  `--abandon-spine <hash>` only after checking partial writes.
- Re-ingest: ask full redo vs analysis-only `--keep-media`, then follow
  `re-ingest-comparison.md`.
- Large prompt read trouble: verify the file with smaller reads or line tools;
  one tool read failure is not proof of corruption.

Dependency failure follows the no-silent-fallback rule: repair the caption,
embedding, LLM, config, or media condition and resume from cache.

## Minimal driver sketch

```python
while True:
    code = run_ingest()
    if code == 0:
        break
    if code != 101:
        raise RuntimeError(f"ingest stopped with {code}")

    task = next_pending_task()
    tmp_result = dispatch_one_fresh_worker(task.prompt)
    validate(task, tmp_result)
    atomic_publish(tmp_result, task.result_path)
```

The real driver must also handle a bounded Stage 2.4 wave, but each task in the
wave follows the same one-worker/one-handoff publication contract.
