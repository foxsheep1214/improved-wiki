# Wiki Project Maintenance & Residual File Cleanup

Periodic cleanup of stale files in a wiki project (`~/Documents/知识库/<Project>`).
Run after large batch ingests or when disk space / clutter becomes noticeable.

## `.digested` files (LEGACY — safe to delete)

`.digested` files are markers from an older pipeline version. The current
pipeline (Stage 0.2) uses `wiki/sources/<raw-rel-path>.md` existence +
wikilink integrity as the sole dedup signal. The codebase has **zero
references** to `.digested`.

- Found in: `raw/` subdirectories (one per category folder)
- Typical content: a digest log listing PDFs and their ✓ status
- Action: **delete all**. They are dead weight.

```bash
find ~/Documents/知识库/<Project> -name ".digested" -type f -delete -print | wc -l
```

## `.llm-wiki/` temp directory cleanup

`.llm-wiki/` is the runtime state directory. Some subdirectories accumulate
stale data and are safe to clean **when no ingest is running**.

**Before cleaning, verify no ingest is active:**
```bash
ps aux | grep ingest.py | grep -v grep
```

### Safe to delete (stale after ingest completes)

| Directory | Purpose | When stale |
|-----------|---------|------------|
| `extract-tmp/` | minerU staging dir for PDF extraction (text/scanned/mixed all use it since 2026-06-23 — no longer PyMuPDF-specific) | After ingest completes |
| `.extract-tmp/` | Legacy back-compat marker checked by `_paths.py` when detecting old `wiki/`-based runtime layout — not an active temp dir on its own | N/A (detection-only path, not written by current code) |
| `conversation/` | LLM prompt/response handoff files | After ingest completes |
| `ingest-progress/` | Crash-recovery checkpoints | When no ingest is running |

**Do NOT delete** (active state):
- `ingest-cache.json` — dedup hash cache (Stage 3.5)
- `lint-cache.json` / `lint-lock` — lint state
- `graph.json` — knowledge graph (Graph command output)
- `embed-cache.json` — embedding cache
- `lancedb/` — vector database
- `page-history/` — wiki page version backups (audit/rollback value; 18MB+ typical)
- `clusters/` — graph community hub pages
- `knowledge-gaps.md` — graph gap analysis output
- `review-suggestions.json` — pending review items

### Cleanup command

```bash
cd ~/Documents/知识库/<Project>
# Verify no ingest running first!
rm -rf .llm-wiki/extract-tmp/ .llm-wiki/.extract-tmp/ .llm-wiki/conversation/ .llm-wiki/ingest-progress/
```

## `wiki/concepts/.md` empty-slug file (BUG RESIDUAL)

**Symptom**: A file literally named `.md` appears in `wiki/concepts/`.
**Cause**: Pipeline bug where a chunk with zero concepts triggers a FILE
block with an empty slug. The code fix exists (`is_safe_ingest_path` now
rejects empty filenames — see known-issues.md §Cleanup batch #2), but
residual files from older ingests persist.

**Action**: Delete the file. It has no content value.

```bash
find ~/Documents/知识库/<Project>/wiki -name ".md" -type f -delete
```

## macOS artifacts

`.DS_Store` files accumulate throughout the directory tree. Safe to delete:

```bash
find ~/Documents/知识库/<Project> -name ".DS_Store" -type f -delete
```

Also check for macOS duplicate files (names with space + number suffix,
e.g. `lint-cache 3.json`) in `.llm-wiki/` — these are Finder copy artifacts,
not real pipeline output.

## `page-history/` decision

`page-history/` (typically 18-50MB, thousands of files) stores wiki page
versions before each overwrite. It has audit/rollback value but grows
unboundedly. Options:

- **Keep** if you want rollback capability
- **Clear** if disk space matters more: `rm -rf .llm-wiki/page-history/`
- **Prune** to recent N days if you want a middle ground (no built-in tool yet)
