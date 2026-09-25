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

# 2. Copy the project contract + anchor files from the skill's templates/
#    schema.md and purpose.md live at the PROJECT ROOT; the 3 aggregate
#    pages (index/overview/log) live under wiki/.
SKILL_DIR=~/.agents/skills/improved-wiki
cp $SKILL_DIR/templates/schema.md    ./schema.md
cp $SKILL_DIR/templates/purpose.md   ./purpose.md
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
cat wiki/sources/Book/My\ Book\ -\ 2024\ -\ Author.md
cat wiki/log.md
```

Step 7 is complete only when the source's `.stages.json` contains `ingested`,
the same run_id exists in `.llm-wiki/ingest-events.jsonl`, and
`validate_ingest.py` passes. A source page alone can be a resumable mid-pipeline
artifact.

```bash
# 9. Install the cron (see references/cron-installation.md)
# Add to your crontab (crontab -e):
# 0 2 * * * $SKILL_DIR/scripts/wiki-monitor.sh
```

---

## Scenario B: retrofit an existing LLM Wiki app project

If you already have a project at e.g. `~/Documents/知识库/MyWiki/` with files in `raw/sources/` (the LLM Wiki app's convention), and you want to use `improved-wiki`'s scripts:

Keep the existing raw layout; supported variants are documented in
`raw-layout-compat.md`. `raw/sources/<type>/` is recognized; a flat
`raw/sources/<file>` defaults to book and can use an explicit `--type paper`.

Do not move raw files and delete the old tree as a setup shortcut: source paths
are persistent identities in page frontmatter, cache and completion history.
If reclassification is required, use the coordinated workflow in
`maintenance-cleanup.md`. Runtime layout changes are independent; preview with
`migrate_runtime.py` as described in `runtime-layout.md`.

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

# Check 2: the project contract + wiki anchor files exist
test -f schema.md && test -f purpose.md && test -f wiki/index.md && test -f wiki/log.md && test -f wiki/overview.md
echo $?  # should be 0

# Check 3: the authoritative completion marker exists for the source
python3 "$SKILL_DIR/scripts/ingest.py" --batch-status
rg '"ingested"' .llm-wiki/ingest-progress/*.stages.json

# Check 4: the authoritative completed-run history exists and is queryable
test -f .llm-wiki/ingest-events.jsonl
python3 "$SKILL_DIR/scripts/ingest_history.py" --project "$IMPROVED_WIKI_ROOT" list --limit 1

# Check 5: the cache file is created/updated after the first ingest
test -f .llm-wiki/ingest-cache.json
cat .llm-wiki/ingest-cache.json | python3 -m json.tool  # should be valid JSON

# Check 6: the human-readable log projection got a run_id entry
tail -12 wiki/log.md  # should show INGEST COMPLETED + Run
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
| Low PDF text density | All PDFs take the unified minerU hybrid-engine/auto path; the dry-run sample is diagnostic only | No action needed |
| `wiki/index.md` is missing pages or lists deleted ones | Stage 3.3 rebuilds index.md from the pages on disk on every ingest (`rebuild_index_deterministic` in `_stage_3_write.py`: grouped by frontmatter `type`, title-sorted, no LLM, no page limit), so drift only appears after pages are added, moved, or deleted outside an ingest. `Stage 3.3 index does not contain a link to <stem>` means the source page's filename stem is `index`, `overview`, or `log` (any case), which the rebuild skips | Run `python3 scripts/rebuild_index.py --project-root <wiki-root>` to preview the diff, then add `--apply`; rename a raw file whose stem collides with an aggregate page |

---

## Performance budget

Current-magnitude expectations (实测口径见 `references/batch-parallel-prefetch.md` 的批量数据)：

| Stage | Expected time |
|---|---|
| Hash check | <1s |
| minerU text extract (API path, text-layer PDF) | minutes — dominated by local minerU server startup/model load, not page count |
| minerU VLM OCR (scanned, ~300p) | 30-60 min (per [来源: mineru-document-parsing] skill gotcha #21: 1.2B VLM is slow on 16GB machines) |
| LLM stages (2.2 chunk analysis + 2.4 generation + spine, conversation mode) | tens of minutes — per-handoff subagent round-trips dominate; a long multi-chunk book's spine can reach ~2h cumulative LLM latency (RadarWiki 实测 125 min, see `batch-parallel-prefetch.md`) |
| File writes | <1s |
| **Total** | ~10-30+ min per book typical; long or scanned books run to multiple hours |

Plan accordingly. A scheduled scan does not complete conversation-mode ingestion; provide an agent handoff driver.

---

## See also

- `SKILL.md` — End-to-end pipeline reference
- `references/ingest-stages-mandatory.md` — ingest stage checklist (15 numbered stages in 4 Phases (0-3))
- `references/cron-installation.md` — How to install the cron job, with crontab snippets
