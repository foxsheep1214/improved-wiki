---
name: improved-wiki
description: "Class-level umbrella for the Karpathy/NashSU LLM-Wiki ingestion pipeline (autoIngestImpl + 16 ingest Stages + 3 lint Stages for knowledge graph). Use when ingesting a PDF/PPTX/DOCX into a wiki project (HardwareWiki, RadarWiki, etc), validating an ingest, debugging failed tasks, or auditing wiki completeness. Phase 0 OCR uses local minerU (free, auto-extracts images). Lint stages include four-signal knowledge graph + Louvain community detection."
tags: [ingest, mandatory, nashsu, pipeline, scan-pdf, mineru, local-ocr, knowledge-graph, louvain]
related_skills: [karpathy-llm-wiki, llm-wiki-local]
---

# improved-wiki

Karpathy LLM-Wiki pattern + NashSU v0.4.24 pipeline. 16 ingest Stages (4 Phases) + 3 lint Stages (knowledge graph).

```
Ingest: 0.0→0.1→0.3→0.5→0.7→0.9→1.1→1.3→2.1→2.3→2.5→3.5→3.8→4.5→4.7→[4.9]

Phase 0: Pre-processing     Phase 2: Generation         Phase 4: Reflect & Finalize
Phase 1: Analysis            Phase 3: Write & Enrich

Lint:  [16: Build Graph] → [17: Louvain] → [18: Insights]
       (post-ingest, batch-triggered, not per-book)
```

## Entry point

**`references/ingest-stages-mandatory.md`** — authoritative 16 ingest-stage + 3 lint-stage checklist with go/no-go gates.

## Reference map

**Pipeline core**:
- `references/ingest-stages-mandatory.md` — 16 ingest + 3 lint stages checklist (Phase 0-4 + Lint, ⭐ easy-to-skip stages marked; **Stage 16-18** knowledge graph section)
- `references/query-generation.md` — Stage 2.3: auto-generate `wiki/queries/`
- `references/comparison-generation.md` — Stage 2.5: auto-generate `wiki/comparisons/` (2.5A disambiguation, 2.5B in-source, 2.5C cross-source)
- `references/knowledge-gap-lint.md` — lint system: synthesis/finding/thesis/methodology formation triggers
- `references/scanned-pdf-ocr-pipeline.md` — minerU scanned PDF OCR pipeline (Path B)
- `references/raw-naming-conventions.md` — raw 文件命名规范检查机制（项目级 `raw/NAMING.md` + auto-check）
- `references/conversation-mode.md` — direct LLM execution mode for ingest
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

- **16-stage auto-ingest**: `python3 scripts/ingest.py file.pdf [file2.pdf ...]`
- **3-stage knowledge graph lint**: `python3 scripts/build_knowledge_graph.py` — four-signal weighted graph + Louvain + insights
- **Parallel pipeline**: 4 levels of parallelism — caption batch ∥ digest (Stage 0.9∥1.1), caption batch dispatch (×6 workers), chunk analysis (×8 workers), per-chunk generation (×configurable)
- **Batch mode**: `--parallel N` for concurrent Phase 0-2 processing across books (default 4)
- **Queue watch**: `--watch --drain` daemon mode consuming `ingest-queue.json`
- **Auto-validation**: `validate_ingest.py` runs at end of every ingest; per-stage `_verify_stage_N()` gates
- **NashSU parity**: aligned with `ingest.ts` v0.4.24 (page merge, path safety, fence-aware parsing, CRLF, error classification, page history, dynamic token budget)
- **Local OCR**: minerU VLM via `~/.venv/bin/mineru -b vlm-auto-engine` (free, serial execution, `MINERU_MAX_CONCURRENT=1`)
- **Raw 文件命名规范**：每个知识库项目必须维护 `raw/NAMING.md` 定义该项目的 raw 文件命名规则。新文件放入 `raw/` 时 skill 应主动检查命名合规性。无规则 → 提醒用户先制定规则。详见 `references/raw-naming-conventions.md`。

## Scripts

| Category | Scripts |
|----------|---------|
| Core | `ingest.py`, `_paths.py`, `_language.py` |
| Lint | `wiki-lint.sh`, `wiki-lint-semantic.py`, `build_knowledge_graph.py`, `validate_ingest.py`, `validate-frontmatter.sh` |
| Queue | `wiki-monitor.sh`, `run-queue.sh` |
| Embeddings | `build_embeddings.py`, `search_wiki.py` |
| Repair | `repair_wiki.py`, `repair_stage_38.py`, `reingest_batch.py` |

## Trigger this skill

**Ingest**: User mentions wiki ingest / PDF OCR / minimax batch / validate-ingest / image caption / local minerU / pilot OCR. **Stage 0.1 rule**: before selecting any file, check `wiki/sources/<path>.md` exists. Never rely on `ingest-cache.json` for dedup.

**Retrieval**: User asks to search wiki / cite knowledge base / query technical content. See `references/kb-retrieval.md`.

## Projects

| Project | Path |
|---------|------|
| HardwareWiki | `~/Documents/知识库/HardwareWiki` |
| RadarWiki | `~/Documents/知识库/RadarWiki` |
| 自然科学知识库 | `~/Documents/知识库/自然科学知识库` |
