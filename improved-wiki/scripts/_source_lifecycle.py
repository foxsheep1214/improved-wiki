#!/usr/bin/env python3
"""
_source_lifecycle.py — Source lifecycle management (NashSU source-lifecycle.ts parity).

delete_source(): removes source page, cache entry, derived concept/entity pages
                 that are exclusively attributable to this source.
list_source_pages(): list all pages derived from a given source.
"""

import json, shutil, time
from pathlib import Path

from _paths import detect_runtime_dir, media_slug
from _core import (
    source_slug_from_raw_path,
    load_schema_md,
    schema_folders,
    BASE_PAGE_DIRS,
)
from _frontmatter_array import parse_frontmatter_array


def delete_source(raw_file: Path, config, dry_run: bool = False) -> int:
    """Delete a source and its derived content. Returns count of files removed.

    When ``dry_run`` is True, NOTHING is written or deleted — each action is
    printed with a ``[dry-run]`` marker and the function returns the count that
    WOULD be removed. (Previously ``--delete --dry-run`` ignored dry_run because
    the delete branch ran before ingest.py's dry_run guard — it actually deleted.)
    """
    wiki_root = config.wiki_root
    raw_root = config.raw_root
    runtime_dir = detect_runtime_dir(wiki_root)
    tag = "[lifecycle][dry-run]" if dry_run else "[lifecycle]"

    # Resolve source path
    try:
        rel = str(raw_file.relative_to(raw_root))
    except ValueError:
        rel = raw_file.name

    removed = 0

    # 1. Delete source page
    # source_slug_from_raw_path() is the canonical path-derivation helper
    # (used by ingest dedup too) — a naive ".pdf" -> ".md" string replace
    # left PPTX/DOCX sources' page paths un-rewritten (extension never
    # became .md), so --delete silently found nothing to remove for them.
    src_path = source_slug_from_raw_path(raw_file, wiki_root)
    if src_path is None:
        src_path = wiki_root / "wiki" / "sources" / Path(rel).with_suffix(".md")
    if src_path.exists():
        if not dry_run:
            # Backup before delete
            history_dir = wiki_root / "page-history"
            history_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            backup = history_dir / f"{ts}_{src_path.name}"
            shutil.copy2(src_path, backup)
            src_path.unlink()
        removed += 1
        print(f"{tag} Deleted source page: {rel}")

    # 2. Remove from ingest cache.
    # Match the cache key case-INSENSITIVELY: the key is stored from whatever
    # casing the raw path had at ingest time (e.g. "paper/..."), but a later
    # rename of the raw dir (e.g. to "Paper/...") makes the exact compare miss,
    # leaving a stale entry that makes the next re-ingest a cache hit (skip).
    cache_path = runtime_dir / "ingest-cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        entries = cache.get("entries", {})
        cache_key = rel.replace("\\", "/")
        match_key = cache_key if cache_key in entries else next(
            (k for k in entries if k.lower() == cache_key.lower()), None
        )
        if match_key is not None:
            if not dry_run:
                del entries[match_key]
                tmp = cache_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
                tmp.rename(cache_path)
            removed += 1
            print(f"{tag} Removed cache entry: {match_key}")

    # 3. Clean up derived pages (concepts/entities whose ONLY source is this file)
    source_stem = raw_file.stem
    derived_count = _cleanup_orphan_pages(wiki_root, source_stem, config, dry_run=dry_run)
    removed += derived_count
    if derived_count:
        verb = "Would clean up" if dry_run else "Cleaned up"
        print(f"{tag} {verb} {derived_count} derived pages")

    # 4. Remove media directory
    slug = media_slug(raw_file, config)
    media_dir = wiki_root / "wiki" / "media" / slug
    if media_dir.exists():
        if not dry_run:
            shutil.rmtree(media_dir)
        removed += 1
        print(f"{tag} Removed media directory: media/{slug}")

    verb = "Would remove" if dry_run else "Total removed"
    print(f"{tag} {verb}: {removed} files/dirs")
    return removed


# schema.md may list folders that aren't LLM-generated per-source page types.
_NON_PAGE_DIRS = {"media", "raw", "page-history", "chats"}


def _cleanup_orphan_pages(wiki_root: Path, source_stem: str, config, dry_run: bool = False) -> int:
    """Remove derived pages whose ONLY source reference is this book.

    Covers concepts, entities, queries, comparisons, plus any schema-defined
    typed folders (NashSU schema-driven routing — people/, methods/, etc.), so a
    page routed there by Stage 2.4 is cleaned on --delete just like a concept page.
    The single-source test below is the guard: pages with ``sources: []`` or with
    more than one source (hand-authored or multi-source hub pages) never match and
    are correctly preserved. synthesis/findings/thesis are intentionally excluded
    (cross-source higher-order pages, not per-source derived).
    """
    base_types = ("concepts", "entities", "queries", "comparisons")
    extra = schema_folders(load_schema_md(config)) - BASE_PAGE_DIRS - _NON_PAGE_DIRS
    page_types = base_types + tuple(sorted(extra))

    removed = 0
    history_dir = wiki_root / "page-history"
    for page_type in page_types:
        page_dir = wiki_root / "wiki" / page_type
        if not page_dir.exists():
            continue
        for page in page_dir.glob("*.md"):
            try:
                text = page.read_text()
            except Exception:
                continue
            # Naive sources_str.split(",") breaks when a source filename
            # itself contains a comma — use the shared frontmatter-array
            # parser (same fix already applied in _stage_3_write.py).
            sources = parse_frontmatter_array(text, "sources")
            if not sources:
                continue
            # Exact basename-stem match, not substring — "LM2596" must not
            # match a sibling source like "raw/.../LM25960.pdf".
            if len(sources) == 1 and Path(sources[0]).stem == source_stem:
                tag = "[lifecycle][dry-run]" if dry_run else "[lifecycle]"
                if not dry_run:
                    history_dir.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%Y%m%d-%H%M%S")
                    shutil.copy2(page, history_dir / f"{ts}_{page.name}")
                    page.unlink()
                print(f"{tag} Deleted orphan page: {page_type}/{page.name}")
                removed += 1
    return removed


