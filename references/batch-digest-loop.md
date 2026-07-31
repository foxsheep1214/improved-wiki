# Batch Digest Operations

This is the operator runbook. The concurrency model, locks, worker leases,
ordering guarantees, and pause semantics are defined in
`batch-parallel-prefetch.md`.

## Choose a mode

### Confirmed file list

Use this when the user has confirmed the exact ordered batch:

```bash
export SKILL_DIR="${SKILL_DIR:-$HOME/.agents/skills/improved-wiki}"
python3 "$SKILL_DIR/scripts/ingest.py" \
  raw/Book/a.pdf raw/Book/b.pdf raw/Book/c.pdf \
  --parallel 4
```

`--parallel` controls the Phase-1 prefetch ceiling. Stage 2.4 is always one
consolidated whole-source handoff, and the cross-book Stage 2.3+ spine remains
serial.

### Queue-driven

```bash
"$SKILL_DIR/scripts/wiki-monitor.sh" --verbose
"$SKILL_DIR/scripts/run-queue.sh"
```

Continuous mode:

```bash
"$SKILL_DIR/scripts/run-queue.sh" --watch --parallel 4
```

The monitor only scans and atomically updates
`.llm-wiki/ingest-queue.json`; the runner consumes it. Queue implementation
lives in `scripts/queue_cli.py`; the shell scripts are compatibility launchers.

## Handoff loop

Exit `101` means one or more conversation answers are needed. It is not a
batch result:

1. inspect pending task manifests and prompt files;
2. dispatch one fresh worker per handoff;
3. validate each `.txt.tmp` and atomically publish `.txt`;
4. re-run the exact batch or queue command.

Repeat until every confirmed source exits `0`. See `delegate-mode.md` for the
policy and `conversation-mode-agent-workflow.md` for response formats.

## Observe and control

```bash
# Read-only snapshot
python3 "$SKILL_DIR/scripts/ingest.py" --batch-status

# Pause only new/background OCR and captions
python3 "$SKILL_DIR/scripts/ingest.py" --pause-prefetch

# Resume OCR/caption prefetch
python3 "$SKILL_DIR/scripts/ingest.py" --resume-prefetch

# Freeze the whole batch and stop verified detached workers
python3 "$SKILL_DIR/scripts/ingest.py" --pause-batch

# Resume the full batch: provide the same confirmed ordered list
python3 "$SKILL_DIR/scripts/ingest.py" \
  --resume-batch raw/Book/a.pdf raw/Book/b.pdf raw/Book/c.pdf
```

Do not delete `ingest.lock`, worker status, task manifests, or the spine
reservation to force progress. If an abandoned spine must be released, inspect
partial writes first, then use `--abandon-spine <hash-or-suffix>`.

## Common outcomes

| Symptom | Meaning | Action |
|---|---|---|
| exit 101 | Conversation handoff pending | Answer, validate, publish, re-run |
| exit 75 | Full batch pause | Resume confirmed list with `--resume-batch` |
| exit 76 | Prefetch pause | `--resume-prefetch` or let ready books finish |
| exit 77 | Spine reservation conflict | Resume the recorded owner first |
| exit 78 | Another batch/watch coordinator | Inspect `--batch-status`; do not start a second |
| Project lock busy | Another write spine is active | Wait; inspect the real flock holder |
| Caption/embedding failure | Required dependency unavailable | Repair dependency and re-run |
| Source skipped | Authoritative `ingested` marker and artifacts are complete | No action |

Headless one-shot LLM commands cannot drive this workflow because each answer
changes the next prompt and Stage 2.2 has a rolling dependency. A persistent
agent must own the exit-101 loop.
