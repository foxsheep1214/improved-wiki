---
name: improved-wiki
description: "Ingest, lint, graph, validate, or repair a Karpathy/NashSU-style LLM Wiki. Use for PDF/PPTX/DOCX ingestion, multi-book batches, conversation handoffs, OCR/caption troubleshooting, deep research, review processing, and wiki completeness audits. Text LLM work uses conversation-mode prompt files; Phase 1 uses minerU plus a configured caption VLM."
---

# improved-wiki

Use this skill as three peer commands: **Ingest**, **Lint**, and **Graph**. Run
commands from the target wiki project; project data stays there, while scripts
run from the installed skill:

```bash
export SKILL_DIR="${SKILL_DIR:-$HOME/.agents/skills/improved-wiki}"
```

## Route the request

| Intent | Command or route | Required confirmation |
|---|---|---|
| Ingest one source | `python3 "$SKILL_DIR/scripts/ingest.py" <file>` | None |
| Ingest 2+ sources | same command with the complete ordered file list | Confirm list and target project |
| Re-ingest | `ingest.py --delete <file>`, then ingest again | Confirm source and full redo vs `--keep-media` |
| Deep research | `/improved-wiki deep-research <topic>` | Confirm one-topic scope; require web search |
| Lint | `"$SKILL_DIR/scripts/wiki-lint.sh"` | Read-only unless fixes are requested |
| Graph | `python3 "$SKILL_DIR/scripts/graph.py"` | None |
| Validate | `python3 "$SKILL_DIR/scripts/validate_ingest.py" ...` | Read-only |

Do not assume a particular vendor agent, browser, MCP server, or shell helper.
If a required capability is missing, report it instead of silently degrading.

## Ingest contract

Active order:

```text
0.1 raw naming → 0.2 source dedup
1.1 text/OCR → 1.2 images → 1.3 captions
2.2 serial chunk analysis + rolling digest
→ 2.3 existing-wiki association
→ 2.4 grounded page generation + in-source dedup
→ 2.6 source page → 2.9 comparisons
→ 3.1 write/merge → 3.2 media injection → 3.4 review
→ 3.5 aggregate repair → 3.7 embeddings → ingested marker
```

Stage 2.7 query generation is retired. Review suggestions are handled by
`process-reviews`; Graph remains a separate explicit command. The authoritative
stage gates are in `references/ingest-stages-mandatory.md`.

### Project schema contract

- Require `<project>/schema.md`; its scoped `## Page Types` table is the
  authoritative `frontmatter type → wiki directory` map.
- Inject the semantic schema into Stage 2.2, 2.4, 2.6, 2.9, and 3.4 prompts,
  matching NashSU. Exclude improved-wiki's machine-only raw naming YAML from
  LLM context while still enforcing it at Stage 0.1.
- Load optional `<project>/purpose.md` into the same prompts: schema defines
  how the wiki is structured; purpose defines why the project exists.
- Resolve schema-typed candidates through the parsed type map, never through an
  LLM-supplied folder string. Auto-correct known type/directory mismatches at
  write time rather than losing a valid page.

### Parallelism

- Stage 2.2 is serial: chunk N+1 consumes chunk N's validated rolling digest.
- Stage 2.4 chunks are independent and run in bounded parallel waves up to
  `--parallel`; `--parallel 1` is the explicit serial mode.
- Across books, Phase 1 overlaps with the current book, but minerU has one
  resource slot and captioning has one coordinated slot.
- Stage 2.3+ is one ordered write spine across books. Never parallelize it.

`references/batch-parallel-prefetch.md` is authoritative for worker leases,
pause markers, reservations, ordering, and recovery.

## Conversation handoffs

Text generation has one route: `ingest.py` writes a prompt and exits
`101` (`HANDOFF_PENDING`). That is an internal yield, not completion.

For every handoff except the tiny context probe:

1. Dispatch one fresh worker/subagent for exactly one self-contained prompt.
2. The main conversation orchestrates; it does not answer the prompt itself.
3. Produce a complete `<stage>.txt.tmp`; validate it; atomically rename to
   `<stage>.txt`.
4. For Stage 2.2, run
   `scripts/qc_stage22.py --file <current-result.txt>` before publication.
5. Re-run the exact ingest command immediately.

Continue until all confirmed sources exit `0`, the user explicitly pauses, or
a real external blocker is reported. A pending prompt, cached answer, or
source waiting behind the spine is not a terminal result.

Stage 2.4 may expose several independent prompts in one bounded wave. Answer
them concurrently with separate fresh workers, validate every result, then
re-invoke for the next wave. Do not convert Stage 2.4 to serial execution.

Policy and rationale: `references/delegate-mode.md`. Per-stage result formats:
`references/conversation-mode-agent-workflow.md`.

## Quality and failure policy

There is no silent quality fallback:

- Captioning requires the configured VLM provider; optional VLM-to-VLM failover
  is allowed only when explicitly configured and logged.
- Embeddings require the configured local stack.
- Every successful Stage 3.7 full LanceDB rewrite runs compact + verified
  old-version pruning. Maintenance is best-effort so a compact failure does not
  invalidate the newly rebuilt index; retry manually with
  `scripts/build_embeddings.py --project <wiki-root> compact`.
- LLM, merge, config, schema, and required-media failures pause the source.
- Corrupt cache/checkpoint files may warn and rebuild because re-derivation is
  the correct recovery.

Extraction, prompt results, task manifests, and stage markers are resumable.
Do not delete lock files to break a live run. Use:

```bash
python3 "$SKILL_DIR/scripts/ingest.py" --batch-status
python3 "$SKILL_DIR/scripts/ingest.py" --pause-prefetch
python3 "$SKILL_DIR/scripts/ingest.py" --pause-batch
```

Resume prefetch with `--resume-prefetch`. Resume a full batch only with the
same confirmed ordered file list plus `--resume-batch`. Abandon a reserved
spine only after inspecting partial writes with `--abandon-spine <hash>`.

## Destructive and human-gated actions

- **Batch ingest:** confirm the complete ordered source list and target project.
- **Re-ingest/delete:** confirm source identity and choose full redo or
  analysis-only `--keep-media`. See `references/re-ingest-comparison.md`.
- **Deep research:** confirm one-topic scope before the web→wiki loop.

Single-source ingest, read-only lint/validate, Graph, and save-chat-to-wiki are
not gated.

## Entry points

- Auto ingest: `scripts/ingest.py`
- Embedding build/search/compact: `scripts/build_embeddings.py`
- Queue scan/run: `scripts/wiki-monitor.sh`, `scripts/run-queue.sh`
- Chat ingest: `references/chat-ingest.md`
- Deep research: `references/deep-research.md`
- Save chat: `references/save-chat-to-wiki.md`
- Review sweep/process: `references/review-sweep.md`,
  `references/process-reviews.md`
- Lint/Graph and all utilities: `references/scripts-reference.md`

## Reference map

- Pipeline: `ingest-stages-mandatory.md`, `batch-parallel-prefetch.md`,
  `batch-digest-loop.md`, `scanned-pdf-ocr-pipeline.md`
- Agent driving: `delegate-mode.md`, `conversation-mode-agent-workflow.md`,
  `context-probe.md`
- Generation: `comparison-generation.md`, `dedup-design.md`,
  `image-caption-strategy.md`, `language-directive.md`
- Conventions: `naming-conventions.md`, `raw-naming-conventions.md`,
  `raw-layout-compat.md`, `review-file-naming.md`
- Operations: `initial-setup.md`, `re-ingest-comparison.md`,
  `maintenance-cleanup.md`, `known-issues.md`, `cron-installation.md`
- Retrieval and search: `kb-retrieval.md`, `nashsu-search-architecture.md`

Templates live under `templates/`. Ingest templates are selected by source
type; aggregate templates cover schema, index, log, and overview.
