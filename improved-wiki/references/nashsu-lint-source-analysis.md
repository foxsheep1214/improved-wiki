# NashSU llm_wiki lint source analysis（re-verified against v0.5.3, 2026-06-29）

Detailed source-level comparison of NashSU's actual lint implementation
(`src/lib/lint.ts`, `src/stores/lint-store.ts`, `src/stores/lint-store.ts`,
`src/components/lint/lint-view.tsx`, `src/lib/ingest.ts`) vs. `improved-wiki`'s
`scripts/wiki-lint.sh` + `scripts/wiki-lint-semantic.py`.

**Source files pulled (2026-06-11)**:
- `src/lib/lint.ts` — 299 lines, contains `runStructuralLint` + `runSemanticLint`
- `src/stores/lint-store.ts` — 66 lines, Zustand store for `useLintStore`
- `src/stores/review-store.ts` — 117 lines, Zustand store for `useReviewStore`
- `src/components/lint/lint-view.tsx` — 426 lines, the "Run Lint" UI button
- `src/lib/ingest.ts` — 2993 lines; lint/review interaction at L888-933 (Stage 3.4) + L1097-1104 (Stage 3.4)

**Repo URL**: <https://github.com/nashsu/llm_wiki> (analysis verified against v0.5.3)

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

**Deviation (confirmed 2026-07-10 against local NashSU v0.6.0 checkout):** NashSU's
`runSemanticLint` prompt lists exactly those 4 types. `wiki-lint-semantic.py` asks for
a 5th — `term-ambiguity` (same slug/term used for two genuinely different concepts,
not disambiguated) — a deliberate improved-wiki-only extension. It's not new: it
started life as `cross-domain-ambiguity` under the now-removed `domain` frontmatter
system (commit 96945f6, 2026-06-29) and was renamed/kept rather than deleted when
`domain` was ripped out, since the underlying check (same term, different meanings)
is still useful without the domain field. Same category of extra as
`missing-frontmatter` on the structural side (§7 below) — deliberate, not drift —
but this one hadn't been written down here until now.

The UI ties them together in `lint-view.tsx` L73-94:
```ts
const structural = await runStructuralLint(pp)
let all = structural
if (runSemantic && hasUsableLlm(llmConfig)) {
  const semantic = await runSemanticLint(pp, llmConfig)
  all = [...structural, ...semantic]
}
addLintItems(all)
```

**Improved-wiki port (2026-06-11)**: `scripts/wiki-lint-semantic.py` is a direct
Python translation of `runSemanticLint`. `wiki-lint.sh --semantic` mirrors the
UI's "if runSemantic" gate.

**Semantic vs Review** — the *third* layer:
- `lint.json` = structural + semantic (4+1 categories, all go to `useLintStore`)
- `review.json` = ingest-generated human-triage items (5 categories, goes to `useReviewStore`)
- These are **two separate stores / two separate files** in the app. Improved-wiki
  preserves the same split.

---

## 2. `runStructuralLint` — the byte-for-byte spec

Full implementation: `src/lib/lint.ts` L69-157 (88 lines). Key behaviors:

### 2.1 Wikilink resolution: case-insensitive + dual-indexed (lint.ts L46-65)

```ts
function buildSlugMap(wikiFiles, wikiRoot) {
  const map = new Map<string, string>()
  for (const f of wikiFiles) {
    const rel = getRelativePath(f.path, wikiRoot).replace(/\.md$/, "")
    map.set(rel.toLowerCase(), f.path)              // by relative path
    map.set(f.name.replace(/\.md$/, "").toLowerCase(), f.path)  // by basename
  }
  return map
}
```

This means:
- `[[entities/foo-bar/transformer]]` resolves to `entities/foo-bar/transformer.md`
- `[[Transformer]]` ALSO resolves to the same file (basename lookup)
- `[[transformer]]` resolves to the same file (case-insensitive)
- `[[TRANSFORMER]]` resolves to the same file (case-insensitive)

**Improved-wiki**: ports `buildSlugMap` 1:1 (`_lint_suggest.py::_build_slug_map`)
with plain lowercase-keyed assignment — on a cross-file basename collision the
last-scanned page wins, exactly like NashSU's `map.set` (last-write-wins).

### 2.2 In-link computation with case-insensitive lookup (lint.ts L101-112)

```ts
const inboundCounts = new Map<string, number>()
for (const p of pages) {
  for (const link of p.outlinks) {
    const lookup = link.toLowerCase()
    const target = slugMap.has(lookup)
      ? relativeToSlug(getRelativePath(slugMap.get(lookup)!, wikiRoot)).toLowerCase()
      : lookup
    inboundCounts.set(target, (inboundCounts.get(target) ?? 0) + 1)
  }
}
```

Each `[[link]]` is lowercased → looked up in slugMap → if found, the **original-case stem** is used as the inbound key. This means `inboundCounts["entities/foo-bar/transformer"]` is incremented whether the wikilink was `[[Transformer]]` or `[[transformer]]`.

**Improved-wiki equivalent**: `resolve_slug()` returns the original-stem (not lowercased) for the dictionary key, so the `in_links[resolved].add(src)` accumulator also works case-insensitively.

### 2.3 The four categories (lint.ts L116-154)

```ts
// Orphan
if (inbound === 0) results.push({ type: "orphan", severity: "info", ... })

// No outbound links
if (p.outlinks.length === 0) results.push({ type: "no-outlinks", severity: "info", ... })

// Broken links (per-outlink, not per-page)
for (const link of p.outlinks) {
  const lookup = link.toLowerCase()
  const basename = getFileName(link).replace(/\.md$/, "").toLowerCase()
  const exists = slugMap.has(lookup) || slugMap.has(basename)
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

---

## 3. `runSemanticLint` — the LLM-driven audit (lint.ts L164-299)

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

### 3.2 Four semantic sub-types

- `contradiction` — two or more pages make conflicting claims
- `stale` — information that appears outdated or superseded
- `missing-page` — important concept is heavily referenced but lacks a dedicated page
- `suggestion` — a question or source worth adding to the wiki

All merged into `type: "semantic"` (not a separate `type` per sub-type), with the raw sub-type preserved in the `detail` string as `[contradiction] ...` etc.

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
  ...parseReviewBlocks(reviewSuggestionOutput, sp),  // Stage 3.4 dedicated review pass
]
if (reviewItems.length > 0) {
  useReviewStore.getState().addItems(reviewItems)
}
```

**Stage 3.4 trigger** (`ingest.ts` L889): `shouldRunDedicatedReviewStage(generation)` fires when generation is ≥10K chars OR ≥4 FILE blocks OR ends with an incomplete REVIEW block.

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

**Improved-wiki**: `scripts/ingest.py` already implements Stage 3.4 (`parseReviewBlocks`) and writes to `.llm-wiki/review.json` with the same 5-type schema. **This is already byte-compatible with the app's `review.json`.**

---

## 5. Persisted vs ephemeral: the BIG divergence

| Layer | Desktop app (0.5.3) | improved-wiki | Notes |
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
auto-migrates a legacy `wiki/lint/` into `<state_dir>/lint/` on first run.
Machine-readable caches (`lint-cache.json`, `lint-semantic.json`) were already
under `<state_dir>/` and are unchanged.

**One-writer discipline** (per `llm-wiki-local` skill): never run
`wiki-lint.sh --semantic` and the desktop app's "Run Lint" button at the same
time. Both write the same `useLintStore` (in app memory) AND (now) the same
`lint-semantic.json` on disk. The Zustand counter (`lint-${++counter}`) is
monotonic but resets on app restart, so the IDs from the two tools will collide
on next app launch if both wrote in the same session.

---

## 6. UI's "Fix" action — ported as `--fix-links` / `--delete-orphans`

In 0.5.3 `lint-view.tsx` `handleFix(item)` actually MUTATES files via
`lint-fixes.ts`:
- `broken-link` → `rewriteWikilinkTarget` (or `ensureBrokenLinkStub` when there
  is no suggestion) — rewrites the link / creates a stub page.
- `orphan` → `appendWikilink` from the suggested source (gives it an inbound
  link), or `handleDeleteOrphan` → `cascadeDeleteWikiPagesWithRefs`.
- `no-outlinks` → `appendWikilink` to the suggested target.
- `semantic` → routed to the Review store for manual resolution.

The port HAS these (CLI, not UI):
- `--fix-links` → `wiki-lint-fix.py` + `_lint_fixes.py`
  (`rewriteWikilinkTarget` / `appendWikilink` / `ensureBrokenLinkStub`).
- `--delete-orphans` → `wiki-lint-fix.py` `cascade_delete_orphans` (file + index
  entry + inbound `[[links]]` + `related:` refs; dry-run by default, `--apply`
  to delete). Embedding-chunk removal is the one piece not done from the CLI (no
  LanceDB handle) — re-embed clears the phantom chunks.

The only genuine gap vs the desktop Fix surface is the per-item Review-store
fallback when a finding has no suggestion (the port silently skips those rather
than handing off to `review.json`).

---

## 7. What to verify before claiming "lint parity"

If you change `wiki-lint.sh` and want to claim "byte-compatible with the app's
lint", check these:

1. **`lint.json` shape** — every finding has `{type, severity, page, detail, id, createdAt}`. No `affectedPages` (that's semantic-only).
2. **Detail strings exact** — `orphan` = "No other pages link to this page." / `no-outlinks` = "This page has no [[wikilink]] references to other pages." / `broken-link` = `` `Broken link: [[${link}]] — target page not found.` ``
3. **Severity values** — `broken-link` is `warning`; the other two are `info`. Never `error` (`missing-frontmatter` is an improved-wiki-only structural extra, emitted by `wiki-lint.sh`, not part of NashSU's `lint.json`).
3b. **`term-ambiguity` is an improved-wiki-only semantic extra** — see §1 above. Not part of NashSU's `runSemanticLint` type list.
3c. **`---LINT:...---` title-group regex intentionally deviates from NashSU's literal source.** NashSU's own regex (`lint.ts` L161-162) uses `[^\n-]+?` for the title capture group, which excludes hyphens — any LLM-generated title containing one (e.g. a model number like "MIL-STD-1553", extremely common in this KB) fails to match at all, silently dropping the whole finding. Confirmed live against a real batch (2026-07-10): 1 of 5 findings lost with the literal-parity regex. `wiki-lint-semantic.py`'s `LINT_BLOCK_REGEX` uses `[^\n]+?` for that group instead — same delimiters, same 4 capture groups, just doesn't choke on a hyphen inside the title. This is a bug-for-bug non-parity by design; don't "fix" it back to match NashSU's literal regex.
4. **Case-insensitive resolution** — `[[Transformer]]` and `[[transformer]]` resolve the same way.
5. **Dual indexing** — `[[foo]]` resolves to `entities/foo.md` if that file exists.
6. **`lint-semantic.json` separate from `lint.json`** — don't merge them; the app's lint view shows them together via the UI but the on-disk state files are separate.

If any of these break, the app may show "0 findings" even when improved-wiki
found issues, because the app filters / sorts / groups by type and severity.
The user's mental model of "improved-wiki and the app agree" will break.
