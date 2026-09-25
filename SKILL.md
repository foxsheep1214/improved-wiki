---
name: improved-wiki
description: "Ingest, lint, graph, validate, or repair a Karpathy/NashSU-style LLM Wiki. Use for PDF/PPTX/DOCX/XLSX/ODT/EPUB/RTF ingestion, multi-book batches, conversation handoffs, OCR/caption troubleshooting, deep research, review processing, and wiki completeness audits. Text LLM work uses conversation-mode prompt files; Phase 1 uses minerU plus a configured caption VLM."
---

# improved-wiki

Run commands from the target wiki project. Scripts live in the installed skill;
project data stays in the project. Do not assume a particular agent vendor,
browser, connector, or shell helper.

```bash
export SKILL_DIR="${SKILL_DIR:-$HOME/.agents/skills/improved-wiki}"
export IMPROVED_WIKI_ROOT="$(pwd)"
```

## Route the request

Read the relevant reference before acting; the linked runbook owns its detailed
contract. Do not load unrelated references for a simple operation.

| Intent | Entry point | Required reference |
|---|---|---|
| Ingest one source | `python3 "$SKILL_DIR/scripts/ingest.py" raw/Book/file.pdf` | [Stage gates](references/ingest-stages-mandatory.md), [handoffs](references/delegate-mode.md), [result formats](references/conversation-mode-agent-workflow.md) |
| Ingest 2+ sources | same command with the complete ordered list | [Batch orchestration](references/batch-parallel-prefetch.md), [selection](references/batch-digest-loop.md) |
| Re-ingest or delete | `ingest.py --delete [--keep-media] <file>` | [Re-ingest](references/re-ingest-comparison.md), [maintenance](references/maintenance-cleanup.md) |
| Lint or repair | `bash "$SKILL_DIR/scripts/wiki-lint.sh"` | [Lint](references/lint.md) |
| Graph | `python3 "$SKILL_DIR/scripts/graph.py"` | [Scripts](references/scripts-reference.md) |
| Validate | `python3 "$SKILL_DIR/scripts/validate_ingest.py" --root "$IMPROVED_WIKI_ROOT" --source raw/Book/file.pdf` | [Stage gates](references/ingest-stages-mandatory.md) |
| Retrieve knowledge/evidence | `search_wiki.py`, `evidence_lookup.py` | [Retrieval](references/kb-retrieval.md) |
| Deep research | collect → synthesize → `write_research_page.py` | [Deep research](references/deep-research.md) |
| Process Reviews | sweep → route → repair/research/skip | [Reviews](references/process-reviews.md), [sweep](references/review-sweep.md) |
| Conversation import/save | source ingestion or query-page save | [Chat ingest](references/chat-ingest.md), [save chat](references/save-chat-to-wiki.md) |
| Setup, OCR, scheduling | project/dependency checks | [Setup](references/initial-setup.md), [MinerU](references/mineru-version-tracking.md), [scheduling](references/cron-installation.md) |

## Shared contracts

- Require project `schema.md`. Its `## Page Types` table owns type-to-directory
  routing; optional `purpose.md` sets priorities. The eight digest templates
  contain domain guidance only. Prompt builders own the output protocol.
- Use complete source identities (`raw/...` or `wiki/queries/...`). Legacy short
  references must resolve uniquely. Preserve case and wrapper directories;
  source identities and persisted cache keys are different contracts.
- Runtime detection never moves files. Mixed `.iwiki-runtime` and `.llm-wiki`
  state requires explicit conflict-checked [migration](references/runtime-layout.md).
- Context capacity comes from verified host settings or `--context-tokens` /
  `IMPROVED_WIKI_CONTEXT_TOKENS`; absent a verified value, use the explicit
  conservative default. See [budget/resume rules](references/context-budget.md).
- Keep Stage 2.2 serial with its rolling digest. Stage 2.4 generates once from
  the whole source. `--parallel` overlaps Phase 1 across books; Stage 2.3 onward
  remains one ordered write spine. No per-type page quotas or automatic creation
  of every mentioned term; schema semantics and source evidence govern pages.
- Missing caption, embedding, LLM, merge, schema, or required-media dependencies
  pause ingest after retries. Corrupt checkpoints may warn and re-derive.
  FILE repair and deterministic missing-source-summary recovery are explicit,
  logged paths described in the stage gates; never fabricate coverage.
- Ingest completes only after page-scoped embeddings, completion event/projections,
  and the matching `ingested` marker. History is append-only; current skip state
  is separate. [Time records](references/time-recording.md) owns this lifecycle.
- Retrieval may report a vector failure and continue keyword-only. Deep Research
  has a best-effort page upsert. Neither weakens ingest's mandatory embedding gate.
  Research saves one query page; it does not auto-ingest or edit aggregates.
- Review repair: read source evidence → snapshot declared `affected_pages` → fix
  within scope → validate/lint → guard finalize. Insufficient evidence or a failed
  check leaves the Review pending. Closing the queue does not prove full wiki health.
- Delete/dedup maintenance shares the ingest lock, rejects a reserved spine, and
  checks source ambiguity/page snapshots before mutation. Never delete live lock
  files. Keep scratch files under `/tmp/codex-work/<task>/`.

## Conversation handoffs

Exit `101` / `HANDOFF_PENDING` is an internal yield, not completion. For every
handoff, dispatch one fresh worker/subagent for exactly one self-contained prompt;
the main conversation orchestrates. Publish a complete `<stage>.txt.tmp`, validate,
then atomically rename to `<stage>.txt`. Stage 2.2 requires `qc_stage22.py` before
publication. Immediately resume the exact invocation with the same context budget.
Continue until all authorized sources exit `0`, the user pauses, or a real external
blocker prevents progress. Pending prompts and spine waiters are not final results.

## Scope and human gates

Existing explicit authorization satisfies a gate; do not reconfirm it.

- Batch ingest needs the complete ordered list and target project.
- Re-ingest/delete needs source identity and full redo versus `--keep-media`.
- Explicit research topics/Review choices are already authorized. Confirm only
  agent-proposed topics/queries before searching.
- Plain lint authorizes its default maintenance. Exit `102` requires confirmation
  for `--delete-orphans-only`, which generates previews/Reviews; actual orphan
  deletion requires separate authorization. See the Lint runbook.
- Review fixes cannot widen declared page scope without authorization.

Single-source ingest, diagnostic lint, validation, Graph, and save-chat have no
additional gate. Report missing capabilities rather than silently changing quality.

## Recovery and development

Use `ingest.py --batch-status`, `--pause-prefetch`, or `--pause-batch` to inspect
and pause. Resume via `--resume-prefetch`, or the same ordered source list plus
`--resume-batch`. Inspect partial writes before `--abandon-spine <hash>`.

The [script inventory and tests](references/scripts-reference.md) identifies module
owners. [Architecture decisions](references/architecture-decisions.md) records
rationale; [known issues](references/known-issues.md) and [roadmap](references/roadmap.md)
distinguish current limitations from planned features. Naming contracts live in
[naming conventions](references/naming-conventions.md), [raw naming](references/raw-naming-conventions.md),
[raw layouts](references/raw-layout-compat.md), and [Review names](references/review-file-naming.md).
