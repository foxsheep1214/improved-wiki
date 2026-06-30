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

## Large-chunk Stage 2.2/2.4: scale extraction density + ground formulas (2026-06-30)

Under a large-context model the chunker emits a few very large chunks (e.g. ~768K
chars ≈ a 1M-context model's 192K-token cap, spanning several book chapters). Two
practices keep these from being under-extracted or formula-drifted:

1. **Scale extraction density with chunk size.** The Stage 2.2 prompt now embeds a
   size-proportional guideline (~1 page-worthy concept per ~20K chars; 768K ≈ 38,
   264K ≈ 13). Honour it: enumerate the chunk **section by section** and produce
   ~30–40 concepts for a multi-chapter chunk, not the ~12 a small chunk warrants.
   It is a guideline, not a quota — list every genuine concept, never pad.

2. **Ground every formula by targeted grep back to source.** Don't transcribe
   formulas from memory. For each formula you cite, locate it in the chunk text or
   the per-page extract and copy the LaTeX verbatim:
   ```bash
   EXTRACT_DIR=".llm-wiki/extract-tmp/<book-stem>"
   grep -n "frac\|tag{2-\|sigma\|lambda" "$EXTRACT_DIR"/p0NNN.txt   # find the eqn
   ```

**Parallel per-chapter extraction (recommended for big chunks).** Because the
conversation handoff is serial (one prompt at a time) but a single chunk spans
several chapters, dispatch one read-only sub-agent per chapter range to deep-read
its pages and return a structured concept inventory (name / importance / definition
/ key_details with verbatim LaTeX / entities), then synthesize the chunk's Stage
2.2 YAML yourself (you control format + dedup vs prior chunks). This both raises
density and keeps formula transcription faithful. Select only the most significant
named systems as entity pages — do not make a page for every model number a survey
handbook mentions (over-extraction). Stage 2.4 generation of the (often 30–40)
pages can likewise be delegated to one sub-agent per chunk, given the chunk's exact
slug list + FILE-block format; verify block-count == requested slugs (minus the
`foo-bar` placeholder) before advancing.

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
