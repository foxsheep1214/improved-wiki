# Deep Research — Closed-Loop Research → Wiki Pipeline

参考 NashSU `deep-research.ts` + `web-search.ts` closed-loop research system. **Strict NashSU minimal implementation** — verbatim synthesis, code-generated References, fixed `tags: [research]`, no review-derived auto-chain.

## Core Idea

Auto-ingest is "消化已有源" — you give the wiki source files and it builds pages from them. Deep research is "主动寻找未知" — the wiki identifies knowledge gaps and fills them from the internet.

The **closed loop** is what makes this powerful:

```
Research topic → web search → LLM synthesis → query page → auto-ingest
    → entity/concept pages → review items → new research topics → loop
```

Each cycle expands the knowledge base without needing new raw source files. The wiki grows itself.

## NashSU Alignment

| NashSU | improved-wiki (calling agent) |
|--------|---------------------------|
| Search via Tavily / SerpAPI / SearXNG / Ollama / Brave / Firecrawl (6 providers; `deepResearchSource` = web / anytxt / both) | Use the runtime's available web-search capability; dedup by URL, cap 20, and synthesize from snippets. |
| `collectResearchSources()` — multi-query, **5 results/query**, **snippet-only**, dedup by URL, cap 20 | The calling agent runs targeted queries, dedups by URL, cap 20 — synthesize from snippets (like NashSU) |
| AnyTXT local-file source mode (`deepResearchSource: anytxt`/`both`) | `search_local.py` — CLI analog of AnyTXT mode: `keyword_search` on wiki/ + `mdfind`/ripgrep on raw/ (not byte-identical to AnyTXT) |
| `executeResearch()` — LLM synthesis, reads `wiki/index.md` for cross-ref | The calling agent synthesizes from sources + `wiki/index.md` |
| Save to `wiki/queries/<slug>-<date>-<HHMMSS>.md` | Same filename rule (Step 4) |
| `autoIngest()` on research result | `ingest.py` on the new query page |
| `queueResearch()` — concurrency queue (maxConcurrent=3) | Serial (conversation-based, one at a time) — CLI adaptation, no persistent queue |
| `onTaskFinished()` → process next **already-queued** task | One topic per invocation (NashSU does NOT derive new topics from reviews) |

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
Agent: [searches wiki] 没有找到。要我 deep research 这个主题并消化到 wiki 里吗？
```

### Step 1: Ground in Wiki Context

NashSU's `executeResearch` reads `wiki/index.md` (only) to ground cross-references. Do the same:

1. Read `wiki/index.md` — what pages exist, what terms to `[[link]]` to.

The topic then goes straight to search — NashSU does **not** insert a clarifying-question step. (If a topic is genuinely too broad, see the "Research Topic Too Broad" edge case, which mirrors NashSU's pre-search scope guard.)

### Step 2: Search for Sources

#### Step 2a: Local source search (CLI analog of NashSU's AnyTXT source mode)

**Before hitting the web**, search the project's own `wiki/` + `raw/` for existing
material. Personal knowledge bases often hold un-ingested PDFs or partially-related
pages that should ground — not be rediscovered from the web.

```bash
python3 "$SKILL_DIR/scripts/search_local.py" "<topic>" --project <project-path> --top 10
```

`search_local.py` reuses `_wiki_keyword.keyword_search` over `wiki/*.md` (curated
knowledge, ranks higher) and searches `raw/` PDF content via macOS Spotlight
(`mdfind`, with ripgrep fallback over text sidecars). Output format:

```
[N] **<title>** (local:wiki)
<snippet>
path: <absolute path>

[N] **<filename>** (local:raw)
<PDF context snippet or fallback>
path: <absolute path>
```

These local hits are **first-class sources** — merge them with web results in Step 3.
For `local:raw` hits, if the snippet is a PDF content match, Read the file (or
`pdftotext` it) to extract fuller context before synthesis. Local wiki hits that
already cover the topic may narrow the research scope (skip what the wiki already knows).

#### Step 2b: Web search

The calling agent runs **3-5 targeted web queries** (not one broad query). Each query should approach the topic from a different angle:

```
Query 1: <topic> 技术原理 最新进展
Query 2: <topic> 行业应用 实际案例
Query 3: <topic> 挑战 局限性 瓶颈
Query 4: <topic> 对比 选型 方案
Query 5: <topic> 最新研究 2025 2026
```

Use the runtime's available web-search and page-reading tools. Deduplicate results by URL.
If no web-search capability is available, **pause before Step 3** and ask the user to
enable it or provide sources; never present local-only work as web research.

Deduplicate results by URL. Cap at 20 sources (NashSU `MAX_RESEARCH_SOURCES`). Prefer recent, authoritative sources.

Synthesize from the search **snippets** — NashSU's `collectResearchSources` passes snippet text only and never fetches full page bodies. Only read a full page when a snippet is too thin to use and the current runtime provides a page-reading capability; treat that as a CLI extra, not NashSU behavior.

### Step 3: Synthesize

The calling agent synthesizes the research into one wiki page from the collected sources
(local Step 2a + web Step 2b). NashSU writes the LLM's synthesis **verbatim**
(stripping only `<think>` blocks) and appends a **code-generated** References list
— so follow these writing rules rather than forcing a fixed section template:

Synthesis prompt (matches NashSU's `executeResearch`):

```
Synthesize a wiki page from the following research sources. Sources include local
knowledge-base hits (local:wiki / local:raw) and web results — treat them
uniformly, preferring local:wiki for claims already in the knowledge base.

## Cross-referencing
- When you mention an entity/concept that exists in the Wiki Index below (or in a
  local:wiki hit), use a [[wikilink]] to connect new research to existing knowledge.

## Writing rules (NashSU)
- Organize into clear sections with ## headings — let the content shape them; do
  NOT force a fixed Overview / Key Findings / Thematic / Contradictions skeleton.
- Cite sources with [N] notation matching the References list.
- Note contradictions between sources (don't paper over them); flag agreement.
- Flag open questions and areas needing further research.
- Neutral, encyclopedic tone. Output language: match the wiki's primary language.
- Do NOT write the frontmatter or the References list yourself — they are added
  deterministically by the steps below.
```

Then assemble the page (NashSU builds this in code, not via the LLM):

```
---
type: query
title: "Research: <topic, with any \" escaped as \\\">"
created: <today, UTC>
origin: deep-research
tags: [research]
---

# Research: <topic>

<the LLM synthesis, verbatim except <think> blocks stripped>

## References
1. [<title>](<url>) — <source>
2. ...
```

Frontmatter is exactly these five keys. **No** `<topic-tags>` (NashSU hardcodes
`tags: [research]`); **no** `sources` field (source URLs live in the code-generated
References list, not in frontmatter). Escape any `"` in the title. The
`origin: deep-research` field mirrors NashSU's marker for research pages (NashSU
sets it; ingest-generated source/concept/entity pages carry no `origin`).

### Step 4: Write to Wiki

Write the synthesized page to `wiki/queries/<slug>-<YYYY-MM-DD>-<HHMMSS>.md`
(port of NashSU `makeDeepResearchFileName` → `makeQueryFileName("research-" + topic)`):

```python
from _core import slugify           # CJK-aware slug
import subprocess
slug = slugify(f"research-{topic}")
ts = subprocess.check_output(["date", "-u", "+%Y-%m-%d-%H%M%S"]).decode().strip()
path = f"wiki/queries/{slug}-{ts}.md"
```

The UTC timestamp suffix guarantees that repeated research on the same topic never
collides — no `-2` versioning needed, matching NashSU.

### Step 5: Auto-Ingest (THE CLOSED LOOP) ⭐

This is the critical step that makes it a closed loop. Immediately after writing the research page, trigger ingest on it:

```bash
python3 "$SKILL_DIR/scripts/ingest.py" wiki/queries/<slug>.md
```

The ingest entry-point accepts a `wiki/queries/` path directly (NashSU `autoIngest` parity): `_bridge_wiki_queries_to_raw` copies the page into `raw/queries/<slug>.md` and ingests that copy, so the rest of the raw-root-centric pipeline sees a normal source. The original `wiki/queries/<slug>.md` stays as the human-readable research page. (NashSU's `autoIngest` is path-agnostic and reads `wiki/queries/` directly; the improved-wiki pipeline derives source identity from a `raw/` path in ~20 places, so the copy is the bridge instead of a full refactor.)

The ingest pipeline will:
1. **Stage 2.2**: Analyze the research page → extract key entities/concepts
2. **Stage 2.4**: Generate entity/concept pages for newly discovered items
3. **Stage 2.9**: Generate comparison pages if relevant
4. **Stage 3.4 (review)**: Generate review items — some may become new research topics (process via process-reviews)
5. **Stage 3.5**: Update index/log/overview
6. **Stage 3.7**: Embed the new pages

This is what turns "a saved search result" into "integrated knowledge."

### Step 6: Present Results

```
## ✅ Deep Research 完成：《<topic>》

**研究页面**: wiki/queries/research-<topic>.md

**本地来源**: N 条 (wiki: M, raw: K)
**网络来源**: 12 个网页（去重后 8 个）

**消化的新知识** (auto-ingest 产出):
- wiki/entities/<新实体1>.md
- wiki/entities/<新实体2>.md
- wiki/concepts/<新概念1>.md
- wiki/concepts/<新概念2>.md
- wiki/comparisons/<对比页>.md

**后续研究方向** (review items):
- ⚠️ <review item 1>
- 🔗 <review item 2>
```

### One Topic Per Invocation (no review-derived chaining)

NashSU's `onTaskFinished()` only advances the **already-queued** research tasks
(`processQueue` pulls the next *queued* task); it does **not** read review items or
derive new topics from them. There is no persistent queue store in conversation
mode, so each invocation researches one topic and stops. The review items produced
by Step 5's ingest are surfaced to the user (Step 6) as candidate next topics — but
auto-chaining onto them is **not** NashSU behavior and is not done here. The user
can start a new deep-research invocation on any surfaced topic.

## Trigger Phrases

- `deep research <topic>` / `deep-research <topic>`
- `深度研究 <主题>`
- `研究一下 <主题> 并写入 wiki`
- `wiki 里缺了 <topic>，帮我研究一下`
- `调查 <topic> 并消化`

## When the calling agent should proactively suggest Deep Research

The calling agent should suggest deep research when:

1. **Wiki query returns nothing**: "wiki 里没有这个主题，要我 deep research 吗？"
2. **Review item is a knowledge gap**: "这个 review item 可以通过 deep research 填补"
3. **Comparison page has one-sided info**: "对比的另一半信息不足，需要研究吗？"
4. **Lint finds isolated/sparse nodes**: "这些孤立页面可能需要 deep research 来扩充连接"
5. **User asks a question wiki can't fully answer**: "现有 wiki 只能部分回答这个问题，补充研究？"

## Variants

### Variant A: From Review Item

```
User: deep research the review item "缺少 GaN HEMT 驱动电路设计"
Agent: [reads the review item → formulates search queries → ...]
```

The review item's `search_queries` field (populated by Stage 3.4 for `suggestion`/`missing-page` reviews — NashSU `searchQueries` parity) provides 2-3 keyword-rich web search queries that seed Step 2b directly, with no extra LLM round-trip. The review item's `affected_pages` field tells the calling agent which pages to read for context. (NashSU also has a separate `optimizeResearchTopic` LLM call that refines a gap into a topic + queries; the improved-wiki skips it — the pre-computed `search_queries` already cover that role. If a review item lacks `search_queries`, fall back to deriving queries from `title` + `affected_pages`.)

### Variant B: From Comparison Gap

```
User: 这个对比页缺少 B 方的数据，研究一下
Agent: [reads the comparison page → identifies what's missing → searches → synthesizes → re-generates comparison]
```

### Variant C: Batch Deep Research

```
User: deep research 这 5 个 review items 里的 missing-page 类型
Agent: [processes each one sequentially, collecting results, presenting summary]
```

### Variant D: Targeted Deep Research (with user-provided URLs)

```
User: deep research GaN power supplies, 重点看这些链接:
  - https://example.com/gan-article-1
  - https://example.com/gan-paper-2
Agent: [reads those URLs when the runtime allows it + complementary web search → synthesizes]
```

## Edge Cases

### Research Topic Too Broad
If the topic would produce >20 high-quality sources with divergent themes, the calling agent should ask the user to narrow scope BEFORE searching. Wasted search on an overbroad topic helps no one.

### Zero Useful Results (NashSU `noResearchSourcesTaskPatch`)
NashSU distinguishes two states — mirror them:
1. **Search errors** → report the failure (task `error`); surface the errors and
   suggest alternative query formulations or better search terms.
2. **No errors but zero results** → write **no page** and run **no ingest**; report
   "No research sources found." (task `done`).

Either way, **never** fabricate a page from thin air.

### Topic Already Well-Covered in Wiki
If the wiki already has extensive coverage, the calling agent should:
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

Both auto-ingest and deep research write through the same `writeFileBlocks` → Stage 3.1 写盘 path. Both update `ingest-cache.json`. Both trigger aggregate repair (Stage 3.5). The wiki doesn't know or care where knowledge came from — only that it's structured, linked, and verified.
