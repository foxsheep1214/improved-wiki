#!/usr/bin/env python3
"""
_source_lifecycle.py — Source lifecycle management (NashSU source-lifecycle.ts parity).

delete_source(): removes source page, cache entry, derived concept/entity pages
                 that are exclusively attributable to this source.
list_source_pages(): list all pages derived from a given source.
"""

import json, shutil, time
from pathlib import Path
from typing import Optional

from _paths import detect_runtime_dir
from _core import source_slug_from_raw_path
from _frontmatter_array import parse_frontmatter_array


def delete_source(raw_file: Path, config) -> int:
    """Delete a source and its derived content. Returns count of files removed."""
    wiki_root = config.wiki_root
    raw_root = config.raw_root
    runtime_dir = detect_runtime_dir(wiki_root)

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
        # Backup before delete
        history_dir = wiki_root / "page-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = history_dir / f"{ts}_{src_path.name}"
        shutil.copy2(src_path, backup)
        src_path.unlink()
        removed += 1
        print(f"[lifecycle] Deleted source page: {rel}")

    # 2. Remove from ingest cache
    cache_path = runtime_dir / "ingest-cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        entries = cache.get("entries", {})
        cache_key = rel.replace("\\", "/")
        if cache_key in entries:
            del entries[cache_key]
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
            tmp.rename(cache_path)
            removed += 1
            print(f"[lifecycle] Removed cache entry: {cache_key}")

    # 3. Clean up derived pages (concepts/entities whose ONLY source is this file)
    source_stem = raw_file.stem
    derived_count = _cleanup_orphan_pages(wiki_root, source_stem)
    removed += derived_count
    if derived_count:
        print(f"[lifecycle] Cleaned up {derived_count} derived pages")

    # 4. Remove media directory
    slug = _derive_media_slug(raw_file, config)
    media_dir = wiki_root / "wiki" / "media" / slug
    if media_dir.exists():
        shutil.rmtree(media_dir)
        removed += 1
        print(f"[lifecycle] Removed media directory: media/{slug}")

    print(f"[lifecycle] Total removed: {removed} files/dirs")
    return removed


def _cleanup_orphan_pages(wiki_root: Path, source_stem: str) -> int:
    """Remove concept/entity pages whose ONLY source reference is this book."""
    removed = 0
    history_dir = wiki_root / "page-history"
    for page_type in ("concepts", "entities"):
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
                history_dir.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%d-%H%M%S")
                shutil.copy2(page, history_dir / f"{ts}_{page.name}")
                print(f"[lifecycle] Deleted orphan page: {page_type}/{page.name}")
                page.unlink()
                removed += 1
    return removed


def _derive_media_slug(raw_file: Path, config) -> str:
    """Derive media slug from raw file path (mirrors _media_slug in ingest.py)."""
    try:
        rel = raw_file.relative_to(config.raw_root)
    except ValueError:
        return raw_file.stem
    parent = rel.parent
    stem = rel.stem
    return str(parent / stem) if str(parent) != "." else stem
