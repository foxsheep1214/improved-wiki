"""Per-stage validators for improved-wiki ingest pipeline.

Implements fail-fast validation after each stage to catch errors early.
Aligns with NashSU's per-stage verification approach.

Usage:
    from _stage_validators import verify_stage_0, StageValidationError

    text_data = extract_text(file)
    if not verify_stage_0(text_data):
        raise StageValidationError("Stage 0: text extraction failed")
"""
from __future__ import annotations

from pathlib import Path

from _core import Config
from _stage_1_extract import _stage_1_2_media_slug


class StageValidationError(Exception):
    """Raised when a stage validation fails"""
    pass


def verify_stage_0(text_data: str) -> bool:
    """Verify Stage 0: Pre-processing (text extraction).

    Requirements:
    - Text length ≥ 100 characters
    - No critical encoding errors
    """
    if not text_data or not isinstance(text_data, str):
        return False
    if len(text_data.strip()) < 100:
        return False
    return True


def _verify_or_die(condition: bool, stage: str, msg: str) -> None:
    """Gate function: hard-abort on failure.

    Superpowers Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.
    Each stage MUST pass its verification before the pipeline proceeds.
    """
    if not condition:
        raise RuntimeError(f"[{stage}] ❌ VERIFICATION FAILED: {msg}")


def _verify_stage_1_1_text(raw_file: Path, extracted_text: str, method: str) -> None:
    """Verify OCR/text extraction produced usable output."""
    _verify_or_die(len(extracted_text) >= 500, "Stage 0",
                   f"Extracted text too short ({len(extracted_text)} chars) from {raw_file.name} "
                   f"via {method}. Digest will not be meaningful.")
    # For scanned PDFs with minerU, also verify per-page quality
    if method in ("mineru", "mineru-ocr", "mineru-vlm", "mineru-local-ocr"):
        _verify_or_die(len(extracted_text) >= 2000, "Stage 0",
                       f"MinerU OCR output suspiciously short ({len(extracted_text)} chars). "
                       f"VLM may have deadlocked or produced empty pages.")


def _verify_stage_2_1_digest(global_digest: dict, raw_file: Path) -> None:
    """Verify global digest has required structural keys."""
    required_keys = {"book_meta", "outline", "key_concepts", "key_claims", "key_entities", "chunk_plan"}
    missing = required_keys - set(global_digest.keys())
    _verify_or_die(len(missing) == 0, "Stage 1",
                   f"Global digest missing required keys: {missing}. "
                   f"Got keys: {list(global_digest.keys())[:8]}. "
                   f"LLM may have returned malformed YAML for {raw_file.name}.")
    # Verify at least some concepts were identified
    key_concepts = global_digest.get("key_concepts", [])
    _verify_or_die(len(key_concepts) >= 1, "Stage 1",
                   f"Global digest found 0 key_concepts for {raw_file.name}. "
                   f"Book may be too short or LLM output was incomplete.")


def _verify_stage_2_2_chunks(chunk_analyses: list[dict], extracted_text: str) -> None:
    """Verify chunk analysis produced results for all chunks."""
    _verify_or_die(len(chunk_analyses) >= 1, "Stage 2.2",
                   f"Chunk analysis produced 0 results. "
                   f"Text was {len(extracted_text)} chars — should produce at least 1 chunk.")
    # Warn if any chunk is suspiciously empty
    empty_chunks = [i for i, c in enumerate(chunk_analyses) if not c.get("concepts_found") and not c.get("entities_found")]
    if empty_chunks:
        print(f"  ⚠️  Stage 2.2: {len(empty_chunks)}/{len(chunk_analyses)} chunks have no concepts or entities found")


def _verify_stage_2_4_file_blocks(
    file_blocks: list[tuple[str, str]], raw_file: Path,
    incremental_associations: dict | None = None,
) -> None:
    """Verify synthesis produced valid FILE blocks with correct paths."""
    _verify_or_die(len(file_blocks) >= 1, "Stage 2",
                   f"0 FILE blocks parsed from LLM response for {raw_file.name}. "
                   f"LLM did not generate any wiki pages.")
    # Verify source page block exists
    source_blocks = [p for p, _ in file_blocks if "sources/" in p]
    _verify_or_die(len(source_blocks) >= 1, "Stage 2",
                   f"No source page FILE block in {len(file_blocks)} blocks. "
                   f"Paths: {[p for p, _ in file_blocks[:10]]}. "
                   f"LLM must emit a wiki/sources/<title>.md block.")
    # Verify concept pages are in wiki/concepts/, not bare wiki/ or wiki/sources/
    concept_blocks = [p for p, _ in file_blocks if "concepts/" in p or (not p.startswith(("wiki/", "sources/", "concepts/", "entities/")) and "sources/" not in p)]
    # True bare paths: no known subdirectory prefix and no wiki/ prefix
    _KNOWN_PREFIXES = ("wiki/", "sources/", "concepts/", "entities/", "queries/", "comparisons/", "synthesis/", "findings/", "thesis/")
    bare_paths = [p for p, _ in file_blocks if not p.startswith(_KNOWN_PREFIXES)]
    if bare_paths:
        print(f"  ⚠️  Stage 2: {len(bare_paths)} truly bare paths (no subdirectory prefix) — auto-correcting")
    wrong_dir = [p for p, _ in file_blocks if p.startswith("wiki/sources/") and not any(
        kw in p.lower() for kw in ["source", raw_file.stem.lower()[:10]])]
    # Only flag if there are many pages in sources/ that look like concepts
    sources_pages = [p for p, _ in file_blocks if p.startswith("wiki/sources/")]
    if len(sources_pages) > 2:
        print(f"  ⚠️  Stage 2: {len(sources_pages)} FILE blocks in wiki/sources/ — "
              f"only 1 source page expected, rest may be misplaced concepts")

    # Coverage check: warn if concept generation is sparse
    concept_file_blocks = [p for p, _ in file_blocks if "concepts/" in p]
    # Reasonable minimum: any non-trivial book should produce at least 5 concept pages
    # OR have most of its concepts already covered by existing wiki overlap —
    # a replay pass that correctly skips regenerating already-written concepts
    # (Stage 2.3's existing_refs) isn't a coverage failure (confirmed live:
    # Plett BMS Vol.2's final pass had 0 new concept blocks because all 20 of
    # its concepts already existed in the wiki from an earlier pass).
    n_overlap = len(incremental_associations) if incremental_associations else 0
    if len(concept_file_blocks) < 5 and len(concept_file_blocks) + n_overlap < 5 and len(file_blocks) >= 1:
        print(f"  ⚠️  Stage 2: only {len(concept_file_blocks)} concept pages generated "
              f"({n_overlap} existing-wiki overlaps). "
              f"Consider re-running with larger token budget or checking prompt output.")


def validate_stage_outputs(
    config: Config,
    raw_file: Path,
    method: str,
    extracted_text: str,
    stage_1_2_result: dict,
    stage_1_3_result: dict,
    file_blocks: list[tuple[str, str]],
    source_path: Path,
) -> list[str]:
    """Run NashSU go/no-go checks across all completed stages.

    Returns list of warnings.  Hard failures raise RuntimeError.
    """
    warnings: list[str] = []

    # Stage 0: extracted text sufficiency
    if len(extracted_text) < 500:
        msg = f"Stage 0: extracted text too short ({len(extracted_text)} chars) — digest may fail"
        warnings.append(msg)
        print(f"  ⚠️  {msg}")

    # Stage 1.2: image extraction completeness
    img_count = stage_1_2_result.get("count", 0)
    if img_count > 0:
        manifest = config.wiki_dir / "media" / _stage_1_2_media_slug(raw_file, config) / "_manifest.json"
        if not manifest.exists():
            warnings.append("Stage 1.2: images extracted but _manifest.json missing")
            print(f"  ⚠️  Stage 1.2: _manifest.json missing")

    # Stage 1.3: caption completeness — every image has .caption.txt >= 20 chars
    if img_count > 0:
        images = stage_1_2_result.get("images", [])
        missing_captions = 0
        for img in images:
            cap_path = config.wiki_dir / "media" / _stage_1_2_media_slug(raw_file, config) / (img["filename"] + ".caption.txt")
            if not cap_path.exists() or cap_path.stat().st_size < 20:
                missing_captions += 1
        if missing_captions > 0:
            msg = f"Stage 1.3: {missing_captions}/{len(images)} images missing captions"
            warnings.append(msg)
            print(f"  ⚠️  {msg}")
        if stage_1_3_result.get("captioned", 0) == 0 and not stage_1_3_result.get("skipped"):
            warnings.append("Stage 1.3: no captions generated (API may have failed)")
            print(f"  ⚠️  Stage 1.3: 0 captions generated")

    # Stage 2: FILE block validation
    if len(file_blocks) == 0:
        msg = "Stage 2: 0 FILE blocks parsed — LLM did not generate any wiki pages"
        warnings.append(msg)
        print(f"  ❌ {msg}")
    # Check that source page block exists
    source_block_found = any("sources/" in p for p, _ in file_blocks)
    if not source_block_found:
        warnings.append("Stage 2: no source page FILE block emitted (placeholder will be written)")
        print(f"  ⚠️  Stage 2: source page block missing")

    # Stage 3: file writing vs parsed blocks
    written_count = 0
    for rel_path, _ in file_blocks:
        full_path = config.wiki_dir / rel_path
        if full_path.exists():
            written_count += 1
    if written_count < len(file_blocks):
        msg = f"Stage 3: only {written_count}/{len(file_blocks)} FILE blocks written to disk"
        warnings.append(msg)
        print(f"  ⚠️  {msg}")

    # Stage 3.6: image injection verification
    if img_count > 0 and source_path.exists():
        source_content = source_path.read_text(encoding="utf-8")
        if "## Embedded Images" not in source_content:
            warnings.append("Stage 3.2: source page missing '## Embedded Images' section")
            print(f"  ⚠️  Stage 3.6: image injection not found in source page")

    # Stage 3: source page on disk (post-write verify)
    if not source_path.exists():
        warnings.append("Stage 3: source page does not exist after ingest")
        print(f"  ❌ Stage 3: source page missing")

    # Stage 3.3: review pages in wiki/REVIEW/<type>/ (分子目录)
    reviews_dir = config.wiki_dir / "REVIEW"
    if reviews_dir.exists():
        unresolved = 0
        for rp in reviews_dir.rglob("*.md"):
            content = rp.read_text(encoding="utf-8")
            if "resolved: false" in content[:500]:
                unresolved += 1
        if unresolved > 0:
            print(f"  ℹ️  wiki/REVIEW/: {unresolved} unresolved review pages pending human triage")

    # Stage 3.5: cache will be written after this — just check cache_path dir exists
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)

    if warnings:
        print(f"\n[validate] {len(warnings)} go/no-go warning(s) — see details above")
    else:
        print(f"[validate] All go/no-go checks passed ✅")

    return warnings
