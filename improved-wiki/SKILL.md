---
name: improved-wiki
description: "Class-level umbrella for the Karpathy/NashSU LLM-Wiki ingestion pipeline (three peer commands: Ingest, Lint, Graph). 18 active ingest Stages (Phase 0 included) across 5 Phases (0-4). Three modes: auto-ingest (batch), chat-ingest (interactive), deep-research (closed-loop web→wiki, NashSU deep-research.ts parity). Use when ingesting a PDF/PPTX/DOCX, researching a topic into the wiki, validating an ingest, debugging failed tasks, or auditing wiki completeness. All text-generation LLM work runs in conversation mode, the only path (no external API key) — the calling agent answers each prompt with the current conversation's model; in multi-book batch ingest, books run one at a time through the wiki-dependent stages (2.3+), and sub-agents may answer the parallel prompts only from the wiki-independent prefetch (Phase 0/1 + Stage 2.1/2.2) of other books. Phase 0 OCR uses local minerU (free); image captioning (Stage 1.3) is the one exception and calls MiniMax VLM. Graph (the knowledge-graph command) is separate from lint — NashSU graph-view CLI parity, four-signal weighted graph + Louvain communities, deterministic (no LLM)."
tags: [ingest, mandatory, nashsu, pipeline, scan-pdf, mineru, local-ocr, knowledge-graph, louvain]
related_skills: [karpathy-llm-wiki, llm-wiki-local]
---

# improved-wiki

Karpathy LLM-Wiki pattern + NashSU v0.4.25 pipeline. Three peer commands: **Ingest** (18 active Stages across 5 Phases — Phase 0 included: 0 pre-processing → 1 extraction → 2 analysis/generation → 3 write & enrich → 4 validation), **Lint** (structural + semantic), **Graph** (knowledge graph — separate from lint, run explicitly; never auto-triggered by ingest/lint, NashSU-aligned).

```
Phase 0: [0.1 raw-naming] → [0.2 source dedup]  (pre-processing gates)
Ingest: 1.1→1.2→1.3→2.1→2.2→2.3→2.4→2.5→2.6→2.7→2.8→2.9→3.1→3.2→3.4→3.5→3.7→4.1
        (execution order per _ingest_prepare.py::_do_prepare / _ingest_write.py::_do_write; 2.3 = existing-wiki association detection (between analysis and generation); 2.6 = source page; 3.4 = review, runs after 3.2 on already-written files; 3.7 = embeddings; same-slug collisions merged at 3.1 write)

Phase 0: Pre-processing gates  (raw naming, source dedup)
Phase 1: Extraction            (text extraction, image extract, caption)
Phase 2: Analysis & Generation (global digest, chunk analysis, concept/entity gen, queries, comparisons, quality review)
Phase 3: Write & Enrich        (file write + same-slug page-merge, image injection, review, aggregate repair, embeddings)
Phase 4: Validation            (Stage 4.1: validate_ingest — auto-runs at end of every ingest)

Single-pass: 2.2 analyze ALL chunks → 2.3 associate (existing wiki) → 2.4 generate per-chunk, grounded (unified, all chunk counts)
Parallel (I/O only): 1.2→1.3∥2.1 image-pipeline∥digest + 1.3 per-image caption dispatch

Lint:  [structural] → [semantic (LLM, conversation mode)]
Graph: [Build Graph (4-signal)] → [Louvain communities] → [cohesion + gaps + clusters]
       (separate command from lint; run explicitly via `python3 scripts/graph.py` — never auto-triggered by ingest/lint, NashSU-aligned; deterministic — no LLM)
```

## LLM execution model

Text generation has exactly one path, routed by
`_llm_api.call_anthropic_protocol`:

- **Conversation mode** (the only mode, no flag needed) — `ingest.py` writes a prompt
  file at each LLM step and hands off (exit 101); the calling agent (the current
  Claude Code conversation) answers with the current model and re-invokes with
  identical args (see `references/delegate-mode.md`). Serial only — each call exits
  the process. Wikilink enrichment also routes through this path now (batched: one
  round-trip per ingest covering every page written, not one per page).

There is no direct-API text-gen path: this skill only runs from a CLI session
with an agent present, so a separate paid text-gen API key has no use case.

**Context-window probe (2026-06-27)**: at ingest start, one conversation round-trip asks the live model for its context window (`_context_probe.resolve_context`); the result drives all chunk/budget sizing via `Config.apply_context`, replacing the former `LLM_CONTEXT_SIZE` env convention. Budgets now adapt to whatever model the agent runs this session (GLM-5.2 1M → 192K-token chunks; a 200K model → 66K). The probed value is cached per-model in `.llm-wiki/probed-context.json` (7-day TTL) so resumes and repeat ingests pay zero round-trips; a model change triggers exactly one probe. **No fallback**: an implausible probe response (outside [8K, 10M]) pauses the ingest rather than guessing. See `references/context-probe.md`.

Two other external-API dependencies (not text generation):
- **Stage 1.3 image captioning** → MiniMax VLM (`anthropic/v1/messages`, one image per call with a context-aware prompt — NashSU `captionImage` parity). This is the only MiniMax dependency; it needs `MINIMAX_CN_API_KEY` / `LLM_API_KEY` for the caption endpoint only. **No fallback**: if the key is missing or caption calls fail consecutively after retries, the ingest **pauses** (raises) — it never silently degrades to OCR figure-text (policy 2026-06-24).
- **Stage 3.7 embeddings** → mandatory (2026-06-21): defaults to local Ollama bge-m3 (`http://127.0.0.1:11434/v1`), no env var export required. **No fallback**: if Ollama isn't running, the model isn't pulled, or `lancedb` isn't installed, the ingest **pauses** (raises) — it never silently continues with keyword-only retrieval (policy 2026-06-24). Not routed through MiniMax.

> **No-silent-fallback policy (2026-06-24)**: the ingest path allows NO silent fallback. If a main path cannot run (missing API key, missing service, LLM call failure after retries, broken config), the pipeline **warns and pauses** (raises `RuntimeError`) rather than degrading quality. Extraction/page writes are cached, so re-running after fixing the dependency resumes from the failed stage. This applies to: caption key missing, caption batch failure, embedding stack missing, LLM page-merge failure, broken `~/.agents/config.json`. (Corrupted cache/stage-progress files are the one exception: they warn loudly and reset, since re-ingesting is correct recovery, not quality degradation.)

## Entry points

- **Auto Ingest**: `python3 scripts/ingest.py file.pdf […]` — fully automated pipeline; the calling agent does each LLM step with the current model
- **Chat Ingest** ⭐: `/improved-wiki chat-ingest <file>` — interactive human-guided ingest (NashSU `startIngest`/`executeIngestWrites` parity). See `references/chat-ingest.md`.
- **Deep Research** ⭐: `/improved-wiki deep-research <topic>` — closed-loop web→wiki research pipeline (NashSU `deep-research.ts` parity). See `references/deep-research.md`.
- **Save Chat to Wiki** ⭐: say "保存到 wiki" after any conversation — captures chat insight as wiki page + auto-ingests (NashSU `chat-save-to-wiki.ts` parity). See `references/save-chat-to-wiki.md`.
- **Review Sweep** ⭐: `/improved-wiki sweep-reviews` — auto-resolves review items satisfied by subsequent ingests (NashSU `sweep-reviews.ts` parity). See `references/review-sweep.md`.
- **Graph** ⭐: `python3 scripts/graph.py` — build/refresh the knowledge graph. Peer of Ingest/Lint, **not** part of lint. NashSU graph-view CLI parity: four-signal weighted graph + Louvain communities + cohesion + gaps + cluster hubs. Deterministic (no LLM). `--mode query --slug <s>` for read-only per-page suggestions.
- **Stage Checklist**: `references/ingest-stages-mandatory.md` — authoritative ingest-stage checklist with go/no-go gates.

## Reference map

**Pipeline core**:
- `references/ingest-stages-mandatory.md` — ingest stage checklist (Phase 0-4 + Lint + Graph, ⭐ easy-to-skip stages marked)
- `references/query-generation.md` — Stage 2.7: auto-generate `wiki/queries/`
- `references/comparison-generation.md` — Stage 2.9: auto-generate `wiki/comparisons/` (in-source concept pairs AND systematic multi-way 3+ comparisons)
- `references/knowledge-gap-lint.md` — lint system: synthesis/finding/thesis/methodology formation triggers
- `references/dedup-design.md` — two dedup tiers: intra-source (Stage 2.5, ingest-time) vs cross-source (CLI, lint-time); distinct responsibilities, not interchangeable
- `references/scanned-pdf-ocr-pipeline.md` — minerU local API extraction pipeline (all PDFs: text/scanned/mixed unified)
- `references/raw-naming-conventions.md` — raw 文件命名规范检查机制（项目级 `raw/NAMING.md` + auto-check）
- `references/chat-ingest.md` ⭐ — interactive human-guided ingest (NashSU startIngest/executeIngestWrites parity)
- `references/deep-research.md` ⭐ — closed-loop web→wiki research pipeline (NashSU deep-research.ts parity)
- `references/save-chat-to-wiki.md` ⭐ — save any conversation as wiki page + auto-ingest (NashSU chat-save-to-wiki.ts parity)
- `references/review-sweep.md` ⭐ — auto-resolve review items satisfied by new ingests (NashSU sweep-reviews.ts parity)
- `references/conversation-mode.md` — **conversation mode** (the only mode): the current conversation does each text-gen LLM step with its own model (serial, prompt-file handoff)
- `references/context-probe.md` — live context-window probe at ingest start (replaces `LLM_CONTEXT_SIZE` env); per-model cache, sanity gate, no-silent-fallback
- `references/delegate-mode.md` — **agent invocation** via `ingest.py`: how a calling agent (Claude Code/Hermes) answers each LLM step. Includes operational pitfalls: venv Python requirement, OCR timeout handling, wikilink merge task batching, re-ingest `--delete` pattern.
- `references/conversation-mode-agent-workflow.md` — concrete per-step prompt-file cheat sheet for a single-book ingest (Stage 2.1/2.2/2.4/2.7/2.9 prompt patterns, merge-loop subagent dispatch, reading extracted text). Companion to `delegate-mode.md` (concept) with the hands-on detail.
- `references/nashsu-search-architecture.md` — NashSU 源码实证：graph-relevance.ts（纯确定性 4 信号）+ search.rs（hybrid keyword+vector+RRF，远程 embedding API，无本地模型）。澄清 "NashSU parity" 在搜索侧的实际覆盖范围

**Conventions**:
- `references/naming-conventions.md` — file naming, frontmatter, wikilink, directory conventions (NashSU-aligned)
- `references/domains.md` — domain classification for graph partitioning / query routing
- `references/raw-layout-compat.md` — raw/ layout convention (type subdirs, nested, template mapping)

**Operations**:
- `references/kb-retrieval.md` — 4-step knowledge retrieval (search → read → cite → declare)
- `references/image-caption-strategy.md` — unified caption pipeline (minerU images, one VLM call per image with NashSU-style context-aware prompt), parallel dispatch, no-fallback (2026-06-24)
- `references/multimodal-vlm-pitfalls.md` — VLM pitfalls (caption collapse, OCR brittleness)
- `references/known-issues.md` — current bugs and workarounds
- `references/initial-setup.md` — first-time project bootstrap
- `references/batch-digest-loop.md` — batch ingest with resume
- `references/batch-digest-patterns.md` — batch ingest pitfalls: why `claude -p` cannot drive the 20-stage pipeline, Python-loop + `wiki/sources/<stem>.md` dedup pattern
- `references/re-ingest-comparison.md` — re-ingest a book to compare old vs new pipeline results (backup → delete → re-ingest → compare)
- `references/maintenance-cleanup.md` — periodic cleanup of stale files (`.digested`, temp dirs, `.DS_Store`, empty-slug `.md` bug residual)
- `references/cron-installation.md` — cron-based automation
- `references/mineru-version-tracking.md` — pinned minerU pip version + VLM model, upgrade notes
- `references/nashsu-lint-source-analysis.md` — NashSU lint.json internals
- `references/scripting-pitfalls.md` — Python + agent tool pitfalls

**Templates** (8 by file type):
`templates/digest-{book,paper,datasheet,applicationnote,designexample,presentation,standard,news}.md`

**Aggregate templates**:
`templates/{overview,schema,index,log}.md`

## Key features

- **Auto-ingest**: `python3 scripts/ingest.py file.pdf [file2.pdf ...]` — NashSU Step 2 parity: Stage 2.4 generation produces concept/entity pages (per-chunk) + source page (from digest). LLM steps run in conversation mode (current model).
- **Chat ingest** ⭐ (NashSU v0.4.25 parity): `/improved-wiki chat-ingest <file>` — interactive two-step: Claude presents digest → you provide guidance → Claude generates guided wiki pages. Human relevance judgment in the loop. See `references/chat-ingest.md`.
- **Deep research** ⭐ (NashSU v0.4.25 parity): `/improved-wiki deep-research <topic>` — closed-loop: web search → LLM synthesis → wiki query page → auto-ingest → entity/concept pages → new review items. Knowledge base grows itself. See `references/deep-research.md`.
- **Save chat to wiki** ⭐ (NashSU v0.4.25 parity): say "保存到 wiki" after any conversation — captures insight as wiki page with `origin: chat-save` + auto-ingests. Conversations become permanent knowledge. See `references/save-chat-to-wiki.md`.
- **Review sweep** ⭐ (NashSU v0.4.25 parity): `/improved-wiki sweep-reviews` — scans pending review items, auto-resolves those satisfied by subsequent ingests (rule-based + LLM semantic judge). Keeps review backlog actionable. See `references/review-sweep.md`.
- **Batch ingest**: `python3 scripts/ingest.py f1.pdf f2.pdf ...` — books are processed ONE AT A TIME through the wiki-write spine (Stage 2.3→write); only the wiki-independent prefetch (Stage 0/1/2.1/2.2) of other books runs in parallel. Cross-book parallelism of wiki-dependent stages is NOT allowed (it makes 2.3 dedup/linking blind to sibling books). See `batch_ingest` Phase A (prefetch) / Phase B (serial spine).
- **Graph** (separate command, peer of Ingest/Lint): `python3 scripts/graph.py` builds the knowledge graph (NashSU graph-view CLI parity — four-signal weighted graph + Louvain communities + cohesion + gaps + cluster hubs). Deterministic, no LLM. Run explicitly only — ingest/lint never auto-trigger it (NashSU-aligned: NashSU has no post-ingest graph rebuild). `--mode query --slug <s>` for read-only per-page wikilink suggestions (manual; not wired into any ingest stage).
- **Unified single-pass pipeline**: Stage 2.2 analyzes ALL chunks (accumulating context) → Stage 2.3 detects existing-wiki associations → Stage 2.4 generates pages (per-chunk, source-grounded; single-shot for ≤1 chunk). Works for all chunk counts (1 to N), with per-chunk checkpoints for crash recovery. Legacy multi-round synthesis retired.
- **Parallel I/O**: caption ∥ digest (Stage 1.3∥2.1), per-image caption dispatch (×12 workers). Pure I/O-bound parallelism only — no quality impact.
- **Heading path tracking** (NashSU parity): each chunk analysis prompt includes full heading hierarchy (`Chapter 3 > Section 3.2 > Subsec 3.2.1`)
- **Overlap context** (NashSU parity): paragraph/sentence-aware overlap text passed between chunks for continuity
- **Page merge** (NashSU v0.4.25): three-layer merge on re-ingest — frontmatter array union + LLM body merge + locked fields. Sources field uses union-merge (preserves multi-source provenance).
- **CJK slug rewriting** (NashSU parity): auto-detects Chinese/Japanese/Korean titles and generates readable CJK slugs
- **PPTX/DOCX support** (NashSU parity): text extraction + image extraction from Office formats via stdlib zipfile
- **Schema routing validation** (NashSU parity): validates frontmatter `type:` against file path directory, auto-corrects mismatches
- **Aggregate repair safety** (NashSU parity): proportional size caps for index + overview, FILE block output filtering
- **Wikilink enrichment**: auto-adds `[[wikilinks]]` after page write (NashSU enrich-wikilinks parity)
- **Source lifecycle**: `--delete` removes source page + cache + orphan concepts/entities + media
- **Lint auto-fix**: `wiki-lint.sh --fix` repairs missing-domain and missing-frontmatter; `--fix-links` applies the suggestion engine's `suggested_target`/`suggested_source` (rewrites broken `[[wikilinks]]`, appends `## Related` links for orphan/no-outlinks)
- **Project-level domains**: `wiki-lint.sh` reads the valid-domain set from `<project>/wiki/domains.md` (override) or the skill default — not hardcoded, so RadarWiki / 自然科学知识库 use their own domains without spurious `invalid-domain` findings
- **Semantic lint batching**: `wiki-lint-semantic.py` splits page summaries into 200-page batches (one conversation handoff each) so it scales to 7594-page wikis without blowing context; cross-batch findings are deduped
- **Queue watch**: `--watch --drain` daemon mode consuming `ingest-queue.json`
- **Auto-validation**: `validate_ingest.py` runs at end of every ingest; per-stage gate functions (`_verify_stage_1_1_text`, `_verify_stage_2_2_chunks`, etc.)
- **NashSU parity**: aligned with `ingest.ts` v0.4.25 on heading path, overlap suffix, accumulating digest, CJK slug, PPTX/DOCX, sources union merge, schema routing, aggregate repair caps, page merge, wikilink enrichment, source lifecycle. Chunk pipeline is single-pass (analyze all chunks → associate → generate per-chunk, unified for all chunk counts)
- **Graph 四信号权重** (built-in): `graph.py` uses NashSU's four-signal model (direct link ×3.0, source overlap ×4.0, Adamic-Adar ×1.5, type affinity ×1.0) for weighted Louvain community detection
- **Per-page 语言门禁** (built-in): `ingest.py` Stage 3.2 detects body language per FILE block, warns on mismatch with expected source language (NashSU contentMatchesTargetLanguage parity)
- **Schema routing validation** (built-in): `ingest.py` validates `type:` frontmatter against file path directory, auto-corrects mismatches (NashSU validateWikiPageRouting parity)
- **Path safety validation** (built-in): `ingest.py` rejects FILE blocks with `..` segments, absolute paths, Windows-invalid names, and non-wiki/ destinations (NashSU isSafeIngestPath parity)
- **Local extraction**: minerU via a persistent local API server (`mineru.cli.fast_api`) + `/file_parse` per 50-page chunk (free, serial, one book at a time via `fcntl.flock`). All PDFs (text/scanned/mixed) take one unified path: backend=`hybrid-engine`, `parse_method=auto`, which routes per-page to txt or VLM OCR internally (garbled-font PDFs force `parse_method=ocr` → `mineru-api-ocr` label). `/file_parse` accepts a per-request `backend` Form field, but hybrid-engine is the verified default (pipeline loses inline-formula recall; the `mineru -b pipeline` CLI also still hits a 502 bug in 3.4.0). txt/md/pptx/docx bypass minerU.

## Scripts

| Category | Scripts |
|----------|---------|
| Core | `ingest.py`, `_core.py`, `_llm_api.py`, `_paths.py`, `_language.py`, `_frontmatter.py` |
| Stage Modules (Phase 0-4) | `_stage_1_extract.py` (1.1 facade → `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py`), `_stage_2_analyze.py` (2.1-2.2), `_stage_2_3_incremental.py` (2.4 sub-step: association verify), `_stage_2_4_generation.py` (2.4), `_stage_2_5_dedup.py` (2.5), `_stage_2_6_source_page.py` (2.4 sub-step: source page), `_stage_2_7_query_generation.py` (2.7), `_stage_2_8_query_resolve.py` (2.8), `_stage_2_9_comparison.py` (2.9), `_stage_3_4_review.py` (3.4), `_stage_2_base.py` (公共导入), `_stage_3_write.py` (3.1 incl. page-merge, 3.5), `_stage_3_2_inject_images.py` (3.2), `_stage_3_7_embed.py` (3.7), `_stage_4_1_validate.py` (4.1), `_stage_validators.py` (Stage 0 验证门 + StageValidationError) |
| Ingest orchestrator splits | `ingest.py` (CLI + `ingest_one`/`batch_ingest`) → `_ingest_skip.py` (Stage 0.2 去重/skip), `_ingest_chunks.py` (chunk 流水线), `_ingest_prepare.py` (综合/source page), `_ingest_write.py` (写盘 + post-ingest) |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | `wiki-lint.sh`, `wiki-lint-semantic.py`, `validate_ingest.py`, `validate-frontmatter.sh`, `normalize_raw_names.py` |
| Graph | `graph.py` (NashSU graph-view CLI parity; four-signal + Louvain; deterministic, no LLM) |
| Queue | `wiki-monitor.sh`, `run-queue.sh` |
| Embeddings | `build_embeddings.py`, `search_wiki.py` |
| Repair | `repair_wiki.py`, `repair_stage_3_2.py`, `reingest_batch.py`, `sweep_reviews.py` |

## Trigger this skill

**Auto Ingest**: User mentions wiki ingest / PDF OCR / batch ingest / validate-ingest / image caption / local minerU. Ingest runs in conversation mode — the current conversation's model does all text-generation LLM work; only image captioning calls MiniMax. A single-book ingest is always serial (no parallel LLM steps). **Batch ingest of multiple books** (`--parallel`/multiple files) processes books ONE AT A TIME through the wiki-dependent spine (Stage 2.3 → write), each fully written before the next book's 2.3 so dedup/linking sees its pages. ONLY the wiki-independent prefetch (Phase 0/1 + Stage 2.1/2.2) of other books may run concurrently — when prefetch produces more than one simultaneously-pending conversation prompt, you MAY spawn one sub-agent per pending prompt to answer those *prefetch* prompts concurrently. Never run two books' wiki-dependent stages (2.3+) in parallel. **Dedup rule**: before selecting any file, check `wiki/sources/<path>.md` exists. Never rely on `ingest-cache.json` for dedup.

**Chat Ingest** ⭐: User mentions chat ingest / interactive ingest / 交互消化 / 对话消化 / 人工引导消化 / 重点消化. User provides a source file and wants to discuss it before generating wiki pages. See `references/chat-ingest.md`.

**Deep Research** ⭐: User mentions deep research / 深度研究 / 研究并消化 / research and ingest / 调查并写入 wiki / 补充知识空缺. User provides a topic (not a source file) and wants web research synthesized into wiki pages via auto-ingest. Also triggered when wiki can't answer a question, or from review items / lint gaps. See `references/deep-research.md`.

**Save Chat to Wiki** ⭐: User says 保存到 wiki / save to wiki / 记住这个 / add to wiki / wiki this after a substantive conversation. Captures chat insight as wiki page + auto-ingests. See `references/save-chat-to-wiki.md`.

**Review Sweep** ⭐: User says sweep reviews / 清理 review / 扫 review / auto-resolve reviews. Triggered after batch ingests to clear stale review items. See `references/review-sweep.md`.

**Retrieval**: User asks to search wiki / cite knowledge base / query technical content. See `references/kb-retrieval.md`.

## Projects

| Project | Path |
|---------|------|
| HardwareWiki | `~/Documents/知识库/HardwareWiki` |
| RadarWiki | `~/Documents/知识库/RadarWiki` |
| 自然科学知识库 | `~/Documents/知识库/自然科学知识库` |
