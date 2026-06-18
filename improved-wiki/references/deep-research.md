# Deep Research — Closed-Loop Research → Wiki Pipeline

NashSU v0.4.25 parity for `deep-research.ts` + `web-search.ts` closed-loop research system.

## Core Idea

Auto-ingest is "消化已有源" — you give the wiki source files and it builds pages from them. Deep research is "主动寻找未知" — the wiki identifies knowledge gaps and fills them from the internet.

The **closed loop** is what makes this powerful:

```
Research topic → web search → LLM synthesis → query page → auto-ingest
    → entity/concept pages → review items → new research topics → loop
```

Each cycle expands the knowledge base without needing new raw source files. The wiki grows itself.

## NashSU Alignment

| NashSU | improved-wiki (Claude Code) |
|--------|---------------------------|
| Search via Tavily/SerpAPI/SearXNG/Firecrawl | WebSearch + WebFetch (Claude Code built-in + Tavily MCP) |
| `collectResearchSources()` — multi-query, dedup, cap 20 | Claude runs 3-5 targeted queries, deduplicates |
| `executeResearch()` — LLM synthesis with wiki index | Claude synthesizes using wiki context + search results |
| Save to `wiki/queries/<slug>.md` | Write via FILE block or direct file write |
| `autoIngest()` on research result | `ingest.py` on the new query page |
| `queueResearch()` — concurrency queue | N/A (conversation-based, one at a time) |
| `onTaskFinished()` → process next queued | User decides when to trigger next research |

## Workflow

### Step 0: Invocation

```
User: /improved-wiki deep-research <topic>

# Or from a review item:
User: deep research this review item about <topic>
```

Or triggered by the user asking a question that the wiki can't answer:

```
User: wiki 里有没有关于 GaN 驱动电路的资料？
Claude: [searches wiki] 没有找到。要我 deep research 这个主题并消化到 wiki 里吗？
```

### Step 1: Understand the Research Scope

Claude first reads the wiki context to ground the research:

1. Read `wiki/index.md` — what pages exist, what terms to link to
2. Read `wiki/overview.md` — what the wiki broadly covers
3. If the topic relates to a specific area, read relevant existing pages

Then Claude **asks clarifying questions** if the topic is too broad:

```
Claude: "反无人机雷达"这个主题比较广。你更关心哪个方面？
  1. 技术瓶颈（探测距离、多目标跟踪、低慢小目标识别）
  2. 成本与部署（单套系统成本、组网方案）
  3. 对抗与反制（诱饵、干扰、隐身）
  4. 市场与产业（供应商对比、采购趋势）
```

### Step 2: Search for Sources

Claude runs **3-5 targeted search queries** (not one broad query). Each query should approach the topic from a different angle:

```
Query 1: <topic> 技术原理 最新进展
Query 2: <topic> 行业应用 实际案例
Query 3: <topic> 挑战 局限性 瓶颈
Query 4: <topic> 对比 选型 方案
Query 5: <topic> 最新研究 2025 2026
```

Use the available search tools:
- **WebSearch** (built-in): General web search
- **Tavily MCP** (`mcp__tavily__tavily_search`): Advanced search with configurable depth
- **WebFetch** (built-in): Fetch full content of promising pages

Deduplicate results by URL. Cap at 20 sources. Prefer recent, authoritative sources.

For each promising source, fetch the full content if the snippet is insufficient.

### Step 3: Synthesize

Claude synthesizes the research into a structured wiki page. The prompt structure:

```
Synthesize a comprehensive wiki page from the following research sources.

## Cross-referencing (CRITICAL)
- The wiki has existing pages listed in the Wiki Index below
- When you mention an entity or concept that exists in the wiki, use [[wikilink]]
- This connects new research to existing knowledge

## Writing Rules
- Organize into clear sections with ## headings
- Cite sources using [N] notation matching the References list
- Note contradictions between sources (don't paper over them)
- Highlight areas where sources agree (stronger signal)
- Flag open questions and areas needing further research
- Neutral, encyclopedic tone
- Output language: match the wiki's primary language

## Output Format
Output a complete wiki page with YAML frontmatter:

---
type: query
title: "Research: <topic>"
created: <today>
origin: deep-research
tags: [research, <topic-tags>]
sources: [<source-urls>]
---

# Research: <topic>

## Overview
...

## Key Findings
...

## <Thematic Sections>
...

## Contradictions & Open Questions
...

## References
[1] [Title](URL) — source
...
```

The `origin: deep-research` frontmatter field marks this as a research page (distinct from `origin: ingest` for source-derived pages).

### Step 4: Write to Wiki

Write the synthesized page to `wiki/queries/<slug>.md`. Use CJK-aware slug generation:

```python
from scripts._paths import make_slug
slug = make_slug(f"research-{topic}")
path = f"wiki/queries/{slug}.md"
```

If a page with the same slug exists, version it: `research-<topic>-2.md`.

### Step 5: Auto-Ingest (THE CLOSED LOOP) ⭐

This is the critical step that makes it a closed loop. Immediately after writing the research page, trigger ingest on it:

```bash
python3 scripts/ingest.py wiki/queries/<slug>.md
```

The ingest pipeline will:
1. **Stage 1.1**: Analyze the research page → extract key entities/concepts
2. **Stage 2.1**: Generate entity/concept pages for newly discovered items
3. **Stage 2.3**: Generate follow-up query pages if open questions found
4. **Stage 2.5**: Generate comparison pages if relevant
5. **Stage 4.5**: Generate review items — some may become new research topics
6. **Stage 4.7**: Update index/log/overview
7. **Stage 4.9**: Embed the new pages

This is what turns "a saved search result" into "integrated knowledge."

### Step 6: Present Results

```
## ✅ Deep Research 完成：《<topic>》

**研究页面**: wiki/queries/research-<topic>.md

**搜索来源**: 12 个网页（去重后 8 个）

**消化的新知识** (auto-ingest 产出):
- wiki/entities/<新实体1>.md
- wiki/entities/<新实体2>.md
- wiki/concepts/<新概念1>.md
- wiki/concepts/<新概念2>.md
- wiki/comparisons/<对比页>.md

**后续研究方向** (review items):
- ⚠️ <review item 1>
- 🔗 <review item 2>

你可以对这些 review item 再次运行 deep research：
  /improved-wiki deep-research "<review item title>"
```

## Trigger Phrases

- `deep research <topic>` / `deep-research <topic>`
- `深度研究 <主题>`
- `研究一下 <主题> 并写入 wiki`
- `wiki 里缺了 <topic>，帮我研究一下`
- `调查 <topic> 并消化`

## When Claude Should Proactively Suggest Deep Research

Claude should suggest deep research when:

1. **Wiki query returns nothing**: "wiki 里没有这个主题，要我 deep research 吗？"
2. **Review item is a knowledge gap**: "这个 review item 可以通过 deep research 填补"
3. **Comparison page has one-sided info**: "对比的另一半信息不足，需要研究吗？"
4. **Lint finds isolated/sparse nodes**: "这些孤立页面可能需要 deep research 来扩充连接"
5. **User asks a question wiki can't fully answer**: "现有 wiki 只能部分回答这个问题，补充研究？"

## Variants

### Variant A: From Review Item

```
User: deep research the review item "缺少 GaN HEMT 驱动电路设计"
Claude: [reads the review item → formulates search queries → ...]
```

The review item's `searchQueries` field (if present) provides the initial queries. The review item's `affectedPages` field tells Claude which pages to read for context.

### Variant B: From Comparison Gap

```
User: 这个对比页缺少 B 方的数据，研究一下
Claude: [reads the comparison page → identifies what's missing → searches → synthesizes → re-generates comparison]
```

### Variant C: Batch Deep Research

```
User: deep research 这 5 个 review items 里的 missing-page 类型
Claude: [processes each one sequentially, collecting results, presenting summary]
```

### Variant D: Targeted Deep Research (with user-provided URLs)

```
User: deep research GaN power supplies, 重点看这些链接:
  - https://example.com/gan-article-1
  - https://example.com/gan-paper-2
Claude: [fetches those URLs + complementary web search → synthesizes]
```

## Edge Cases

### Research Topic Too Broad
If the topic would produce >20 high-quality sources with divergent themes, Claude should ask the user to narrow scope BEFORE searching. Wasted search on an overbroad topic helps no one.

### Zero Useful Results
If all search queries return garbage, Claude should:
1. Report the failure honestly
2. Suggest alternative query formulations
3. Ask the user for better search terms
4. NOT fabricate a page from thin air

### Topic Already Well-Covered in Wiki
If the wiki already has extensive coverage, Claude should:
1. Point to existing pages
2. Identify what's NEW that the research could add
3. Only proceed if there's genuine incremental value
4. If proceeding, focus on the delta (new info vs existing)

### Source Paywalls
If key sources are behind paywalls, note them in the research page as "References (behind paywall)" with URLs — the user might have institutional access.

## Integration with Existing Pipeline

Deep research is the **outward-facing** complement to auto-ingest's **inward-facing** pipeline:

```
                    ┌──────────────────┐
                    │   Raw Sources    │
                    │  (PDF/PPTX/DOCX) │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │   Auto Ingest    │
                    │  (inward: 消化)   │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │   Wiki Pages     │◄──────────┐
                    │  (knowledge base) │            │
                    └────────┬─────────┘            │
                             │                      │
              ┌──────────────┼──────────────┐       │
              │              │              │       │
     ┌────────▼─────┐ ┌──────▼──────┐ ┌─────▼──────┐│
     │  Lint/Gaps   │ │ Review Items│ │   Queries  ││
     │ (knowledge   │ │ (missing,   │ │ (user asks)││
     │  gaps found) │ │  contradict)│ │            ││
     └────────┬─────┘ └──────┬──────┘ └─────┬──────┘│
              │              │              │       │
              └──────────────┼──────────────┘       │
                             │                      │
                    ┌────────▼─────────┐            │
                    │  Deep Research   │            │
                    │ (outward: 扩展)   │────────────┘
                    └──────────────────┘
```

Both auto-ingest and deep research write through the same `writeFileBlocks` → `validate_ingest.py` path. Both update `ingest-cache.json`. Both trigger aggregate repair. The wiki doesn't know or care where knowledge came from — only that it's structured, linked, and verified.
