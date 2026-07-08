# Initial setup — using `improved-wiki` on a real project

This guide is the "first run" recipe for getting `improved-wiki` working on a brand-new project (or retrofitting an existing one).

---

## Scenario A: brand-new project (start from scratch)

```bash
# 1. Create the project root + ALL 11 wiki/ subfolders (9 typed dirs + media + REVIEW,
#    per references/naming-conventions.md §1.1) + mandatory raw/ subfolders
PROJECT=~/Documents/知识库/MyNewWiki
mkdir -p $PROJECT/wiki/{sources,concepts,entities,queries,comparisons,findings,synthesis,thesis,methodology,media,REVIEW}
# Mandatory 3 — every knowledge base needs these:
mkdir -p $PROJECT/raw/{Book,Paper,Presentation}
# Optional — add per domain (HardwareWiki: Datasheet/Applicationnote/Designexample; RadarWiki: Standard; etc.):
# mkdir -p $PROJECT/raw/{Datasheet,Applicationnote,Designexample,Standard,News}
cd $PROJECT

# 2. Copy the anchor files from the skill's templates/
#    schema.md lives at the PROJECT ROOT; the 3 aggregate
#    pages (index/overview/log) live under wiki/.
SKILL_DIR=~/.agents/skills/improved-wiki
cp $SKILL_DIR/templates/schema.md    ./schema.md
cp $SKILL_DIR/templates/index.md     ./wiki/index.md
cp $SKILL_DIR/templates/log.md       ./wiki/log.md
cp $SKILL_DIR/templates/overview.md  ./wiki/overview.md

# 3. (raw/ subfolders already created in step 1)

# 4. Drop your first batch of source files into raw/<type>/
#    e.g. raw/Book/My Book - 2024 - Author.pdf

# 5. Set the project root env var. Text-generation LLM work runs in
#    conversation mode (the calling agent's current model) — no LLM API
#    key needed. Image captioning (VLM provider) is configured separately in
#    ~/.agents/config.json (see references/image-caption-strategy.md) — only
#    needed if your source has images to caption:
export IMPROVED_WIKI_ROOT=$(pwd)
# Optional: force the wiki's output language (NashSU outputLanguage parity).
# 'auto' (default / unset) detects per source; set to e.g. Chinese or English
# to force every generated page + lint directive into that language.
export IMPROVED_WIKI_OUTPUT_LANGUAGE=auto

# 6. Dry-run to verify detection
$SKILL_DIR/scripts/ingest.py raw/Book/My\ Book\ -\ 2024\ -\ Author.pdf --dry-run

# 7. Process the first file for real (conversation mode is the only mode:
#    the calling agent answers each LLM step with the current model)
$SKILL_DIR/scripts/ingest.py raw/Book/My\ Book\ -\ 2024\ -\ Author.pdf

# 8. Inspect the output
ls wiki/sources/
cat wiki/sources/My\ Book\ -\ 2024\ -\ Author.md
cat wiki/log.md
```

If step 7 wrote a `wiki/sources/...` page, step 6-7 worked. Move on to step 9.

```bash
# 9. Install the cron (see references/cron-installation.md)
# Add to your crontab (crontab -e):
# 0 2 * * * $SKILL_DIR/scripts/wiki-monitor.sh
```

---

## Scenario B: retrofit an existing LLM Wiki app project

If you already have a project at e.g. `~/Documents/知识库/MyWiki/` with files in `raw/sources/` (the LLM Wiki app's convention), and you want to use `improved-wiki`'s scripts:

**Two paths**:

### B1. Move sources to the new layout (recommended)

```bash
cd ~/Documents/知识库/MyWiki

# Move sources into the new layout (per references/naming-conventions.md §1.2)
mkdir -p raw
mv raw/sources/book/*  raw/Book/
mv raw/sources/paper/* raw/Paper/
mv raw/sources/*/*.pdf raw/Book/  # top-level PDFs
rm -rf raw/sources
# ... adjust per your situation
```

After this, the layout matches what `improved-wiki` expects, and `wiki/` stays untouched (the LLM Wiki app's wiki/ structure is compatible with NashSU's, which is what `improved-wiki` follows).

### B2. Override the type per file (workaround, no restructure)

If you want to keep `raw/sources/` flat (e.g. legacy layout):

```bash
# Process each file with explicit --type
$SKILL_DIR/scripts/ingest.py raw/sources/X.pdf --type book
$SKILL_DIR/scripts/ingest.py raw/sources/Y.pdf --type paper
```

This works for the script but the **folder-detection auto-classification breaks**. You'll need to write your own queue generator that hardcodes the type per file.

---

## Scenario C: existing personal KB (Obsidian, Notion, Apple Notes, etc.)

`improved-wiki` is opinionated — it assumes the Karpathy three-layer model + NashSU's wiki/ layout. If your existing KB uses a different structure (e.g. Obsidian vault with non-NashSU conventions), you have two options:

1. **Migrate**: export your existing notes, run the wikilink audit (per `llm-wiki-local` skill's `scripts/wikilink-audit.py`), fix all broken links to use full filename stems (per `improved-wiki` references/naming-conventions.md §3.3), then ingest new raw sources through the pipeline.

2. **Run in parallel**: keep your existing KB for daily use, use `improved-wiki` for new long-form source ingestion. Migrate gradually as you see value.

Don't try to make `improved-wiki` adapt to an existing non-standard structure — its assumptions (Layer 1 immutable raw / Layer 2 LLM-generated wiki / Layer 3 schema) are load-bearing.

---

## Verifying the install

After setup, the following should all be true:

```bash
# Check 1: dry-run finds the file and reports the template
$SKILL_DIR/scripts/ingest.py raw/Book/X.pdf --dry-run
# Expected: prints "DRY RUN: would process X" and "template: digest-book"

# Check 2: the wiki anchor files exist
test -f schema.md && test -f wiki/index.md && test -f wiki/log.md && test -f wiki/overview.md
echo $?  # should be 0

# Check 3: the cache file is created/updated after the first ingest
test -f .llm-wiki/ingest-cache.json
cat .llm-wiki/ingest-cache.json | python3 -m json.tool  # should be valid JSON

# Check 4: the log file got an entry
tail -10 wiki/log.md  # should show the most recent ingest
```

If any check fails, the most common cause is **the wrong `IMPROVED_WIKI_ROOT`** — the script defaults to `os.getcwd()`. Always pass it explicitly:

```bash
export IMPROVED_WIKI_ROOT=/Users/skyfend/Documents/知识库/MyNewWiki
```

---

## Common first-run failures

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: Unknown raw folder 'X'` | File is in a folder the script doesn't recognize | Either move the file to a recognized first-level folder (Book/Paper/Datasheet/... — Titlecase) or pass `--type X` |
| Caption step fails / pauses at Stage 1.3 | No `caption_provider` configured (text gen needs no key — it runs in conversation mode) | Set `caption_provider` + a matching `providers.<name>` entry (`api_key`+`base_url`+`protocol`+`model`) in `~/.agents/config.json`; only the Stage 1.3 caption step calls the VLM |
| `LLM API HTTP 401` (caption only) | Wrong caption key or endpoint | Check the caption provider key/endpoint used by Stage 1.3 |
| `Template not found: ...` | Skill not installed in expected path | Verify `SKILL_DIR` points to the actual improved-wiki installation |
| `mineru CLI not found` | minerU not installed | Re-install minerU per the `mineru-document-parsing` skill |
| Scanned PDF detected | Normal — all PDFs take the unified minerU hybrid-engine/auto path; the PyMuPDF type sample only labels the `--dry-run` estimate | No action needed |
| `wiki/index.md` is missing the new source link | Stage 3.5 normally rewrites index.md via the LLM; the deterministic fallback (`_index_append_fallback` in `_stage_3_write.py`) inserts after the `## Sources` header line via regex `^##\s+Sources.*$`, so a bilingual `## Sources（来源）` header works. The link is only skipped if there is no `## Sources` header at all | Make sure your `index.md` has a `## Sources` (or `## Sources（来源）`) header line |

---

## Performance budget

For a typical 300-page book with full text layer:

| Stage | Expected time |
|---|---|
| Hash check | <1s |
| minerU text extract (API path, text-layer PDF) | minutes, not seconds — dominated by local minerU server startup/model load, not page count |
| LLM Analysis call (conversation mode) | 30-90s (current model, per-chunk) |
| LLM Generation call (conversation mode) | 60-180s (this is the big one) |
| File writes | <1s |
| **Total** | ~2-4 min per book |

For a scanned 300-page book:

| Stage | Expected time |
|---|---|
| Hash check | <1s |
| minerU VLM OCR | 30-60 min (per [来源: mineru-document-parsing] skill gotcha #21: 1.2B VLM is slow on 16GB machines) |
| LLM Analysis + Generation | 2-4 min |
| **Total** | ~30-65 min per book |

Plan accordingly. The cron at 02:00 daily will only have time to process 1-2 scanned books per night.

---

## See also

- `SKILL.md` — End-to-end pipeline reference
- `references/ingest-stages-mandatory.md` — ingest stage checklist (16 numbered stages in 4 Phases (0-3))
- `references/cron-installation.md` — How to install the cron job, with crontab snippets
