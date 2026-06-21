# Review Sweep — Auto-Resolution of Review Items

NashSU v0.4.25 parity for `sweep-reviews.ts`: automatically resolve pending review items when subsequent ingests address their underlying condition.

## Core Idea

When you ingest a source, the pipeline generates review items (missing pages, contradictions, suggestions, duplicates). These sit in `wiki/REVIEW/` waiting for human attention. But as you ingest MORE sources, some of those review items become obsolete — the missing page was created, the contradiction was resolved, the suggested comparison was written.

Review Sweep automatically detects these and marks them `resolved`, so your review backlog reflects what ACTUALLY still needs attention, not stale items from last month.

```
New source ingested → sweep pending reviews → 
  rule-match: did this ingest create the missing page?
  LLM-judge: does the new content resolve this contradiction?
  → auto-resolve satisfied items
  → report what was resolved and what remains
```

## NashSU Alignment

| NashSU | improved-wiki |
|--------|--------------|
| `sweep-reviews.ts` — runs when ingest queue drains | `/improved-wiki sweep-reviews` — manual or post-batch trigger |
| `buildWikiIndex()` — scans wiki/ for all page IDs + titles | `scripts/sweep_reviews.py` or Claude-direct scan |
| Rule matching: filename / frontmatter title / affectedPages | Same: page path match, title match, affected page existence |
| LLM semantic judgment for remaining items | Claude reads review item + wiki context → judges |
| Auto-resolve + update review store | Update `resolved: true` + `resolved_at` in REIVEW .md files |

## Workflow

### Step 0: Trigger

```
# After a batch ingest
/improved-wiki sweep-reviews

# Or triggered automatically after N new ingests
```

### Step 1: Build Wiki Index

Claude (or the sweep script) scans `wiki/` to build an index of:
- All page IDs (filename without `.md`)
- All page titles (from frontmatter `title:`)
- All wikilinks per page

This is the "what exists now" snapshot against which review items are checked.

### Step 2: Rule-Based Matching (Fast Path)

For each pending review item (where `resolved: false`):

**Missing-page items**: Check if the missing page now exists.
```
Review: "缺少 GaN HEMT 驱动电路设计页面"
Check: wiki/concepts/gan-hemt-驱动电路设计.md exists? → YES → auto-resolve
Check: Any page with title matching "GaN HEMT 驱动电路设计"? → YES → auto-resolve
```

**Duplicate items**: Check if the duplicate pages still both exist.
```
Review: "[[GaN-driver]] 疑似与 [[GaN-驱动]] 重复"
Check: Both pages still exist? → NO (one was deleted/merged) → auto-resolve
```

**Affected pages check**: For any review with `affected_pages`, check if ALL listed pages still exist and have been updated since the review was created.
```
Review: contradiction about [[concepts/emi-filter]]
Check: page modified time > review created time? → YES → escalate to LLM judge
```

### Step 3: LLM Semantic Judgment (Slow Path)

For items that pass the rule check but need deeper evaluation, Claude reads the relevant wiki pages and judges:

```
Read: wiki/REVIEW/contradiction/2025-06-01-source-emi-filter-contradiction.md
Read: wiki/concepts/emi-filter.md (current state)
Read: wiki/sources/new-book-about-emi.md (newly ingested source)

Judge: Does the newly ingested content resolve the contradiction
  described in this review item?

If YES → mark resolved, note which source resolved it
If PARTIALLY → add a note, leave unresolved
If NO → leave unresolved
```

### Step 4: Output Report

```
## Review Sweep 结果

**扫描**: 23 个 pending review items
**自动消解**: 5 个

### 已消解 ✅
- ✅ "缺少 GaN HEMT 驱动电路设计" → 页面已由《Power GaN》ingest 创建
- ✅ "[[EMI-filter]] 与 [[EMC-filter]] 疑似重复" → [[EMC-filter]] 已删除
- ✅ "第3章公式推导需要人工验证" → 新 ingest《信号完整性》确认了推导
- ✅ "建议补充 SiC 与 GaN 对比" → 对比页已由用户手动创建
- ✅ "2024 数据过时" → 新 ingest 包含了 2025 数据

### 仍待处理 ⚠️
- ⚠️ "ADS 仿真设置文档缺失" (affected: [[concepts/ads-setup]])
- ⚠️ "传输线理论与实际测量的偏差" (affected: [[concepts/transmission-line]])
- ... (18 more)
```

## When to Run

| Trigger | Recommendation |
|---------|---------------|
| After batch ingest (≥5 new sources) | Run sweep |
| After single important ingest | Optional (if the ingest is clearly related to pending reviews) |
| Weekly cron | `0 9 * * 1 /improved-wiki sweep-reviews` |
| Before manual review session | Run sweep first to clear stale items |
| When lint finds many review items | Sweep to reduce noise before investigating |

> **Read-only preview via lint**: `wiki-lint.sh --sweep` runs `sweep_reviews.py` in dry-run and prints a one-line count of auto-resolvable items (e.g. `1 of 3 auto-resolvable, 2 still pending`). It never mutates review files — use it to gauge backlog; run `sweep_reviews.py --apply` (or `/improved-wiki sweep-reviews`) to actually close items.

## Implementation Notes

The sweep can be implemented in two ways:

### A. Claude-Direct (conversation mode)
Claude reads `wiki/REVIEW/` directory, builds wiki index, applies rules, judges ambiguous cases. Best for small wikis (<500 pages, <50 review items).

### B. Script (`scripts/sweep_reviews.py`)
Python script that:
1. Scans `wiki/REVIEW/` for unresolved items
2. Parses frontmatter for `affected_pages`, `type`, `created`
3. Builds page index from `wiki/` file tree
4. Applies rule-based matching (fast)
5. Outputs items needing LLM judgment as JSON
6. Claude reads the JSON and applies semantic judgment
7. Script updates resolved items

Best for large wikis. Share the same wiki index building logic as `graph.py`.

## Review Item Format

Each review item in `wiki/REVIEW/`:

```markdown
---
type: review
review_type: missing-page
title: "缺少 GaN HEMT 驱动电路设计页面"
created: 2025-06-15
resolved: false
resolved_at: null
resolved_by: null
affected_pages:
  - wiki/concepts/gan-hemt.md
  - wiki/entities/gan-systems.md
search_queries:
  - GaN HEMT gate driver design
  - enhancement mode GaN driver IC
---

# 缺少 GaN HEMT 驱动电路设计页面

...description...

OPTIONS: Create Page | Skip
```

When resolved, the sweep updates:

```markdown
resolved: true
resolved_at: 2025-06-20
resolved_by: "auto-sweep: page created by ingest of 《Power GaN》"
```
