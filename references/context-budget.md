# Conversation context budget

Use a verified host/worker context capacity, in tokens. Do not ask the model to
report its own identity/capacity or infer capacity from its name.

```bash
python3 "$SKILL_DIR/scripts/ingest.py" raw/Book/file.pdf --context-tokens 128000
# Equivalent, also used by semantic lint and inherited by batch workers:
export IMPROVED_WIKI_CONTEXT_TOKENS=128000
```

CLI overrides the environment for that invocation and its children. Accepted
values are integers from 64,000 to 10,000,000. Without a setting, the pipeline
prints and uses a conservative **64,000-token planning budget**. This is not a
claim about the actual host; use a worker with at least that capacity. The skill
cannot discover the calling host's capacity reliably.

`_config.py` owns all budget math: chunk targets scale with context, retain the
64K hard ceiling (configurable by `IMPROVED_WIKI_TARGET_TOKENS_CEIL`), and reserve
room for instructions/project context/output. The consolidated `source_budget`
is in tokens; consumers convert using measured source chars/token. Complete
analysis fields are reduced in a fixed order, with reductions disclosed in the
prompt. Templates are short domain notes included in full.

These are planning limits, not enforcement of a worker's actual output limit.
`max_tokens` in conversation tasks is metadata; the host controls generation.

## Resume compatibility

Keep the same budget across exit-101 resumes, worker switches and background
runs. The task manifest records it. A changed budget during a partial write is
rejected before updating the manifest; restore the recorded verified budget.
Earlier analysis checkpoints remain subject to chunk-plan/contract validation.
Never clear checkpoints merely to bypass a mismatch after pages were written.

The former self-report probe, model table, seven-day cache and `--reprobe` flow
are retired. `--reprobe` returns a migration error. Existing `probed-context.json`
files are ignored and left untouched; they do not override explicit settings or
supply trusted capacity to a new session.
