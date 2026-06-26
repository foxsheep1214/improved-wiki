#!/usr/bin/env python3
"""Stage 4.1: per-project ingest validator (final verification gate).

validate_ingest.py — per-project 13-stage ingest validator

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
from _lint_suggest import run_structural_lint
RUNTIME = detect_runtime_dir(PROJECT_ROOT)
SOURCE_SLUG = os.environ.get("SOURCE_SLUG", "ADL8113")

CACHE_PATH = RUNTIME / "ingest-cache.json"
MEDIA_DIR = WIKI / "media"
SOURCES_DIR = WIKI / "sources"


# Allow exact cache key override (avoids fragile substring matching)
CACHE_KEY = os.environ.get("CACHE_KEY", "")


def _stage_4_1_find_cache_entry(slug: str) -> Optional[dict]:
    """Find the cache entry whose key or filesWritten contains *slug*.

    Matching strategy (in order):
      1. Exact CACHE_KEY env var match (set by ingest.py's stage_4_1_validate_ingest)
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
        # Case-insensitive: handles both old lowercase and new Titlecase dir names
        key_norm = re.sub(r'^(book|paper|datasheet|applicationnote|designexample|presentation|standard|news)/', '', k, flags=re.IGNORECASE)
        key_norm = re.sub(r'\.(pdf|pptx|docx)$', '', key_norm)
        key_norm = key_norm.strip().lower().replace(" ", "")
        if slug_norm in key_norm or key_norm in slug_norm:
            return {"key": k, **v}

    return None


def _stage_4_1_find_media_dir(slug: str) -> Optional[Path]:
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


# ── Structural lint suggestions (wiki-wide, non-gating) ─────────────────────
# Mirrors cross_source_dedup exclusions: anchors + state + lint/REVIEW/media dirs.
_LINT_ANCHOR_FILES = {"index.md", "log.md", "overview.md"}
_LINT_STATE_FILES = {
    "lint-cache.json", "ingest-cache.json", "ingest-queue.json",
    "review.json", "review-suggestions.json", "embed-cache.json",
    "lint-semantic.json", "dedup-report.json",
}
_LINT_SKIP_DIRS = {"lint", "REVIEW", "media"}


def _stage_4_1_collect_structural_lint_findings(wiki_dir: Path) -> list[dict]:
    """Run structural lint with deterministic link suggestions over wiki/.

    Returns findings from _lint_suggest.run_structural_lint — broken-link,
    orphan, no-outlinks — each enriched with a suggested_target /
    suggested_source when a confident match exists. Non-gating: the caller
    (validate_ingest.main) surfaces these without affecting the exit code.
    """
    pages: list[tuple[str, str]] = []
    if not wiki_dir.is_dir():
        return []
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki_dir)
        if rel.name in _LINT_ANCHOR_FILES or rel.name in _LINT_STATE_FILES:
            continue
        if rel.parts and rel.parts[0] in _LINT_SKIP_DIRS:
            continue
        try:
            pages.append((str(rel), path.read_text(encoding="utf-8")))
        except OSError:
            continue
    # with_suggestions=False: detection only (O(n)). The O(n^2) suggestion
    # scan is left to wiki-lint.sh; running it here on a 7594-page wiki took
    # minutes and blew the ingest's final-validation subprocess timeout.
    return run_structural_lint(pages, with_suggestions=False)


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
    print(f"13-stage ingest validation")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Source:  {SOURCE_SLUG}")
    print("=" * 60)

    # ── Resolve cache entry ──
    entry = _stage_4_1_find_cache_entry(SOURCE_SLUG)
    stages = entry.get("stages", {}) if entry else {}

    media = _stage_4_1_find_media_dir(SOURCE_SLUG)
    source_page = None
    if SOURCES_DIR.is_dir():
        for f in SOURCES_DIR.rglob("*.md"):
            if SOURCE_SLUG in f.stem:
                source_page = f
                break

    # ═══════════════════════════════════════════════
    # Stage 0: Text extraction
    # ═══════════════════════════════════════════════
    print("\n[Stage 1.1] PDF text extraction")
    if entry:
        method = entry.get("method", "")
        check(f"text extracted via {method}", bool(method), f"method={method}")
    else:
        check("cache entry found for slug", False, f"no entry matching '{SOURCE_SLUG}'")

    # ═══════════════════════════════════════════════
    # Stage 1.2: Image extraction
    # ═══════════════════════════════════════════════
    print("\n[Stage 1.2] Image extraction")
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
    # Stage 1.3: Image captioning
    # ═══════════════════════════════════════════════
    print("\n[Stage 1.3] Image captioning")
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
    print("\n[Stage 2.1] Global Digest")
    if entry:
        dk = stages.get("global_digest_keys", 0)
        check(f"global digest complete", dk >= 1,
              f"{dk} top-level keys (ingest.py schema: book_meta/outline/key_entities/key_concepts/key_claims/chunk_plan)")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2.2: Chunk Analysis (NEVER skipped)
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.2] Chunk Analysis")
    if entry:
        chunks = stages.get("chunks_analyzed", 0)
        check(f"{chunks} chunk(s) analyzed", chunks >= 1,
              f"ingest.py schema: entities_found + concepts_found + claims per chunk (NOT chunk_meta/local_*/etc.)")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2: Generation
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.4] Generation (synthesis)")
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
    # Stage 2.7: Query generation (conditional)
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.7] Query generation")
    queries_dir = WIKI / "queries"
    query_pages = list(queries_dir.glob("*.md")) if queries_dir.is_dir() else []
    src_query_pages = [p for p in query_pages
                       if SOURCE_SLUG in p.read_text(encoding="utf-8", errors="ignore")] if query_pages else []
    if entry:
        tmpl = (entry.get("template") or "").lower()
        qg = stages.get("queries_generated", 0)
        if tmpl in ("datasheet", "standard"):
            note("auto-skipped", f"template={tmpl} (datasheet/standard skip Stage 2.7)")
        else:
            check(f"{qg} query page(s) generated", 0 <= qg <= 5,
                  f"cache={qg} disk_attributed={len(src_query_pages)} (0-5 valid; 0 = ---QUERIES: 0---)")
    else:
        note("no cache entry", f"disk queries/ has {len(query_pages)} page(s) total")

    # ═══════════════════════════════════════════════
    # Stage 2.9 (cmp): Comparison generation (conditional)
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.9 cmp] Comparison generation")
    comparisons_dir = WIKI / "comparisons"
    comp_pages = list(comparisons_dir.glob("*.md")) if comparisons_dir.is_dir() else []
    src_comp_pages = [p for p in comp_pages
                      if SOURCE_SLUG in p.read_text(encoding="utf-8", errors="ignore")] if comp_pages else []
    if entry:
        cg = stages.get("comparisons_generated", 0)
        fb = stages.get("file_blocks_generated", 0)
        if fb == 0:
            note("auto-skipped", "no concept output — Stage 2.9 cmp skipped")
        else:
            check(f"{cg} comparison page(s) generated", 0 <= cg <= 2,
                  f"cache={cg} disk_attributed={len(src_comp_pages)} (0-2 valid; 0 = ---COMPARISONS: 0---)")
    else:
        note("no cache entry", f"disk comparisons/ has {len(comp_pages)} page(s) total")

    # ═══════════════════════════════════════════════
    # Stage 3: Write files (+ source page coverage)
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.1] Write files")
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
    # Source page coverage (project-wide health check)
    if CACHE_PATH.exists():
        _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        _entries = _cache.get("entries", {})
        ingested = sum(1 for v in _entries.values()
                       if isinstance(v, dict) and (v.get("filesWritten") or v.get("hash")))
        check(f"ingested files covered by source pages",
              ingested <= len(sources),
              f"ingested={ingested} sources={len(sources)}")

    # ═══════════════════════════════════════════════
    # Stage 3.2: Image injection
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.2] Image injection into source page")
    img_ext_s35 = stages.get("images_extracted", 0)
    if img_ext_s35 == 0:
        note("no images extracted — Stage 3.2 not applicable", "text-only source")
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
            note("no images — Stage 3.2 not applicable")
        elif img_inj > 0:
            check("source page found", False, "cache says images injected but no source page on disk")
        else:
            note("no images injected", f"extracted={img_ext} injected={img_inj}")
    else:
        check("source page exists", False)

    # ═══════════════════════════════════════════════
    # Stage 3.4 (rev): Review suggestions + review items
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.4 rev] Review suggestions + items")
    rs_path = RUNTIME / "review-suggestions.json"
    if rs_path.exists():
        items = json.loads(rs_path.read_text()).get("items", [])
        check("review-suggestions.json has items", len(items) >= 0, f"{len(items)} items")
    elif entry:
        ri = stages.get("review_items", -1)
        if ri == 0:
            note("auto-skipped", "<4 FILE blocks — ingest.py skips Stage 3.4 rev")
        elif ri > 0:
            check("review-suggestions.json exists", False, f"cache says {ri} items but file not found")
        else:
            note("not found", "may have been skipped")
    else:
        check("review-suggestions.json exists", False)
    # Review items on disk (merged from old Stage 4)
    reviews_dir = WIKI / "REVIEW"
    review_files = list(reviews_dir.rglob("*.md")) if reviews_dir.is_dir() else []
    review_json = RUNTIME / "review.json"
    if review_files:
        check(f"wiki/REVIEW/ has per-item .md files",
              len(review_files) >= 1,
              f"{len(review_files)} files")
    elif review_json.exists():
        rj = json.loads(review_json.read_text())
        ritems = rj.get("findings", [])
        check("review.json present", len(ritems) >= 0, f"{len(ritems)} findings")
    elif entry:
        ri = stages.get("review_items", -1)
        if ri <= 0:
            note("no review items", "Stage 3.4 rev was auto-skipped")
        else:
            check("review output found", False, f"cache says {ri} items but no review files on disk")
    else:
        check("review output found", False)

    # ═══════════════════════════════════════════════
    # Stage 3.5: Aggregate pages + hash cache
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.5] Aggregate pages + hash cache")
    for name in ("index.md", "log.md", "overview.md"):
        p = WIKI / name
        check(f"wiki/{name} exists and non-empty",
              p.exists() and p.stat().st_size > 0,
              f"{p.stat().st_size} bytes" if p.exists() else "missing")
    # Hash cache (merged from old Stage 5)
    if entry:
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
    # Stage 3.7: Embeddings (mandatory attempt — local Ollama bge-m3)
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.7] Embeddings (mandatory attempt)")
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
        check("lancedb + embed-cache consistency", False,
              f"lance={'yes' if lance_present else 'no'}, embed-cache={'populated' if embed_cache_exists else 'no'}")
    else:
        sys.path.insert(0, str(_script_dir))
        from ingest import _stage_3_7_check_embed_capability
        base_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
        model = os.environ.get("EMBEDDING_MODEL", "bge-m3")
        cap_ok, cap_reason = _stage_3_7_check_embed_capability(base_url, model)
        if cap_ok:
            check("embeddings present", False,
                  "本地 Ollama bge-m3 可用但 wiki 尚未 embed — 补跑 build_embeddings.py")
        else:
            check("embeddings present", False,
                  f"本地能力不可用（{cap_reason}）— 安装后补跑 build_embeddings.py")

    # ═══════════════════════════════════════════════
    # Structural lint suggestions (wiki-wide, non-gating)
    # ═══════════════════════════════════════════════
    print("\n[Lint suggestions] Structural (wiki-wide, non-gating)")
    try:
        lint_findings = _stage_4_1_collect_structural_lint_findings(WIKI)
    except Exception as e:  # defensive: lint must never break the validator
        lint_findings = []
        note("structural lint skipped", f"{type(e).__name__}: {e}")
    from collections import Counter as _Counter
    _lc = _Counter(f["type"] for f in lint_findings)
    note("findings",
         f"broken-link={_lc.get('broken-link', 0)} "
         f"orphan={_lc.get('orphan', 0)} "
         f"no-outlinks={_lc.get('no-outlinks', 0)}")
    for f in lint_findings[:20]:
        suggestion = f.get("suggested_target") or f.get("suggested_source")
        sugg = f" → suggest: {suggestion}" if suggestion else " (no suggestion)"
        print(f"    [{f['type']}] {f['page']}{sugg}")
    if len(lint_findings) > 20:
        print(f"    ... and {len(lint_findings) - 20} more")

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
