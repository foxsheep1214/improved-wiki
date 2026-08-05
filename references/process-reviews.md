# Process Reviews — 人工裁决 pending review items

参考 NashSU `review-view.tsx`（审核面板）: the CLI/agent counterpart of NashSU's
review panel. Sweep（`review-sweep.md`）is the **automatic** side — it clears
items already satisfied by later ingests. Process-reviews is the **human** side —
the user decides what to do with each still-pending item, one at a time, using
the predefined options NashSU offers per item: **Deep Research / Create Page /
Skip**.

This flow is where `wiki/queries/` pages are born: ingest flags an open research
question as a REVIEW suggestion (with `search_queries`), and a query page only materializes when the user
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
| Review panel lists pending items | Calling agent scans `wiki/REVIEW/*/` for `resolved: false` |
| Per-item buttons: Deep Research / Create Page / Skip | Present the same three options to the user per item |
| `__deep_research__` → `queueResearch(topic, searchQueries)` | run the deep-research flow (`deep-research.md`) with the item's `search_queries` as seed queries |
| Explicit Deep Research with no configured selected source → alert and leave unresolved | report the missing `web`/`anytxt` capability; keep pending and let the user configure it or choose Create Page |
| `createReviewPageDrafts` type routing | same routing rules (below) |
| Select-all checkbox + **Mark selected resolved** / **Dismiss selected** (`handleBatchResolve` / `handleBatchDismiss`) | `scripts/batch_resolve_reviews.py` — human supplies the filter and `--apply`; without `--apply` it only previews |
| `dismissItem(id)` removes the item from the store | the file is deleted — NashSU has no persistence for review items at all (no `persist` middleware, `ingest.ts` never writes one to disk), so the in-memory store IS the record and removal from it is the whole lifecycle; `--dismiss` matches that exactly (user decision 2026-08-05, overriding this project's earlier "never delete" convention for this one verb) |
| `resolveItem(id, action)` — resolved in store, never deleted | frontmatter `resolved: true` + `resolved_at` + `resolved_reason` — file kept on disk (audit trail, same convention as sweep) |

## Workflow

### Step 1: Scan

List pending items: `wiki/REVIEW/*/` files with `resolved: false`.
Default focus: **suggestion** and **missing-page** (the two types that carry
`search_queries` and map to actions). Include the other three types
(confirm/contradiction/duplicate) only when the user asks for a full pass —
those usually need judgment/editing rather than one of the three buttons.

Present a short queue summary first (count by type). When the backlog is
large, offer the batch route below before grinding item-by-item —
measured on RadarWiki, 510 actionable items is 128 rounds of
four-at-a-time questions. Otherwise process items
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
- choosing the option confirms this topic; do not ask the same scope question again
- if the source mode has no usable configured capability, do not silently switch
  modes or auto-create a page; leave the review pending and offer configuration or
  the separate Create Page choice (`both` may proceed when either branch is configured)
- do **not** auto-ingest the resulting query page
- resolve only after the page has been written successfully:
  `resolved_reason: "Research saved: wiki/queries/<saved-file>.md"`; search,
  synthesis, or write failure leaves the item pending

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

### Step 3b: Batch route (NashSU select-all parity)

For a backlog the user wants cleared in bulk rather than adjudicated one by one:

```bash
# preview exactly what a filter selects — writes nothing
python3 "$SKILL_DIR/scripts/batch_resolve_reviews.py" --project <wiki-root> \
    --type suggestion --created-before 2026-08-01
# act on that same set
... --reason "Superseded by later ingest" --apply
```

- The **user** chooses the filter (`--type`, `--created-before`,
  `--title-contains`, `--limit`) and authorizes `--apply`. Always show the
  preview and the count first; treat `--apply` as the click on NashSU's button.
- `--dismiss` **deletes** the matched files instead of resolving them — NashSU
  parity, not a resolution with a reason. `--apply` is still required; without
  it the tool only previews what would be deleted. This is the one place in
  the review workflow where a file is removed rather than kept.
- Already-resolved items are never re-touched, so a filter is safe to re-run.
- This does not replace per-item adjudication for anything that needs judgment
  (Deep Research / Create Page). Use it for the stale tail; keep the three
  buttons for items with real research value.
- Run `sweep_reviews.py` first when the backlog predates later ingests — sweep
  is the automatic side and removes items that no longer need any human at all.

### Step 4: Report

Summary table: N processed — X research pages saved, Y pages created, Z skipped,
W left pending. Separately report research attempts that failed and therefore
left their reviews pending.

## Boundaries

- Never auto-choose for the user. The *agent* must not decide; the human does.
  That does **not** mean one decision per item: NashSU's panel offers select-all
  plus batch resolve/dismiss, so one human decision may legitimately cover N
  items (`scripts/batch_resolve_reviews.py`). What is forbidden is the agent
  picking the filter, or firing `--apply`, on its own — a bulk action needs the
  same explicit instruction a single one does.
- Resolved review files stay on disk (audit trail) — never delete them.
  Exception: `batch_resolve_reviews.py --dismiss` deletes on purpose, matching
  NashSU's `dismissItem`; that is the only sanctioned deletion path for a
  review file.
- Deep Research here follows all `deep-research.md` gates (🔴 topic confirmed
  by the very act of choosing the option; no auto-chain to new topics).
- Launching/queuing research is not resolution. The saved path is the success
  boundary, matching NashSU v0.6.7's `resolveReviewForSavedResearch` guard.
