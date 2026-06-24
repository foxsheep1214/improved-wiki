"""_ingest_skip.py — Stage 0.2 dedup/skip + stage go/no-go (extracted from ingest.py)."""
from __future__ import annotations

import re
from pathlib import Path

from _core import Config, file_sha256, is_stage_done
from _stage_3_write import _stage_3_1_wiki_path_for_source

def _should_stop_after(config: Config, stage: str, result: dict) -> bool:
    """Check if we should stop after completing `stage`. Progress already saved before call."""
    if config.stop_after_stage == stage:
        print(f"\n[stop-after-stage] Stage {stage} complete — clean exit (--stop-after-stage={stage})")
        return True
    return False

def _stage_0_2_should_skip(raw_file: Path, config: Config) -> bool:
    """Return True if the source page already exists and is reasonably complete.

    Stage 0.2: Re-ingest when source page is missing >80% of linked concept/entity
    pages (corrupt / partial prior run); otherwise skip.

    Verification checklist:
    1. Source page file exists
    2. Frontmatter type == "source"
    3. ≥80% of wikilinks point to existing concept/entity pages

    Primary gate (Option A): skip only once the ingest has fully completed
    (stage_4_1 marker set).  This prevents a mid-flight conversation-mode
    resume — where pages are written but post-review stages (3.5-4.1) are
    still pending — from being short-circuited by the "source page exists"
    heuristic below.
    """
    h = file_sha256(raw_file)
    if is_stage_done(config, h, "stage_4_1"):
        if not _stage_3_1_wiki_path_for_source(raw_file, config).exists():
            # Stale marker (source page deleted externally) — clear and re-ingest.
            from _core import stages_path as _sp
            _sp(config, h).unlink(missing_ok=True)
            return False
        print(f"  [skip] Ingest complete (stage_4_1 marker present)")
        return True

    source_page = _stage_3_1_wiki_path_for_source(raw_file, config)
    if not source_page.exists():
        return False

    # Source page exists but stage_4_1 not done → mid-flight resume.  Do NOT
    # skip: post-review stages (3.5-4.1) may still be pending.  The write_phase
    # marker inside _do_write handles skipping the non-idempotent 3.1 loop.
    print(f"  [skip:resume] Source page exists, stage_4_1 not done — resuming")
    return False

    # Verify source page is readable and has valid frontmatter
    try:
        source_text = source_page.read_text(encoding="utf-8", errors="strict")
    except Exception as e:
        print(f"  [skip:error] Source page unreadable ({e}) — re-ingesting")
        return False

    # Verify frontmatter type is "source"
    if not source_text.startswith("---"):
        print(f"  [skip:error] Source page missing frontmatter — re-ingesting")
        return False

    try:
        fm_end = source_text.find("---", 3)
        if fm_end == -1:
            print(f"  [skip:error] Source page frontmatter unclosed — re-ingesting")
            return False
        frontmatter_block = source_text[3:fm_end]
        fm_type = None
        for line in frontmatter_block.split("\n"):
            if line.strip().startswith("type:"):
                fm_type = line.split(":", 1)[1].strip().strip("'\"")
                break
        if fm_type != "source":
            print(f"  [skip:error] Source page type is '{fm_type}', not 'source' — re-ingesting")
            return False
    except Exception as e:
        print(f"  [skip:error] Frontmatter parse error ({e}) — re-ingesting")
        return False

    # Extract wikilinks: [[slug]] or [[slug|display]]
    # Improved regex: match [[ ... ]] with no nested brackets
    refs = re.findall(r'\[\[([^\[\]]+)\]\]', source_text)
    # Wikilinks may be type-prefixed ([[concepts/foo]], per Stage 2.4/2.6 convention)
    # or bare ([[foo]], per the wikilink-enrichment convention) — support both.
    known_type_dirs = ("concepts", "entities", "sources", "queries", "comparisons",
                        "synthesis", "findings", "thesis", "methodology")
    missing = []
    for ref in refs:
        slug = ref.split("|")[0].strip()
        if not slug:
            continue
        prefix, _, rest = slug.partition("/")
        if prefix in known_type_dirs and rest:
            target_path = config.wiki_dir / prefix / f"{rest}.md"
            if not target_path.exists():
                missing.append(slug)
            continue
        concept_path = config.wiki_dir / "concepts" / f"{slug}.md"
        entity_path = config.wiki_dir / "entities" / f"{slug}.md"
        if not concept_path.exists() and not entity_path.exists():
            missing.append(slug)

    if not refs or len(missing) > len(refs) * 0.8:
        ratio_str = f"{len(missing)}/{len(refs)}" if refs else "0/0"
        print(f"  [skip:warn] Source page exists but {ratio_str} linked pages missing — re-ingesting")
        return False

    ratio_found = len(refs) - len(missing)
    print(f"  [skip] Source page exists ({ratio_found}/{len(refs)} linked pages found)")
    return True
