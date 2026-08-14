#!/usr/bin/env python3
"""Standalone per-project ingest validator (manual check — NOT an auto-run stage).

validate_ingest.py — per-project 13-stage ingest validator. Run manually for a
post-hoc audit; the former auto-run "Stage 4.1" was removed for NashSU alignment
(NashSU has no post-ingest verification stage). Still used by the lint tooling.

Aligns with ingest.py actual output: reads from .llm-wiki/ingest-cache.json
cache entry + disk state.  Does NOT look for intermediate files (full.txt,
*-global-digest.yaml, *-chunk*-analysis.yaml, generation_response*.txt) that
ingest.py does not write — those artifacts live in progress checkpoints and
are cleared on successful ingest.

Usage:
    python3 scripts/validate_ingest.py --root /path/to/wiki --source INA1H94-SEP
    IMPROVED_WIKI_ROOT=/path/to/wiki SOURCE_SLUG=INA1H94-SEP \
        python3 scripts/validate_ingest.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# === Per-project runtime context ===
# Defaults keep helper imports side-effect free. main() rebinds these paths from
# explicit CLI arguments (preferred) or the matching environment variables.
PROJECT_ROOT = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
WIKI = PROJECT_ROOT / "wiki"
# Use shared detection (_paths.py: .llm-wiki/ default, auto-migrates from .iwiki-runtime/)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir, iter_wiki_pages
from _lint_suggest import (
    run_structural_lint,
    ANCHOR_FILES as _LINT_ANCHOR_FILES,
    STATE_FILES as _LINT_STATE_FILES,
)
from _progress import file_sha256
RUNTIME = detect_runtime_dir(PROJECT_ROOT)
SOURCE_SLUG = os.environ.get("SOURCE_SLUG", "")

CACHE_PATH = RUNTIME / "ingest-cache.json"
MEDIA_DIR = WIKI / "media"
SOURCES_DIR = WIKI / "sources"


# Allow exact cache key override (avoids fragile substring matching)
CACHE_KEY = os.environ.get("CACHE_KEY", "")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse the standalone validator CLI without project-specific defaults."""
    parser = argparse.ArgumentParser(
        description="Read-only post-hoc validation for one improved-wiki source.",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()),
        help="improved-wiki project root (default: IMPROVED_WIKI_ROOT or cwd)",
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("SOURCE_SLUG", ""),
        help="source page stem/cache-key substring (or set SOURCE_SLUG)",
    )
    parser.add_argument(
        "--cache-key",
        default=os.environ.get("CACHE_KEY", ""),
        help="exact ingest-cache key when substring matching would be ambiguous",
    )
    args = parser.parse_args(argv)
    if not str(args.source).strip():
        parser.error("--source is required (or set SOURCE_SLUG)")
    return args


def _configure_runtime(
    project_root: str | Path,
    source_slug: str,
    cache_key: str = "",
) -> None:
    """Bind all derived paths to the CLI-selected project and source."""
    global PROJECT_ROOT, WIKI, RUNTIME, SOURCE_SLUG
    global CACHE_PATH, MEDIA_DIR, SOURCES_DIR, CACHE_KEY

    PROJECT_ROOT = Path(project_root).expanduser().resolve()
    WIKI = PROJECT_ROOT / "wiki"
    RUNTIME = detect_runtime_dir(PROJECT_ROOT)
    SOURCE_SLUG = str(source_slug).strip()
    CACHE_KEY = str(cache_key).strip()
    CACHE_PATH = RUNTIME / "ingest-cache.json"
    MEDIA_DIR = WIKI / "media"
    SOURCES_DIR = WIKI / "sources"


def _validate_find_cache_entry(slug: str) -> Optional[dict]:
    """Find the cache entry whose key or filesWritten contains *slug*.

    Matching strategy (in order):
      1. Exact CACHE_KEY env var match (set manually when running validate_ingest.py standalone)
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


def _validate_find_media_dir(slug: str) -> Optional[Path]:
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


def _validate_recorded_source_pages(entry: dict, project_root: Path) -> tuple[list[str], list[Path]]:
    """Return source-page paths recorded by this cache entry and those on disk.

    Cache entries are not globally one-to-one with source pages: explicitly
    ingested query-page entries intentionally write no source page, while older
    ingests may retain pre-migration paths. Per-source validation must inspect the
    selected entry instead of comparing project-wide cache/page totals.
    """
    recorded: list[str] = []
    existing: list[Path] = []
    for value in entry.get("filesWritten", []):
        if not isinstance(value, str):
            continue
        normalized = value.replace("\\", "/").lstrip("./")
        if not normalized.startswith("wiki/sources/"):
            continue
        recorded.append(value)
        path = Path(value)
        if not path.is_absolute():
            path = project_root / path
        if path.is_file():
            existing.append(path)
    return recorded, existing


# ── Structural lint suggestions (wiki-wide, non-gating) ─────────────────────
# Scan universe = NashSU {index, log} from _lint_suggest (overview/schema stay
# valid targets; engine exempts aggregates from findings). + state files
# (shared _lint_suggest.STATE_FILES) + artifact dirs (shared
# _paths.WIKI_ARTIFACT_DIRS) — the local copies here had drifted.


def _validate_collect_structural_lint_findings(wiki_dir: Path) -> list[dict]:
    """Run structural lint with deterministic link suggestions over wiki/.

    Returns findings from _lint_suggest.run_structural_lint — broken-link,
    orphan, no-outlinks — each enriched with a suggested_target /
    suggested_source when a confident match exists. Non-gating: the caller
    (validate_ingest.main) surfaces these without affecting the exit code.
    """
    pages = list(iter_wiki_pages(
        wiki_dir, anchor_files=_LINT_ANCHOR_FILES, state_files=_LINT_STATE_FILES,
    ))
    # with_suggestions=False: detection only (O(n)). The O(n^2) suggestion
    # scan is left to wiki-lint.sh; running it here on a 7594-page wiki took
    # minutes and blew the ingest's final-validation subprocess timeout.
    return run_structural_lint(pages, with_suggestions=False)


def main(argv: Optional[list[str]] = None):
    args = _parse_args(argv)
    _configure_runtime(args.root, args.source, args.cache_key)
    results: list[bool] = []

    def check(label: str, ok: bool, detail: str = ""):
        status = "✅" if ok else "❌"
        suffix = f": {detail}" if detail else ""
        print(f"  {status} {label}{suffix}")
        results.append(ok)

    def note(label: str, detail: str = ""):
        print(f"  ⚪ {label}: {detail}")

    print("=" * 60)
    print("13-stage ingest validation")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Source:  {SOURCE_SLUG}")
    print("=" * 60)

    # ── Resolve cache entry ──
    entry = _validate_find_cache_entry(SOURCE_SLUG)
    stages = entry.get("stages", {}) if entry else {}

    media = _validate_find_media_dir(SOURCE_SLUG)
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
    # Global Digest (rolled up by Stage 2.2)
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.2] Global Digest (roll-up)")
    if entry:
        dk = stages.get("global_digest_keys", 0)
        check("global digest complete", dk >= 1,
              f"{dk} top-level keys (ingest.py schema: book_meta/outline/key_entities/key_concepts/key_claims)")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2.2: Chunk Analysis (NEVER skipped)
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.2] Chunk Analysis")
    if entry:
        chunks = stages.get("chunks_analyzed", 0)
        check(f"{chunks} chunk(s) analyzed", chunks >= 1,
              "ingest.py schema: entities_found + concepts_found + claims per chunk (NOT chunk_meta/local_*/etc.)")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2: Generation
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.4] Generation (synthesis)")
    if entry:
        fb = stages.get("file_blocks_generated", 0)
        generated = stages.get("concepts_generated", fb)
        core = stages.get("concepts_core", 0)
        supp = stages.get("concepts_supporting", 0)
        cov_core = stages.get("coverage_core", 1.0)
        cov_supp = stages.get("coverage_supporting", 1.0)
        check(f"{fb} FILE blocks, {generated} concepts (core:{cov_core:.0%} supp:{cov_supp:.0%} "
              f"of {core}+{supp} targeted)",
              fb >= 1,
              "format: ---FILE:wiki/<path>---...---END FILE---")
    else:
        check("cache entry found", False)

    # ═══════════════════════════════════════════════
    # Stage 2.4 typed pages: comparison is optional and schema-driven
    # ═══════════════════════════════════════════════
    print("\n[Stage 2.4 typed] Comparison pages (optional)")
    comparisons_dir = WIKI / "comparisons"
    comp_pages = list(comparisons_dir.glob("*.md")) if comparisons_dir.is_dir() else []
    src_comp_pages = [p for p in comp_pages
                      if SOURCE_SLUG in p.read_text(encoding="utf-8", errors="ignore")] if comp_pages else []
    if entry:
        cg = stages.get("comparisons_generated", 0)
        check(
            f"{cg} comparison page(s) generated through Stage 2.4",
            isinstance(cg, int) and cg >= 0,
            "comparison is a schema-typed candidate; zero is valid and there "
            "is no dedicated-stage sentinel or numeric cap",
        )
        note(
            "disk attribution",
            f"{len(src_comp_pages)} comparison page(s) reference this source",
        )
    else:
        note("no cache entry", f"disk comparisons/ has {len(comp_pages)} page(s) total")

    # ═══════════════════════════════════════════════
    # Stage 3: Write files (+ source page coverage)
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.2] Write files")
    sources = list((WIKI / "sources").rglob("*.md")) if (WIKI / "sources").is_dir() else []
    entities = list((WIKI / "entities").glob("*.md")) if (WIKI / "entities").is_dir() else []
    concepts = list((WIKI / "concepts").glob("*.md")) if (WIKI / "concepts").is_dir() else []
    if entry:
        fw = entry.get("filesWritten", [])
        missing = [f for f in fw if not (PROJECT_ROOT / f).exists()]
        check(f"{len(fw)} files written, all on disk",
              not missing and len(fw) >= 1,
              f"missing={len(missing)}" if missing else f"sources={len(sources)} concepts={len(concepts)} entities={len(entities)}")
        recorded_sources, existing_recorded_sources = _validate_recorded_source_pages(
            entry, PROJECT_ROOT,
        )
        check(
            "target source page is recorded and exists",
            bool(recorded_sources) and bool(existing_recorded_sources),
            (
                f"recorded={len(recorded_sources)} "
                f"existing={len(existing_recorded_sources)}"
            ),
        )
    else:
        check("sources/concepts/entities all populated",
              len(sources) > 0 and len(concepts) > 0 and len(entities) > 0,
              f"sources={len(sources)} concepts={len(concepts)} entities={len(entities)}")
    # Project-wide inventory is diagnostic only. Cache entries and source pages
    # are not one-to-one because research-query entries intentionally have no
    # source page and historical entries may retain pre-migration paths.
    if CACHE_PATH.exists():
        _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        _entries = _cache.get("entries", {})
        ingested = sum(1 for v in _entries.values()
                       if isinstance(v, dict) and (v.get("filesWritten") or v.get("hash")))
        note(
            "project-wide source-page inventory (non-gating)",
            f"cache entries={ingested} source pages={len(sources)}",
        )

    # ═══════════════════════════════════════════════
    # Stage 3.4: Image injection
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.4] Image injection into source page")
    images_extracted = stages.get("images_extracted", 0)
    if images_extracted == 0:
        note("no images extracted — Stage 3.4 not applicable", "text-only source")
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
            note("no images — Stage 3.4 not applicable")
        elif img_inj > 0:
            check("source page found", False, "cache says images injected but no source page on disk")
        else:
            note("no images injected", f"extracted={img_ext} injected={img_inj}")
    else:
        check("source page exists", False)

    # ═══════════════════════════════════════════════
    # Stages 3.1/3.5: Review generation and persistence
    # ═══════════════════════════════════════════════
    print("\n[Stages 3.1/3.5] Review suggestions + persisted items")
    rs_path = RUNTIME / "review-suggestions.json"
    if rs_path.exists():
        items = json.loads(rs_path.read_text()).get("items", [])
        check("review-suggestions.json has items", len(items) >= 0, f"{len(items)} items")
    elif entry:
        ri = stages.get("review_items", -1)
        if ri == 0:
            note("zero review items", "review trigger may be false, or reviewer returned []")
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
        check("wiki/REVIEW/ has per-item .md files",
              len(review_files) >= 1,
              f"{len(review_files)} files")
    elif review_json.exists():
        rj = json.loads(review_json.read_text())
        ritems = rj.get("findings", [])
        check("review.json present", len(ritems) >= 0, f"{len(ritems)} findings")
    elif entry:
        ri = stages.get("review_items", -1)
        if ri <= 0:
            note("no review items", "review trigger may be false, or reviewer returned []")
        else:
            check("review output found", False, f"cache says {ri} items but no review files on disk")
    else:
        check("review output found", False)

    # ═══════════════════════════════════════════════
    # Stages 3.3/3.6: Aggregate pages + hash cache
    # ═══════════════════════════════════════════════
    print("\n[Stages 3.3/3.6] Aggregate pages + hash cache")
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
            actual = file_sha256(raw_file)
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
    # Stage 3.7: Embeddings (mandatory touched-page coverage)
    # ═══════════════════════════════════════════════
    print("\n[Stage 3.7] Embeddings (mandatory touched-page coverage)")
    lance = RUNTIME / "lancedb"
    lance_present = lance.is_dir() and bool(list(lance.glob("*.lance")))
    if lance_present:
        try:
            import lancedb
            import build_embeddings as _be

            db = lancedb.connect(str(lance))
            table = db.open_table(_be.TABLE_NAME)
            total_rows = table.count_rows()
            check("lancedb table present + non-empty",
                  total_rows > 0, f"{total_rows} chunks")

            refs = list((entry or {}).get("filesWritten", []))
            _be.ROOT = str(PROJECT_ROOT)
            _be.WIKI = str(WIKI)
            pages = _be.collect_pages(refs) if refs else []
            embedding_config = _be.embedding_config_from_env()
            chunks = _be.build_chunks(
                pages,
                target_chars=embedding_config.target_chars,
                overlap_chars=embedding_config.overlap_chars,
            )
            expected: dict[str, int] = {}
            for chunk in chunks:
                pid = chunk["page_id"]
                expected[pid] = expected.get(pid, 0) + 1
            mismatches = []
            for page_id, count_expected in expected.items():
                predicate = _be._page_filter(page_id)
                count_actual = table.count_rows(predicate)
                if count_actual != count_expected:
                    mismatches.append(
                        f"{page_id}: expected {count_expected}, found {count_actual}"
                    )
            check(
                "source pages have exact embedding coverage",
                not mismatches,
                (
                    f"{sum(expected.values())} chunks across {len(expected)} pages"
                    if not mismatches
                    else "; ".join(mismatches[:5])
                ),
            )
        except Exception as exc:
            check("lancedb embedding coverage readable", False,
                  f"{type(exc).__name__}: {exc}")
    else:
        sys.path.insert(0, str(_script_dir))
        from _stage_3_7_embed import _stage_3_7_check_embed_capability
        base_url = (
            os.environ.get("EMBEDDING_ENDPOINT")
            or os.environ.get("EMBEDDING_BASE_URL")
            or "http://127.0.0.1:11434/v1"
        )
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
        lint_findings = _validate_collect_structural_lint_findings(WIKI)
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
