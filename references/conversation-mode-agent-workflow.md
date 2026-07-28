# Conversation Handoff Response Guide

This is the hands-on format/QC guide. The one-fresh-worker rule, atomic
publication, and completion lifecycle are authoritative in `delegate-mode.md`.

## Common guardrails

- Read the whole prompt and embedded source segment.
- Never emit index, log, or overview pages; Stage 3.5 owns aggregates.
- Every generated page needs the required frontmatter fields:
  `type`, `title`, `tags`, `related`, `created`, `updated`; include `sources`
  where applicable.
- Only use paths and link targets allowed by the prompt.
- Write one complete `.txt.tmp`; do not stream into the final `.txt`.

## Stage formats

| Stage | Prompt pattern | Required answer |
|---|---|---|
| Context probe | `ctxprobe*.md` | Plausible integer context size; only main-conversation exception |
| 2.2 | `Stage-2-2-Chunk-N-*.md` | Valid YAML containing chunk index, entities, concepts, claims, formulas, existing-wiki connections, and the five-field `updated_global_digest` |
| 2.4 | `Stage-2-4-Generation-*.md` | Exact requested `---FILE:wiki/<path>--- … ---END FILE---` blocks |
| 2.6 | `Stage-2-6-SourcePage-*.md` | One non-empty source-page FILE block at the exact requested path; headings are source-driven |
| 2.9 | `Stage-2-9-ComparisonReview-*.md` | Comparison FILE blocks or the exact zero-comparison sentinel |
| 3.4 | `Stage-3-4-Review-*.md` | Strict YAML array of real findings; empty `[]` is valid |
| Page merge | `LLM-task-*.md` | Merged body without frontmatter; preserve richer facts and wikilinks |
| Wikilink enrichment | JSON `LLM-task-*.md` | Requested JSON mapping; `{}` is valid when no safe addition exists |

## Stage 2.2 quality release

Before publication:

```bash
python3 "$SKILL_DIR/scripts/qc_stage22.py" \
  --file <current-result.txt.tmp>
```

The answer must:

- identify only genuinely important new/materially updated concepts and
  entities; an honestly sparse or empty candidate list is valid;
- avoid placeholder names such as “chunk 3”, “technical content”, or
  “reference material”;
- give every emitted claim a non-empty evidence anchor; `source_quotes` is
  optional audit support rather than a quota;
- carry a complete five-field rolling digest:
  `book_meta`, `outline`, `key_entities`, `key_concepts`, `key_claims`;
- remain grounded in the current chunk, not memory of earlier prompts.

First chunk establishes book metadata and outline. Later chunks refine and
append; never discard correct prior digest content. Stage 2.2 is serial.

For formulas, locate the exact equation in the embedded chunk or cached
per-page extract and copy it faithfully. Do not reconstruct equations from
memory.

## Stage 2.4 quality release

Stage 2.4 may be answered concurrently within the wave emitted by `ingest.py`.
For each prompt:

- generate only the recommended key owner slugs requested by that prompt,
  excluding ALREADY COVERED/SKIP items and never adding supplementary pages;
- do not pad or split concepts to reach a FILE-block count;
- write definitions, mechanisms, equations, constraints, and source-specific
  evidence rather than generic summaries;
- preserve proper nouns and technical identifiers;
- add wikilinks only from the prompt's Linkable pages universe.

Validate all results in the wave, atomically publish them, then re-invoke. Do
not serialize normal Stage 2.4 operation and do not exceed `--parallel`.

## Source, comparisons, and review

Stage 2.6 validates one exact-path, closed, non-empty FILE block. Choose the
smallest useful source-driven structure; summarize core material and do not
enumerate every generated page or per-chunk claim.

Stage 2.9 comparisons need a why-compare section, a table with at least four
useful dimensions, a selection guide, and see-also links. Use the language
requested by the prompt.

Stage 3.4 runs after pages are written. Each item requires:

- `type`, `title`, `description`, `affected_pages`, `severity`,
  `search_queries`;
- safe wiki `.md` paths in `affected_pages`;
- two or three unique search queries for `suggestion` and `missing-page`;
- `search_queries: []` for other types.

Report only real findings. One malformed item rejects the whole response, so
validate the array before publication.

## Merge and enrichment tasks

For page merge prompts, preserve the union of supported content. Prefer the
richer formulation, keep valid frontmatter-owned metadata out of the body, and
never drop existing wikilinks merely to simplify the result.

Redundant identical duplicate writes are skipped in code. If the same merge
reappears for byte-identical inputs, treat it as a regression rather than an
expected manual workaround.

Enrichment normally skips pages that already have outgoing wikilinks. When a
task is emitted, add only safe, relevant links from its allowed universe; an
empty mapping is preferable to invented links.

## Re-ingest

Before deletion, ask whether the user wants:

- full redo: OCR, images, captions, analysis, and generation; or
- analysis-only: `--delete --keep-media`.

Follow `re-ingest-comparison.md` for backup, delete, resume, and comparison.
