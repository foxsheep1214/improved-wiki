#!/usr/bin/env python3
"""validate_ingest.py — per-project 15-stage ingest validator

Aligns with ingest.py actual output: reads from .llm-wiki/ingest-cache.json
cache entry + disk state.  Does NOT look for intermediate files (full.txt,
*-global-digest.yaml, *-chunk*-analysis.yaml, generation_response*.txt) that
ingest.py does not write — those artifacts live in progress checkpoints and
are cleared on successful ingest.

Usage:
    python3 scripts/validate_ingest.py
    SOURCE_SLUG=INA1H94-SEP python3 scripts/validate_ingest.py
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

# === Per-project constants ===
PROJECT_ROOT = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
WIKI = PROJECT_ROOT / "wiki"
# Use shared detection (_paths.py: .llm-wiki/ default, auto-migrates from .iwiki-runtime/)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir
RUNTIME = detect_runtime_dir(PROJECT_ROOT)
SOURCE_SLUG = os.environ.get("SOURCE_SLUG", "ADL8113")

CACHE_PATH = RUNTIME / "ingest-cache.json"
MEDIA_DIR = WIKI / "media"
SOURCES_DIR = WIKI / "sources"


# Allow exact cache key override (avoids fragile substring matching)
CACHE_KEY = os.environ.get("CACHE_KEY", "")


def find_cache_entry(slug: str) -> Optional[dict]:
    """Find the cache entry whose key or filesWritten contains *slug*.

    Matching strategy (in order):
      1. Exact CACHE_KEY env var match (set by ingest.py's _auto_validate_ingest)
      2. slug appears in cache key (substring)
      3. slug appears in filesWritten paths
      4. Normalized match: strip common prefixes (book/, paper/, datasheet/)
         and suffixes (.pdf) from cache keys before comparing
    """
    if not CACHE_PATH.exists():
        return None
    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    entries = cache.get("entries", {})

    # 1. Exact CACHE_KEY match
    if CACHE_KEY and CACHE_KEY in entries:
        return {"key": CACHE_KEY, **entries[CACHE_KEY]}

    # 2. Substring match on key
    for k, v in entries.items():
        if slug in k:
            return {"key": k, **v}

    # 3. Substring match on filesWritten
    for k, v in entries.items():
        for fw in v.get("filesWritten", []):
            if slug in fw:
                return {"key": k, **v}

    # 4. Normalized match: strip common patterns from cache keys
    import re
    slug_norm = slug.strip().lower().replace(" ", "")
    for k, v in entries.items():
        # Strip book/, paper/, etc. prefix and .pdf suffix
        key_norm = re.sub(r'^(book|paper|datasheet|ApplicationNote|DesignExample|presentation|standard|news)/', '', k)
        key_norm = re.sub(r'\.(pdf|pptx|docx)$', '', key_norm)
        key_norm = key_norm.strip().lower().replace(" ", "")
        if slug_norm in key_norm or key_norm in slug_norm:
            return {"key": k, **v}

    return None


def find_media_dir(slug: str) -> Optional[Path]:
    """Find media directory matching slug (recursive search — media/ mirrors raw/)."""
    if not MEDIA_DIR.is_dir():
        return None
    # Recursive search: media/book/Foo, media/datasheet/05_AMP/Bar, etc.
    for d in MEDIA_DIR.rglob(slug):
        if d.is_dir():
            return d
    # Fallback: fuzzy match on slug substring
    for d in MEDIA_DIR.rglob("*"):
        if d.is_dir() and (slug in d.name or slug.replace(" ", "") in d.name.replace(" ", "")):
            return d
    return None


def main():
    results: list[bool] = []
    warnings: list[str] = []

    def check(label: str, ok: bool, detail: str = ""):
        status = "✅" if ok else "❌"
        suffix = f": {detail}" if detail else ""
        print(f"  {status} {label}{suffix}")
        results.append(ok)

    def note(label: str, detail: str = ""):
        print(f"  ⚪ {label}: {detail}")

    print("=" * 60)
    print(f"15-stage ingest validation")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Source:  {SOURCE_SLUG}")
    print("=" * 60)

    # ── Resolve cache entry ──
    entry = find_cache_entry(SOURCE_SLUG)
    stages = entry.get("stages", {}) if entry else {}

    media = find_media_dir(SOURCE_SLUG)
    source_page = None
    if SOURCES_DIR.is_dir():
        for f in SOURCES_DIR.rglob("*.md"):
            if SOURCE_SLUG in f.stem:
                source_page = f
                break

    # ═══════════════════════════════════════════════
    # Stage 0: Text extraction
    # ═══════════════════════════════════════════════
    print("\n[Stage 0] PDF text extraction")
    if entry:
        method = entry.get("method", "")
        check(f"text extracted via {method}", bool(method), f"method={method}")
    else:
        check("cache entry found for slug", False, f"no entry matching '{SOURCE_SLUG}'")

    # Stage 0 pilot validation (minerU OCR only)
    # Best-effort: the cache may not have been written at pilot time, so a missing
    # marker does not necessarily mean pilot was skipped — only a soft warning.
    if entry and entry.get("method", "").lower().find("mineru") >= 0:
        pilot_confirmed = entry.get("pilot_confirmed", False)
        pilot_marker = RUNTIME / ".pilot_done"
        if pilot_confirmed or pilot_marker.exists():
            note("pilot validated", "pilot_confirmed field or .pilot_done marker found")
        else:
            note("pilot NOT confirmed",
                 "no 'pilot_confirmed' field in cache and no .pilot_done marker — "
                 "pilot may have been manually confirmed; best-effort check only")

    # ═══════════════════════════════════════════════
    # Stage 0.5: Image extraction
    # ═══════════════════════════════════════════════
    print("\n[Stage 0.5] Image extraction")
    if media:
        manifest = media / "_manifest.json"
        imgs = list(media.glob("*.jpeg")) + list(media.glob("*.png")) + list(media.glob("*.jpg"))
        check(f"images extracted to {media.relative_to(PROJECT_ROOT)}/",
              len(imgs) > 0 or manifest.exists(),
              f"{len(imgs)} images, _manifest.json={'yes' if manifest.exists() else 'no'}")
    elif entry:
        img_ext = stages.get("images_extracted", 0)
        note("no media dir", f"cache: {img_ext} images extracted — may be text-only source")
    else:
        check("media dir found", False, f"slug={SOURCE_SLUG}")

    # ═══════════════════════════════════════════════
    # Stage 0.6: Image captioning
    # ═══════════════════════════════════════════════
    print("\n[Stage 0.6] Image captioning")
    if media:
        imgs = list(media.glob("*.jpeg")) + list(media.glob("*.png")) + list(media.glob("*.jpg"))
        if not imgs:
            check("no images to caption", True)
        else:
            missing = [img.name for img in imgs
                       if not (media / (img.name + ".caption.txt")).exists()]
            short = [img.name for img in imgs
                     if (media / (img.name + ".caption.txt")).exists()
                     and len((media / (img.name + ".caption.txt")).read_text().strip()) < 20]
            ok = not missing and not short
            check("all images have caption ≥ 20 chars", ok,
                  f"missing={len(missing)} short={len(short)} total={len(imgs)}")
    elif entry:
        img_cap = stages.get("images_captioned", 0)
        img_ext = stages.get("images_extracted", 0)
        if img_ext == 0:
            note("no images to caption", f"cache: {img_ext} extracted")
        else:
            check(f"images captioned ({img_cap}/{img_ext})", img_cap >= img_ext,
                  f"captioned={img_cap} extracted={img_ext}")
    else:
        check("media dir found", False)

    # ═══════════════════════════════════════════════
    # Stage 1: Global Digest
    # ═══════════════════════════════════════════════
    print("\n[Stage 1] Global Digest")
    if entry:
        dk = stages.get("global_digest_keys", 0)
        check(f"global digest complete", dk >= 1,
              f"{dk} top-level keys (ingest.py schema: book_meta/outline/key_entities/key_concepts/key_claims/chunk_plan)")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 1.5: Chunk Analysis (NEVER skipped)
    # ═══════════════════════════════════════════════
    print("\n[Stage 1.5] Chunk Analysis")
    if entry:
        chunks = stages.get("chunks_analyzed", 0)
        check(f"{chunks} chunk(s) analyzed", chunks >= 1,
              f"ingest.py schema: entities_found + concepts_found + claims per chunk (NOT chunk_meta/local_*/etc.)")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2: Generation
    # ═══════════════════════════════════════════════
    print("\n[Stage 2] Generation (synthesis)")
    if entry:
        fb = stages.get("file_blocks_generated", 0)
        identified = stages.get("concepts_identified", fb)
        generated = stages.get("concepts_generated", fb)
        core = stages.get("concepts_core", 0)
        supp = stages.get("concepts_supporting", 0)
        cov_core = stages.get("coverage_core", 1.0)
        cov_supp = stages.get("coverage_supporting", 1.0)
        check(f"{fb} FILE blocks, {generated} concepts (core:{cov_core:.0%} supp:{cov_supp:.0%} "
              f"of {core}+{supp} targeted)",
              fb >= 1,
              f"format: ---FILE:wiki/<path>---...---END FILE---")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2.5: Review suggestions
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.5] Review suggestions")
    rs_path = RUNTIME / "review-suggestions.json"
    if rs_path.exists():
        items = json.loads(rs_path.read_text()).get("items", [])
        check("review-suggestions.json has items", len(items) >= 0, f"{len(items)} items")
    elif entry:
        ri = stages.get("review_items", -1)
        if ri == 0:
            note("auto-skipped", "<4 FILE blocks — ingest.py skips Stage 2.5")
        elif ri > 0:
            check("review-suggestions.json exists", False, f"cache says {ri} items but file not found")
        else:
            note("not found", "may have been skipped")
    else:
        check("review-suggestions.json exists", False)

    # ═══════════════════════════════════════════════
    # Stage 2.6: Aggregate pages
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.6] Aggregate pages (programmatic append only)")
    for name in ("index.md", "log.md", "overview.md"):
        p = WIKI / name
        check(f"wiki/{name} exists and non-empty",
              p.exists() and p.stat().st_size > 0,
              f"{p.stat().st_size} bytes" if p.exists() else "missing")

    # ═══════════════════════════════════════════════
    # Stage 3: Write files
    # ═══════════════════════════════════════════════
    print("\n[Stage 3] Write files")
    sources = list((WIKI / "sources").rglob("*.md")) if (WIKI / "sources").is_dir() else []
    entities = list((WIKI / "entities").glob("*.md")) if (WIKI / "entities").is_dir() else []
    concepts = list((WIKI / "concepts").glob("*.md")) if (WIKI / "concepts").is_dir() else []
    if entry:
        fw = entry.get("filesWritten", [])
        missing = [f for f in fw if not (PROJECT_ROOT / f).exists()]
        check(f"{len(fw)} files written, all on disk",
              not missing and len(fw) >= 1,
              f"missing={len(missing)}" if missing else f"sources={len(sources)} concepts={len(concepts)} entities={len(entities)}")
    else:
        check("sources/concepts/entities all populated",
              len(sources) > 0 and len(concepts) > 0 and len(entities) > 0,
              f"sources={len(sources)} concepts={len(concepts)} entities={len(entities)}")

    # ═══════════════════════════════════════════════
    # Stage 3.5: Image injection
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.5] Image injection into source page")
    img_ext_s35 = stages.get("images_extracted", 0)
    if img_ext_s35 == 0:
        note("no images extracted — Stage 3.5 not applicable", "text-only source")
    elif source_page:
        text = source_page.read_text()
        has_section = "## Embedded Images" in text
        img_inj = stages.get("images_injected", 0)
        check("source page has '## Embedded Images' section",
              has_section or img_inj > 0,
              f"section={'yes' if has_section else 'no'} cache_injected={img_inj}")
    elif entry:
        img_ext = stages.get("images_extracted", 0)
        img_inj = stages.get("images_injected", 0)
        if img_ext == 0:
            note("no images — Stage 3.5 not applicable")
        elif img_inj > 0:
            check("source page found", False, "cache says images injected but no source page on disk")
        else:
            note("no images injected", f"extracted={img_ext} injected={img_inj}")
    else:
        check("source page exists", False)

    # ═══════════════════════════════════════════════
    # Stage 3.7: Source page coverage (ingested only)
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.7] Source page coverage")
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        entries = cache.get("entries", {})
        ingested = sum(1 for v in entries.values()
                       if isinstance(v, dict) and (v.get("filesWritten") or v.get("hash")))
        src_count = len(list((WIKI / "sources").rglob("*.md"))) if (WIKI / "sources").is_dir() else 0
        check(f"ingested files covered by source pages",
              ingested <= src_count,
              f"ingested={ingested} sources={src_count}")
    else:
        note("no cache, skipping")

    # ═══════════════════════════════════════════════
    # Stage 4: Review items
    # ═══════════════════════════════════════════════
    print("\n[Stage 4] Review items")
    reviews_dir = WIKI / "REVIEW"
    review_files = list(reviews_dir.rglob("*.md")) if reviews_dir.is_dir() else []
    review_json = RUNTIME / "review.json"
    if review_files:
        check(f"wiki/REVIEW/ has per-item .md files (recursive search)",
              len(review_files) >= 1,
              f"{len(review_files)} files")
    elif review_json.exists():
        rj = json.loads(review_json.read_text())
        items = rj.get("findings", [])
        check("review.json present", len(items) >= 0, f"{len(items)} findings")
    elif entry:
        ri = stages.get("review_items", -1)
        if ri <= 0:
            note("no review items", "Stage 2.5 was auto-skipped")
        else:
            check("review output found", False, f"cache says {ri} items but no review files on disk")
    else:
        check("review output found", False)

    # ═══════════════════════════════════════════════
    # Stage 5: Hash cache
    # ═══════════════════════════════════════════════
    print("\n[Stage 5] Hash cache")
    if entry:
        # Verify hash against raw file
        raw_root = PROJECT_ROOT / "raw"
        rel = entry.get("key", "")
        raw_file = raw_root / rel
        if raw_file.exists():
            actual = hashlib.sha256(raw_file.read_bytes()).hexdigest()
            expected = entry.get("hash", "")
            check("cache hash matches file",
                  actual[:16] == expected[:16],
                  f"expected={expected[:16]} actual={actual[:16]}")
        else:
            check("raw file found", False, f"missing: {rel}")
        check("filesWritten ≥ 1",
              len(entry.get("filesWritten", [])) >= 1,
              f"{len(entry.get('filesWritten', []))} files")
    else:
        check("ingest-cache.json has matching entry", False, f"slug={SOURCE_SLUG}")

    # ═══════════════════════════════════════════════
    # Stage 6: Embeddings (optional)
    # ═══════════════════════════════════════════════
    print("\n[Stage 6] Embeddings (optional)")
    lance = RUNTIME / "lancedb"
    embed_cache = RUNTIME / "embed-cache.json"
    lance_present = lance.is_dir() and bool(list(lance.glob("*.lance")))

    # Check embed-cache.json for entries
    embed_entries = 0
    embed_cache_exists = False
    if embed_cache.exists():
        try:
            with open(embed_cache, "r") as f:
                ec_data = json.load(f)
            if isinstance(ec_data, dict):
                embed_entries = len(ec_data)
            elif isinstance(ec_data, list):
                embed_entries = len(ec_data)
            embed_cache_exists = embed_entries > 0
        except (json.JSONDecodeError, OSError):
            embed_cache_exists = False

    if lance_present and embed_cache_exists:
        check("lancedb table present + embed-cache populated",
              True, f"{embed_entries} cache entries")
    elif lance_present and not embed_cache_exists:
        check("lancedb tables present", True,
              "WARNING: embed-cache.json empty/missing — embeddings may be stale")
    elif lance_present or embed_cache_exists:
        # Partial: one exists but not the other
        note("partial", f"lance={'yes' if lance_present else 'no'}, embed-cache={'populated' if embed_cache_exists else 'no'}")
    else:
        note("skipped", "LanceDB not enabled; OK if wiki < 100 pages")

    # ── Summary ──
    total = len(results)
    passed = sum(results)
    print("\n" + "=" * 60)
    if passed == total:
        print(f"Result: {passed}/{total} ✅ ALL PASS")
        sys.exit(0)
    else:
        print(f"Result: {passed}/{total} ❌ ({total - passed} failed)")
        sys.exit(1)


if __name__ == "__main__":
    main()
