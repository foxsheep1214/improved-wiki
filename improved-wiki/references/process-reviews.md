# Process Reviews — 人工裁决 pending review items

参考 NashSU `review-view.tsx`（审核面板）: the CLI/agent counterpart of NashSU's
review panel. Sweep（`review-sweep.md`）is the **automatic** side — it clears
items already satisfied by later ingests. Process-reviews is the **human** side —
the user decides what to do with each still-pending item, one at a time, using
the predefined options NashSU offers per item: **Deep Research / Create Page /
Skip**.

This flow is where `wiki/queries/` pages are born after the 2026-07-12 Stage 2.7
removal: ingest flags "open question worth researching" as a REVIEW suggestion
item (with `search_queries`), and a query page only materializes when the user
chooses Deep Research (research result page) or Create Page here. Query pages
carry answers, not bare questions — NashSU's `queries/ = 保存的聊天回答 + 研究`.

## Trigger

- `/improved-wiki process-reviews`
- "处理 review" / "裁决 review" / "过一遍 review" / "process reviews"
- Naturally after a batch ingest + sweep: sweep clears the stale items,
  process-reviews handles what genuinely needs a human.

## NashSU Alignment

| NashSU review-view.tsx | improved-wiki |
|---|---|
| Review panel lists pending items | Claude scans `wiki/REVIEW/*/` for `resolved: false` |
| Per-item buttons: Deep Research / Create Page / Skip | AskUserQuestion per item with the same three options |
| `__deep_research__` → `queueResearch(topic, searchQueries)` | run the deep-research flow (`deep-research.md`) with the item's `search_queries` as seed queries |
| No search API configured → falls back to Create Page | no Tavily/WebSearch available → offer Create Page instead |
| `createReviewPageDrafts` type routing | same routing rules (below) |
| `resolveItem(id, action)` — resolved in store, never deleted | frontmatter `resolved: true` + `resolved_at` + `resolved_reason` — file kept on disk (audit trail, same convention as sweep) |

## Workflow

### Step 1: Scan

List pending items: `wiki/REVIEW/*/` files with `resolved: false`.
Default focus: **suggestion** and **missing-page** (the two types that carry
`search_queries` and map to actions). Include the other three types
(confirm/contradiction/duplicate) only when the user asks for a full pass —
those usually need judgment/editing rather than one of the three buttons.

Present a short queue summary first (count by type), then process items
one by one or in small batches (AskUserQuestion supports up to 4 questions
per call — one item per question).

### Step 2: Present each item

Show: title, description (trimmed), affected_pages, and its `search_queries`.
Options (NashSU OPTIONS parity — do not invent custom actions):

1. **Deep Research**（推荐，当 search_queries 非空且已配置搜索源）
2. **Create Page**
3. **Skip**

### Step 3: Execute the choice

**Deep Research** → run the `deep-research.md` flow:
- topic = item title (strip leading "Save to Wiki:"/"Create:"/"Research:" prefixes)
- seed search queries = the item's `search_queries` (NashSU passes them to
  `queueResearch` verbatim; fall back to the title if empty)
- one topic per invocation still applies — with multiple Research choices,
  run them serially
- resolve the item: `resolved_reason: "Queued for research"` (mark when the
  research is launched, matching NashSU)

**Create Page** → NashSU `createReviewPageDrafts` parity:
- page type routing (first match wins):
  - title/description matches entity keywords (person/tool/org/product/型号) → `entities/`
  - matches concept keywords (method/technique/理论/原理) → `concepts/`
  - contains comparison/compare/比较 → `comparisons/`
  - contains synthesis/综合 → `synthesis/`
  - else: missing-page item → `concepts/`; suggestion/contradiction → `queries/`
- missing-page items: create one page per missing `[[target]]` named in the item
- page body: `# <title>` + the item's description as seed content; frontmatter
  `type/title/created/tags: []/related: []`
- update `wiki/index.md` (section for the dir) + `wiki/log.md` entry
- resolve the item: `resolved_reason: "Created page(s): <names>"`

**Skip** → resolve only: `resolved_reason: "Skipped"`.

### Step 4: Report

Summary table: N processed — X research launched, Y pages created, Z skipped,
W left pending.

## Boundaries

- Never auto-choose for the user — every pending item gets an explicit human
  decision (that is the whole point vs sweep).
- Resolved review files stay on disk (audit trail) — never delete them.
- Deep Research here follows all `deep-research.md` gates (🔴 topic confirmed
  by the very act of choosing the option; no auto-chain to new topics).
