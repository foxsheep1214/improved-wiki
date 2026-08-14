---
name: improved-wiki
description: "Ingest, lint, graph, validate, or repair a Karpathy/NashSU-style LLM Wiki. Use for PDF/PPTX/DOCX/XLSX/ODT/EPUB/RTF ingestion, multi-book batches, conversation handoffs, OCR/caption troubleshooting, deep research, review processing, and wiki completeness audits. Text LLM work uses conversation-mode prompt files; Phase 1 uses minerU plus a configured caption VLM."
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
| Deep research | `/improved-wiki deep-research <topic>` | An explicit topic or Review choice is already confirmed; confirm only agent-derived suggestions. Source mode defaults to `web` |
| Lint | `"$SKILL_DIR/scripts/wiki-lint.sh"` | Default maintenance is authorized; ask at exit 102 before delete-orphans |
| Graph | `python3 "$SKILL_DIR/scripts/graph.py"` | None |
| Validate | `python3 "$SKILL_DIR/scripts/validate_ingest.py" --root "$WIKI_ROOT" --source "<source stem>"` | Read-only |

Do not assume a particular vendor agent, browser, MCP server, or shell helper.
If a required capability is missing, report it instead of silently degrading.

## Deep Research contract (NashSU v0.6.8)

- Select `web`, `anytxt`, or `both`; default to `web`. Direct topic research uses
  exactly `[topic]`. Review-provided non-empty `search_queries` are passed
  verbatim; graph-derived gaps use the context-aware one-topic/three-query
  optimizer and require confirmation before search.
- Web collection requests 5 results per query. The project-local AnyTXT analogue
  rewrites to 1–3 compact keyword queries and returns at most 15 results via
  `scripts/search_local.py`. In `both` mode collect the selected sources in
  parallel when the runtime permits.
- Deduplicate case-insensitively by URL, falling back to
  `source:title:snippet`, and keep at most 20 sources globally. Synthesize only
  from numbered title/source/snippet records plus `wiki/index.md`; do not fetch
  full result pages or local files in the parity path.
- Preserve the v0.6.7 synthesis prompt and save the LLM body without editorial
  rewriting. Use `scripts/write_research_page.py` to strip thinking blocks and
  deterministically write one `wiki/queries/research-*.md` page with exact
  frontmatter and code-generated References.
- The writer gates on synthesis completeness (v0.6.8): a body under 120
  meaningful characters, one with no block of 40+, or one citing none of the
  collected sources exits `4` and writes nothing. Treat `4` as retryable —
  re-synthesize from the same sources; never hand-patch the body to clear it.
- **Do not call `ingest.py` on the result.** Do not create typed pages/reviews or
  mutate index/log/overview. If embeddings are explicitly enabled, a page-only
  `build_embeddings.py ... upsert --page <saved-page>` is optional and
  non-critical. Resolve a source Review only after the page exists, with
  `Research saved: <path>`.
- Clean zero results complete without a page; zero results plus a source error
  fail; partial source failures proceed with successful results and are reported.

The exact prompts, source ordering, error semantics, writer invocation, and
manual compatibility boundary are authoritative in
`references/deep-research.md`.

## Lint contract

- Plain `wiki-lint.sh` runs structural + semantic checks and, by default,
  `emit-review`, `fix`, `fix-links`, `sweep`, and one `dedup` round.
  `--no-<action>` overrides an individual default.
- Semantic/sweep/dedup may return exit 101 for conversation handoff. Answer the
  prompt and resume the exact invocation. Exit-101 re-entry continues one
  durable logical lint run: semantic performs one complete scan for that run
  and completed stages are not restarted. Sweep likewise preserves NashSU's
  single hard budget of at most five 40-item judge batches across re-entry and
  stops at the first batch that resolves zero items. Use `--reset-lint-run`
  only to discard an abandoned checkpoint and intentionally start over. After
  one requested dedup round, continue remaining stages with `--no-dedup`
  unless the user explicitly asks for full convergence.
- After all preceding default stages finish, plain lint exits **102** with
  `DELETE_ORPHANS_CONFIRMATION_REQUIRED`. This is a required pause: ask the
  user whether to run delete-orphans. Do not infer consent.
  - If approved: run `wiki-lint.sh --delete-orphans-only`; it performs a fresh
    structural scan, then emits orphan preview/Review items.
  - If declined: stop; the preceding lint/fix/sweep/dedup work is already done.
- `--diagnostic-only` keeps structural + semantic lint but disables every wiki
  mutation and the exit-102 checkpoint. `--structural-only` is the deterministic
  structural-only diagnostic route.
- Delete-orphans remains preview + Review generation; it does **not** delete
  pages. Real deletion is the separately confirmed
  `wiki-lint-fix.py --delete-orphans --apply` command.
- Keep improved-wiki's documented semantic batching/safety extensions; v0.6.6
  parity covers normalized indexed structural suggestions and exact normalized
  filtering of false `missing-page` findings.
- Graph is a peer command, not a lint phase. `wiki-lint.sh` never invokes
  `graph.py`; run Graph explicitly when graph artifacts are requested.

## Ingest contract

Active order:

```text
0.1 raw naming → 0.2 source dedup
1.1 text/OCR → 1.2 images → 1.3 captions
2.2 serial chunk analysis + rolling digest
→ 2.3 existing-wiki association
→ 2.4 one consolidated whole-source generation: mandatory source page
  + key/schema-typed pages, then in-source dedup
→ 3.1 pre-write review generation
→ 3.2 write/merge → 3.3 aggregate repair → 3.4 media injection
→ 3.5 review persistence → 3.6 cache
→ 3.7 touched-page embedding upsert → ingested marker
```

Ingest does not create unanswered query pages. Comparison, synthesis, finding,
thesis, and methodology pages use Stage 2.2→2.4's shared schema-typed
lifecycle. Review suggestions are handled by
`process-reviews`; Graph remains a separate explicit command. The authoritative
stage gates are in `references/ingest-stages-mandatory.md`.

### Project schema contract

- Require `<project>/schema.md`; its scoped `## Page Types` table is the
  authoritative `frontmatter type → wiki directory` map.
- Inject the semantic schema into Stage 2.2, 2.4, and 3.1 prompts,
  matching NashSU. Exclude improved-wiki's machine-only raw naming YAML from
  LLM context while still enforcing it at Stage 0.1.
- Load optional `<project>/purpose.md` into the same prompts: schema defines
  how the wiki is structured; purpose defines why the project exists.
- Resolve schema-typed candidates through the parsed type map, never through an
  LLM-supplied folder string. Auto-correct known type/directory mismatches at
  write time rather than losing a valid page.

### NashSU generation policy

- Stage 2.2 identifies new or materially updated **key** entities/concepts and
  core claims. Be thorough but concise; passing mentions and background
  prerequisites are not page candidates.
- No concept-page, entity-page, or claim-count target exists. `mentioned`
  concepts are analysis context only and never reserve a generated slug.
- Stage 2.4 generates the recommended key and schema-typed pages after
  all chunk analyses and the Stage 2.3 association extension. Comparison,
  synthesis, finding, thesis, methodology, and custom declared types follow
  the same selection, routing, grounding, and FILE generation path. Stage 2.3
  existing-wiki association and the post-generation in-source semantic dedup
  are documented improved-wiki extensions; they do not split the single final
  generation call. Schema semantics remain mandatory. Under NashSU's bundled
  semantics, a source may seed a cross-cutting synthesis or a speculative
  working thesis; later source ingests merge evidence and update thesis
  confidence/status. A project schema may impose a stricter evidence gate.
- A Stage 2.3 match in the candidate's own schema route is an exact **update
  target**, not a reason to skip the candidate: Stage 2.4 emits that existing
  FILE path and Stage 3.2 merges it. A cross-type association remains link-only
  so one subject is not duplicated into a second generic/type-specific page.
- On corrected-source re-ingest, Stage 3.2 replaces the stale body of a page
  owned solely by that source while preserving locked fields and array unions.
  Multi-source pages still use the semantic merger so other sources survive.
- There is no per-type page quota or separate comparison cap. Stage 2.4 never
  invents supplementary foundational pages or automatically backfills every
  analyzed term.
- Stage 2.4 also emits the mandatory source page — one concise, free-form
  summary in the SAME call (NashSU parity, merged 2026-08-01). It links only
  materially relevant pages and selects core claims; it does not dump all
  generated pages/chunk claims or require a fixed H2 set. When the model
  omits it, a deterministic fallback is written from the complete Stage 2
  analysis — never a second LLM call.
- An unclosed `FILE` block is dropped and gets one exact-path targeted repair
  call. Unrequested repair pages are rejected; an unrecovered recommended
  key/schema-typed page pauses instead of publishing partial content.
- If the source summary is still missing or malformed, write NashSU's
  deterministic fallback from the complete Stage 2 analysis. Neither recovery
  path is a per-concept coverage backfill or a page-count mechanism.

### Parallelism

- Stage 2.2 is serial: chunk N+1 consumes chunk N's validated rolling digest.
- Stage 2.4 runs exactly one consolidated generation handoff after every
  Stage 2.2 chunk has been analyzed. Its prompt carries the final rolling
  digest, every chunk analysis, and bounded raw evidence from every chunk.
  Over budget, whole low-value analysis FIELDS are dropped in a fixed priority
  order so every per-chunk payload stays a complete parseable object; raw
  evidence takes the leftover budget. The context states which fields it gave
  up — this is never a silent cap. An oversized final digest or minimum-detail
  analysis uses a valid JSON head/tail envelope rather than a mid-string cut.
- `--parallel` controls cross-book Phase 1 OCR/caption prefetch only; it does
  not split or parallelize Stage 2.4.
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
   `scripts/qc_stage22.py --file <current-result.txt.tmp>` before publication.
5. Re-run the exact ingest command immediately.

Continue until all confirmed sources exit `0`, the user explicitly pauses, or
a real external blocker is reported. A pending prompt, cached answer, or
source waiting behind the spine is not a terminal result.

Stage 2.4 exposes exactly one whole-source generation prompt (source page included). Answer it with
one fresh worker, validate and atomically publish the result, then re-invoke.

Policy and rationale: `references/delegate-mode.md`. Per-stage result formats:
`references/conversation-mode-agent-workflow.md`.

## Quality and failure policy

There is no silent quality fallback:

- FILE repair and the guaranteed source-summary fallback are explicit,
  logged NashSU recovery paths; they never fabricate extra key/schema-typed
  coverage.
- Captioning requires the configured VLM provider; optional VLM-to-VLM failover
  is allowed only when explicitly configured and logged.
- Ingest embeddings require the configured stack. The default remains local
  Ollama/bge-m3, while `EMBEDDING_ENDPOINT` plus provider/model settings can
  select Google, Volcengine/Doubao, or another OpenAI-compatible exact request
  endpoint. The per-request timeout defaults to NashSU's 8 seconds and can be
  overridden with `EMBEDDING_TIMEOUT_SECONDS`; legacy `EMBEDDING_BASE_URL`
  remains supported as a base URL.
- Stage 3.7 follows NashSU 0.6.6's page-scoped lifecycle: re-chunk and replace
  only the pages written by this ingest. Every touched page must have exact
  chunk coverage before `ingested` may be set. It never performs an implicit
  full-wiki rebuild and does not use the legacy `embed-cache.json`.
- `scripts/build_embeddings.py ... embed` is the explicit full re-index route.
  It prepares every current chunk before overwriting the live table and verifies
  the final row count. Both incremental and full successful writes run compact
  + verified old-version pruning. Maintenance is best-effort so a compact
  failure does not invalidate the successful index write; retry manually with
  `scripts/build_embeddings.py --project <wiki-root> compact`.
- Source deletion and lint orphan deletion remove the corresponding LanceDB
  rows after the Markdown delete, using NashSU's non-critical lifecycle
  semantics. For a manual one-page cleanup use
  `scripts/build_embeddings.py --project <wiki-root> delete --page <path.md>`.
  Direct filesystem deletions bypass this lifecycle and require a full re-index.
- Existing indexes have no chunker-version metadata. After upgrading from the
  legacy full-rebuild/cache implementation, run one explicit full re-index;
  subsequent ingests remain page-scoped.
- Vector retrieval follows NashSU's optional search behavior: a vector failure
  is surfaced and search continues keyword-only. This does not weaken the
  mandatory Stage 3.7 ingest gate. NashSU can make ingest embedding optional
  because keyword + graph retrieval remains usable and its vector index is a
  search enhancement; improved-wiki intentionally uses a stronger completion
  contract in which `ingested` means Markdown pages and their semantic index
  are synchronized. A failed upsert therefore pauses at 3.7 and resumes there
  instead of declaring a partially indexed source complete.
- Deep Research is outside ingest: its optional page-scoped query-page upsert is
  best-effort, matching v0.6.7, and cannot roll back an already saved research
  page. This exception does not weaken the mandatory ingest Stage 3.7 gate.
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
- **Deep research:** an explicit topic or Review action is already confirmed.
  Confirm only a topic/queries proposed from Graph, lint, or another agent-derived
  knowledge gap before the selected source search begins.

Single-source ingest, diagnostic-only lint/validate, Graph, and save-chat-to-wiki
are not gated. Plain lint's first five maintenance actions are authorized by
default; its delete-orphans continuation is always human-gated at exit 102.

## Entry points

- Auto ingest: `scripts/ingest.py`
- Embedding build/search/compact: `scripts/build_embeddings.py`
- Queue scan/run: `scripts/wiki-monitor.sh`, `scripts/run-queue.sh`
- Chat ingest: `references/chat-ingest.md`
- Deep research: `references/deep-research.md`,
  `scripts/search_local.py`, `scripts/write_research_page.py`
- Save chat: `references/save-chat-to-wiki.md`
- Review sweep/process: `references/review-sweep.md`,
  `references/process-reviews.md`
- Lint/Graph and all utilities: `references/scripts-reference.md`

## Reference map

- Pipeline: `ingest-stages-mandatory.md`, `batch-parallel-prefetch.md`,
  `batch-digest-loop.md`, `scanned-pdf-ocr-pipeline.md`,
  `mineru-version-tracking.md`
- Agent driving: `delegate-mode.md`, `conversation-mode-agent-workflow.md`,
  `context-probe.md`
- Generation: `comparison-generation.md`, `dedup-design.md`,
  `image-caption-strategy.md`, `language-directive.md`
- Conventions: `naming-conventions.md`, `raw-naming-conventions.md`,
  `raw-layout-compat.md`, `review-file-naming.md`
- Operations: `initial-setup.md`, `re-ingest-comparison.md`,
  `maintenance-cleanup.md`, `known-issues.md`, `cron-installation.md`
- Retrieval and search: `kb-retrieval.md`, `nashsu-search-architecture.md`
- Background: `architecture-decisions.md`, `roadmap.md` (planned, not built),
  `nashsu-lint-source-analysis.md`

Templates live under `templates/`. Ingest templates are selected by source
type; aggregate templates cover schema, index, log, and overview.
