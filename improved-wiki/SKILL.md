---
name: improved-wiki
description: "Class-level umbrella for the Karpathy/NashSU LLM-Wiki ingestion pipeline (15-stage ingest + lint + graph). Three modes: auto-ingest (batch), chat-ingest (interactive), deep-research (closed-loop web→wiki, NashSU deep-research.ts parity). Use when ingesting a PDF/PPTX/DOCX, researching a topic into the wiki, validating an ingest, debugging failed tasks, or auditing wiki completeness. Phase 0 OCR uses local minerU (free). Lint includes knowledge graph + Louvain community detection."
tags: [ingest, mandatory, nashsu, pipeline, scan-pdf, mineru, local-ocr, knowledge-graph, louvain]
related_skills: [karpathy-llm-wiki, llm-wiki-local]
---

# improved-wiki

Karpathy LLM-Wiki pattern + NashSU v0.4.25 pipeline. 15 ingest Stages (4 Phases) + lint + graph (auto-triggered).

```
Ingest: 0→0.5→0.6→1→1.5→2.0→2.x→2.3→2.5→2.6→3.5→[4]

Phase 0: Pre-processing (text extraction, image extract, caption, pilot OCR)
Phase 1: Analysis        (global digest, sequential chunk analysis)
Phase 2: Generation      (source page, per-chunk concept/entity, queries, comparisons, review)
Phase 3: Write & Enrich  (file write, image injection, aggregate repair)
Phase 4: Embeddings      (auto-triggered if EMBEDDING_BASE_URL set)

Sequential (NashSU parity): 1.5 chunk analysis + 2.x per-chunk generation
Parallel (I/O only):        0.6∥1 caption∥digest + 0.6 caption batch dispatch

Lint:  [Build Graph] → [Louvain] → [Insights]
       (post-ingest auto-triggered via AUTO_BUILD_GRAPH=1, 30min staleness guard)
```

## Entry points

- **Auto Ingest**: `python3 scripts/ingest.py file.pdf […]` — fully automated pipeline
- **Chat Ingest** ⭐: `/improved-wiki chat-ingest <file>` — interactive human-guided ingest (NashSU `startIngest`/`executeIngestWrites` parity). See `references/chat-ingest.md`.
- **Deep Research** ⭐: `/improved-wiki deep-research <topic>` — closed-loop web→wiki research pipeline (NashSU `deep-research.ts` parity). See `references/deep-research.md`.
- **Save Chat to Wiki** ⭐: say "保存到 wiki" after any conversation — captures chat insight as wiki page + auto-ingests (NashSU `chat-save-to-wiki.ts` parity). See `references/save-chat-to-wiki.md`.
- **Review Sweep** ⭐: `/improved-wiki sweep-reviews` — auto-resolves review items satisfied by subsequent ingests (NashSU `sweep-reviews.ts` parity). See `references/review-sweep.md`.
- **Stage Checklist**: `references/ingest-stages-mandatory.md` — authoritative ingest-stage checklist with go/no-go gates.

## Reference map

**Pipeline core**:
- `references/ingest-stages-mandatory.md` — ingest stage checklist (Phase 0-4 + Lint, ⭐ easy-to-skip stages marked; knowledge graph section)
- `references/query-generation.md` — Stage 2.3: auto-generate `wiki/queries/`
- `references/comparison-generation.md` — Stage 2.5: auto-generate `wiki/comparisons/` (2.5A disambiguation, 2.5B in-source, 2.5C cross-source)
- `references/knowledge-gap-lint.md` — lint system: synthesis/finding/thesis/methodology formation triggers
- `references/scanned-pdf-ocr-pipeline.md` — minerU scanned PDF OCR pipeline (Path B)
- `references/raw-naming-conventions.md` — raw 文件命名规范检查机制（项目级 `raw/NAMING.md` + auto-check）
- `references/chat-ingest.md` ⭐ — interactive human-guided ingest (NashSU startIngest/executeIngestWrites parity)
- `references/deep-research.md` ⭐ — closed-loop web→wiki research pipeline (NashSU deep-research.ts parity)
- `references/save-chat-to-wiki.md` ⭐ — save any conversation as wiki page + auto-ingest (NashSU chat-save-to-wiki.ts parity)
- `references/review-sweep.md` ⭐ — auto-resolve review items satisfied by new ingests (NashSU sweep-reviews.ts parity)
- `references/conversation-mode.md` — direct LLM execution mode for ingest (manual step-by-step, no scripts)
- `references/delegate-mode.md` — agent orchestration for batch ingest

**Conventions**:
- `references/naming-conventions.md` — file naming, frontmatter, wikilink, directory conventions (NashSU-aligned)
- `references/domains.md` — domain classification for disambiguation
- `references/raw-layout-compat.md` — raw/ layout convention (type subdirs, nested, template mapping)

**Operations**:
- `references/kb-retrieval.md` — 4-step knowledge retrieval (search → read → cite → declare)
- `references/image-caption-strategy.md` — unified Path A+B caption pipeline, parallel dispatch, preprocessing (2026-06-17)
- `references/multimodal-vlm-pitfalls.md` — VLM pitfalls (caption collapse, OCR brittleness)
- `references/known-issues.md` — current bugs and workarounds
- `references/initial-setup.md` — first-time project bootstrap
- `references/batch-digest-loop.md` — batch ingest with resume
- `references/cron-installation.md` — cron-based automation
- `references/nashsu-lint-source-analysis.md` — NashSU lint.json internals
- `references/scripting-pitfalls.md` — Python + agent tool pitfalls

**Templates** (8 by file type):
`templates/digest-{book,paper,datasheet,applicationnote,designexample,presentation,standard,news}.md`

**Aggregate templates**:
`templates/{overview,schema,index,log,disambiguation}.md`

## Key features

- **Auto-ingest**: `python3 scripts/ingest.py file.pdf [file2.pdf ...]` — NashSU two-step: Stage 2.0 dedicated source page + Stage 2.x concept/entity generation
- **Chat ingest** ⭐ (NashSU v0.4.25 parity): `/improved-wiki chat-ingest <file>` — interactive two-step: Claude presents digest → you provide guidance → Claude generates guided wiki pages. Human relevance judgment in the loop. See `references/chat-ingest.md`.
- **Deep research** ⭐ (NashSU v0.4.25 parity): `/improved-wiki deep-research <topic>` — closed-loop: web search → LLM synthesis → wiki query page → auto-ingest → entity/concept pages → new review items. Knowledge base grows itself. See `references/deep-research.md`.
- **Save chat to wiki** ⭐ (NashSU v0.4.25 parity): say "保存到 wiki" after any conversation — captures insight as wiki page with `origin: chat-save` + auto-ingests. Conversations become permanent knowledge. See `references/save-chat-to-wiki.md`.
- **Review sweep** ⭐ (NashSU v0.4.25 parity): `/improved-wiki sweep-reviews` — scans pending review items, auto-resolves those satisfied by subsequent ingests (rule-based + LLM semantic judge). Keeps review backlog actionable. See `references/review-sweep.md`.
- **Batch ingest**: `python3 scripts/ingest.py f1.pdf f2.pdf ...` — parallel Stage 0-2 per book, serial Stage 3+ write
- **Knowledge graph**: `AUTO_BUILD_GRAPH=1` auto-rebuilds graph after ingest; manual: `python3 scripts/build_knowledge_graph.py`
- **Sequential Stage 1.5 + 2.x** (NashSU parity): chunk analysis (Stage 1.5) and per-chunk generation (Stage 2.x) run sequentially with accumulating context — each chunk builds on all previous chunks' discoveries. Per-chunk checkpoint for crash recovery.
- **Parallel I/O**: caption ∥ digest (Stage 0.6∥1), caption batch dispatch (×6 workers). Pure I/O-bound parallelism only — no quality impact.
- **Heading path tracking** (NashSU parity): each chunk analysis prompt includes full heading hierarchy (`Chapter 3 > Section 3.2 > Subsec 3.2.1`)
- **Overlap context** (NashSU parity): paragraph/sentence-aware overlap text passed between chunks for continuity
- **Page merge** (NashSU v0.4.25): three-layer merge on re-ingest — frontmatter array union + LLM body merge + locked fields. Sources field uses union-merge (preserves multi-source provenance).
- **CJK slug rewriting** (NashSU parity): auto-detects Chinese/Japanese/Korean titles and generates readable CJK slugs
- **PPTX/DOCX support** (NashSU parity): text extraction + image extraction from Office formats via stdlib zipfile
- **Schema routing validation** (NashSU parity): validates frontmatter `type:` against file path directory, auto-corrects mismatches
- **Aggregate repair safety** (NashSU parity): proportional size caps for index + overview, FILE block output filtering
- **Wikilink enrichment**: auto-adds `[[wikilinks]]` after page write (NashSU enrich-wikilinks parity)
- **Source lifecycle**: `--delete` removes source page + cache + orphan concepts/entities + media
- **Lint auto-fix**: `wiki-lint.sh --fix` repairs missing-domain and missing-frontmatter
- **Queue watch**: `--watch --drain` daemon mode consuming `ingest-queue.json`
- **Auto-validation**: `validate_ingest.py` runs at end of every ingest; per-stage gate functions (`_verify_stage_0_text`, `_verify_stage_1_5_chunks`, etc.)
- **NashSU parity**: aligned with `ingest.ts` v0.4.25 (deep research closed-loop, chat ingest startIngest/executeIngestWrites, review sweep auto-resolution, save-chat-to-wiki, sequential chunk gen, heading path, overlap suffix, CJK slug, PPTX/DOCX, sources union merge, schema routing, aggregate repair caps, page merge, wikilink enrichment, source lifecycle)
- **Graph 四信号权重** (built-in): `build_knowledge_graph.py` uses NashSU's four-signal model (direct link ×3.0, source overlap ×4.0, Adamic-Adar ×1.5, type affinity ×1.0) for weighted Louvain community detection
- **Per-page 语言门禁** (built-in): `ingest.py` Stage 3.5 detects body language per FILE block, warns on mismatch with expected source language (NashSU contentMatchesTargetLanguage parity)
- **Schema routing validation** (built-in): `ingest.py` validates `type:` frontmatter against file path directory, auto-corrects mismatches (NashSU validateWikiPageRouting parity)
- **Path safety validation** (built-in): `ingest.py` rejects FILE blocks with `..` segments, absolute paths, Windows-invalid names, and non-wiki/ destinations (NashSU isSafeIngestPath parity)
- **Local OCR**: minerU VLM via `~/.venv/bin/mineru -b vlm-auto-engine` (free, serial, `MINERU_MAX_CONCURRENT=1`)

## Scripts

| Category | Scripts |
|----------|---------|
| Core | `ingest.py`, `_core.py`, `_llm_api.py`, `_paths.py`, `_language.py`, `_frontmatter.py` |
| Stage Modules | `_stage_0_extract.py`, `_stage_1_analyze.py`, `_stage_2_generate.py`, `_stage_3_write.py` |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | `wiki-lint.sh`, `wiki-lint-semantic.py`, `build_knowledge_graph.py`, `validate_ingest.py`, `validate-frontmatter.sh`, `normalize_raw_names.py` |
| Queue | `wiki-monitor.sh`, `run-queue.sh` |
| Embeddings | `build_embeddings.py`, `search_wiki.py` |
| Repair | `repair_wiki.py`, `repair_stage_38.py`, `reingest_batch.py`, `sweep_reviews.py` |

## Trigger this skill

**Auto Ingest**: User mentions wiki ingest / PDF OCR / minimax batch / validate-ingest / image caption / local minerU / pilot OCR. **Dedup rule**: before selecting any file, check `wiki/sources/<path>.md` exists. Never rely on `ingest-cache.json` for dedup.

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
