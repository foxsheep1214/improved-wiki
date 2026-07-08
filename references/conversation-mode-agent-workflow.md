# Conversation Mode — Agent Driving Pattern

When an agent (Hermes, Claude Code) drives the improved-wiki pipeline, it must
answer each LLM step that `ingest.py` delegates via prompt files. This file
documents the practical workflow for a single-book ingest.
(机制与政策见 `references/delegate-mode.md`；本文是逐 stage 作答的 hands-on cheat sheet。)

## Generation guardrails (any FILE-block prompt)

- **Never generate index/log/overview pages** — Stage 3.5 handles these three
  programmatically (index/log appended, overview LLM-rewritten). An LLM-emitted
  full rewrite silently drops history entries (the ADL8113 incident).
- **Frontmatter completeness**: every page needs the 6 required fields
  (`type`/`title`/`tags`/`related`/`created`/`updated`; `sources` is an
  additional field where applicable — see `references/naming-conventions.md`).

## Prerequisites

- **Python**: `~/.venv/bin/python3` (3.10+). System python3 (3.9) fails on PEP 604 — see `scripting-pitfalls.md` Pitfall 4.
- **Environment**: `IMPROVED_WIKI_ROOT=<project-path>` exported or prefixed.
- **minerU**: Local API server on port 19999 must be running (auto-started by pipeline).

## LLM Step Sequence (single-book, serial)

Each step: `ingest.py` exits 101 → read prompt `.md` → write response `.txt` → re-run `ingest.py`.

| Step | Prompt file pattern | What to produce | Key tips |
|------|-------------------|-----------------|----------|
| Stage 2.1 | `Stage-2-1-Global-Digest-*.md` | YAML with 6 top-level keys (book_meta, outline, key_entities, key_concepts, key_claims, chunk_plan) | **Read full text from `.llm-wiki/extract-tmp/<stem>/p*.txt`** — the prompt only includes ~4K chars sampled from the middle |
| Stage 2.2 | `Stage-2-2-Chunk-N-*.md` | YAML with chunk_index, entities_found, concepts_found, claims, formulas, connections_to_existing_wiki | Include detailed concept definitions with key_details — these feed directly into generation |
| Stage 2.4 | `Stage-2-4-Generation-*.md` | FILE blocks (`---FILE:wiki/<path>---\n...\n---END FILE---`) for source + concepts + entities | The largest step. Generate a page for EVERY concept/entity listed. Use exact slugs from the prompt. Only link to pages in the "Linkable pages" list. |
| Stage 2.7 | `Stage-2-7-QueryGeneration-*.md` | 0-5 query FILE blocks or `---QUERIES: 0---` | Each query: type=query, title, background, clues, to-explore, see-also |
| Stage 2.9 | `Stage-2-9-ComparisonReview-*.md` | 0-N comparison FILE blocks or `---COMPARISONS_IN_SOURCE: 0---` | Each comparison: why compare, table (≥4 dimensions), selection guide, see-also. |
| Stage 3.4 | `Stage-3-4-Review-*.md` | YAML array of ≥5 review items (`type`/`title`/`description`/`affected_pages`/`severity`/`search_queries`) | Runs **after** Stage 3.1 write, on the already-written pages. Single handoff, no chunk chain — same as 2.1/2.6/2.7/2.9: just answer it and move on, no cap/dispatch decision to make. |
| Merge tasks | `LLM-task-*.md` | Merged page body (no frontmatter) | **Delegate to subagent** — see below |
| Wikilink enrichment | `LLM-task-*.md` (JSON) | `{}` to skip | Safe to skip if Stage 2.4 already added inline wikilinks |

## Handling the merge loop

After Stage 3.1 write, the pipeline generates many `LLM-task-*.md` merge prompts.
These are repetitive — the same pages may be re-merged across runs.

**Pattern**: Dispatch a `delegate_task` subagent with:
- `toolsets: ['terminal', 'file']`
- Instructions to loop: read `.md` → write `.txt` → re-run `ingest.py` → repeat
- For merge tasks: output merged body (prefer richer version, keep all wikilinks)
- For JSON wikilink tasks: output `{}`
- Stop when `ingest.py` exits 0 (pipeline complete) or a non-merge/non-JSON LLM stage appears

## Stage 2.2 quality gate (mandatory, revised 2026-07-08)

**Incident (Skolnik, 14 chunks, 2026-07-07)**: a driving sub-agent kept answering
`CONVERSATION →` turn after turn without ever exiting. Context accumulated
monotonically; Stage 2.4 prompts are 290–440 KB each (they embed the full chunk
source text), and after chaining the sub-agent degraded to placeholder outputs
("Radar Handbook Content" instead of real concept names).

**Incident (EW and Radar Systems Handbook, 5 chunks, 2026-07-08)**: the driving
agent answered all 5 chunks in the main conversation (not via subagent dispatch).
The main conversation's context accumulated the same way a chained subagent's
would — by chunk 5, attention was spread across the whole book and the model
generated thin, generic output (17 concepts / 12 claims for a 455-page handbook).
C1/C3 hard gates caught the symptom; the root cause was **main-conversation context
accumulation**, identical to the Skolnik chaining failure but in the parent agent.

**Root cause (both incidents)**: stateful conversation mode — the driving agent's
context window grows with every chunk answered, degrading attention on later chunks.
This is NOT a model-capability problem; it is an architecture problem. NashSU is
immune because each `streamChat` is a stateless subprocess (fresh process, zero
history). improved-wiki's conversation mode is stateful, so it must isolate each
chunk's LLM call into a fresh subagent to achieve the same effect.

**Policy (revised 2026-07-08 — supersedes the old L4 "max 2 handoffs" rule)**:
- Stage 2.2 / 2.4: **dispatch a fresh subagent per chunk, max 1 handoff, then exit.**
  See `references/delegate-mode.md` L4 revision.
- The main conversation MUST NOT answer chunk prompts directly — doing so turns
  the main conversation into an unbounded "super-subagent" with no isolation.
- If a driving agent ever finds itself answering a 2nd consecutive same-stage
  chunk prompt, that is the bug to fix (the per-chunk dispatch was skipped).

**Quality gate (catches degradation at the cheapest point, Stage 2.2, before it
propagates into Stage 2.4's generated pages)**: after every Stage 2.2 response,
before advancing, verify:
- ≥ 5 real concepts (count `- name:` entries in `concepts_found`)
- No placeholder names (regex: `(?i)chunk \d|handbook content|reference material|technical content|book content`)
- Response size ≥ 3000 bytes
- C1 gate: source_quotes present, ≥3 claims with evidence anchors (enforced in code)
- C3 gate: no concept with >5 key_details (enforced in code)

Run `scripts/qc_stage22.py` (scans every `Stage-2-2-Chunk-*.txt` under
`.llm-wiki/conversation/*/`) to check all responses at once. If a response
fails the gate, delete the `.txt` and re-dispatch that chunk's subagent.

## Reading extracted text for Stage 2.1

```bash
EXTRACT_DIR=".llm-wiki/extract-tmp/<book-stem>"
# Sample pages across the book
for i in 1 15 30 50 70 90 110 130 150 170 190 210 230 250 270; do
  f=$(printf "%s/p%04d.txt" "$EXTRACT_DIR" "$i")
  [ -f "$f" ] && echo "=== Page $i ===" && head -10 "$f"
done
# Count total
ls "$EXTRACT_DIR"/p*.txt | wc -l
```

## Stage 2.2/2.4: scale extraction density + ground formulas (updated 2026-07-01)

At the **64K default ceiling** a large book splits into several ~256K-char chunks
(~2–3 chapters each), each analyzed and generated in ONE inline pass. Two practices
keep each chunk well-extracted and formula-faithful:

1. **Enumerate section by section — completeness, not a count.** The Stage 2.2
   prompt nudges you to read the WHOLE chunk section by section and list every
   genuine page-worthy concept the source defines or uses. It does **not** set a
   per-char concept quota (the old ~1-per-20K-chars target was dropped 2026-07-02:
   density is a property of content, not char count, and a number invited
   padding/splitting). Quality over count — never pad, never split one concept into
   several, never skip a real one to keep the list short. Select only the most
   significant named systems/people as entity pages — do not make a page for every
   model number a survey handbook mentions (over-extraction).

2. **Ground every formula by targeted grep back to source.** Don't transcribe
   formulas from memory. For each formula you cite, locate it in the chunk text or
   the per-page extract and copy the LaTeX verbatim:
   ```bash
   EXTRACT_DIR=".llm-wiki/extract-tmp/<book-stem>"
   grep -n "frac\|tag{2-\|sigma\|lambda" "$EXTRACT_DIR"/p0NNN.txt   # find the eqn
   ```

**Dispatch a FRESH subagent per chunk — do NOT answer chunks in the main conversation.**
Each Stage 2.2 / 2.4 chunk prompt embeds ~250K chars of source text. If the driving
agent answers chunk N in the main conversation, that full prompt + response stays in
the main context window — by chunk N+1 the model's attention is spread across all
prior chunks, not focused on the current one. This is the root cause of the
"progressive thinning" failure (EW and Radar Systems Handbook, 2026-07-08: chunk 1
had 10 concepts, but by chunk 5 the model was generating from domain memory rather
than reading the source). C1/C3 hard gates (source_quotes, key_details≤5) catch the
*symptom* — thin output — but do not prevent the *cause*: context accumulation
degrading attention on later chunks.

The fix is structural isolation: **one fresh subagent per chunk, max 1 handoff,
then exit.** The subagent sees ONLY that chunk's prompt (source text + schema +
prior digest), answers, and is destroyed — zero cross-chunk accumulation. This is
the subagent equivalent of NashSU's stateless `streamChat` subprocess (each call is
a fresh process with no memory of prior calls). The driving agent's main-conversation
context stays clean for orchestration (dispatch, re-invoke, progress tracking).

The 2026-07-01 A/B test (64K no-fan-out vs 192K with-fan-out) measured *chunk-size*
tradeoffs but NOT *context-isolation* tradeoffs — it never compared "answer in main
conversation" vs "fresh subagent per chunk." The quality benefit of per-chunk
isolation was therefore never measured and never documented until the 2026-07-08
EW/Radar incident. The ~15% wall-clock savings from L4 chained answering (max 2
handoffs) is abandoned in favor of quality — see `references/delegate-mode.md` L4
revision (2026-07-08).

For Stage 2.4, the subagent generates that chunk's exact slug list; verify
block-count == requested slugs (minus the `foo-bar` placeholder) before advancing.

## Re-ingest (comparison or correction)

完整流程（backup → delete → re-ingest → compare）见 `references/re-ingest-comparison.md`；速查命令：

```bash
# 1. Delete old ingest
~/.venv/bin/python3 ~/.agents/skills/improved-wiki/scripts/ingest.py \
  --delete "raw/Book/<file>.pdf"

# 2. Re-run fresh
IMPROVED_WIKI_ROOT="$(pwd)" ~/.venv/bin/python3 \
  ~/.agents/skills/improved-wiki/scripts/ingest.py \
  "raw/Book/<file>.pdf"
```
