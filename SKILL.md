---
name: improved-wiki
description: "Class-level umbrella for the Karpathy/NashSU LLM-Wiki ingestion pipeline (three peer commands: Ingest, Lint, Graph). ~13 numbered ingest stages + 2 pre-gates. Three modes: auto-ingest (batch), chat-ingest (interactive), deep-research (closed-loop web→wiki, NashSU deep-research.ts parity). Use when ingesting a PDF/PPTX/DOCX, researching a topic into the wiki, validating an ingest, debugging failed tasks, or auditing wiki completeness. All text-generation LLM work runs in conversation mode (the calling agent spawns sub-agents using the current conversation's model — no external API key). Phase 0 OCR uses local minerU (free); image captioning (Stage 1.3) is the one exception and calls MiniMax VLM. Graph (the knowledge-graph command) is separate from lint — NashSU graph-view CLI parity, four-signal weighted graph + Louvain communities, deterministic (no LLM)."
tags: [ingest, mandatory, nashsu, pipeline, scan-pdf, mineru, local-ocr, knowledge-graph, louvain]
related_skills: [karpathy-llm-wiki, llm-wiki-local]
---

# improved-wiki

Karpathy LLM-Wiki pattern + NashSU v0.4.25 pipeline. Three peer commands: **Ingest** (~13 numbered Stages in 4 Phases + 2 pre-gates 0.1 dedup / 0.3 pilot OCR), **Lint** (structural + semantic), **Graph** (knowledge graph — separate from lint). Graph auto-triggers post-ingest behind `AUTO_BUILD_GRAPH=1`.

```
Pre-gates: [0.1 raw-naming] → [0.2 source dedup] → [0.3 pilot OCR]   (gate ingest; not in the numbered data-flow below)
Ingest: 1.1→1.2→1.3→2.1→2.2→2.3→2.4→2.5→2.6→3.1→3.2→3.3→3.4→[3.5]→4.1
        (numbered stages per ingest.py; "2.x" = per-chunk concept/entity gen; "3" = file write)

Phase 0: Pre-processing (text extraction, image extract, caption, pilot OCR)
Phase 1: Analysis        (global digest, sequential chunk analysis)
Phase 2: Generation      (source page, per-chunk concept/entity, queries, comparisons, review)
Phase 3: Write & Enrich  (file write, image injection, aggregate repair)
Phase 4: Embeddings      (auto-triggered if EMBEDDING_BASE_URL set)

Barrier-free: 1.5∥2.1 analyze→generate per chunk (unified, all chunk counts)
Parallel (I/O only): 0.6∥1 caption∥digest + 0.6 caption batch dispatch

Lint:  [structural] → [semantic (LLM, conversation mode)]
Graph: [Build Graph (4-signal)] → [Louvain communities] → [cohesion + gaps + clusters]
       (separate command from lint; post-ingest auto-triggered via AUTO_BUILD_GRAPH=1, 30min staleness guard; deterministic — no LLM)
```

## LLM execution model

Text generation has two paths (round iii, 2026-06-21), routed by
`_llm_api.call_anthropic_protocol`:

- **Direct API** (default, no `--conversation`) — `call_anthropic_direct` makes a
  real HTTP call to the configured provider (OpenAI or Anthropic protocol). Fast,
  parallelizable (the only path that can run concurrent chunk analysis). Needs
  `LLM_API_KEY`. **Wikilink enrichment uses this path unconditionally** (high-volume,
  low-value-per-call), even when the rest of the pipeline runs in conversation mode.
- **Conversation mode** (`--conversation`) — `ingest.py` writes a prompt file at each
  LLM step and hands off (exit 101); the calling agent (the current Claude Code
  conversation) answers with the current model and re-invokes
  (see `references/delegate-mode.md`). Serial only — each call exits the process.

Two other external-API dependencies (not text generation):
- **Stage 1.3 image captioning** → MiniMax VLM (`anthropic/v1/messages` multi-image batch). This is the only MiniMax dependency; it needs `MINIMAX_CN_API_KEY` / `LLM_API_KEY` for the caption endpoint only.
- **Stage 3.5 embeddings** → optional; configured separately if you want vector retrieval. Not routed through MiniMax.

## Entry points

- **Auto Ingest**: `python3 scripts/ingest.py file.pdf […] --conversation` — fully automated pipeline; the calling agent does each LLM step with the current model
- **Chat Ingest** ⭐: `/improved-wiki chat-ingest <file>` — interactive human-guided ingest (NashSU `startIngest`/`executeIngestWrites` parity). See `references/chat-ingest.md`.
- **Deep Research** ⭐: `/improved-wiki deep-research <topic>` — closed-loop web→wiki research pipeline (NashSU `deep-research.ts` parity). See `references/deep-research.md`.
- **Save Chat to Wiki** ⭐: say "保存到 wiki" after any conversation — captures chat insight as wiki page + auto-ingests (NashSU `chat-save-to-wiki.ts` parity). See `references/save-chat-to-wiki.md`.
- **Review Sweep** ⭐: `/improved-wiki sweep-reviews` — auto-resolves review items satisfied by subsequent ingests (NashSU `sweep-reviews.ts` parity). See `references/review-sweep.md`.
- **Graph** ⭐: `python3 scripts/graph.py` — build/refresh the knowledge graph. Peer of Ingest/Lint, **not** part of lint. NashSU graph-view CLI parity: four-signal weighted graph + Louvain communities + cohesion + gaps + cluster hubs. Deterministic (no LLM). `--mode query --slug <s>` for read-only per-page suggestions.
- **Stage Checklist**: `references/ingest-stages-mandatory.md` — authoritative ingest-stage checklist with go/no-go gates.

## Reference map

**Pipeline core**:
- `references/ingest-stages-mandatory.md` — ingest stage checklist (Phase 0-4 + Lint + Graph, ⭐ easy-to-skip stages marked)
- `references/query-generation.md` — Stage 2.5: auto-generate `wiki/queries/`
- `references/comparison-generation.md` — Stage 2.3.5: auto-generate `wiki/comparisons/` (2.5A disambiguation, 2.5B in-source, 2.5C cross-source)
- `references/knowledge-gap-lint.md` — lint system: synthesis/finding/thesis/methodology formation triggers
- `references/scanned-pdf-ocr-pipeline.md` — minerU scanned PDF OCR pipeline (Path B)
- `references/raw-naming-conventions.md` — raw 文件命名规范检查机制（项目级 `raw/NAMING.md` + auto-check）
- `references/chat-ingest.md` ⭐ — interactive human-guided ingest (NashSU startIngest/executeIngestWrites parity)
- `references/deep-research.md` ⭐ — closed-loop web→wiki research pipeline (NashSU deep-research.ts parity)
- `references/save-chat-to-wiki.md` ⭐ — save any conversation as wiki page + auto-ingest (NashSU chat-save-to-wiki.ts parity)
- `references/review-sweep.md` ⭐ — auto-resolve review items satisfied by new ingests (NashSU sweep-reviews.ts parity)
- `references/conversation-mode.md` — **conversation mode** (`--conversation`): the current conversation does each text-gen LLM step with its own model (serial, prompt-file handoff)
- `references/delegate-mode.md` — **agent invocation** via `ingest.py --conversation`: how a calling agent (Claude Code/Hermes) answers each LLM step; notes that direct API is the default path without `--conversation`
- `references/nashsu-search-architecture.md` — NashSU 源码实证：graph-relevance.ts（纯确定性 4 信号）+ search.rs（hybrid keyword+vector+RRF，远程 embedding API，无本地模型）。澄清 "NashSU parity" 在搜索侧的实际覆盖范围

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
- `references/maintenance-cleanup.md` — periodic cleanup of stale files (`.digested`, temp dirs, `.DS_Store`, empty-slug `.md` bug residual)
- `references/cron-installation.md` — cron-based automation
- `references/nashsu-lint-source-analysis.md` — NashSU lint.json internals
- `references/scripting-pitfalls.md` — Python + agent tool pitfalls

**Templates** (8 by file type):
`templates/digest-{book,paper,datasheet,applicationnote,designexample,presentation,standard,news}.md`

**Aggregate templates**:
`templates/{overview,schema,index,log,disambiguation}.md`

## Key features

- **Auto-ingest**: `python3 scripts/ingest.py file.pdf [file2.pdf ...] --conversation` — NashSU two-step: Stage 2.4 dedicated source page + Stage 2.3.x concept/entity generation. LLM steps run in conversation mode (current model).
- **Chat ingest** ⭐ (NashSU v0.4.25 parity): `/improved-wiki chat-ingest <file>` — interactive two-step: Claude presents digest → you provide guidance → Claude generates guided wiki pages. Human relevance judgment in the loop. See `references/chat-ingest.md`.
- **Deep research** ⭐ (NashSU v0.4.25 parity): `/improved-wiki deep-research <topic>` — closed-loop: web search → LLM synthesis → wiki query page → auto-ingest → entity/concept pages → new review items. Knowledge base grows itself. See `references/deep-research.md`.
- **Save chat to wiki** ⭐ (NashSU v0.4.25 parity): say "保存到 wiki" after any conversation — captures insight as wiki page with `origin: chat-save` + auto-ingests. Conversations become permanent knowledge. See `references/save-chat-to-wiki.md`.
- **Review sweep** ⭐ (NashSU v0.4.25 parity): `/improved-wiki sweep-reviews` — scans pending review items, auto-resolves those satisfied by subsequent ingests (rule-based + LLM semantic judge). Keeps review backlog actionable. See `references/review-sweep.md`.
- **Batch ingest**: `python3 scripts/ingest.py f1.pdf f2.pdf ...` — parallel Stage 1.1-2 per book, serial Stage 3.1+ write
- **Graph** (separate command, peer of Ingest/Lint): `python3 scripts/graph.py` builds the knowledge graph (NashSU graph-view CLI parity — four-signal weighted graph + Louvain communities + cohesion + gaps + cluster hubs). Deterministic, no LLM. `AUTO_BUILD_GRAPH=1` auto-rebuilds after ingest (30-min staleness guard). `--mode query --slug <s>` for read-only per-page wikilink suggestions during ingest.
- **Unified barrier-free pipeline**: Stage 2.2 + 2.1 merged — analyze chunk → generate pages → next chunk. Works for all chunk counts (1 to N). Accumulating context + per-chunk checkpoint for crash recovery. Legacy multi-round synthesis retired.
- **Parallel I/O**: caption ∥ digest (Stage 1.3∥1), caption batch dispatch (×6 workers). Pure I/O-bound parallelism only — no quality impact.
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
- **NashSU parity**: aligned with `ingest.ts` v0.4.25 on heading path, overlap suffix, accumulating digest, CJK slug, PPTX/DOCX, sources union merge, schema routing, aggregate repair caps, page merge, wikilink enrichment, source lifecycle. Chunk pipeline improved to barrier-free (analyze→generate per chunk, unified for all chunk counts)
- **Graph 四信号权重** (built-in): `graph.py` uses NashSU's four-signal model (direct link ×3.0, source overlap ×4.0, Adamic-Adar ×1.5, type affinity ×1.0) for weighted Louvain community detection
- **Per-page 语言门禁** (built-in): `ingest.py` Stage 3.2 detects body language per FILE block, warns on mismatch with expected source language (NashSU contentMatchesTargetLanguage parity)
- **Schema routing validation** (built-in): `ingest.py` validates `type:` frontmatter against file path directory, auto-corrects mismatches (NashSU validateWikiPageRouting parity)
- **Path safety validation** (built-in): `ingest.py` rejects FILE blocks with `..` segments, absolute paths, Windows-invalid names, and non-wiki/ destinations (NashSU isSafeIngestPath parity)
- **Local OCR**: minerU VLM via `~/.venv/bin/mineru -b vlm-engine` (free, serial, `MINERU_MAX_CONCURRENT=1`; override via `MINERU_BACKEND` env)

## Scripts

| Category | Scripts |
|----------|---------|
| Core | `ingest.py`, `_core.py`, `_llm_api.py`, `_paths.py`, `_language.py`, `_frontmatter.py` |
| Stage Modules | `_stage_1_extract.py`, `_stage_2_analyze.py`, `_stage_2_generate.py`, `_stage_3_write.py` |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | `wiki-lint.sh`, `wiki-lint-semantic.py`, `validate_ingest.py`, `validate-frontmatter.sh`, `normalize_raw_names.py` |
| Graph | `graph.py` (NashSU graph-view CLI parity; four-signal + Louvain; deterministic, no LLM) |
| Queue | `wiki-monitor.sh`, `run-queue.sh` |
| Embeddings | `build_embeddings.py`, `search_wiki.py` |
| Repair | `repair_wiki.py`, `repair_stage_38.py`, `reingest_batch.py`, `sweep_reviews.py` |

## Trigger this skill

**Auto Ingest**: User mentions wiki ingest / PDF OCR / batch ingest / validate-ingest / image caption / local minerU / pilot OCR. Ingest runs in conversation mode — the current conversation's model does all text-generation LLM work (spawn sub-agents as needed); only image captioning calls MiniMax. **Dedup rule**: before selecting any file, check `wiki/sources/<path>.md` exists. Never rely on `ingest-cache.json` for dedup.

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
