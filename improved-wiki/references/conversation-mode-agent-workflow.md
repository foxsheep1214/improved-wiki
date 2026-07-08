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
| Stage 2.2 | `Stage-2-2-Chunk-N-*.md` | YAML with chunk_index, entities_found, concepts_found, claims, formulas, connections_to_existing_wiki, **updated_global_digest** (5 fields: book_meta/outline/key_entities/key_concepts/key_claims — rolls up across chunks; standalone 2.1 removed 2026-07-08) | Include detailed concept definitions with key_details — these feed directly into generation. First chunk establishes book_meta + outline; later chunks refine and append. |
| Stage 2.4 | `Stage-2-4-Generation-*.md` | FILE blocks (`---FILE:wiki/<path>---\n...\n---END FILE---`) for source + concepts + entities | The largest step. Generate a page for EVERY concept/entity listed. Use exact slugs from the prompt. Only link to pages in the "Linkable pages" list. |
| Stage 2.7 | `Stage-2-7-QueryGeneration-*.md` | 0-5 query FILE blocks or `---QUERIES: 0---` | Each query: type=query, title, background, clues, to-explore, see-also |
| Stage 2.9 | `Stage-2-9-ComparisonReview-*.md` | 0-N comparison FILE blocks or `---COMPARISONS_IN_SOURCE: 0---` | Each comparison: why compare, table (≥4 dimensions), selection guide, see-also. |
| Stage 3.4 | `Stage-3-4-Review-*.md` | YAML array of ≥5 review items (`type`/`title`/`description`/`affected_pages`/`severity`/`search_queries`) | Runs **after** Stage 3.1 write, on the already-written pages. Single handoff, no chunk chain — but still dispatch a fresh subagent, same as every handoff (policy 2026-07-08). |
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

**Policy — dispatch a fresh subagent per handoff (EVERY prompt file: chunked
2.2/2.4 AND single-shot 2.6/2.7/2.9/3.4/dedup-confirm/merge/wikilink), max 1
handoff, then exit; the main conversation answers NO prompts (sole exception:
the context probe).** Chained or main-conversation answering accumulates prompt
text in one context window and degrades later output into thin/placeholder
content (两起事故：Skolnik 连答 14 chunks 2026-07-07；EW/Radar Handbook 主对话
直答 5 chunks 2026-07-08；单发 handoff 在 batch 多书时同样累积). 事故全程、根因
分析与政策沿革的**权威版在 `references/delegate-mode.md`（L4 修订）**——本文件
只保留操作检查。If you find yourself answering ANY handoff in the main
conversation (other than the probe), the per-handoff dispatch was skipped —
that is the bug.

**Quality gate (catches degradation at the cheapest point, Stage 2.2, before it
propagates into Stage 2.4's generated pages)**: after every Stage 2.2 response,
before advancing, verify:
- ≥ 5 real concepts (count `- name:` entries in `concepts_found`)
- No placeholder names (regex: `(?i)chunk \d|handbook content|reference material|technical content|book content`)
- Response size ≥ 3000 bytes
- source_quotes present + non-empty, and every claim carries a non-empty evidence
  anchor (checked by `qc_stage22.py` — the in-pipeline C1/C3 hard gates were removed
  2026-07-08 in favor of per-chunk subagent isolation, and these two checks moved
  into the offline scanner; 2.4 consumes at most 5 key_details per concept regardless)

Run `scripts/qc_stage22.py` (scans every `Stage-2-2-Chunk-*.txt` under
`.llm-wiki/conversation/*/`) to check all responses at once. If a response
fails the gate, delete the `.txt` and re-dispatch that chunk's subagent.

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

**Per-chunk subagent dispatch applies here too** — each 2.2/2.4 chunk prompt embeds
~250K chars of source; structural isolation (fresh subagent, max 1 handoff, then
exit) is what keeps chunk N+1's attention on chunk N+1 instead of the whole book.
Policy and incident record: `references/delegate-mode.md` L4 revision (2026-07-08).

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
