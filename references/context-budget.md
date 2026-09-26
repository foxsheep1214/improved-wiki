# Conversation context budget

Use a verified host/worker context capacity, in tokens. Do not ask the model to
report its own identity/capacity or infer capacity from its name.

```bash
# Once per project: the verified window of the workers that answer handoffs.
python3 "$SKILL_DIR/scripts/ingest.py" --set-context-tokens 200000
# One-off overrides (also used by semantic lint and inherited by batch workers):
python3 "$SKILL_DIR/scripts/ingest.py" raw/Book/file.pdf --context-tokens 128000
export IMPROVED_WIKI_CONTEXT_TOKENS=128000
```

Precedence: `--context-tokens` > `IMPROVED_WIKI_CONTEXT_TOKENS` > the project
setting `<runtime>/context-budget.json` > default. The CLI value also applies to
that invocation's children. Accepted values are integers from 64,000 to
10,000,000. Without any setting, the pipeline prints and uses a conservative
**64,000-token planning budget** — about a quarter of the Stage 2.4 evidence a
200K worker gets, with roughly three times as many Stage 2.2 chunks. This is
not a claim about the actual host; use a worker with at least that capacity.
The skill cannot discover the calling host's capacity reliably, so save the
verified value in each project rather than relying on per-session variables.

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
runs; the project setting makes this the default. The task manifest records the
budget. Once Stage 2.2 has started and until the source is `ingested`, a change
to the chunk targets (`target_tokens`, `target_chars`, overlap) — or, before the
Stage 2.4 result is cached, to `source_budget` — is rejected before the manifest
is updated. Resume with the recorded `--context-tokens`, or explicitly
`ingest.py --delete --keep-media <source>` to re-analyze under the new budget.
A change that leaves those fields equal (for example 200K vs 1M, both at the
64K chunk ceiling) keeps every finished analysis. Never clear checkpoints merely
to bypass a mismatch after pages were written.

The former self-report probe, model table, seven-day cache and `--reprobe` flow
are retired. `--reprobe` returns a migration error. Existing `probed-context.json`
files are ignored and left untouched; they do not override explicit settings or
supply trusted capacity to a new session.
