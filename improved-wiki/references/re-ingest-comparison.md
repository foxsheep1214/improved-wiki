# Re-Ingest Comparison Workflow

When you need to re-ingest a book that's already been digested (e.g., to compare
old vs new pipeline results, or to fix a broken ingest):

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

## Step 1: Backup old results

```bash
BOOK="从零开始学散热 - 2014 - 陈继良"
PROJECT="~/Documents/知识库/HardwareWiki"

# Backup source page
cp "$PROJECT/wiki/sources/Book/$BOOK.md" /tmp/wiki-compare-backup/source-old.md

# Backup concepts (grep for book name in sources field)
mkdir -p /tmp/wiki-compare-backup/concepts
find "$PROJECT/wiki/concepts" -name "*.md" -exec grep -l "$BOOK" {} \; | \
  while read f; do cp "$f" /tmp/wiki-compare-backup/concepts/; done

# Backup entities, queries, comparisons, reviews similarly
```

## Step 2: Delete old ingest

```bash
~/.venv/bin/python3 ~/.agents/skills/improved-wiki/scripts/ingest.py \
  --delete "raw/Book/$BOOK.pdf"
```

`--delete` removes: source page + orphan concepts/entities (whose only source was
this book) + media directory + cache entry. Prints a summary of all removed files.

## Step 3: Re-ingest

```bash
# Phase 1: OCR (may timeout on 200+ page books, re-run resumes from cache)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/$BOOK.pdf" --stop-after-stage 0

# Phase 2: LLM stages (conversation mode, multiple exit-101 cycles)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/$BOOK.pdf"
```

## Step 4: Compare

| Metric | How to measure |
|--------|---------------|
| Source page size | `wc -c` old vs new |
| Concept count | `find wiki/concepts -name "*.md" -exec grep -l "$BOOK" {} \; | wc -l` |
| Entity count | Same for `wiki/entities/` |
| Query count | Same for `wiki/queries/` |
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
  → Stage 1.3 needs more MiniMax VLM calls)
