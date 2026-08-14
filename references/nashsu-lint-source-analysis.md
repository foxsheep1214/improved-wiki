# NashSU llm_wiki lint source analysis（re-verified against v0.6.6, 2026-07-29）

Detailed source-level comparison of NashSU's actual lint implementation
(`src/lib/lint.ts`, `src/lib/lint-structural-core.ts`,
`src/components/lint/lint-view.tsx`, `src/stores/lint-store.ts`,
`src/stores/review-store.ts`) vs. `improved-wiki`'s `scripts/wiki-lint.sh`,
`scripts/_lint_suggest.py`, and `scripts/wiki-lint-semantic.py`.

**Source snapshot checked (2026-07-29)**:
- local release: `/Users/skyfend/Downloads/llm_wiki-0.6.6`
- `src/lib/lint.ts` — file loading, semantic prompt, missing-page filter
- `src/lib/lint-structural-core.ts` — indexed structural scan and suggestions
- `src/components/lint/lint-view.tsx` — scan/fix/delete UI behavior
- `src/stores/lint-store.ts` / `review-store.ts` — persisted result models

**Repo URL**: <https://github.com/nashsu/llm_wiki> (analysis verified against v0.6.6)

This file is the **why** behind the skill's lint design（SKILL.md 的 Lint 命令部分）. Read it when
adding lint features, debugging parity issues, or porting the lint to a new
implementation.

---

## 1. Two operations, not one

`src/lib/lint.ts` exports **two** public functions:

| Function | Purpose | LLM? |
|---|---|---|
| `runStructuralLint(projectPath)` | Pure mechanical scan | No |
| `runSemanticLint(projectPath, llmConfig)` | LLM-driven contradiction / stale / missing-page / suggestion | Yes |

**Deviation (re-confirmed against local NashSU v0.6.6):** NashSU's
`runSemanticLint` prompt lists exactly those 4 types. `wiki-lint-semantic.py` asks for
a 5th — `term-ambiguity` (same slug/term used for two genuinely different concepts,
not disambiguated) — a deliberate improved-wiki-only extension. It's not new: it
started life as `cross-domain-ambiguity` under the now-removed `domain` frontmatter
system (commit 96945f6, 2026-06-29) and was renamed/kept rather than deleted when
`domain` was ripped out, since the underlying check (same term, different meanings)
is still useful without the domain field. Same category of extra as
`missing-frontmatter` on the structural side (§7 below) — deliberate, not drift —
but this one hadn't been written down here until now.

The UI can tie them together in `lint-view.tsx`, but `runSemantic` defaults to
`false` in v0.6.6:
```ts
const structural = await runStructuralLint(pp)
let all = structural
if (runSemantic && hasUsableLlm(llmConfig)) {
  const semantic = await runSemanticLint(pp, llmConfig)
  all = [...structural, ...semantic]
}
addLintItems(all)
```

**Improved-wiki policy (user-selected, 2026-07-29)**:
`scripts/wiki-lint-semantic.py` remains part of a plain `wiki-lint.sh` run, so
default lint means **structural + semantic**, followed by default
`emit-review`, `fix`, `fix-links`, `sweep`, and one `dedup` round. This default
automation differs from NashSU's unchecked semantic toggle and per-item
human-clicked fixes; the scan algorithms themselves follow v0.6.6.

After those stages, plain lint exits 102 and requires the calling agent to ask
the user before `delete-orphans`. Approval runs
`wiki-lint.sh --delete-orphans-only`, which refreshes the structural cache and
creates preview/Review items without deleting pages. `--diagnostic-only` and
`--structural-only` retain non-mutating alternatives.

**Semantic vs Review** — the *third* layer:
- `lint.json` = structural + semantic findings (all go to `useLintStore`)
- `review.json` = ingest-generated human-triage items (5 categories, goes to `useReviewStore`)
- These are **two separate stores / two separate files** in the app. Improved-wiki
  preserves the same split. Semantic warnings enter improved-wiki's
  `wiki/REVIEW/` by default; `--no-emit-review` disables that routing.

---

## 2. `runStructuralLint` — the v0.6.6 behavioral contract

File loading/tokenization lives in `src/lib/lint.ts`; the indexed scan lives in
`src/lib/lint-structural-core.ts`. Key behaviors:

### 2.1 Wikilink resolution: normalized + dual-indexed

```ts
function normalizeTarget(target: string): string {
  return target.replace(/\\/g, "/")
    .replace(/^wiki\//i, "")
    .replace(/\.md$/i, "")
    .trim()
    .toLowerCase()
}

slugMap.set(normalizeTarget(page.slug), index)
slugMap.set(normalizeTarget(basename), index)
```

This means:
- `[[entities/foo-bar/transformer]]` resolves to `entities/foo-bar/transformer.md`
- `[[Transformer]]` ALSO resolves to the same file (basename lookup)
- `[[transformer]]` and `[[TRANSFORMER]]` resolve case-insensitively
- `[[wiki/entities/foo-bar/transformer.md]]` resolves after prefix/suffix removal
- `[[entities\foo-bar\transformer.md]]` resolves after slash normalization

**Improved-wiki**: `normalize_link_target()` and `_build_slug_map()` port this
v0.6.6 behavior. On a cross-file basename collision the last-scanned page wins,
exactly like NashSU's `Map.set` (last-write-wins).

### 2.2 In-link computation uses the same normalization and basename fallback

```ts
const target = slugMap.get(normalizeTarget(link))
  ?? slugMap.get(normalizeTarget(fileName(link).replace(/\.md$/i, "")))
if (target !== undefined) {
  inboundCounts.set(target, (inboundCounts.get(target) ?? 0) + 1)
}
```

Inbound counts are keyed by page index, so every syntactic form listed in §2.1
prevents the target from being falsely reported as orphaned. Improved-wiki uses
the same normalized exact-path-then-basename lookup for both inbound counts and
broken-link existence checks.

### 2.3 The three structural categories

```ts
// Orphan
if (inbound === 0) results.push({ type: "orphan", severity: "info", ... })

// No outbound links
if (p.outlinks.length === 0) results.push({ type: "no-outlinks", severity: "info", ... })

// Broken links (per-outlink, not per-page)
for (const link of p.outlinks) {
  const basename = fileName(link).replace(/\.md$/i, "")
  const exists =
    slugMap.has(normalizeTarget(link)) ||
    slugMap.has(normalizeTarget(basename))
  if (!exists) results.push({ type: "broken-link", severity: "warning", ... })
}
```

Detail strings (verbatim, used in both `lint.json` files for app-interop):
- `orphan`: `"No other pages link to this page."`
- `no-outlinks`: `"This page has no [[wikilink]] references to other pages."`
- `broken-link`: `` `Broken link: [[${link}]] — target page not found.` ``

**Improved-wiki**: matches all three strings exactly, and emits orphan /
no-outlinks for **every** content page — no frontmatter filter, no stub-length
filter — identical to the app. (The only exclusions are `ANCHOR_FILES` and the
`AGGREGATE_FILES` finding-exemption; see §2.4.)

### 2.4 Excluded from orphan check (lint.ts L80-82)

```ts
const contentFiles = wikiFiles.filter(
  (f) => f.name !== "index.md" && f.name !== "log.md"
)
```

**Improved-wiki equivalent**: the *exclusion* set is exactly NashSU's two files —
`ANCHOR_FILES = {"index.md", "log.md"}` (`_lint_suggest.py:43`), dropped from the
scan entirely. A **separate** `AGGREGATE_FILES = {"index.md", "log.md",
"overview.md", "schema.md"}` is still *scanned* (so `overview.md`/`schema.md`
outlinks count toward inbound, preventing false orphans on pages only the overview
links to) but is *exempt from emitted findings*, so the headless auto-fixer never
mutates a generated aggregate.

### 2.5 No short-stub / frontmatter filter (parity, re-verified 2026-06-30)

The app emits `no-outlinks` and `orphan` for **all** content pages, including
short stubs. Improved-wiki matches this exactly:
`_lint_suggest.run_structural_lint` applies **no** `len(text) < 200` filter and
**no** frontmatter filter. The cost is that a fresh single-ingest wiki will emit
many `no-outlinks`/`orphan` findings — that is the intended NashSU-aligned behavior.

### 2.6 v0.6.6 indexed suggestions

NashSU no longer scores every page against every finding. It builds:

- a token inverted index for orphan/no-outlink related-page suggestions;
- an NFKC character-bigram index for broken-link suggestions;
- a score-descending/index-ascending top-64 candidate window;
- common-token pruning when a token appears in more than
  `max(20, ceil(page_count × 0.25))` pages.

The scoring thresholds remain `0.74` for broken-link candidates and `0.08` for
related pages, with same-folder `+0.08`, single-CJK-token weight `0.35`,
same-basename `0.96`, and contains-target `0.82`.

`_lint_suggest.py` now ports these indexes, normalization rules, stable ordering,
and candidate cap. Improved-wiki retains its headless safety extensions:
aggregate pages are not suggested as mutation targets, ambiguous top-score ties
are withheld, and `suggested_score` is persisted so batch fixes can apply a
stricter confidence gate.

---

## 3. `runSemanticLint` — the LLM-driven audit

Full implementation: 135 lines. Key behaviors:

### 3.1 The LINT block format (lint.ts L161-162)

```ts
const LINT_BLOCK_REGEX =
  /---LINT:\s*([^\n|]+?)\s*\|\s*([^\n|]+?)\s*\|\s*([^\n-]+?)\s*---\n([\s\S]*?)---END LINT---/g
```

**Format**:
```
---LINT: type | severity | Short title---
Description of the issue.
PAGES: page1.md, page2.md
---END LINT---
```

**Regex character classes decoded**:
- `([^\n|]+?)` — non-greedy capture, stops at newline OR pipe
- `([\s\S]*?)` — body is anything (including newlines), non-greedy
- Requires both `---LINT:` and `---END LINT---` to match — truncation breaks the regex

### 3.1b Single un-batched call vs improved-wiki's batching + `dedup_findings()` (re-confirmed against v0.6.6)

**NashSU has no batching and no deduplication anywhere in the lint pipeline.**
`runSemanticLint` builds `summaries.join("\n\n")` — every page summary in the
wiki, concatenated into **one** prompt, sent in **one** `streamChat` call
(`lint.ts` L356-401). The caller (`lint-view.tsx` `handleRunLint`,
L88-93) does `all = [...structural, ...semantic]` — a plain array spread,
no reconciliation of any kind, not even structural-vs-semantic. Grepping
`dedup`/`Dedup`/`duplicate` across `lib/lint.ts` and
`components/lint/lint-view.tsx` returns zero matches.

**Improved-wiki must batch** (a wiki that doesn't fit in one call would
silently truncate NashSU's single-call design), and batching is itself
already a documented divergence — see §3.3/§5. `dedup_findings()` in
`wiki-lint-semantic.py` (dedup key: lowercased page + raw_type + first 80
chars of detail) exists **only because batching creates a problem NashSU
never has**: each batch's LLM call is blind to what other batches found, so
the same real-world issue can surface once per batch that happens to see
both affected pages (e.g. a cross-page contradiction flagged from both
ends). NashSU's single call has full-wiki context in one shot, so this
duplication can't occur there in the first place — there was never anything
to dedup. Likewise the `lint-semantic-<n>` id renumbering after dedup is
improved-wiki-only bookkeeping: NashSU's ids are assigned client-side by the
ephemeral Zustand store when items are added, not persisted by `lint.ts`
itself (see §5).

**Do not read `dedup_findings()` as a NashSU-parity feature.** It is a
correctness patch for a deviation improved-wiki already made (batching), not
a port of anything in `lint.ts`.

### 3.2 Four semantic sub-types

- `contradiction` — two or more pages make conflicting claims
- `stale` — information that appears outdated or superseded
- `missing-page` — important concept is heavily referenced but lacks a dedicated page
- `suggestion` — a question or source worth adding to the wiki

All merged into `type: "semantic"` (not a separate `type` per sub-type), with the raw sub-type preserved in the `detail` string as `[contradiction] ...` etc.

### 3.2b v0.6.6 missing-page false-positive filter

NashSU v0.6.6 builds an `existingPageNames` set from every page basename and
frontmatter title. Both candidates and LLM titles are normalized with
`normalizeReviewTitle(...).normalize("NFKC").trim().toLowerCase()`. A
`missing-page` block is dropped only when its normalized short title is an
**exact** member of that set; substring matching is deliberately rejected.

The prompt also requires a missing-page `Short title` to contain only the exact
missing concept/entity name, without explanatory prefixes or suffixes.
`wiki-lint-semantic.py` now ports both protections. Its existence index covers
the whole wiki even when `--limit` caps the summaries sent for a diagnostic run.

### 3.3 Per-page summary size: 500 chars (lint.ts L196)

```ts
const preview = content.slice(0, 500) + (content.length > 500 ? "..." : "")
```

Plus the frontmatter is included if it's at the top of the content (frontmatter comes first, so `content.slice(0, 500)` includes it).

### 3.4 Language detection (lint.ts L213)

```ts
const summarySample = summaries.join("\n").slice(0, 2000)
buildLanguageDirective(summarySample)  // auto-detects non-English wikis
```

The first 2000 chars of concatenated summaries are used to auto-detect the
output language. This is the same auto-detection the Ingest pipeline uses.

**Improved-wiki implementation**: semantic lint calls the shared
`_language.build_language_directive` (`scripts/_language.py`) — the **same**
module the Ingest pipeline uses — which detects 25+ languages via Unicode
script ranges + Latin diacritic/word patterns, not a CJK-vs-Latin heuristic.
Set `IMPROVED_WIKI_OUTPUT_LANGUAGE` to force a fixed output language.

### 3.5 Output: `useLintStore.addItems(results)` (lint.ts L285-290)

The semantic results go into the **same** Zustand store as the structural
results. The UI does not distinguish between them in the Lint tab. They are
distinguishable by `type: "semantic"` vs the other 3.

**Improved-wiki**: writes to `lint-semantic.json` (separate file) for now. This
is a deliberate **divergence from the app** — see "Persisted vs ephemeral"
below. Reason: the app keeps lint results in memory only; improved-wiki needs
them on disk so cron output and review workflows can consume them.

### 3.6 Truncation failure mode (LINT blocks must be complete)

If `max_tokens` is too low, the LLM may emit 10 `---LINT:` blocks but only
complete 3 of them with `---END LINT---`. The regex requires both — a truncated
block is silently dropped.

**Verified 2026-06-11 on radar wiki (198 pages, 108K input chars, max_tokens=4096)**:
LLM produced 19 starting `---LINT:` blocks, **0 `---END LINT---` markers** in
the output. Parsed: 0 findings. Workaround: `--max-tokens 8192` for large wikis.

---

## 4. `useReviewStore` — the separate human-triage layer

`src/stores/review-store.ts` defines 5 review types:
```ts
export interface ReviewItem {
  type: "contradiction" | "duplicate" | "missing-page" | "confirm" | "suggestion"
  title, description, sourcePath?, affectedPages?, searchQueries?
  options: ReviewOption[]   // 1+ action buttons ("Approve" / "Skip" / "Create Page" / etc.)
  resolved: boolean
  resolvedAction?: string
  createdAt: number
}
```

**Persisted to**: `.llm-wiki/review.json` (NashSU v0.4.23+).

**Auto-deduplication** (review-store.ts L51-96): bulk adds use
`type::normalizeReviewTitle` as a dedup key. When a duplicate is found, the
incoming item's `description` / `sourcePath` override the old, and
`affectedPages` / `searchQueries` are unioned.

**Sources of review items** (`ingest.ts` L1097-1104):
```ts
const reviewItems = [
  ...parseReviewBlocks(generation, sp),         // Stage 2.3's FILE/REVIEW blocks
  ...parseReviewBlocks(reviewSuggestionOutput, sp),  // dedicated review pass
]
if (reviewItems.length > 0) {
  useReviewStore.getState().addItems(reviewItems)
}
```

**Dedicated-review trigger** (`ingest.ts` L889):
`shouldRunDedicatedReviewStage(generation)` fires when generation is ≥10K chars,
contains ≥4 FILE blocks, or ends with an incomplete REVIEW block.

**REVIEW block format** (`ingest.ts` L1623):
```ts
const REVIEW_BLOCK_REGEX = /---REVIEW:\s*(\w[\w-]*)\s*\|\s*(.+?)\s*---\n([\s\S]*?)---END REVIEW---/g
```

```
---REVIEW: contradiction | This page says X, page Y says Z---
Two pages give conflicting values for ADC SNR.
OPTIONS: Resolve now | Skip
PAGES: concepts/snr-budget.md, sources/Radar Handbook - 2008 - Skolnik.md
SEARCH: ADC SNR budget | radar SNR budget
---END REVIEW---
```

**Improved-wiki**: Stage 3.1 implements the same five review types and validates
the complete response before mutation. Stage 3.5 persists one Markdown page
per item plus `.llm-wiki/review-suggestions.json`. The content contract is
aligned, but the storage layout is intentionally not byte-identical to the
app's single `.llm-wiki/review.json` array.

---

## 5. Persistence model

| Layer | Desktop app (0.6.6) | improved-wiki | Notes |
|---|---|---|---|
| structural lint | On disk: `.llm-wiki/lint.json` (`persist.ts` saveLintItems/loadLintItems + `auto-save.ts` debounced 1s, flush-on-switch, load-on-open) | On disk: `<state_dir>/lint-cache.json` | Both persist; only filename/shape differ |
| semantic lint | On disk: same `lint.json` store (`useLintStore`) | On disk: `<state_dir>/lint-semantic.json` (kept separate — see §7.6) | Both persist |
| `review.json` | On disk (`.llm-wiki/review.json`) | On disk (`.llm-wiki/review.json`) | Aligned |

**Human-browsable lint pages location (2026-06-21)**: NashSU has no on-disk lint
pages at all (app UI renders findings from `useLintStore`). improved-wiki writes
one `.md` per finding for CLI browsing — these live under `<state_dir>/lint/`
(i.e. `.llm-wiki/lint/`), **not** under `wiki/`. Rationale: lint pages are
derived diagnostic output, not source knowledge; keeping them out of `wiki/`
prevents `collect_summaries` (semantic lint's `wiki_dir.rglob("*.md")`) and any
future wiki-tree scan from ingesting its own previous findings. `wiki-lint.sh`
leaves any legacy `wiki/lint/` untouched during a diagnostic run and writes all
new findings to `<state_dir>/lint/`.
Machine-readable caches (`lint-cache.json`, `lint-semantic.json`) were already
under `<state_dir>/` and are unchanged.

**One-writer discipline** (per `llm-wiki-local` skill): never run
plain `wiki-lint.sh` (or `--semantic`) and the desktop app's "Run Lint" button at the same
time. Both write the same `useLintStore` (in app memory) AND (now) the same
`lint-semantic.json` on disk. The Zustand counter (`lint-${++counter}`) is
monotonic but resets on app restart, so the IDs from the two tools will collide
on next app launch if both wrote in the same session.

---

## 6. UI's "Fix" action — ported as `--fix-links` / `--delete-orphans`

In 0.6.6 `lint-view.tsx` fix/delete handlers mutate files only after a user
clicks the relevant item action:
`lint-fixes.ts`:
- `broken-link` → `rewriteWikilinkTarget` (or `ensureBrokenLinkStub` when there
  is no suggestion) — rewrites the link / creates a stub page.
- `orphan` → `appendWikilink` from the suggested source (gives it an inbound
  link), or `handleDeleteOrphan` → `cascadeDeleteWikiPagesWithRefs`.
- `no-outlinks` → `appendWikilink` to the suggested target.
- `semantic` → routed to the Review store for manual resolution.

The port has these CLI equivalents. The first five are enabled by a plain lint
run (each has a `--no-*` override):
- `--fix` → repair missing frontmatter (improved-wiki structural extension).
- `--fix-links` → `wiki-lint-fix.py` + `_lint_fixes.py`
  (`rewriteWikilinkTarget` / `appendWikilink`; unsafe or unsuggestable findings
  become Review items rather than silent drops).
- `--emit-review` → route warning-severity semantic findings into
  `wiki/REVIEW/` for human triage.
- `--sweep` / `--dedup` → higher-level maintenance workflows.

`delete-orphans` is different: a plain run stops at exit 102 and asks the user.
Only approved `--delete-orphans-only` runs the fresh scan + preview/Review
stage. Real
  cascade deletion remains the separately explicit
  `wiki-lint-fix.py --delete-orphans --apply` operation (file + index entry +
  inbound `[[links]]` + `related:` refs). Re-embedding is still needed to clear
  removed pages' vector chunks.

This is a user-selected headless workflow policy, not a NashSU default-UI
parity claim. The destructive boundary remains explicit: orphan preview is
human-gated and actual deletion needs a second, separately confirmed command.

---

## 7. What to verify before claiming "lint parity"

If you change `wiki-lint.sh`, `_lint_suggest.py`, or
`wiki-lint-semantic.py` and want to claim v0.6.6 behavioral alignment, check:

1. **Finding shape/detail/severity** — the three NashSU structural categories
   retain their exact strings; broken links are `warning`, orphan/no-outlinks
   are `info`.
2. **Resolution normalization** — case, `wiki/`, `.md`, and backslashes
   normalize identically; exact relative path and basename are both indexed.
3. **Indexed suggestion engine** — token/bigram indexes, common-token pruning,
   deterministic top-64 window, and v0.6.6 thresholds remain covered by tests.
4. **Missing-page filter** — basename + page title, NFKC, review-title prefix
   normalization, and exact equality only.
5. **Interaction policy** — plain lint runs the first five maintenance actions;
   delete-orphans pauses at exit 102, and actual deletion remains separately
   confirmed. Diagnostic-only modes must stay non-mutating.
6. **Persistence split** — improved-wiki keeps structural and semantic JSON
   caches separate even though the app presents them together.

Intentional improved-wiki extensions remain:

- `missing-frontmatter` on the structural side and `term-ambiguity` on the
  semantic side;
- a hyphen-safe LINT title regex (`[^\n]+?`) instead of NashSU's
  `[^\n-]+?`, which drops titles such as `MIL-STD-1553`;
- context-sized batching, cross-batch `dedup_findings()`, and id renumbering;
- `suggested_score`, ambiguous-tie suppression, and aggregate-page mutation
  guards for safe headless fixes;
- separate `lint-cache.json` / `lint-semantic.json` diagnostic files and
  human-readable `.llm-wiki/lint/*.md` pages.

If any of these break, the app may show "0 findings" even when improved-wiki
found issues, because the app filters / sorts / groups by type and severity.
The user's mental model of "improved-wiki and the app agree" will break.
