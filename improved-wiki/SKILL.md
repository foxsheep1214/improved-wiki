---
name: improved-wiki
description: "Class-level umbrella for the Karpathy/NashSU LLM-Wiki ingestion pipeline (three peer commands: Ingest, Lint, Graph). 17 active ingest Stages (Phase 0 included) across 4 Phases (0-3). Three modes: auto-ingest (batch), chat-ingest (interactive), deep-research (closed-loop web→wiki, 参考 NashSU deep-research.ts). Use when ingesting a PDF/PPTX/DOCX, researching a topic into the wiki, validating an ingest, debugging failed tasks, or auditing wiki completeness. All text-generation LLM work runs in conversation mode, the only path (no external API key) — the calling agent answers each prompt with the current conversation's model; in multi-book batch ingest, books run one at a time through the wiki-dependent stages (2.3+), and sub-agents may answer the parallel prompts only from the wiki-independent prefetch (Phase 0/1 + Stage 2.1/2.2) of other books. Phase 0 OCR uses local minerU (free); image captioning (Stage 1.3) uses a configurable VLM provider (via ~/.agents/config.json). Graph (the knowledge-graph command) is separate from lint — NashSU graph-view CLI parity, four-signal weighted graph + Louvain communities, deterministic (no LLM)."
tags: [ingest, mandatory, nashsu, pipeline, scan-pdf, mineru, local-ocr, knowledge-graph, louvain]
related_skills: [karpathy-llm-wiki, llm-wiki-local]
---

# improved-wiki

Karpathy LLM-Wiki pattern，参考 NashSU 最新版本。 Three peer commands: **Ingest** (17 active Stages across 4 Phases — Phase 0 included: 0 pre-processing → 1 extraction → 2 analysis/generation → 3 write & enrich), **Lint** (structural + semantic), **Graph** (knowledge graph — full rules under Key features). (No post-ingest validation phase — see "Standalone validation" under Key features.)

```
Phase 0: [0.1 raw-naming] → [0.2 source dedup]  (pre-processing gates)
Ingest: 1.1→1.2→1.3→2.1→2.2→2.3→2.4→2.6→2.7→2.9→3.1→3.2→3.4→3.5→3.7
        (execution order per _ingest_prepare.py::_do_prepare / _ingest_write.py::_do_write; 2.3 = existing-wiki association detection (between analysis and generation); 2.4 includes an in-source concept-dedup closing sub-step (former Stage 2.5); 2.6 = source page; 2.7 includes a cross-source query-resolution closing sub-step (former Stage 2.8); 3.4 = review, runs after 3.2 on already-written files; 3.7 = embeddings; same-slug collisions merged at 3.1 write)

Phase 0: Pre-processing gates  (raw naming, source dedup)
Phase 1: Extraction            (text extraction, image extract, caption)
Phase 2: Analysis & Generation (global digest, chunk analysis, concept/entity gen, queries, comparisons, quality review)
Phase 3: Write & Enrich        (file write + same-slug page-merge, image injection, review, aggregate repair, embeddings — last stage; sets completion marker)

Single-pass: 2.2 analyze ALL chunks → 2.3 associate (existing wiki) → 2.4 generate per-chunk, grounded (unified, all chunk counts)
Parallel (I/O only): 1.2→1.3∥2.1 image-pipeline∥digest + 1.3 per-image caption dispatch

Lint:  [structural] → [semantic (LLM, conversation mode)]
Graph: [Build Graph (4-signal)] → [Louvain communities] → [cohesion + gaps + clusters]
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

**Context-window probe (2026-06-27)**: at ingest start, one conversation round-trip asks the live model for its context window (`_context_probe.resolve_context`); the result drives all chunk/budget sizing via `Config.apply_context`, replacing the former `LLM_CONTEXT_SIZE` env convention. Budgets now adapt to whatever model the agent runs this session (chunk-token ceiling is 64K by default — 1M or 200K ctx → 64K-token chunks, a 128K model → ~42K; override via `IMPROVED_WIKI_TARGET_TOKENS_CEIL`). The probed value is cached per-model in `.llm-wiki/probed-context.json` (7-day TTL) so resumes and repeat ingests pay zero round-trips; a model change triggers exactly one probe. **No fallback**: an implausible probe response (outside [8K, 10M]) pauses the ingest rather than guessing. See `references/context-probe.md`.

Two other external-API dependencies (not text generation):
- **Stage 1.3 image captioning** → configurable VLM provider via `~/.agents/config.json` (one image per call with a context-aware prompt — NashSU `captionImage` parity). Supports anthropic and openai protocols (e.g. local Ollama models). Reads `caption_provider` from config; falls back to env `CAPTION_API_KEY` / `LLM_API_KEY`. **No fallback**: if no provider is configured or caption calls fail consecutively after retries, the ingest **pauses** (raises) — it never silently degrades to OCR figure-text (policy 2026-06-24).
- **Stage 3.7 embeddings** → mandatory (2026-06-21): defaults to local Ollama bge-m3 (`http://127.0.0.1:11434/v1`), no env var export required. **No fallback**: if Ollama isn't running, the model isn't pulled, or `lancedb` isn't installed, the ingest **pauses** (raises) — it never silently continues with keyword-only retrieval (policy 2026-06-24).

> **No-silent-fallback policy (2026-06-24)**: the ingest path allows NO silent fallback. If a main path cannot run (missing API key, missing service, LLM call failure after retries, broken config), the pipeline **warns and pauses** (raises `RuntimeError`) rather than degrading quality. Extraction/page writes are cached, so re-running after fixing the dependency resumes from the failed stage. This applies to: caption key missing, caption batch failure, embedding stack missing, LLM page-merge failure, broken `~/.agents/config.json`. (Corrupted cache/stage-progress files are the one exception: they warn loudly and reset, since re-ingesting is correct recovery, not quality degradation.)

## Entry points

- **Auto Ingest**: `python3 scripts/ingest.py file.pdf […]` — fully automated pipeline; the calling agent does each LLM step with the current model
- **Chat Ingest** ⭐: `/improved-wiki chat-ingest <file>` — interactive human-guided ingest (CLI revival of the interactive ingest NashSU later removed; a skill enhancement on the FILE-block write path, not current `executeIngestWrites` parity). See `references/chat-ingest.md`.
- **Deep Research** ⭐: `/improved-wiki deep-research <topic>` — closed-loop web→wiki research pipeline (参考 NashSU `deep-research.ts`). See `references/deep-research.md`.
- **Save Chat to Wiki** ⭐: say "保存到 wiki" after any conversation — captures chat insight as wiki page + auto-ingests (参考 NashSU `chat-save-to-wiki.ts`). See `references/save-chat-to-wiki.md`.
- **Review Sweep** ⭐: `/improved-wiki sweep-reviews` — auto-resolves review items satisfied by subsequent ingests (参考 NashSU `sweep-reviews.ts`). See `references/review-sweep.md`.
- **Graph** ⭐: `python3 scripts/graph.py` — build/refresh the knowledge graph (full rules under Key features).
- **Stage Checklist**: `references/ingest-stages-mandatory.md` — authoritative ingest-stage checklist with go/no-go gates.

## Reference map

**Pipeline core**:
- `references/ingest-stages-mandatory.md` — ingest stage checklist (Phase 0-3 + Lint + Graph, ⭐ easy-to-skip stages marked)
- `references/query-generation.md` — Stage 2.7: auto-generate `wiki/queries/`
- `references/comparison-generation.md` — Stage 2.9: auto-generate `wiki/comparisons/` (in-source concept pairs AND systematic multi-way 3+ comparisons)
- `references/dedup-design.md` — two dedup tiers: intra-source (Stage 2.4 closing sub-step, ingest-time) vs cross-source (CLI, lint-time); distinct responsibilities, not interchangeable
- `references/scanned-pdf-ocr-pipeline.md` — minerU local API extraction pipeline (all PDFs: text/scanned/mixed unified)
- `references/raw-naming-conventions.md` — raw 文件命名规范检查机制（项目级 `raw/NAMING.md` + auto-check）
- `references/chat-ingest.md` ⭐ — interactive human-guided ingest (CLI revival of interactive ingest removed in a later NashSU version)
- `references/deep-research.md` ⭐ — closed-loop web→wiki research pipeline (参考 NashSU deep-research.ts)
- `references/save-chat-to-wiki.md` ⭐ — save any conversation as wiki page + auto-ingest (参考 NashSU chat-save-to-wiki.ts)
- `references/review-sweep.md` ⭐ — auto-resolve review items satisfied by new ingests (参考 NashSU sweep-reviews.ts)
- `references/context-probe.md` — live context-window probe at ingest start (replaces `LLM_CONTEXT_SIZE` env); per-model cache, sanity gate, no-silent-fallback
- `references/delegate-mode.md` — **agent invocation** via `ingest.py`: how a calling agent (Claude Code/Hermes) answers each LLM step. Includes operational pitfalls: venv Python requirement, OCR timeout handling, wikilink merge task batching, re-ingest `--delete` pattern.
- `references/conversation-mode-agent-workflow.md` — concrete per-step prompt-file cheat sheet for a single-book ingest (Stage 2.1/2.2/2.4/2.7/2.9 prompt patterns, merge-loop subagent dispatch, reading extracted text). Companion to `delegate-mode.md` (concept) with the hands-on detail.
- `references/nashsu-search-architecture.md` — NashSU 源码实证：graph-relevance.ts（纯确定性 4 信号）+ search.rs（hybrid keyword+vector+RRF，远程 embedding API，无本地模型）。澄清 "NashSU parity" 在搜索侧的实际覆盖范围

**Conventions**:
- `references/naming-conventions.md` — file naming, frontmatter, wikilink, directory conventions (NashSU-aligned)
- `references/raw-layout-compat.md` — raw/ layout convention (type subdirs, nested, template mapping)
- `references/language-directive.md` — output-language policy (NashSU `outputLanguage` parity): auto-detect per source vs lock the whole KB, injection sites, proper-noun preservation

**Operations**:
- `references/scripts-reference.md` — full script inventory by category
- `references/kb-retrieval.md` — 4-step knowledge retrieval (search → read → cite → declare)
- `references/image-caption-strategy.md` — unified caption pipeline (minerU images, one VLM call per image with NashSU-style context-aware prompt), parallel dispatch, no-fallback (2026-06-24); includes VLM endpoint/retry pitfalls
- `references/known-issues.md` — current bugs and workarounds
- `references/roadmap.md` — planned enhancements not yet built (e.g. cross-source Synthesize command)
- `references/initial-setup.md` — first-time project bootstrap
- `references/batch-digest-loop.md` — batch ingest with resume + pitfalls (why `claude -p` cannot drive the 17-stage pipeline, source-page dedup, failure modes)
- `references/batch-parallel-prefetch.md` — batch_ingest 内部设计: Phase A 预取 / Phase B 串行 spine, 并行边界
- `references/re-ingest-comparison.md` — re-ingest a book to compare old vs new pipeline results (backup → delete → re-ingest → compare); authoritative `--delete` re-ingest flow
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
- **Chat ingest** ⭐ (CLI revival of interactive ingest — NashSU **removed** the entry point in a later version; skill enhancement, not current parity): `/improved-wiki chat-ingest <file>` — interactive two-step: Claude presents digest → you provide guidance → Claude generates guided wiki pages. Human relevance judgment in the loop. See `references/chat-ingest.md`.
- **Deep research** ⭐ (参考 NashSU `deep-research.ts`): `/improved-wiki deep-research <topic>` — closed-loop: web search → LLM synthesis → wiki query page → auto-ingest → entity/concept pages → new review items. Knowledge base grows itself. Verbatim synthesis + code-generated References; `tags: [research]`; one topic per invocation (no review-derived auto-chain). See `references/deep-research.md`.
- **Save chat to wiki** ⭐ (参考 NashSU `chat-save-to-wiki.ts`): say "保存到 wiki" after any conversation — cleans the assistant answer and writes it verbatim to `wiki/queries/<slug>-<date>-<HHMMSS>.md` (frontmatter `type/title/created/tags` only) + updates index/log + auto-ingests. Conversations become permanent knowledge. See `references/save-chat-to-wiki.md`.
- **Review sweep** ⭐ (参考 NashSU `sweep-reviews.ts`): `/improved-wiki sweep-reviews` — scans pending review items, auto-resolves those satisfied by subsequent ingests (rule-based pass, then a conversation-mode LLM judge; resolved pages are kept on disk). Details in `references/review-sweep.md`.
- **Batch ingest** (authoritative parallelism rule): `python3 scripts/ingest.py f1.pdf f2.pdf ...` — books are processed ONE AT A TIME through the wiki-write spine (Stage 2.3→write), each fully written before the next book's 2.3 so dedup/linking sees its pages; only the wiki-independent prefetch (Phase 0/1 + Stage 2.1/2.2) of other books runs in parallel. When prefetch produces multiple simultaneously-pending conversation prompts, one sub-agent per pending *prefetch* prompt may answer them concurrently. Never run two books' wiki-dependent stages (2.3+) in parallel. See `batch_ingest` Phase A (prefetch) / Phase B (serial spine) and `references/batch-parallel-prefetch.md`.
- **Graph** (separate command, peer of Ingest/Lint): `python3 scripts/graph.py` builds the knowledge graph (NashSU graph-view CLI parity — four-signal weighted graph + Louvain communities + cohesion + gaps + cluster hubs). Deterministic, no LLM. Run explicitly only — ingest/lint never auto-trigger it (NashSU-aligned: NashSU has no post-ingest graph rebuild). `--mode query --slug <s>` for read-only per-page wikilink suggestions (manual; not wired into any ingest stage).
- **Unified single-pass pipeline**: Stage 2.2 analyzes ALL chunks (accumulating context) → Stage 2.3 detects existing-wiki associations → Stage 2.4 generates pages (per-chunk, source-grounded; single-shot for ≤1 chunk). Works for all chunk counts (1 to N), with per-chunk checkpoints for crash recovery. Legacy multi-round synthesis retired.
- **Parallel I/O**: caption ∥ digest (Stage 1.3∥2.1), per-image caption dispatch (×12 workers). Pure I/O-bound parallelism only — no quality impact.
- **Heading path tracking** (NashSU parity): each chunk analysis prompt includes full heading hierarchy (`Chapter 3 > Section 3.2 > Subsec 3.2.1`)
- **Overlap context** (NashSU parity): paragraph/sentence-aware overlap text passed between chunks for continuity
- **Page merge**: three-layer merge on re-ingest — frontmatter array union + LLM body merge + locked fields. Sources field uses union-merge (preserves multi-source provenance).
- **CJK slug rewriting** (NashSU parity): auto-detects Chinese/Japanese/Korean titles and generates readable CJK slugs
- **PPTX/DOCX support** (NashSU parity): text extraction + image extraction from Office formats via stdlib zipfile
- **Aggregate repair safety** (NashSU parity): proportional size caps for index + overview, FILE block output filtering
- **Wikilink enrichment**: auto-adds `[[wikilinks]]` after page write (NashSU enrich-wikilinks parity)
- **Source lifecycle**: `--delete` removes source page + cache + orphan concepts/entities + media
- **Lint auto-fix**: `wiki-lint.sh --fix` repairs missing-frontmatter; `--fix-links` applies the suggestion engine's `suggested_target`/`suggested_source` (rewrites broken `[[wikilinks]]`, appends `## Related` links for orphan/no-outlinks); `--delete-orphans` previews an orphan cascade-delete (file + index entry + inbound `[[links]]` + `related:` refs — dry-run in lint; apply via `wiki-lint-fix.py --delete-orphans --apply`, NashSU `cascadeDeleteWikiPagesWithRefs` parity)
- **Semantic lint batching**: `wiki-lint-semantic.py` splits page summaries into 200-page batches (one conversation handoff each) so it scales to 7594-page wikis without blowing context; cross-batch findings are deduped
- **Queue watch**: `--watch --drain` daemon mode consuming `ingest-queue.json`
- **Standalone validation** (no longer auto-run): `validate_ingest.py` is a manual post-ingest check (the auto-run Stage 4.1 was removed for NashSU alignment). In-pipeline go/no-go gates still run per-stage via `_stage_validators.py` (`_verify_stage_1_1_text`, `_verify_stage_2_2_chunks`, etc.).
- **🔒 项目锁冲突诊断** (2026-07-04): 看到 `Could not acquire project lock — another ingest may be running`，**先查 `ps` 是不是有 OCR 后台在跑**，不要直接 kill 或抢锁。等 OCR 自然完成 `lock.release()` 后重跑同一本书的 `ingest.py`，从 Stage 2.1 续上。详见 `maintenance-cleanup.md`。
- **NashSU parity**: aligned with NashSU `ingest.ts` on heading path, overlap suffix, accumulating digest, CJK slug, PPTX/DOCX, sources union merge, schema routing, aggregate repair caps, page merge, wikilink enrichment, source lifecycle. Chunk pipeline is single-pass (analyze all chunks → associate → generate per-chunk, unified for all chunk counts)
- **Graph 四信号权重** (built-in): `graph.py` uses NashSU's four-signal model (direct link ×3.0, source overlap ×4.0, Adamic-Adar ×1.5, type affinity ×1.0) for weighted Louvain community detection
- **Per-page 语言门禁** (built-in): `ingest.py` Stage 3.2 detects body language per FILE block, warns on mismatch with expected source language (NashSU contentMatchesTargetLanguage parity)
- **Schema routing validation** (built-in): `ingest.py` validates `type:` frontmatter against file path directory, auto-corrects mismatches (NashSU validateWikiPageRouting parity)
- **Path safety validation** (built-in): `ingest.py` rejects FILE blocks with `..` segments, absolute paths, Windows-invalid names, and non-wiki/ destinations (NashSU isSafeIngestPath parity)
- **Local extraction**: minerU via a persistent local API server (`mineru.cli.fast_api`) + `/file_parse` per 50-page chunk (free, serial, one book at a time via `fcntl.flock`). All PDFs (text/scanned/mixed) take one unified path: backend=`hybrid-engine`, `parse_method=auto`, which routes per-page to txt or VLM OCR internally (garbled-font PDFs force `parse_method=ocr` → `mineru-api-ocr` label). `/file_parse` accepts a per-request `backend` Form field, but hybrid-engine is the verified default (pipeline loses inline-formula recall; the `mineru -b pipeline` CLI also still hits a 502 bug in 3.4.0). txt/md/pptx/docx bypass minerU.

## Scripts

Full script inventory: `references/scripts-reference.md`. Entry points (bold in the reference): `ingest.py`, `graph.py`, `wiki-lint.sh`, `build_embeddings.py`, `search_wiki.py`, `validate_ingest.py`, `cross_source_dedup.py`, `sweep_reviews.py`.

## Trigger this skill

**Auto Ingest**: User mentions wiki ingest / PDF OCR / batch ingest / validate-ingest / image caption / local minerU. Ingest runs in conversation mode (see "LLM execution model"); a single-book ingest is always serial. Batch parallelism rules: see "Batch ingest" under Key features. **Dedup rule**: before selecting any file, check `wiki/sources/<path>.md` exists. Never rely on `ingest-cache.json` for dedup.

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
