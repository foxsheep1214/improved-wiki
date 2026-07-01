# Session lessons — improved-wiki behavioral principles

Pruned 2026-06-29: the original was a 2026-06-11/12 debug log (~20 entries) that
has gone stale — `~/.hermes/` paths, `--delegate` mode (now conversation mode),
`validate-ingest.sh`/15-stage (now `validate_ingest.py`, 17 stages), the
mmx-CLI/MiniMax OCR path (now local minerU), and point-in-time project snapshots.
Only the timeless, still-accurate principles are kept below.

---

## 1. The skill owns scripts; projects invoke, never fork

`ingest.py`, `wiki-lint.sh`, `graph.py`, `validate_ingest.py`, etc. ship with
this skill. Wiki projects **invoke** them via
`~/.claude/skills/improved-wiki/scripts/<name>` — they do **not** copy scripts
into their own tree. A project lacking a `scripts/` dir is expected, not
"missing infrastructure." Per-project choices (VLM backend, batch size) belong in
the project's `wiki/methodology/`, as decisions — not forked code.

## 2. Stage-by-stage authorization for long pipelines, not fire-and-forget

Multi-stage background work pauses at each natural resync point, reports progress,
and waits for an explicit "go" before the next stage. Do **not** auto-chain stage
N+1 when stage N completes. (Same preference as memory
[[remind-before-long-decision-points]].) Within-stage retries are fine; cross-stage
transitions need a human gate.

## 3. Report engineering progress AND goal achievement

When reporting "done", state both: (a) did the stages execute / tests pass, and
(b) does the wiki now contain useful, retrievable content. "15/15 ✅, cache
written" says nothing about whether the user can find anything. Always cross-check
at least one user-visible artifact (a concept page has the expected terms, a
source page links its images, search returns the book).

## 4. Read the skill + cross-check the filesystem before concluding

Before claiming a wiki's state, read the skill's documented position first
(`ingest-stages-mandatory.md`, `known-issues.md`), then cross-check the actual
filesystem (`sources/`, `concepts/`, `entities/`, `media/`) — never trust a single
state file's completion claim. `ingest-cache.json` is one signal among several;
an empty `filesWritten` does not mean nothing was written.

## 5. Honor a user backend/tool override immediately; record the deviation

Skill defaults are recommendations, not contracts. When the user mandates a
specific backend/path, honor it without re-pitching the default, record the
deviation in the project's `wiki/methodology/<source>-decisions.md`, and move on.
If the override contradicts a memory entry, the live directive wins (verify the
memory is still active rather than arguing from a stale snapshot).

## 6. Justify heuristic thresholds with data, not "feels right"

Any threshold (brightness, page count, batch size, similarity cutoff) must be
backed by a data distribution, a documented standard, or an empirical benchmark.
If you can't justify it, it's a guess — mark it as such and validate it. Applies
to skill defaults too.

## 7. Watch for skill bugs while a long ingest is running, not just at the end

Check for skill-level bugs (validator false positives, missing END FILE markers,
uncaught exceptions, silent no-ops like the Stage 3.7 path bug) continuously during
a run — not only after the whole batch finishes. Each time a background ingest is
checked, or each time a handoff is re-invoked, read the relevant log once (task
`.output` files, `/tmp/ingest-*.log`) for tracebacks or validator errors. Fix the
script immediately if found, then resume — don't let a skill bug silently pollute
an entire batch. Pairs with lesson 2 (check once per handoff, don't tail
continuously).

## 8. Route knowledge by KIND, not keyword match

The wiki holds the user's **domain** knowledge (datasheets, designs, analyses).
Agent runtime metadata (model context limits, API retry behavior, prompt-cache
quirks) belongs in a skill, not the wiki — even if a wiki page happens to contain
a string that rhymes (e.g. "512K" Flash vs a 512K token window). Match on what
*kind* of knowledge it is.

---

## See also
- `references/ingest-stages-mandatory.md` — the authoritative ingest-stage checklist
- `references/known-issues.md` — pipeline debugging recipes + current bugs
- `references/scripting-pitfalls.md` — Python + agent-tool gotchas
- `references/scanned-pdf-ocr-pipeline.md` — unified minerU OCR pipeline (all PDFs)
- `references/nashsu-lint-source-analysis.md` / `nashsu-search-architecture.md` — NashSU parity notes
