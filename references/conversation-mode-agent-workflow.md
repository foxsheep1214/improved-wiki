# Conversation Mode — Agent Driving Pattern

When an agent (Hermes, Claude Code) drives the improved-wiki pipeline, it must
answer each LLM step that `ingest.py` delegates via prompt files. This file
documents the practical workflow for a single-book ingest.

## Prerequisites

- **Python**: `~/.venv/bin/python3` (3.11+). System python3 (3.9) fails on PEP 604 type hints.
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

**Answer each chunk DIRECTLY yourself — do NOT fan out to per-chapter sub-agents.**
A ~256K-char chunk (~2–3 chapters) is directly manageable in a single analyze pass
and a single generate pass. A 2026-07-01 A/B ingest confirmed this: the 64K arm ran
the whole book in **10 native round-trips with no fan-out**, cleaner than a 192K
whole-book single chunk that had to be fanned out into per-chapter helpers +
split-generation groups (which stalled repeatedly on orchestration for no quality
gain). Sub-agent fan-out is only worth considering if you deliberately override the
ceiling far up (`IMPROVED_WIKI_TARGET_TOKENS_CEIL=192000`) so one chunk spans the
whole book — which is not the default and not recommended for dense references.
For Stage 2.4, generate the chunk's exact slug list inline; verify block-count ==
requested slugs (minus the `foo-bar` placeholder) before advancing.

## Re-ingest (comparison or correction)

```bash
# 1. Delete old ingest
~/.venv/bin/python3 ~/.agents/skills/improved-wiki/scripts/ingest.py \
  --delete "raw/Book/<file>.pdf"

# 2. Re-run fresh
IMPROVED_WIKI_ROOT="$(pwd)" ~/.venv/bin/python3 \
  ~/.agents/skills/improved-wiki/scripts/ingest.py \
  "raw/Book/<file>.pdf"
```
