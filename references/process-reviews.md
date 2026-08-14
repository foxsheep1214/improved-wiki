# Process Reviews — 人工裁决 pending review items

参考 NashSU `review-view.tsx`（审核面板）: the CLI/agent counterpart of NashSU's
review panel. Sweep（`review-sweep.md`）is the **automatic** side — it clears
items already satisfied by later ingests. Process-reviews is the **human** side —
the user decides what to do with each still-pending item, using the options
that item actually carries.

**The routing and the writes are code, not prose.** Two modules port
`review-view.tsx`'s `handleResolve` and `review-create-page.ts`. Consult them;
do not re-derive behaviour from this document.

- `scripts/review_actions.py` — **pure**, decides. Which buttons an item offers
  (`buttons_for`), what a chosen action means (`route_review_action`), and
  which page(s) a Create Page produces (`create_review_page_drafts`,
  `create_page_decision` — the latter also fixes each draft's filename and
  `created` date).
- `scripts/_review_write.py` — **writes**. `write_created_pages` and
  `write_saved_query_page` create the page(s), add them to `wiki/index.md`,
  and append to `wiki/log.md`.

This file describes the *flow*; the modules own the *rules*. Everything moved
out of prose because prose cannot be tested and this section had silently
drifted from NashSU — first on all three routing questions (fixed 2026-08-05),
then twice more on the write side while the routing stayed correct.

In NashSU every one of these rules is code, inline in `handleResolve`. There is
no prose layer there at all: prose is improved-wiki's own surface, and so its
own drift risk. Treat any behaviour that exists here but not in a module as
already suspect.

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
| `item.options` — per-item data, parsed from the ingest REVIEW block | `options:` in the review file's frontmatter, derived from the type by `review_actions.buttons_for` |
| Deep Research button is UI-added for `suggestion`/`missing-page` only | same gate — the other three types never offer it |
| No OPTIONS line → parser default `Approve \| Skip` | `confirm` (NashSU's unrecognized-type bucket) offers `Approve \| Skip`, not `Create Page` |
| `__deep_research__` → `queueResearch(topic, searchQueries)` | run the deep-research flow (`deep-research.md`) with the item's `search_queries` as seed queries |
| Explicit Deep Research with no configured source → alert, leave unresolved | `blocked_no_search_source`: report the missing `web`/`anytxt` capability; keep pending |
| Heuristic research action with no configured source → falls through to Create Page | same fallback — this path does **not** block (it is the opposite of the explicit button) |
| `open:` / bare "open"/"查看" → preview the page, do **not** resolve | `open_page` with `resolves: false` — looking at a page is not triaging it |
| `delete:<path>` → delete file, resolve "Deleted" | `delete_file` |
| `save:<base64>` → decode, write `wiki/queries/`, resolve "Saved to Wiki" | `save_page` |
| `createReviewPageDrafts` type routing | `review_actions.create_review_page_drafts` — a direct port, two documented defect fixes |
| Create Page / `save:` 写页 + 更新 `index.md` + 追加 `log.md`（`handleResolve` 内联，:196-259 / :87-131） | `_review_write.write_created_pages` / `write_saved_query_page` — index 插入用全行匹配，修掉 NashSU 用 `includes()` 判断却用 `\n` 锚定正则插入导致的条目静默丢失 |
| Select-all checkbox + **Mark selected resolved** / **Dismiss selected** (`handleBatchResolve` / `handleBatchDismiss`) | `scripts/batch_resolve_reviews.py` — human supplies the filter and `--apply`; without `--apply` it only previews |
| `dismissItem(id)` removes the item from the store | the file is deleted. NashSU **does** persist review items — externally, not via a `persist` middleware: `auto-save.ts` subscribes to the store and debounce-writes `.llm-wiki/review.json`, and `App.tsx` rehydrates it with `loadReviewItems` on project open. What makes dismiss a deletion is *what* gets persisted: `dismissItem` does `items.filter(i => i.id !== id)` and auto-save writes that shorter array back, so the item leaves the stored record too. Deleting the file reproduces that net effect (user decision 2026-08-05, overriding this project's earlier "never delete" convention for this one verb) |
| `resolveItem(id, action)` — resolved in store, never deleted | frontmatter `resolved: true` + `resolved_at` + `resolved_reason` — file kept on disk (audit trail, same convention as sweep) |
| `clearResolved()` — `items.filter(i => !i.resolved)`, the button at review-view.tsx:332 | `batch_resolve_reviews.py --clear-resolved` (added 2026-08-13; previously the only way to shed a triaged backlog was `rm`). Selects the resolved set instead of the pending one and deletes; `--apply` still required, preview otherwise. **It costs more here than in NashSU and the CLI says so**: a resolved page on disk also suppresses its own regeneration and feeds sweep's resolved-wins dedup, so a cleared finding can return as a fresh pending item |
| `addItem` / `addItems` dedup against ALL existing items **including resolved ones** — review-store.ts:120 names the bug this fixes: deduping only against pending items "is exactly why re-surfacing a review during ingest discarded its resolved state" | `_review_utils.is_resolved_review_file(path)`, consulted by **every** regenerating writer before it writes (Stage 3.5, the caption-skip emitter, all four `wiki-lint-fix.py` branches, `wiki-lint-semantic.py`). A resolved page is left byte-for-byte untouched; a pending one is still refreshed, so a better description or a longer *Referenced by* list lands. Five of those seven sites regenerated `resolved: false` straight over a triaged page until 2026-08-13 — the exact regression NashSU calls out. **Divergence:** NashSU's `addItems` additionally unions `affectedPages`/`searchQueries` into a pending twin; the file port replaces the pending page wholesale instead |

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

Offer exactly the item's own options — read `options:` from its frontmatter,
or call `review_actions.buttons_for(review_type)`. They are **not** the same
for every type, and never invent a label outside that list:

| review_type | buttons |
|---|---|
| `suggestion`, `missing-page` | Deep Research · Create Page · Skip |
| `contradiction`, `duplicate` | Create Page · Skip |
| `confirm` | Approve · Skip |

Deep Research is recommended when `search_queries` is non-empty and a search
source is configured. Older review files predate the `options:` field; fall
back to `buttons_for(review_type)` for those.

### Step 3: Execute the choice

**Deep Research** → run the `deep-research.md` flow:
- topic = item title (strip leading "Save to Wiki:"/"Create:"/"Research:" prefixes)
- seed search queries = the item's `search_queries` (NashSU passes them to
  `queueResearch` verbatim; fall back to the title if empty)
- one topic per invocation still applies — with multiple Research choices,
  run them serially
- choosing the option confirms this topic; do not ask the same scope question again
- if the source mode has no usable configured capability, the behaviour depends
  on **which** research path was taken, and the two are opposites:
  - the explicit **Deep Research button** (`__deep_research__`) blocks — leave
    the review pending and offer configuration or the separate Create Page
    choice (`both` may proceed when either branch is configured)
  - a **heuristic** research action (any label containing research/investigate/
    explore/研究/调研/探索) falls through and creates a page instead
  `route_review_action` returns `blocked_no_search_source` vs `create_page`
  accordingly — do not decide this by hand
- do **not** auto-ingest the resulting query page
- resolve only after the page has been written successfully:
  `resolved_reason: "Research saved: wiki/queries/<saved-file>.md"`; search,
  synthesis, or write failure leaves the item pending

**Create Page** → call `review_actions.create_review_page_drafts(item, action)`
and create exactly the drafts it returns. Do not route by hand. The rules it
implements (first match wins, matched over **action + title + description** —
the action string participates):
- literal `entity`/`entities`/`实体` → `entities/`
- literal `concept`/`concepts`/`概念` → `concepts/`
- `comparison`/`compare`/`比较` → `comparisons/`
- `synthesis`/`综合` → `synthesis/`
- else by type: missing-page → `concepts/`; **every other type** → `queries/`

Only the literal words match. Semantic keyword lists (person/tool/org/product/
型号, method/technique/理论/原理) were this document's own invention and routed
pages NashSU would have sent to `queries/`.

- missing-page items fan out to one page per extracted candidate (colon tails,
  `缺少 X 页面`, `missing X`) — not per `[[target]]` wikilink
写入直接交给 `_review_write.write_created_pages(project, item, decision["drafts"])`，
不要手写文件、也不要手改 index/log：

```python
decision = route_review_action(item, action, has_search_source=...)
created = write_created_pages(project, item, decision["drafts"])
# 然后用 decision["resolve_reason"] 回填 review
```

它一次完成三件事，并且是原子写：

- 建页：frontmatter `type/title/created/tags: []/related: []`，正文
  `# <title>` + item 的 description 作为种子内容
- 记 `wiki/index.md`：条目插到该 dir 的 `## <Dir>` 小节紧下方；小节不存在就新建
- 记 `wiki/log.md`：`- <created>: Created N page(s) from review: \`<file>\`, ...`

文件名和 `created` 日期由 `create_page_decision` 定死在 draft 里，写入端直接用——
两边各算一次就会不一致，而 resolve 文案引的正是这个文件名。

resolve 文案用 `decision["resolve_reason"]`，不要自拟：单页
`Created: wiki/<dir>/<file_name>`，多页 `Created N pages`。单页记的是**可直接打开
的路径**而不是标题——审计轨迹要能定位到文件。

任何一页写失败即抛错：review 停在 pending，好过标成已解决却只写了一半。

**Skip** / **Approve** → resolve only, recording the action verbatim as
`resolved_reason`. Every dismissal label (skip / dismiss / ignore / approve /
keep existing / no / 跳过 / 忽略) lands here; anything else creates a page.

#### Other actions NashSU routes (previously undocumented)

An item's `options` may carry an action beyond the standard labels, and lint
or a human may supply one directly. Pass it through `route_review_action` —
these three branches exist in `handleResolve` and were missing here entirely:

- `open:<path>`, or a bare `open`/`view`/`打开`/`查看` → **preview the page and
  leave the item pending.** Viewing is not triaging. Without an explicit path
  it targets the first `affected_pages` entry, then `source_path`; with
  neither it is a no-op. This is the only non-blocking action that
  deliberately does not resolve.
- `delete:<path>` → delete that file, then resolve `"Deleted"`. Confirm the
  path with the user first — deletion is outside the default authorization for
  this flow.
- `save:<base64>` → 解码后交给
  `_review_write.write_saved_query_page(project, title, content)`，它写
  `wiki/queries/` 页并**同时更新 `wiki/index.md` 与 `wiki/log.md`**，
  resolve `"Saved to Wiki"`。注意这条路的 frontmatter **没有** `related` 键
  （NashSU review-view.tsx:98），与 Create Page 不同。
  An undecodable payload resolves `"Save failed"` rather than silently
  dropping the item.

  ⚠️ 这里和 Deep Research 的写入契约**相反，不要类比**：`save:` 和 Create Page
  都写 index+log（review-view.tsx:102-119、:219-241），而 Deep Research 只
  **读** `wiki/index.md` 做 grounding、从不写 index/log
  （deep-research.ts:339）。把两者当同一套会直接违反 SKILL.md 对 Deep Research
  “不得改动 index/log/overview” 的硬约束。

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
