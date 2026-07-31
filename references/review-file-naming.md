# Review File Naming Convention

Authoritative naming rule for every item under `wiki/REVIEW/<type>/` —
across all five types (`contradiction` / `suggestion` / `missing-page` /
`confirm` / `duplicate`, plus the improved-wiki-only `orphan`) and every
generation source (ingest Stage 3.4, structural/semantic lint, deep-research,
process-reviews, caption-skip).

## How NashSU actually stores reviews (the alignment target)

NashSU (`llm_wiki`) has **no per-file review naming at all**. Every review is
one entry in a single JSON array persisted to `.llm-wiki/review.json`
(`src/lib/persist.ts::saveReviewItems`). Each entry is identified by a
**content-derived hash id** (`src/stores/review-store.ts::reviewIdFor`):

```
id = review-{FNV-1a-32bit( type + "::" + normalizeReviewTitle(title) ) as 8 hex}
```

- **Deterministic**: the same `(type, normalized-title)` always yields the same
  id — so a review survives ingest regeneration, file moves, and reloads.
- **Collision = merge**: two items with the same id are the *same* review and
  are merged (resolved-wins, array fields unioned), never duplicated.
- Explicitly **not** counter-based (`review-N`) and **not** keyed on file path.

`normalizeReviewTitle` strips leading `Missing page:` / `缺失页面：` / etc.,
collapses whitespace, and lowercases.

improved-wiki ports this verbatim in `scripts/_review_utils.py`
(`review_id_for`, `normalize_review_title`) — byte-identical, including the
UTF-16 code-unit hashing that matches JS `charCodeAt`.

## improved-wiki's fork: per-file .md + readable names

improved-wiki is a CLI/agent tool with no persistent app store, so it writes
**one `.md` per review** under `wiki/REVIEW/<type>/`. Two independent pieces:

| Piece | Value | Purpose |
|-------|-------|---------|
| **frontmatter `review_id`** | NashSU content hash `review_id_for(type, title)` | canonical identity — sweep / process-reviews / dedup key on THIS, never the filename |
| **filename** | `<type>-<topic>-<YYYYMMDD>.md` | human browsability only |

The filename is decoupled from the id on purpose: humans page through
`wiki/REVIEW/<type>/` and want to read `contradiction-开关电源效率矛盾-20260715.md`,
not `review-4aa93911.md`. Tooling never parses the filename — it reads
`review_id` from frontmatter.

### Filename format

```
<review_type>-<topic>-<YYYYMMDD>.md
```

- **`<review_type>`** — the review type = the parent directory name
  (`contradiction`, `suggestion`, `missing-page`, `confirm`, `duplicate`,
  `orphan`).
- **`<topic>`** — a readable subject derived from the title:
  type-marker (`[contradiction]`) and `Missing page:` prefixes stripped,
  wikilinks unwrapped, path separators turned into word breaks, filesystem-
  unsafe + quote/comma/paren noise removed, whitespace → hyphens, CJK
  preserved, truncated to 40 characters.
- **`<YYYYMMDD>`** — the review's `created` date, compact.
- **Collision suffix** — if two *different* reviews derive the same base name,
  the later one gets `-<last-4-hex-of-review_id>` appended (deterministic).

Examples:

```
contradiction-分割地平面建议不一致-20260629.md
missing-page-characteristic-impedance-20260705.md
suggestion-Uncertain-link-rewrite-conceptsS-parameters-20260710.md
confirm-Verify-Carsons-rule-98%-power-bandwidth-20260701.md
orphan-Orphan-delete-candidate-concepts-foo.md-20260710.md
```

## The one code entry point

**Every write site MUST use `_review_utils.resolve_review_path()`** — do not
hand-build filenames:

```python
from _review_utils import resolve_review_path

reviews_dir = wiki_dir / "REVIEW" / rtype
reviews_dir.mkdir(parents=True, exist_ok=True)
page_path, review_id = resolve_review_path(reviews_dir, rtype, title, "20260715")
# ... write frontmatter including `review_id: {review_id}` ...
```

`resolve_review_path(review_dir, rtype, title, date_compact) -> (Path, id)`:

1. Computes `review_id = review_id_for(rtype, title)` (NashSU content hash).
2. **Idempotent by content id, not filename**: scans `review_dir` for an
   existing file already carrying `review_id: <id>` (even under a different
   date or an older name) and reuses its path — so a re-run on another day
   never forks a duplicate.
3. Otherwise builds `<type>-<topic>-<date>.md`; only a base-name clash with a
   *different* review appends the `-<id4>` suffix.

Helpers behind it: `derive_review_topic(title, rtype)` and
`review_filename(rtype, title, date_compact, review_id=None, *, disambiguate=False)`.

## Write sites (all routed through `resolve_review_path`)

| Source | File | Type(s) |
|--------|------|---------|
| Ingest Stage 3.4 review | `scripts/_stage_3_4_review.py` | all 5 |
| Structural lint `--fix-links` (missing target) | `scripts/wiki-lint-fix.py` | missing-page |
| Structural lint (unsuggestable orphan/no-outlinks) | `scripts/wiki-lint-fix.py` | suggestion |
| Structural lint (uncertain rewrite, score-gated) | `scripts/wiki-lint-fix.py` | suggestion |
| Structural lint (orphan delete candidate) | `scripts/wiki-lint-fix.py` | orphan |
| Semantic lint warnings | `scripts/wiki-lint-semantic.py` | contradiction/duplicate/missing-page/suggestion |
| Stage 1.3 caption-skip (no API key) | `scripts/_stage_1_3_caption.py` | suggestion |

## Frontmatter shape

```yaml
---
type: review
review_id: review-<8hex>          # NashSU content hash — REQUIRED, canonical id
review_type: contradiction        # one of the five (+ orphan)
severity: low | medium | high     # where applicable
affected_pages: [ ... ]
search_queries: [ ... ]           # Deep-Research seeds (suggestion/missing-page)
resolved: false
created: YYYY-MM-DD
source_ingest: "Book Title - Year - Author"   # when ingest-sourced
---
```

`review_id` MUST be present and MUST equal `review_id_for(review_type, title)`.
Sweep (`sweep_reviews.py`) skips any item lacking a stable `review_id`.

## Migration note (2026-07-15)

Existing HardwareWiki + RadarWiki review files (≈1770) were migrated to this
scheme in one pass: every `review_id` recomputed to the NashSU content hash,
every file renamed to `<type>-<topic>-<date>.md`. Two genuine content-hash
duplicates and one corrupted file were moved to
`<project>/.llm-wiki/review-dupes-backup/` (reversible). The migration is
idempotent — re-running finds nothing to change.

## See Also

- `process-reviews.md` — human adjudication (creates review items)
- `review-sweep.md` — auto-resolve satisfied items (keys on `review_id`)
- `nashsu-lint-source-analysis.md` — NashSU review.json internals
