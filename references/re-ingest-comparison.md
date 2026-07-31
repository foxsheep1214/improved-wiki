# Re-Ingest Comparison Workflow

When you need to re-ingest a book that's already been digested (e.g., to fix
weak digestion, or to compare old vs new pipeline results):

## 🔴 Ask first: full redo, or analysis-only?

**Before touching anything**, ask the user which of the two flows below they
want. Do not default to a full `--delete` wipe — the two flows are not
interchangeable and one of them is only partially reversible:

| | Full redo | Analysis-only (`--keep-media`) |
|---|---|---|
| OCR text | re-extracted | reused from `.llm-wiki/extract-tmp/` cache |
| Images + captions | re-extracted | **kept as-is**, `wiki/media/<slug>/` untouched |
| Stage 2.2+ analysis/generation | redone | redone |
| Time cost | full minerU re-run (minutes-per-book) | skips minerU entirely |
| When to use | user suspects OCR/caption quality issues, or media was never any good | user confirms OCR/images/captions were already fine — only the analysis (concepts/entities/source page) was weak |

**Why this matters (2026-07-10 incident)**: a full `--delete` was run on 5
books whose OCR/captions were actually fine — only the Stage 2.2+ digestion
was weak. `--delete` removed `wiki/media/<slug>/` (all harvested images +
caption sidecar files) with no way to cheaply regenerate just that piece:
minerU's image harvest runs **inline with the same `/file_parse` API call**
as text extraction — there is no separate image/caption cache anywhere else.
Once `wiki/media/` is gone, the *only* way to get images/captions back is a
full re-call of minerU (which re-does the text too, harmlessly but wastefully
if the text was already fine).

**OCR text, by contrast, IS separately cacheable**: `.llm-wiki/extract-tmp/<book>/`
keeps per-page `p{NNNN}.txt` files independent of the wiki-side `--delete`.
A known gotcha: the cache-hit check compares chunk-boundary names (e.g.
`"0-50"`) against the CURRENT `MINERU_CHUNK_SIZE` (32 as of 2026-07-08) — if a
book was originally ingested under an older chunk size, the boundary names
won't match and Stage 1.1 will look uncached and re-run minerU even though
every page's `.txt` already exists. If OCR reuse unexpectedly seems to be
missing, check `.llm-wiki/extract-tmp/<book>/_mineru_stats.json`'s
`completed_chunks` against page-file coverage before assuming the cache is
genuinely absent.

## Rule: never read prior digest output as source material

Do not reuse the previously ingested artifacts (old source page, old concept/entity
pages, old digest) as reference material while re-ingesting. Read entirely fresh
from `raw/` originals + `.llm-wiki/extract-tmp/` extracted text — do not open the
book's existing `wiki/sources/`, `wiki/concepts/`, `wiki/entities/` pages for
guidance during re-analysis.

**Why:** an old digest can carry forward a bias or error from the original ingest;
reusing it as a reference defeats the point of re-ingesting from a clean source. This
applies even though Step 1 below backs up the old pages — the backup is for
comparison in Step 4, not for reading during re-generation.

---

## Flow A: Full redo (OCR + images + captions + analysis)

### Step 1: Backup old results

```bash
BOOK="从零开始学散热 - 2014 - 陈继良"
PROJECT="$HOME/Documents/知识库/HardwareWiki"

# Backup source page
cp "$PROJECT/wiki/sources/Book/$BOOK.md" /tmp/wiki-compare-backup/source-old.md

# Backup concepts (grep for book name in sources field)
mkdir -p /tmp/wiki-compare-backup/concepts
find "$PROJECT/wiki/concepts" -name "*.md" -exec grep -l "$BOOK" {} \; | \
  while read f; do cp "$f" /tmp/wiki-compare-backup/concepts/; done

# Backup entities, queries, comparisons, reviews similarly
```

`--delete` also backs up the source/concept/entity pages it removes to
`page-history/` on its own (and, since 2026-07-10, the media directory to
`page-history/media/` too) — the manual backup above is for side-by-side
comparison in Step 4, not the only safety net.

### Step 2: Delete old ingest (full redo, including media)

```bash
~/.venv/bin/python3 "$SKILL_DIR/scripts/ingest.py" \
  --delete "raw/Book/$BOOK.pdf"
```

`--delete` removes: source page + orphan concepts/entities (whose only source was
this book) + media directory (now backed up to `page-history/media/` first) + cache entry.
Prints a summary of all removed files.

### Step 3: Re-ingest

```bash
# Phase 1: OCR (may timeout on 200+ page books, re-run resumes from cache)
~/.venv/bin/python3 "$SKILL_DIR/scripts/ingest.py" "raw/Book/$BOOK.pdf" --stop-after-stage 0

# Phase 2: LLM stages (conversation mode, multiple exit-101 cycles)
~/.venv/bin/python3 "$SKILL_DIR/scripts/ingest.py" "raw/Book/$BOOK.pdf"
```

## Flow B: Analysis-only re-ingest (reuse existing OCR/images/captions)

Use when the user confirms OCR/images/captions are fine and only wants Stage
2.2+ (concepts/entities/source page) redone.

### Step 1: Backup old results

Same as Flow A Step 1 (source/concept/entity `.md` pages only — media isn't
touched by this flow, so it doesn't need backing up).

### Step 2: Delete, keeping media

```bash
~/.venv/bin/python3 "$SKILL_DIR/scripts/ingest.py" \
  --delete --keep-media "raw/Book/$BOOK.pdf"
```

This removes the source page + orphan concepts/entities + cache entry, same
as Flow A, but leaves `wiki/media/<slug>/` untouched.

### Step 3: Re-ingest

Same commands as Flow A Step 3. Stage 1.1 will hit the `extract-tmp` per-page
cache and skip minerU entirely **if the chunk-boundary names match the
current `MINERU_CHUNK_SIZE`** (see the gotcha above) — verify the ingest log
shows `(cached)` for each chunk rather than real elapsed times (`OK (Ns, ...)`);
if it doesn't, the boundary-name mismatch is likely the cause and text will be
harmlessly (but wastefully) re-extracted even though `--keep-media` correctly
preserved the images.

## Step 4: Compare

| Metric | How to measure |
|--------|---------------|
| Source page size | `wc -c` old vs new |
| Concept count | `find wiki/concepts -name "*.md" -exec grep -l "$BOOK" {} \; | wc -l` |
| Entity count | Same for `wiki/entities/` |
| Query count | Same for `wiki/queries/`（注：Stage 2.7 已于 2026-07-12 移除——新 ingest 恒为 0，此行仅对历史消化的旧结果有意义） |
| Comparison count | Same for `wiki/comparisons/` |
| Review count | Same for `wiki/REVIEW/` |
| Media count | `find "wiki/media/Book/$BOOK" -type f | wc -l` |
| Validation | Check pipeline stdout for `Result: N/M` |
| Cross-references | `grep -c '\[\[' source-page.md` (wikilink density) |

## Key findings from 2026-06-24 comparison (old pipeline vs new)

- New pipeline produces **fewer but better-connected** pages (73 vs 84 concepts,
  but 52 cross-references to existing wiki vs ~0 in old)
- New stages add significant value: **queries** (3 new), **comparisons** (2 new),
  **reviews** (8 items), **embeddings** (20354 entries)
- Source page is more structured but smaller (43KB vs 76KB) — less narrative
  detail, more concise structure
- Main quality bottleneck: image captions (202 of 528 images missing captions
  → Stage 1.3 needs more VLM calls)
