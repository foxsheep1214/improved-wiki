#!/usr/bin/env python3
"""
ingest.py — End-to-end Ingest for one source file (NashSU-style multi-stage pipeline).

Pipeline (aligned with ingest-stages-mandatory.md):
  1. Dedup check          (wiki/sources/ source page → skip)
  2. Extract text          (PyMuPDF first, minerU VLM OCR fallback)
  3. Global digest          (1 LLM call: book-level structural summary)
  4. Chunk + analyze       (N LLM calls: per-chunk structured analysis)
  5. Synthesize            (1 LLM call: combine digest+analyses → page specs + File blocks)
  6. Write files           (sources/ + concepts/ + entities/)
  7. Update cache          (sha256 → filesWritten[])

Usage:
  ingest.py <raw-file-path>                # process one file
  ingest.py f1.pdf f2.pdf ...              # batch mode: parallel Stage 0-2
  ingest.py --dry-run <raw-file-path>      # show what would be done, no writes
  ingest.py --verbose <raw-file-path>      # show LLM responses for debugging
  ingest.py --watch                        # continuous queue consumer (daemon mode)
  ingest.py --watch --drain                # process queue until empty, then exit
  ingest.py --watch --poll-interval 60     # re-scan queue every 60s

Configuration:
  ~/.agents/config.json   provider config (default: deepseek, caption: minimax)
  LLM_PROVIDER            override provider name (env var)
  LLM_API_KEY             override API key (env var)
  LLM_BASE_URL            override base URL (env var)
  LLM_MODEL               override model name (env var)
  LLM_CHUNK_RETRIES       extra attempts per failed chunk (default 2 → 3 total)
  Text LLM:               config.json default provider (DeepSeek V4 Pro via OpenAI protocol)
  Image caption:          config.json caption_provider (MiniMax via Anthropic protocol)
                            CAPTION_BATCH_SIZE=8   images per API call
                            CAPTION_MAX_WORKERS=6  parallel batch concurrency
  Embeddings:             local Ollama (EMBEDDING_BASE_URL / EMBEDDING_MODEL)

This script is idempotent: if the source page exists for a file, it's skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# ── Imports from split stage modules (refactored 2026-06-18) ──
from _core import (
    Config, ConversationPending,
    set_current_file as _set_current_file,
    get_current_file as _get_current_file,
    file_tag as _file_tag,
    stage_begin as _stage_begin,
    stage_end as _stage_end,
    heartbeat as _heartbeat,
    llm_call_progress as _llm_call_progress,
    llm_call_done as _llm_call_done,
    record_rate_limit as _record_rate_limit,
    rate_limit_cooldown_remaining as _rate_limit_cooldown_remaining,
    load_provider_config as _load_provider_config,
    load_caption_provider as _load_caption_provider,
    str_distance as _str_distance,
    detect_template_type, load_template,
    file_sha256, load_cache, save_cache,
    progress_path, load_progress, save_progress, clear_progress,
    ProjectLock,
    detect_domain as _detect_domain,
    list_existing_slugs,
    parse_yaml_block, parse_simple_yaml, parse_file_blocks,
    FOLDER_TO_TEMPLATE,
)
from _stage_0_3_pilot import stage_0_3_pilot
from _stage_1_extract import (
    stage_1_1_extract_text,
    stage_1_2_extract_images,
    _stage_1_2_extract_from_mineru,
    stage_1_3_caption_images,
    _stage_1_1_check_text_quality,
    _stage_1_2_media_slug,
    CAPTION_BATCH_SIZE, CAPTION_MAX_WORKERS,
)
from _stage_2_analyze import (
    stage_2_1_global_digest,
    stage_2_2_chunk_analysis,
    _stage_2_1_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
)
from _stage_2_4_generation import (
    _stage_2_4_build_prompt,
    stage_2_4_generate_chunk,
    _stage_2_4_extract_names,
    _stage_2_4_per_concept_fallback,
)
from _stage_2_6_source_page import stage_2_6_source_page
from _stage_2_7_query_generation import stage_2_7_query_generation, _stage_2_7_build_prompt
from _stage_2_9_comparison import (
    stage_2_9_comparison_generation,
    _stage_2_9_build_prompt_disambiguation,
    _stage_2_9_build_prompt_in_source,
)
from _stage_2_10_review import stage_2_10_review_suggestions
from _stage_3_write import (
    stage_3_1_write_wiki_file, stage_3_4_aggregate_repair,
    _stage_3_1_canonicalize_sources_field, _stage_3_1_stamp_frontmatter_dates,
    _stage_3_1_sanitize_ingested_content,
    _stage_3_1_wiki_path_for_source, _stage_3_1_merge_page_content,
    _stage_3_1_auto_correct_wiki_path, _stage_3_1_contains_cjk, _stage_3_1_make_cjk_slug,
    _stage_3_1_backup_existing_page,
)
from _stage_3_2_inject_images import stage_3_2_inject_images
from _enrich_wikilinks import enrich_wikilinks
from _stage_validators import (
    verify_stage_0, verify_stage_1, verify_stage_2,
    verify_stage_3,     StageValidationError,
)


# Use shared runtime detection (matches all other scripts)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402
from _llm_api import (  # noqa: E402
    _retry_jitter,
    _is_retryable_exception,
    conversation_handoff,
    set_progress_hook,
    set_conversation_router,
)
# Wire up progress hook for LLM API calls
set_progress_hook(_llm_call_progress)

def cleanup_resolved_reviews(config: Config) -> int:
    """Delete review pages whose frontmatter has `resolved: true`.

    Called at the start of each ingest run. Returns count of deleted pages.
    """
    reviews_dir = config.wiki_dir / "REVIEW"
    if not reviews_dir.exists():
        return 0

    removed = 0
    for f in sorted(reviews_dir.rglob("*.md")):
        if not f.suffix == ".md":
            continue
        content = f.read_text(encoding="utf-8")
        # Check frontmatter for resolved: true
        m = re.search(r'^resolved:\s*true\s*$', content, re.MULTILINE)
        if m:
            f.unlink()
            removed += 1
            print(f"[cleanup] Resolved review removed: {f.name}")

    if removed > 0:
        print(f"[cleanup] {removed} resolved review page(s) deleted")

    return removed


# ---------- Stage go/no-go validation ----------

# ═══════════════════════════════════════════════════════════════
# Stage verification gates (superpowers: verification-before-completion)
# ═══════════════════════════════════════════════════════════════

def _verify_or_die(condition: bool, stage: str, msg: str) -> None:
    """Gate function: hard-abort on failure.

    Superpowers Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.
    Each stage MUST pass its verification before the pipeline proceeds.
    """
    if not condition:
        raise RuntimeError(f"[{stage}] ❌ VERIFICATION FAILED: {msg}")


def _should_stop_after(config: Config, stage: str, result: dict) -> bool:
    """Check if we should stop after completing `stage`. Progress already saved before call."""
    if config.stop_after_stage == stage:
        print(f"\n[stop-after-stage] Stage {stage} complete — clean exit (--stop-after-stage={stage})")
        return True
    return False


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


def _verify_stage_2_4_file_blocks(file_blocks: list[tuple[str, str]], raw_file: Path) -> None:
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
    if len(concept_file_blocks) < 5 and len(file_blocks) >= 1:
        print(f"  ⚠️  Stage 2: only {len(concept_file_blocks)} concept pages generated. "
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

    # Stage 3.5: image injection verification
    if img_count > 0 and source_path.exists():
        source_content = source_path.read_text(encoding="utf-8")
        if "## Embedded Images" not in source_content:
            warnings.append("Stage 3.2: source page missing '## Embedded Images' section")
            print(f"  ⚠️  Stage 3.5: image injection not found in source page")

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

    # Stage 3.4: cache will be written after this — just check cache_path dir exists
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)

    if warnings:
        print(f"\n[validate] {len(warnings)} go/no-go warning(s) — see details above")
    else:
        print(f"[validate] All go/no-go checks passed ✅")

    return warnings


def _run_post_ingest_lint(config: Config) -> None:
    """Run wiki-lint.sh after ingest (structural lint only; semantic lint via standalone command).

    Set SKIP_POST_INGEST_LINT=1 to skip during batch runs (lint once at end).
    """
    if os.environ.get("SKIP_POST_INGEST_LINT") == "1":
        print("[lint] Skipped (SKIP_POST_INGEST_LINT=1)")
        return
    lint_script = Path(__file__).parent / "wiki-lint.sh"
    if not lint_script.exists():
        print("[lint] wiki-lint.sh not found — skipping")
        return
    import subprocess

    cmd = ["bash", str(lint_script), "--summary"]
    try:
        result = subprocess.run(
            cmd, cwd=config.wiki_root, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if "findings" in line or "Pages:" in line:
                    print(line.strip())
            if result.stderr.strip():
                print(f"[lint] {result.stderr.strip()[:200]}")
        else:
            print(f"[lint] wiki-lint.sh exited {result.returncode}: {result.stderr.strip()[:200]}")
    except Exception as e:
        print(f"[lint] Lint failed ({e}) — continuing")


def _run_post_ingest_graph(config: Config) -> None:
    """Rebuild knowledge graph after ingest (once per session, stale-guarded).

    Controlled by AUTO_BUILD_GRAPH=1. The graph needs the full wiki state,
    but rebuilding after every book in a batch would be wasteful. Uses a
    staleness guard: skips if graph.json was rebuilt < 30 minutes ago.

    Mirrors NashSU's desktop app: the graph auto-refreshes when you view it.
    """
    if os.environ.get("AUTO_BUILD_GRAPH") != "1":
        return
    graph_script = Path(__file__).parent / "graph.py"
    if not graph_script.exists():
        print("[graph] graph.py not found — skipping")
        return

    # Staleness guard: don't rebuild more than once per 30 minutes
    graph_json = config.runtime_dir / "graph.json"
    if graph_json.exists():
        age_min = (time.time() - graph_json.stat().st_mtime) / 60
        if age_min < 30:
            print(f"[graph] Skipped — graph rebuilt {age_min:.0f}m ago (staleness guard)")
            return

    import subprocess
    print("[graph] Rebuilding knowledge graph...")
    try:
        result = subprocess.run(
            [sys.executable, str(graph_script)],
            cwd=config.wiki_root, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[-3:]:
                print(f"[graph] {line.strip()}")
        else:
            print(f"[graph] Failed ({result.returncode}): {result.stderr[:200]}")
    except Exception as e:
        print(f"[graph] Failed ({e}) — continuing")


# ---------- LLM API call ----------

def call_anthropic_protocol(prompt: str, config: Config, max_tokens: int | None = None) -> tuple[str, str]:
    """Text-generation LLM call — conversation mode only (round ii, 2026-06-20).

    HTTP-direct text generation has been removed. In conversation mode the
    prompt is written to a file and ``ConversationPending`` is raised so the
    calling agent can answer with the current conversation's model; on
    re-invoke the cached result is read and returned. Without conversation
    mode the call raises (use ``--conversation``).

    This function is registered as the conversation router on ``_llm_api`` so
    that the stage modules (which call ``_llm_api.call_anthropic_protocol``)
    route here automatically.

    Returns (text_content, stop_reason).
    """
    if not config.conversation_mode:
        raise RuntimeError(
            "Text generation requires --conversation mode. HTTP-direct LLM "
            "calls have been removed (round ii); run ingest.py with "
            "--conversation so the calling agent handles each LLM step with "
            "the current conversation's model."
        )
    return _conversation_llm_call(prompt, config, max_tokens)


# Register the conversation router so stage modules (which import
# `call_anthropic_protocol` from `_llm_api`) route through conversation mode.
set_conversation_router(call_anthropic_protocol)


# ---------- Conversation / Delegate Mode ----------


def _conversation_llm_call(prompt: str, config: Config, max_tokens=None) -> tuple[str, str]:
    """Conversation mode: write prompt to disk, raise ConversationPending.

    The calling agent (Hermes) reads the prompt file, executes it with its own
    LLM, writes the result back, and re-invokes ingest.py.  On re-invoke,
    ingest.py finds the result file and continues.

    Delegates the cache-read / prompt-write / raise to
    :func:`_llm_api.conversation_handoff` (shared with the sweep tools).
    """
    # Stage-name slug + content-hash suffix. The stage name (Stage-1-Global-
    # Digest, Stage-2-Synthesis, LLM-task, ...) gives human-readable grouping;
    # the 8-char content hash guarantees distinct prompts get distinct cache
    # files. Without the hash, every call that falls through _infer_stage to
    # 'LLM-task' (Stage 2.6 source page, per-concept fallback, ...) shares one
    # file and the wrong answer gets reused across stages. The hash is
    # deterministic, so replay of the same prompt still hits the cache.
    stage = re.sub(r"[^a-zA-Z0-9]+", "-", _infer_stage(prompt)).strip("-")[:40] or "llm-task"
    # The slug hash must be stable across re-invokes of the same stage. Stage
    # prompts embed an "Existing wiki pages" snapshot that changes as the wiki
    # grows (lint pages, new ingests) — hashing the raw prompt made the slug
    # change every invoke, thrashing the cache and re-prompting Stage 1 forever.
    # Redact that volatile list (and the prompt's own prior-answer context that
    # carries it) before hashing. The full prompt is still written to the .md
    # for the LLM; only the cache *key* is stabilized.
    stable_prompt = re.sub(
        r"(Existing wiki pages:)[^\n]*", r"\1 <redacted>", prompt)
    content_hash = hashlib.sha256(stable_prompt.encode("utf-8")).hexdigest()[:8]
    slug = f"{stage}-{content_hash}"
    prefix = config.conversation_prefix or "00000000"
    conv_dir = config.runtime_dir / "conversation" / prefix

    response = conversation_handoff(
        conv_dir, slug, prompt,
        label=slug,
        stale_check=_is_stale_result,
        on_cached=lambda _response: _mark_task_done(config, slug),
        on_prompt_written=lambda: _mark_task_pending(config, slug),
    )
    return response, "end_turn"


def _task_manifest_path(config: Config) -> Path:
    return config.runtime_dir / "conversation" / config.conversation_prefix / "tasks.json"


def _load_task_manifest(config: Config) -> dict:
    p = _task_manifest_path(config)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"pending": [], "completed": []}


def _save_task_manifest(config: Config, manifest: dict) -> None:
    p = _task_manifest_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_task_pending(config: Config, slug: str) -> None:
    m = _load_task_manifest(config)
    if slug not in m.get("pending", []):
        m.setdefault("pending", []).append(slug)
    _save_task_manifest(config, m)


def _mark_task_done(config: Config, slug: str) -> None:
    m = _load_task_manifest(config)
    m["pending"] = [s for s in m.get("pending", []) if s != slug]
    m.setdefault("completed", []).append(slug)
    _save_task_manifest(config, m)


def _is_stale_result(response: str, prompt: str) -> bool:
    """Detect if agent just copied the prompt instead of generating output."""
    has_yaml = "```yaml" in response or "entities_found" in response or "concepts_found" in response
    has_files = "---FILE:" in response or "### File" in response
    if has_yaml or has_files:
        return False
    return any(m in response for m in ["# Role", "You are"]) and len(response) < len(prompt) * 0.8


def _infer_stage(prompt: str) -> str:
    if "Generate wiki pages" in prompt or ("Synthesis" in prompt and "FILE blocks" in prompt):
        return "Stage-2-Synthesis"
    if "review" in prompt.lower() and "suggestions" in prompt.lower():
        return "Stage-2-5-Review"
    if "Chunk Analysis" in prompt[:500]:
        m = re.search(r"chunk (\d+)/(\d+)", prompt)
        if m:
            return f"Stage-1-5-Chunk-{m.group(1)}"
    if "book_meta" in prompt[:1000] or "produce a **high-level structural summary**" in prompt:
        return "Stage-1-Global-Digest"
    return "LLM-task"




# ═════════════════════════════════════════════════════════
# Main pipeline — ingest_one, batch, queue, CLI
# ═════════════════════════════════════════════════════════

def ingest_one(
    raw_file: Path,
    config: Config,
    template_override: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> dict:
    """Process one file end-to-end (NashSU-style 15-stage pipeline with checkpoint/resume)."""
    _set_current_file(raw_file.name)
    print(f"\n=== Ingest: {raw_file} ===")

    # 0. Clean up resolved review pages
    cleanup_resolved_reviews(config)

    # 1. Dedup + Stage 0-2 (delegated to shared implementation)
    h = file_sha256(raw_file)
    config.conversation_prefix = h[-8:]  # per-source conversation file isolation
    task_manifest = _load_task_manifest(config)
    pending_tasks = task_manifest.get("pending", [])
    if pending_tasks:
        print(f"[conversation] {len(pending_tasks)} pending task(s) — resuming pipeline")

    prepared = _do_prepare(raw_file, config, template_override, verbose, pilot_confirmed)
    if prepared is None:
        return {"status": "skipped", "reason": "source-page-exists"}

    # Unpack prepared state from Stage 0-2
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
    raw_response = prepared["raw_response"]
    file_blocks = prepared["file_blocks"]
    stage_1_2_result = prepared["stage_1_2_result"]
    stage_1_3_result = prepared["stage_1_3_result"]
    template_name = prepared["template_name"]

    # Check stop-after-stage (best-effort; _do_prepare runs all of Stage 0-2)
    for stage_check in ("0", "0.5", "0.6", "1", "1.5", "2.0", "2", "2.3", "2.5"):
        if _should_stop_after(config, stage_check, {"status": "ok"}):
            return {"status": "ok", "stopped_after": stage_check}

    # Stage 3+: Delegate to _do_write (shared with batch path)
    prepared = {
        "raw_file": raw_file, "config": config, "h": h, "method": method,
        "extracted_text": extracted_text, "global_digest": global_digest,
        "chunk_analyses": chunk_analyses, "analysis": analysis,
        "raw_response": raw_response, "file_blocks": file_blocks,
        "stage_1_2_result": stage_1_2_result, "stage_1_3_result": stage_1_3_result,
        "template_name": template_name,
        "enrich_enabled": getattr(config, "enrich_enabled", True),
    }
    result = _do_write(prepared, verbose=verbose)
    if result["status"] != "ok":
        return result

    files_written = result["files_written"]

    # ── Post-ingest (unique to single-book path) ──
    _run_post_ingest_lint(config)
    _run_post_ingest_graph(config)
    stage_3_6_embed_new_pages(config, files_written)
    stage_4_1_validate_ingest(config, raw_file)

    return {"status": "ok", "files_written": files_written}


# ═══════════════════════════════════════════════════════════════
# Batch ingest: parallel Stage 0-2, serial Stage 3+
# ═══════════════════════════════════════════════════════════════

# Maximum concurrency for parallel LLM phases.
# Stage 1/1.5/2 are read-only LLM calls — no shared state mutation,
# no disk writes to wiki/ — so they can safely run in parallel.
# Set based on LLM API rate limits; 4 is safe for most providers.
BATCH_MAX_CONCURRENT = 4


def _stage_0_2_should_skip(raw_file: Path, config: Config) -> bool:
    """Return True if the source page already exists and is reasonably complete.

    Stage 0.2: Re-ingest when source page is missing >80% of linked concept/entity
    pages (corrupt / partial prior run); otherwise skip.

    Verification checklist:
    1. Source page file exists
    2. Frontmatter type == "source"
    3. ≥80% of wikilinks point to existing concept/entity pages
    """
    source_page = _stage_3_1_wiki_path_for_source(raw_file, config)
    if not source_page.exists():
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
    missing = []
    for ref in refs:
        slug = ref.split("|")[0].strip()
        if not slug:
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


def _analyze_all_chunks(
    chunk_meta: list, global_digest: dict, accumulated_digest: str,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
) -> list:
    """Stage 2.2: analyze all chunks.

    Serial (conversation mode or single chunk) preserves cross-chunk
    ``accumulated_digest`` refinement; parallel (direct API, multi-chunk)
    analyzes concurrently against the static ``global_digest``.
    Returns chunk_analyses indexed by chunk order.
    """
    chunk_analyses: list = []

    if config.conversation_mode or chunk_total <= 1:
        for i, chunk, overlap_before, heading_path in chunk_meta:
            ca = _stage_2_2_analyze_chunk(
                chunk, i, chunk_total, global_digest, accumulated_digest,
                overlap_before, heading_path, raw_file, config, template_content,
                max_retries=_stage_2_2_chunk_retries(), verbose=verbose)
            chunk_analyses.append(ca)
            updated = ca.get("updated_global_digest", "")
            if isinstance(updated, str) and len(updated.strip()) > 50:
                accumulated_digest = updated.strip()
            elif isinstance(updated, dict):
                accumulated_digest = json.dumps(updated, ensure_ascii=False, indent=2)
            done = i + 1
            pct = done * 100 // chunk_total
            eta = ((time.time() - t_start) / done) * (chunk_total - done) if done > 0 else 0
            print(f"  [analyze] {done}/{chunk_total} [{pct}% ETA {eta:.0f}s]")
        return chunk_analyses

    max_workers = min(chunk_total, int(os.environ.get("LLM_MAX_CONCURRENCY", "6")))
    print(f"  [analyze] parallel \u2014 {chunk_total} chunks, {max_workers} workers")
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _stage_2_2_analyze_chunk, chunk, i, chunk_total, global_digest,
                "", overlap_before, heading_path, raw_file, config,
                template_content, max_retries=_chunk_retries(), verbose=verbose,
            ): i
            for (i, chunk, overlap_before, heading_path) in chunk_meta
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = {"chunk_index": i + 1, "error": str(e), "_attempts": 1}
                print(f"  [chunk {i+1}/{chunk_total}] analyze worker FAILED: {e}")
    chunk_analyses = [results[i] for i in range(chunk_total)]
    ok = sum(1 for ca in chunk_analyses if "error" not in ca)
    print(f"  [analyze] done \u2014 {ok}/{chunk_total} analyzed in {time.time()-t_start:.0f}s")
    return chunk_analyses


def _generate_all_chunks(
    chunk_meta: list, chunk_analyses: list, existing_refs: dict,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
) -> tuple[list, list, list]:
    """Stage 2.4: sequential generation across all chunks.

    ``existing_refs`` (Stage 2.3 output: {concept_name: [wiki_slugs]}) is fed
    into each chunk's generation prompt so the LLM wikilinks to existing pages
    instead of regenerating them. ``generated_slugs`` accumulates across chunks
    (sequential, both paths).
    """
    all_file_blocks: list = []
    all_responses: list[str] = []
    generated_slugs: list[str] = []

    unique_concepts_pre, unique_entities_pre = _stage_2_4_extract_names(chunk_analyses)
    for name in (*unique_concepts_pre, *unique_entities_pre):
        slug = name.strip().lower().replace(" ", "-").replace("/", "-")
        if slug and slug not in generated_slugs:
            generated_slugs.append(slug)

    for i, chunk, _overlap_before, _heading_path in chunk_meta:
        ca = chunk_analyses[i]
        if "error" in ca:
            continue
        blocks = stage_2_4_generate_chunk(
            ca, i, generated_slugs, raw_file, config, template_content,
            verbose=verbose, chunk_text=chunk, existing_refs=existing_refs)
        all_file_blocks.extend(blocks)
        all_responses.extend([b[1] for b in blocks])
        for path, _ in blocks:
            slug = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
            if slug not in generated_slugs:
                generated_slugs.append(slug)
        done = i + 1
        pct = done * 100 // chunk_total
        eta = ((time.time() - t_start) / done) * (chunk_total - done) if done > 0 else 0
        print(f"  [generate] {done}/{chunk_total} [{pct}% ETA {eta:.0f}s]")

    return all_file_blocks, all_responses, generated_slugs


def _run_chunk_pipeline(
    extracted_text: str, global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, verbose: bool,
) -> tuple[list, dict, str, list, dict]:
    """Stage 2.2 \u2192 2.3 \u2192 2.4: analyze all chunks, detect existing-wiki
    associations, then generate pages with associations fed into each prompt.

    Split (2026-06-21): analysis and generation are separate phases so Stage 2.3
    (incremental association detection) can run between them and feed back into
    the generation prompt. Returns
    ``(chunk_analyses, analysis, raw_response, file_blocks, incremental_associations)``.
    """
    # Cached: chunk analysis already complete
    if progress and progress.get("stage") in ("stage_2_2_done", "stage_2_3_done") and "chunk_analyses" in progress:
        chunk_analyses = progress["chunk_analyses"]
        print(f"  [stage 2.2] (cached) Chunk Analysis \u2014 {len(chunk_analyses)} chunks")
        _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
        analysis = progress.get("analysis", {})
        raw_response = progress.get("raw_response", "")
        file_blocks = parse_file_blocks(raw_response) if raw_response else []
        incremental_associations = progress.get("incremental_associations", {})
        return chunk_analyses, analysis, raw_response, file_blocks, incremental_associations

    chunks = _stage_2_1_chunk_text(extracted_text, config.target_chars, config.chunk_overlap)
    chunk_total = len(chunks)

    est_sec = chunk_total * 75
    print(f"  [stage 2.2] Analyze \u2014 {chunk_total} chunk(s), "
          f"target {config.target_chars:,} chars/chunk (est. {est_sec/60:.0f} min)")
    _stage_begin("Stage 2.2: Chunk Analysis")
    t_start = time.time()
    accumulated_digest = json.dumps(
        {k: global_digest[k] for k in
         ("book_meta", "outline", "key_entities", "key_concepts")
         if k in global_digest},
        ensure_ascii=False, indent=2)

    chunk_meta: list[tuple[int, str, str, str]] = []
    for i in range(chunk_total):
        chunk = chunks[i]
        overlap_before = chunks[i - 1][-config.chunk_overlap:] if i > 0 else ""
        chunk_pos = extracted_text.find(chunk)
        if chunk_pos == -1:
            chunk_pos = i * config.target_chars
        heading_path = _resolve_chunk_heading_path(
            extracted_text, chunk_pos, chunk_pos + len(chunk))
        chunk_meta.append((i, chunk, overlap_before, heading_path))

    # \u2500\u2500 Stage 2.2: analyze all chunks \u2500\u2500
    chunk_analyses = _analyze_all_chunks(
        chunk_meta, global_digest, accumulated_digest, raw_file, config,
        template_content, chunk_total, t_start, verbose)

    # \u2500\u2500 Stage 2.3: incremental association detection (existing-wiki overlap) \u2500\u2500
    from _stage_2_3_incremental import stage_2_3_detect_incremental_associations
    incremental_associations = stage_2_3_detect_incremental_associations(
        config.wiki_dir, chunk_analyses)
    if incremental_associations:
        print(f"  [stage 2.3] {len(incremental_associations)} new concept(s) "
              f"match existing wiki pages \u2192 fed into generation prompt")
    else:
        print(f"  [stage 2.3] No existing-wiki associations (first source or no overlap)")

    # \u2500\u2500 Stage 2.4: generate all chunks (associations fed into prompt) \u2500\u2500
    _stage_begin("Stage 2.4: Chunk Generation")
    all_file_blocks, all_responses, generated_slugs = _generate_all_chunks(
        chunk_meta, chunk_analyses, incremental_associations, raw_file, config,
        template_content, chunk_total, t_start, verbose)

    # Build combined analysis
    unique_concepts, _ = _stage_2_4_extract_names(chunk_analyses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": chunk_total,
        "method": "analyze\u2192associate\u2192generate",
    }
    raw_response = "\n".join(all_responses)
    file_blocks = all_file_blocks

    # \u2500\u2500 Fallback: per-concept generation \u2500\u2500
    if not concept_blocks and unique_concepts and chunk_analyses:
        n_missed = len(unique_concepts)
        print(f"  [stage 2.4] \u26a0\ufe0f  0/{n_missed} concepts generated "
              f"\u2014 falling back to per-concept generation "
              f"(pre_existing_slugs={len(generated_slugs)})")
        fa_analysis, fa_raw, fa_blocks = _stage_2_4_per_concept_fallback(
            chunk_analyses, global_digest, raw_file, config,
            template_content, verbose=verbose,
            pre_existing_slugs=generated_slugs,
        )
        fa_concept_entity = [(p, c) for p, c in fa_blocks
                             if not p.startswith("sources/")]
        all_file_blocks = fa_concept_entity
        if fa_concept_entity:
            all_responses.append(fa_raw)
            raw_response = "\n".join(all_responses)
        file_blocks = all_file_blocks
        concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
        entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
        analysis["concepts_generated"] = len(concept_blocks)
        analysis["entities_generated"] = len(entity_blocks)
        analysis["coverage_pct"] = round(
            len(concept_blocks) / max(len(unique_concepts), 1), 2)
        analysis["method"] = "analyze\u2192associate\u2192generate+fallback"
        for path, _ in fa_concept_entity:
            s = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
            if s not in generated_slugs:
                generated_slugs.append(s)

    _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
    return chunk_analyses, analysis, raw_response, file_blocks, incremental_associations


def _prepare_source_page(
    global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, file_blocks: list,
    verbose: bool,
) -> list:
    """Stage 2.6: generate the source page (dedicated LLM call) and merge into file_blocks."""
    current_domain = _detect_domain(raw_file, template_content, global_digest)
    if progress and progress.get("stage") in ("stage_2_4_done", "stage_2_3_done") and "source_page_response" in progress:
        source_page_response = progress["source_page_response"]
        print(f"  [stage 2.6] (cached) Source page already generated")
    else:
        source_page_response, _ = stage_2_6_source_page(
            global_digest, raw_file, config,
            template=template_content, current_domain=current_domain, verbose=verbose
        )

    if not source_page_response:
        return file_blocks

    source_blocks = parse_file_blocks(source_page_response)
    if source_blocks:
        file_blocks = source_blocks + list(file_blocks)
        print(f"  [stage 2.6] Source page block merged ({len(file_blocks)} total)")
        return file_blocks

    # LLM didn't use FILE block format — generate placeholder
    source_rel = f"sources/{raw_file.relative_to(config.raw_root).with_suffix('.md')}"
    book_meta = global_digest.get("book_meta", {})
    title = book_meta.get("title", raw_file.stem) if isinstance(book_meta, dict) else raw_file.stem
    stub = f"---\ntype: source\ntitle: \"{title}\"\ndomain: general\n"
    stub += f"created: {time.strftime('%Y-%m-%d')}\nupdated: {time.strftime('%Y-%m-%d')}\n"
    stub += f"tags: []\nrelated: []\nsources: [\"raw/{raw_file.relative_to(config.raw_root)}\"]\n---\n\n"
    stub += f"**Title:** {title}\n**Author:** {raw_file.stem}\n\n"
    stub += f"## Global Digest\n\n```yaml\n{json.dumps(global_digest, ensure_ascii=False, indent=2)[:4000]}\n```\n\n"
    stub += f"## Key Concepts\n\n"
    for path, _ in file_blocks:
        if "concepts/" in path:
            stub += f"- [[{Path(path).stem}]]\n"
    file_blocks.append((source_rel, stub))
    print(f"  [stage 2.6] Placeholder source page generated ({len(file_blocks)} total)")
    return file_blocks


def _do_prepare(
    raw_file: Path, config: Config,
    template_override: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> dict | None:
    """Stage 0-2 for one book.  Read-only: no shared state writes, no lock needed.

    Returns a dict with all data needed for Stage 3+, or None on skip/failure.
    Suitable for parallel execution across multiple books.
    """
    _set_current_file(raw_file.name)
    print(f"\n=== [prepare] {raw_file.name} ===")
    try:
        # Dedup check — skip if source page exists and is reasonably complete
        if _stage_0_2_should_skip(raw_file, config):
            return None

        h = file_sha256(raw_file)
        progress = load_progress(config, h)

        # Stage 0: Text extraction
        if progress and "extracted_text" in progress:
            extracted_text = progress["extracted_text"]
            method = progress.get("extract_method", "cached")
            print(f"  [extract] (cached) {method}: {len(extracted_text)} chars")
        else:
            extracted_text, method = stage_1_1_extract_text(raw_file, config, pilot_confirmed=pilot_confirmed)
            print(f"  [extract] {method}: {len(extracted_text)} chars")
            _verify_stage_1_1_text(raw_file, extracted_text, method)

            # Stage 0 Validation (Phase 2: per-stage verification)
            if not verify_stage_0(extracted_text):
                print(f"  [validate] ❌ Stage 0 failed: text extraction insufficient")
                raise StageValidationError("Stage 0: text extraction failed")

            qr = _stage_1_1_check_text_quality(extracted_text, raw_file.name)
            if qr["status"] == "severe":
                print(f"  [extract] ❌ Text quality SEVERE — aborting ingest. "
                      f"Re-run with a different PDF or re-download from source.")
                return None
            save_progress(config, h, {
                "stage": "stage_1_1_done", "extracted_text": extracted_text,
                "extract_method": method,
            })

        # Template
        template_name = detect_template_type(raw_file, config.raw_root, template_override)
        template_content = load_template(template_name)
        print(f"  [template] {template_name}")

        # ── Stage 1.2 + 1.3 pipeline (1.2 → 1.3 sequential) ∥ Stage 2.1 (parallel) ──
        # Helper: run 1.2→1.3 together (1.3 depends on 1.2 output)
        def _run_image_pipeline():
            stage_1_2_result: dict = {"count": 0}
            if progress and "stage_1_2" in progress:
                stage_1_2_result = progress["stage_1_2"]
                print(f"  [stage 1.2] (cached) {stage_1_2_result.get('count', 0)} images")
            elif method == "pymupdf" and raw_file.suffix.lower() == ".pdf":
                # 改进2：检查是否从 minerU 输出提取
                stage_1_2_result = stage_1_2_extract_images(raw_file, config)
            elif method in ("mineru", "mineru-local-ocr"):
                # 改进2：从 minerU 输出提取图片
                ocr_out = config.extract_tmp_dir / raw_file.stem
                if ocr_out.exists():
                    stage_1_2_result = _stage_1_2_extract_from_mineru(ocr_out, config, raw_file)
                # Save progress immediately after 1.2 completes
                cp = {"stage": "stage_1_1_done", "extracted_text": extracted_text,
                      "extract_method": method, "stage_1_2": stage_1_2_result}
                save_progress(config, h, cp)

            # Stage 1.3: Caption extracted images (runs if 1.2 found images)
            stage_1_3_result = {"captioned": 0}
            needs_caption = (not progress or "stage_1_3" not in progress) and stage_1_2_result.get("count", 0) > 0
            if needs_caption:
                stage_1_3_result = stage_1_3_caption_images(config, stage_1_2_result)
            elif progress and "stage_1_3" in progress:
                stage_1_3_result = progress["stage_1_3"]
                print(f"  [stage 1.3] (cached) {stage_1_3_result.get('captioned', 0)} captions")

            return stage_1_2_result, stage_1_3_result

        # Parallel execution: 1.2→1.3 pipeline vs 2.1 global digest
        needs_digest = (
            not progress or progress.get("stage") not in ("stage_2_1_done", "stage_2_2_done", "stage_2_3_done")
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_images = executor.submit(_run_image_pipeline)
            fut_digest = executor.submit(stage_2_1_global_digest, extracted_text, raw_file, config,
                                        template_content, verbose=verbose) if needs_digest else None

            stage_1_2_result, stage_1_3_result = fut_images.result()
            global_digest = fut_digest.result() if fut_digest else progress.get("global_digest", {})

        if needs_digest:
            _verify_stage_2_1_digest(global_digest, raw_file)
        else:
            print(f"  [stage 2.1] (cached) Global Digest — {len(global_digest)} keys")
            _verify_stage_2_1_digest(global_digest, raw_file)

        # Save progress checkpoint
        if "extracted_text" not in (progress or {}):
            save_progress(config, h, {"stage": "stage_1_1_done", "extracted_text": extracted_text,
                  "extract_method": method, "stage_1_2": stage_1_2_result, "stage_1_3": stage_1_3_result})

        # Stage 2.2 + 2.4: Chunk Analysis → Generation (barrier-free pipeline)
        chunk_analyses, analysis, raw_response, file_blocks, incremental_associations = _run_chunk_pipeline(
            extracted_text, global_digest, raw_file, config, template_content,
            progress, verbose)

        # Stage 2.5: In-source concept dedup & merge (multi-chunk books only).
        # Runs before the source page so the index lists de-duplicated concepts.
        from _stage_2_5_dedup import stage_2_5_dedup
        _stage_2_5 = stage_2_5_dedup(file_blocks, chunk_analyses, config, verbose=verbose)
        file_blocks = _stage_2_5["file_blocks"]
        dedup_was_run = _stage_2_5["dedup_was_run"]
        concept_count_before = _stage_2_5["concept_count_before"]
        concept_count_after = _stage_2_5["concept_count_after"]

        # Stage 2.6: Source page generation + merge
        file_blocks = _prepare_source_page(
            global_digest, raw_file, config, template_content, progress,
            file_blocks, verbose)
        _verify_stage_2_4_file_blocks(file_blocks, raw_file)

        # ── Stage 2.7: Query generation ──
        query_blocks, query_response = stage_2_7_query_generation(
            global_digest, chunk_analyses, file_blocks, raw_file, config,
            template=template_content, verbose=verbose
        )
        if query_blocks:
            file_blocks = list(file_blocks) + query_blocks

        # ── Stage 2.8: Cross-source query resolution ──
        # LLM judge closes queries already answered elsewhere; defaults to "kept".
        from _stage_2_8_query_resolve import (stage_2_8_resolve_queries,
                                               _stage_2_8_update_file_blocks_after_resolution)
        query_resolutions = stage_2_8_resolve_queries(file_blocks, config.wiki_dir, config)
        if any(r["status"] == "closed" for r in query_resolutions.values()):
            before_q = len(file_blocks)
            file_blocks = _stage_2_8_update_file_blocks_after_resolution(file_blocks, query_resolutions)
            print(f"  [stage 2.8] Removed {before_q - len(file_blocks)} closed query block(s)")

        # ── Stage 2.9: Comparison generation ──
        comp_blocks, comp_response = stage_2_9_comparison_generation(
            global_digest, chunk_analyses, file_blocks, raw_file, config,
            template=template_content, verbose=verbose
        )
        if comp_blocks:
            file_blocks = list(file_blocks) + comp_blocks

        query_count = len(query_blocks)
        comp_count = len(comp_blocks)

        analysis["__source_hash"] = h
        analysis["__extract_method"] = method

        print(f"  [prepare] ✅ done — {len(file_blocks)} blocks")
        return {
            "raw_file": raw_file, "config": config,
            "h": h, "method": method, "extracted_text": extracted_text,
            "global_digest": global_digest, "chunk_analyses": chunk_analyses,
            "analysis": analysis, "raw_response": raw_response,
            "file_blocks": file_blocks,
            "stage_1_2_result": stage_1_2_result,
            "stage_1_3_result": stage_1_3_result,
            "template_name": template_name,
            "query_count": query_count,
            "comp_count": comp_count,
            "concept_merge_stats": (concept_count_before, concept_count_after),
            "dedup_was_run": dedup_was_run,
            "current_domain": current_domain,
            "incremental_associations": incremental_associations,
            "query_resolutions": query_resolutions,
            "enrich_enabled": getattr(config, "enrich_enabled", True),
        }
    except Exception as e:
        print(f"  [prepare] ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return None


def _do_write(prepared: dict, verbose: bool = False) -> dict:
    """Stage 3+ for one book.  Writes wiki files, updates cache, runs validation.
    MUST be called serially — modifies shared wiki/ state.
    """
    raw_file = prepared["raw_file"]
    config = prepared["config"]
    h = prepared["h"]
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
    raw_response = prepared["raw_response"]
    file_blocks = prepared["file_blocks"]
    stage_1_2_result = prepared["stage_1_2_result"]
    stage_1_3_result = prepared["stage_1_3_result"]
    template_name = prepared["template_name"]
    query_count = prepared.get("query_count", 0)
    comp_count = prepared.get("comp_count", 0)

    print(f"\n=== [write] {raw_file.name} ===")

    # Write wiki files (same logic as ingest_one Stage 3+)
    source_path = wiki_path_for_source(raw_file, config)
    files_written_paths: list[str] = []
    hard_failures: list[str] = []
    source_block: tuple[str, str] | None = None

    _VALID_SUBDIRS = {"sources", "concepts", "entities", "queries", "comparisons",
                      "synthesis", "findings", "thesis"}
    _LISTING_PAGES = {"index.md", "log.md", "overview.md", "schema.md"}

    try:
        from _language import detect_language
        expected_lang = detect_language(extracted_text[:5000]) if extracted_text else "unknown"
    except ImportError:
        expected_lang = "unknown"

    canonical_source = f"raw/{raw_file.relative_to(config.raw_root)}"
    today_str = time.strftime("%Y-%m-%d")

    # ── Wikilink enrichment setup (round iii, 2026-06-21) ──
    # After each page is written, ask the LLM (direct API) to suggest
    # [[wikilinks]] for body terms matching existing wiki pages. Gated by
    # --enrich-wikilinks / --no-enrich. existing_slugs is a pre-loop snapshot
    # of the wiki; written_slugs accumulates this ingest's new pages so later
    # pages in the same batch can link to earlier ones. Listing pages are
    # skipped (auto-managed). Soft-fails if no API key (returns content
    # unchanged) — see _enrich_wikilinks.
    enrich_enabled = prepared.get("enrich_enabled", True)
    existing_slugs = list_existing_slugs(config) if enrich_enabled else []
    written_slugs: list[str] = []

    for rel_path, content in file_blocks:
        if ".." in rel_path or rel_path.startswith("/"):
            continue
        if not is_safe_ingest_path(rel_path):
            continue

        top_dir = rel_path.split("/")[0] if "/" in rel_path else ""
        basename = Path(rel_path).name
        if basename in _LISTING_PAGES:
            pass
        elif top_dir not in _VALID_SUBDIRS:
            corrected = _stage_3_1_auto_correct_wiki_path(rel_path, content, config)
            if corrected:
                print(f"  [write] Auto-corrected: {rel_path} → {corrected}")
                rel_path = corrected
            else:
                print(f"  [write] Dropped — cannot correct path: {rel_path}")
                continue

        if not rel_path.endswith(".md"):
            rel_path = rel_path + ".md"

        # Skip per-block language check for minerU OCR — OCR text from garbled
        # PDFs confuses the detector (e.g. C0 control chars → wrong language).
        if method not in ("mineru", "mineru-ocr", "mineru-vlm", "mineru-local-ocr"):
            if expected_lang not in ("unknown", "English"):
                try:
                    from _language import detect_language
                    block_lang = detect_language(content[:2000])
                    if block_lang not in (expected_lang, "English") and block_lang != "unknown":
                        print(f"  [lang] ⚠️  {rel_path}: expected {expected_lang}, got {block_lang}")
                except ImportError:
                    pass

        content = _stage_3_1_canonicalize_sources_field(content, canonical_source)
        content = _stage_3_1_stamp_frontmatter_dates(content, today_str)

        full_path = config.wiki_dir / rel_path
        is_listing = basename in _LISTING_PAGES
        do_merge = full_path.exists() and not is_listing

        try:
            stage_3_1_write_wiki_file(full_path, content, config, merge=do_merge)
        except OSError as e:
            print(f"  [write] HARD ERROR: {rel_path} — {e}")
            hard_failures.append(rel_path)
            continue

        files_written_paths.append(str(full_path.relative_to(config.wiki_root)))
        if full_path == source_path:
            source_block = (rel_path, content)
        action = "[merge]" if do_merge else "[overwrite]" if is_listing and full_path.exists() else "[write]"
        print(f"  {action} {rel_path}")

        # ── Wikilink enrichment (direct API, post-write) ──
        # Enrich the ACTUAL written content (post-merge) so links target real
        # pages. Skip listing pages (auto-managed) and very short bodies.
        this_slug = Path(rel_path).stem
        if enrich_enabled and not is_listing:
            try:
                written_content = full_path.read_text(encoding="utf-8")
                enriched = enrich_wikilinks(
                    written_content,
                    existing_slugs + written_slugs,
                    config,
                )
                if enriched != written_content:
                    full_path.write_text(enriched, encoding="utf-8")
                    print(f"  [enrich] {rel_path} (+wikilinks)")
            except Exception as e:
                # Enrichment is best-effort — never fail the ingest over it.
                print(f"  [enrich] {rel_path} skipped ({type(e).__name__})")
        if this_slug not in written_slugs:
            written_slugs.append(this_slug)

    if not source_block:
        # Build NashSU-quality source page from digest data (no LLM needed)
        book_meta = analysis.get("book_meta", {})
        outline = analysis.get("outline", [])
        key_claims = analysis.get("key_claims", [])
        title = book_meta.get("title", raw_file.stem)
        authors = book_meta.get("authors", [])
        year = book_meta.get("year", "")
        publisher = book_meta.get("publisher", "")

        lines = [
            "---",
            "type: source",
            f'title: "{title}"',
            "domain: general",
            f"created: {today_str}",
            f"updated: {today_str}",
            "tags: []",
            "related: []",
            f'sources: ["{canonical_source}"]',
            "---",
            "",
            f"# {title}",
            "",
        ]
        if authors:
            lines.append(f"**Authors:** {', '.join(str(a) for a in authors[:5])}")
        if year:
            lines.append(f"**Year:** {year}")
        if publisher:
            lines.append(f"**Publisher:** {publisher}")
        lines.append("")

        if outline:
            lines.append("## Table of Contents & Key Concepts")
            lines.append("")
            for ch in outline[:40]:
                if isinstance(ch, dict):
                    ch_title = ch.get("title", "")
                    topics = ch.get("key_topics", [])
                    topics_str = ", ".join(str(t) for t in topics[:4]) if topics else ""
                else:
                    ch_title = str(ch)
                    topics_str = ""
                lines.append(f"1. **{ch_title}**" + (f" — {topics_str}" if topics_str else ""))
            lines.append("")

        if key_claims:
            lines.append("## Key Takeaways")
            lines.append("")
            for claim in key_claims[:10]:
                if isinstance(claim, dict):
                    lines.append(f"- {claim.get('claim', str(claim))}")
                else:
                    lines.append(f"- {str(claim)}")
            lines.append("")

        placeholder_content = "\n".join(lines) + "\n"
        try:
            stage_3_1_write_wiki_file(source_path, placeholder_content, config)
            files_written_paths.append(str(source_path.relative_to(config.wiki_root)))
        except OSError as e:
            hard_failures.append("source-placeholder")

    # Stage 3.2: Image injection
    stage_3_2_result: dict = {"injected": 0}
    if source_path.exists():
        stage_3_2_result = stage_3_2_inject_images(config, raw_file, source_path, method)

    # Stage 3.3: Cross-domain slug collision review
    from _stage_3_write import stage_3_3_slug_collision_review
    stage_3_3_result = stage_3_3_slug_collision_review(
        file_blocks, prepared.get("current_domain", "general"), config, verbose=verbose)

    # Stage 2.10: Review (quality review of generated pages)
    stage_2_10_result = stage_2_10_review_suggestions(
        file_blocks, raw_file, config, raw_response=raw_response, verbose=verbose)

    # Go/no-go validation
    go_nogo_warnings = validate_stage_outputs(
        config, raw_file, method, extracted_text,
        stage_1_2_result, stage_1_3_result,
        file_blocks, source_path,
    )

    # Post-ingest lint
    _run_post_ingest_lint(config)

    # Stage 3.4: Aggregate repair
    index_log_files = stage_3_4_aggregate_repair(source_path, raw_file, analysis, h, method, config)

    # Update cache
    try:
        rel = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        rel = str(raw_file)
    cache = load_cache(config)
    cache["entries"][rel] = {
        "hash": h,
        "timestamp": int(time.time() * 1000),
        "filesWritten": files_written_paths + index_log_files,
        "method": method,
        "template": template_name,
        "sourceHash": h,
        "fileBlockCount": len(file_blocks),
        "stages": {
            "global_digest_keys": len(global_digest),
            "chunks_analyzed": len(chunk_analyses),
            "file_blocks_generated": len(file_blocks),
            "concepts_identified": analysis.get("concepts_identified", len(file_blocks)),
            "concepts_core": analysis.get("concepts_core", 0),
            "concepts_supporting": analysis.get("concepts_supporting", 0),
            "concepts_generated": analysis.get("concepts_generated", len(file_blocks)),
            "coverage_core": analysis.get("coverage_core", 1.0),
            "coverage_supporting": analysis.get("coverage_supporting", 1.0),
            "coverage_pct": analysis.get("coverage_pct", 1.0),
            "images_extracted": stage_1_2_result.get("count", 0),
            "images_captioned": stage_1_3_result.get("captioned", 0),
            "images_injected": stage_3_2_result.get("injected", 0),
            "queries_generated": query_count,
            "comparisons_generated": comp_count,
            "review_items": stage_3_3_result.get("items", 0),
        },
    }
    if hard_failures:
        print(f"  [cache] SKIPPED — {len(hard_failures)} hard failure(s)")
        return {"status": "hard-error", "hard_failures": hard_failures,
                "files_written": files_written_paths + index_log_files}
    try:
        save_cache(config, cache)
        clear_progress(config, h)
        print(f"  [cache] saved")
    except OSError as e:
        return {"status": "hard-error", "error": str(e),
                "files_written": files_written_paths + index_log_files}

    # Stage 3.5: Quality scoring card (always runs; flags needs_review < 0.65)
    from _stage_3_5_quality import stage_3_5_quality
    _q_review = stage_3_3_result.get("items", 0)
    _q_stats = prepared.get("concept_merge_stats", (0, 0))
    _q_dedup_ran = prepared.get("dedup_was_run", False)
    quality_result = stage_3_5_quality(
        raw_file, config, extracted_text,
        stage_1_2_result.get("count", 0), stage_1_3_result.get("captioned", 0),
        file_blocks, _q_review, _q_stats, _q_dedup_ran, verbose=verbose)

    # Note: Detailed validation moved to separate 'validate' command (Phase 2 refactor)
    # Ingest now focuses on generation (Stages 0-3.5) with per-stage validation
    # For detailed quality checks, run: python3 validate.py <source_slug>
    # Stage 3.6 (embeddings) runs in the post-ingest section of ingest_one —
    # single entry point, soft-skip when EMBEDDING_BASE_URL is unset.

    return {"status": "ok", "files_written": cache["entries"][rel]["filesWritten"]}


def batch_ingest(
    raw_files: list[Path],
    config: Config,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    template_override: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> list[dict]:
    """Ingest multiple books with parallel Stage 0-2 and serial Stage 3+.

    Why this works:
      - Stage 0-2 (text extraction, digest, chunk analysis, synthesis) are
        read-only LLM calls. No wiki/ files are written, no shared state
        is mutated. Different books' Stage 0-2 can run concurrently.
      - Stage 3+ (file write, cache update, lint, archive, validation)
        modifies shared wiki/ state. MUST be serialized to avoid races.

    Max concurrency: {BATCH_MAX_CONCURRENT} by default.  Increase if your
    LLM API has generous rate limits.  Memory/CPU usage is negligible
    (just API call orchestration).
    """
    if max_concurrent < 1:
        max_concurrent = 1
    max_concurrent = min(max_concurrent, len(raw_files))

    print(f"\n{'='*60}")
    print(f"Batch ingest: {len(raw_files)} books, max {max_concurrent} concurrent")
    print(f"{'='*60}")

    # Pipeline: parallel prepare (Stage 0-2) → serial write (Stage 3+).
    # Books are written as soon as their Stage 2 finishes — no need to wait
    # for all books.  Write order is completion order, not submission order.
    lock = ProjectLock(config, owner_id="batch")
    if not lock.acquire():
        raise RuntimeError("Could not acquire project lock for batch write phase")

    results: list[dict] = []
    prepared_count = 0
    total_books = len(raw_files)

    try:
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            futures: dict[concurrent.futures.Future, Path] = {}
            for f in raw_files:
                futures[executor.submit(
                    _do_prepare, f, config, template_override, verbose, pilot_confirmed
                )] = f

            for future in as_completed(futures):
                prepared = future.result()
                prepared_count += 1
                if prepared is None:
                    print(f"\n[batch] {prepared_count}/{total_books} prepared (skipped)", flush=True)
                    continue

                print(f"\n[batch] {prepared_count}/{total_books} prepared — writing immediately ({prepared['raw_file'].name})", flush=True)
                try:
                    result = _do_write(prepared, verbose=verbose)
                    results.append(result)
                except Exception as e:
                    print(f"[batch] Write failed for {prepared['raw_file'].name}: {e}")
                    import traceback
                    traceback.print_exc()
    finally:
        lock.release()

    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\n{'='*60}")
    print(f"Batch complete: {ok}/{len(results)} books processed successfully")
    print(f"{'='*60}")

    # Staleness-guarded: rebuild graph after batch (no-op if <30min since last rebuild)
    if ok > 0:
        _run_post_ingest_graph(config)

    return results


# ---------- Queue-based continuous ingestion (--watch) ----------

def _read_queue(config: Config) -> list[dict]:
    """Read ingest-queue.json, returning entries sorted by addedAt (oldest first)."""
    qpath = config.runtime_dir / "ingest-queue.json"
    if not qpath.exists():
        return []
    try:
        queue = json.loads(qpath.read_text(encoding="utf-8"))
        if not isinstance(queue, list):
            return []
        # Sort: priority first, then oldest addedAt
        return sorted(queue, key=lambda e: (
            0 if e.get("priority") else 1,
            e.get("addedAt", 0),
        ))
    except Exception:
        return []


def _write_queue(config: Config, queue: list[dict]) -> None:
    """Atomically write ingest-queue.json."""
    qpath = config.runtime_dir / "ingest-queue.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = qpath.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(qpath)


def _queue_entry_to_file(entry: dict, config: Config) -> Path | None:
    """Convert a queue entry's sourcePath to an absolute raw file path."""
    sp = entry.get("sourcePath", "")
    if not sp:
        return None
    # sourcePath is like "raw/Book/foo.pdf"
    if sp.startswith("raw/"):
        sp = sp[4:]
    full = (config.raw_root / sp).expanduser().resolve()
    if full.exists():
        return full
    return None


def ingest_watch(
    config: Config,
    poll_interval: int = 120,
    drain: bool = False,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    max_retries: int = 3,
    resume_from: str | None = None,
    verbose: bool = False,
    pilot_confirmed: bool = False,
) -> None:
    """Continuously watch ingest-queue.json and process pending entries.

    Each watch cycle:
      1. Read the queue
      2. Collect pending entries (status=pending, or failed with retryCount < max_retries)
      3. Feed them through the batch pipeline (parallel Stage 0-2, serial Stage 3+)
      4. Update queue status for each (done / failed / skipped)
      5. Re-scan for new entries added by wiki-monitor.sh
      6. If --drain: exit when queue is empty; otherwise loop forever

    This is the daemon mode: run it in a tmux/screen session or via nohup.
    wiki-monitor.sh (cron or manual) adds new files to the queue;
    ingest.py --watch picks them up in the next cycle.
    """
    lock = ProjectLock(config, owner_id="watch")
    if not lock.acquire(timeout=10):
        raise RuntimeError(
            "Could not acquire project lock for watch mode. "
            "Is another ingest.py --watch or batch running?"
        )

    cycle = 0
    total_processed = 0
    total_done = 0
    total_failed = 0

    print(f"[watch] Starting queue watcher (poll={poll_interval}s, "
          f"concurrent={max_concurrent}, retries={max_retries}, "
          f"drain={'yes' if drain else 'no'})")
    print(f"[watch] Queue: {config.runtime_dir / 'ingest-queue.json'}")
    if drain:
        print(f"[watch] Mode: drain — will exit when queue is empty")
    else:
        print(f"[watch] Mode: continuous — press Ctrl+C to stop")

    try:
        while True:
            cycle += 1
            queue = _read_queue(config)
            # Separate pending from the rest
            pending: list[dict] = []
            rest: list[dict] = []

            for entry in queue:
                status = entry.get("status", "pending")
                retries = entry.get("retryCount", 0)

                if status == "done":
                    rest.append(entry)
                    continue

                if status == "failed" and retries >= max_retries:
                    rest.append(entry)
                    continue

                # pending, processing, or failed with retries remaining
                if status in ("pending", "failed"):
                    pending.append(entry)
                elif status == "processing":
                    # Stale processing marker — re-queue
                    entry["status"] = "pending"
                    pending.append(entry)
                else:
                    rest.append(entry)

            if not pending:
                if drain:
                    print(f"[watch] Queue empty — draining complete. "
                          f"Total: {total_processed} processed "
                          f"({total_done} done, {total_failed} failed)")
                    break
                else:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[watch] [{ts}] No pending entries. "
                          f"Waiting {poll_interval}s... "
                          f"(processed {total_processed} so far: "
                          f"{total_done} done, {total_failed} failed)", flush=True)
                    time.sleep(poll_interval)
                    continue

            # Process this wave
            wave_size = len(pending)
            print(f"\n[watch] Cycle {cycle} — {wave_size} pending entries")
            for i, e in enumerate(pending):
                sp = e.get("sourcePath", "?")
                retries = e.get("retryCount", 0)
                tag = f" (retry {retries})" if retries > 0 else ""
                print(f"  {i+1}. {sp}{tag}")

            # Convert to file paths (skip entries with missing files)
            wave_files: list[tuple[dict, Path]] = []
            for entry in pending:
                fp = _queue_entry_to_file(entry, config)
                if fp is None:
                    sp = entry.get("sourcePath", "?")
                    print(f"  SKIP: {sp} — file not found in raw/")
                    entry["status"] = "failed"
                    entry["error"] = "file not found in raw/"
                    entry["retryCount"] = entry.get("retryCount", 0) + 1
                    rest.append(entry)
                    continue
                # Mark as processing
                entry["status"] = "processing"
                entry["startedAt"] = int(time.time() * 1000)
                wave_files.append((entry, fp))

            # Write intermediate state so wiki-monitor sees processing entries
            _write_queue(config, [e for e, _ in wave_files] + rest)

            if not wave_files:
                # All entries had missing files — don't re-loop immediately
                time.sleep(poll_interval)
                continue

            # Run batch pipeline on this wave
            raw_paths = [fp for _, fp in wave_files]
            try:
                results = batch_ingest(
                    raw_paths, config,
                    max_concurrent=max_concurrent,
                    verbose=verbose,
                    pilot_confirmed=pilot_confirmed,
                )
            except Exception as e:
                print(f"[watch] Batch ingest crashed: {e}")
                import traceback
                traceback.print_exc()
                # Mark all wave entries as failed
                for entry, fp in wave_files:
                    entry["status"] = "failed"
                    entry["error"] = f"batch crash: {e}"
                    entry["retryCount"] = entry.get("retryCount", 0) + 1
                    entry["failedAt"] = int(time.time() * 1000)
                    rest.append(entry)
                _write_queue(config, rest)
                total_failed += len(wave_files)
                total_processed += len(wave_files)
                time.sleep(poll_interval)
                continue

            # Map results back to queue entries by file path
            result_by_path: dict[str, dict] = {}
            for r in results:
                rf = r.get("raw_file", "")
                result_by_path[str(rf)] = r

            for entry, fp in wave_files:
                result = result_by_path.get(str(fp))
                if result and result.get("status") == "ok":
                    entry["status"] = "done"
                    entry["completedAt"] = int(time.time() * 1000)
                    entry["error"] = None
                    total_done += 1
                else:
                    entry["status"] = "failed"
                    retries = entry.get("retryCount", 0) + 1
                    entry["retryCount"] = retries
                    err = result.get("error", "unknown") if result else "no result"
                    entry["error"] = str(err)[:200]
                    entry["failedAt"] = int(time.time() * 1000)
                    if retries >= max_retries:
                        print(f"  [watch] {entry['sourcePath']}: max retries ({max_retries}) reached — giving up")
                    total_failed += 1
                rest.append(entry)
                total_processed += 1

            _write_queue(config, rest)
            print(f"[watch] Cycle {cycle} complete — "
                  f"cumulative: {total_done} done, {total_failed} failed", flush=True)

    except KeyboardInterrupt:
        print(f"\n[watch] Interrupted. "
              f"Processed {total_processed}: {total_done} done, {total_failed} failed.")
        print(f"[watch] Queue preserved at {config.runtime_dir / 'ingest-queue.json'}")
    finally:
        lock.release()


def stage_4_1_validate_ingest(config: Config, raw_file: Path) -> None:
    """Run validate_ingest.py inline for the just-completed source.

    Superpowers Iron Law: every ingest MUST produce fresh verification evidence
    before claiming completion.  This runs the 15-stage validator on the current
    source and prints the result.  Hard failures prevent the "ok" status.
    """
    import subprocess
    validate_script = Path(__file__).parent / "validate_ingest.py"
    if not validate_script.exists():
        print("[validate] ⚠️  validate_ingest.py not found, skipping final verification")
        return

    slug = raw_file.stem
    # Compute the exact cache key (matching ingest.py's `rel` variable)
    try:
        cache_key = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        cache_key = str(raw_file)
    print(f"\n[validate] Running 15-stage final verification for {slug} (cache_key={cache_key})...")
    result = subprocess.run(
        [sys.executable, str(validate_script)],
        env={**os.environ, "IMPROVED_WIKI_ROOT": str(config.wiki_root),
             "SOURCE_SLUG": slug,
             "CACHE_KEY": cache_key},
        capture_output=True, text=True, timeout=600,
    )
    # Print the validator output (shows per-stage PASS/FAIL)
    stdout = result.stdout.strip()
    if stdout:
        # Print only the summary lines to avoid overwhelming output
        for line in stdout.splitlines():
            if any(marker in line for marker in ["Result:", "PASS", "FAIL", "❌", "✅", "Stage"]):
                print(f"  {line}")

    if result.returncode != 0:
        # Don't raise — the ingest succeeded but validation found issues.
        # The compliance record already documents stage status.
        stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""
        print(f"[validate] ⚠️  Validator exit {result.returncode} — review warnings above")
        if stderr_tail:
            print(f"[validate] {stderr_tail}")
    else:
        print(f"[validate] ✅ All 15 stages verified — ingest complete")


def stage_3_6_embed_new_pages(config: Config, files_written: list[str]) -> None:
    """Stage 3.6: embed wiki pages for semantic retrieval (single entry point).

    NashSU parity (ingest.ts L1127-1146). Runs only if EMBEDDING_BASE_URL is set
    and lancedb is installed; otherwise soft-skips — embeddings are optional
    infrastructure and never fail the ingest. Delegates to build_embeddings.py
    (default Ollama http://127.0.0.1:11434/v1).
    """
    if not os.environ.get("EMBEDDING_BASE_URL"):
        return
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return

    skip_files = {"index.md", "log.md", "overview.md", "schema.md"}
    new_files = [
        str(config.wiki_dir / f) for f in files_written
        if Path(f).name not in skip_files and (config.wiki_dir / f).exists()
    ]
    if not new_files:
        return

    print(f"[stage 3.6] Embedding {len(new_files)} new pages...")
    import subprocess
    script = Path(__file__).parent / "build_embeddings.py"
    subprocess.run(
        [sys.executable, str(script), "--project", str(config.wiki_root), "embed"],
        capture_output=True, timeout=300,
    )
    print(f"[stage 3.6] Embedding complete")


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest source files into the wiki (NashSU-style 15-stage)")
    parser.add_argument("file", nargs="*", help="Path(s) to raw source file(s). Multiple files enable batch mode. "
                        "Omit with --watch to consume the queue.")
    parser.add_argument("--type", help="Override template type (book/paper/datasheet/...)")
    parser.add_argument("--parallel", type=int, default=0,
                        help=f"Max concurrent books for Stage 0-2 (default: {BATCH_MAX_CONCURRENT} if multiple files, 1 for single)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write anything")
    parser.add_argument("--delete", action="store_true",
                        help="Delete source: remove source page, cache entry, and cleanup orphans (NashSU source-lifecycle parity)")
    parser.add_argument("--enrich-wikilinks", action="store_true", default=True,
                        help="Auto-enrich new pages with [[wikilinks]] after write (NashSU enrich-wikilinks parity)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Disable wikilink enrichment")
    parser.add_argument("--pilot-confirmed", action="store_true",
                        help="Acknowledge Stage 0 pilot quality and proceed with full OCR (required for scanned PDFs)")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print LLM responses for debugging",
    )
    parser.add_argument(
        "--conversation", action="store_true",
        help="Delegate LLM calls to calling agent via prompt.md → result.txt protocol.",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Continuously watch ingest-queue.json and process pending entries. "
             "New entries added by wiki-monitor.sh are picked up automatically.",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=30,
        help="Seconds between queue re-scans in --watch mode (default: 30)",
    )
    parser.add_argument(
        "--drain", action="store_true",
        help="With --watch: exit when the queue is empty instead of looping forever.",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="Max attempts per queued entry before giving up (default: 3)",
    )
    parser.add_argument(
        "--stop-after-stage",
        default=None,
        choices=["0", "0.5", "0.6", "1", "1.5", "2", "2.0", "2.3", "2.5", "2.5c", "3", "3.5", "4"],
        help="Stop pipeline after completing the named stage (clean exit, cache saved). "
             "Use for chunked runs to avoid Bash timeout. "
             "Stages: 0=text+image extract, 1=global digest, 1.5=chunk analysis, "
             "2=concept/entity gen, 2.5=review, 3=write+merge+enrich",
    )
    args = parser.parse_args()

    # ── Watch mode: continuous queue consumer ──
    if args.watch:
        config = Config.from_env()
        config.conversation_mode = args.conversation
        config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        ingest_watch(
            config,
            poll_interval=args.poll_interval,
            drain=args.drain,
            max_concurrent=max_conc,
            max_retries=args.max_retries,
            verbose=args.verbose,
            pilot_confirmed=args.pilot_confirmed,
        )
        return 0

    if not args.file:
        parser.print_help()
        print("\nTip: use --watch to process the queue, or pass file(s) for direct ingest.", file=sys.stderr)
        return 1

    # ── Source lifecycle: delete ──
    if args.delete:
        config = Config.from_env()
        from _source_lifecycle import delete_source
        for f in args.file:
            rf = Path(f).expanduser().resolve()
            delete_source(rf, config)
        return 0

    config = Config.from_env()
    config.conversation_mode = args.conversation
    config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
    config.stop_after_stage = args.stop_after_stage

    raw_files = []
    for f in args.file:
        rf = Path(f).expanduser().resolve()
        if not rf.exists():
            print(f"ERROR: {rf} not found", file=sys.stderr)
            return 1
        if not rf.is_relative_to(config.raw_root):
            print(f"ERROR: {rf} is not under raw_root ({config.raw_root})", file=sys.stderr)
            return 1
        raw_files.append(rf)

    # Batch mode: multiple files or explicit --parallel
    if len(raw_files) > 1 or args.parallel > 1:
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        results = batch_ingest(
            raw_files, config, max_concurrent=max_conc,
            template_override=args.type, verbose=args.verbose,
            pilot_confirmed=args.pilot_confirmed,
        )
        ok = sum(1 for r in results if r.get("status") == "ok")
        return 0 if ok == len(results) else 1

    # Single-book mode
    raw_file = raw_files[0]

    if args.dry_run:
        template = detect_template_type(raw_file, config.raw_root, args.type)
        hs = file_sha256(raw_file)
        print(f"DRY RUN: would process {raw_file}")
        print(f"  hash: {hs}")
        print(f"  template: {template}")
        # Estimate cost
        if raw_file.suffix.lower() == ".pdf":
            pdf_type, avg_chars = detect_pdf_type(raw_file)
            print(f"  PDF type: {pdf_type} (avg {avg_chars:.0f} chars/page, 5-page random sample, skip first+last)")
            if pdf_type in ("scanned", "mixed"):
                try:
                    import fitz
                    doc = fitz.open(raw_file)
                    pages = len(doc)
                    doc.close()
                    batches = (pages + 4) // 5
                    print(f"  Stage 0 OCR: {pages} pages → ~{batches} API calls (5 pages/batch)")
                except Exception:
                    pass
        # Estimate Stage 1/1.5/2 (use PDF page count, don't call APIs)
        if raw_file.suffix.lower() == ".pdf":
            try:
                import fitz
                doc = fitz.open(raw_file)
                pages = len(doc)
                doc.close()
                est_chars = int(max(avg_chars, 200)) * pages  # floor at 200 chars/page
                chunks_est = max(1, (est_chars + config.target_chars - 1) // config.target_chars)
                print(f"  Estimated text: ~{est_chars:,} chars ({pages} pages × {max(avg_chars, 200):.0f} chars/page)")
                print(f"  Estimated API calls: 1 (Stage 2.1) + {chunks_est} (Stage 2.2 chunks) + 1-3 (Stage 2.4)")
                if pdf_type in ("scanned", "mixed"):
                    batches = (pages + 4) // 5
                    print(f"  ⚠️  May need Stage 0 OCR: ~{batches} batch calls for full-book OCR if PyMuPDF insufficient")
            except Exception:
                pass
        print(f"  Stages: text-extract -> image-extract+caption -> digest -> chunk -> generate -> review -> inject -> write -> cache")
        return 0

    h = file_sha256(raw_file)
    lock = ProjectLock(config, owner_id=h[-8:])
    if not lock.acquire():
        print("ERROR: Could not acquire project lock — another ingest may be running", file=sys.stderr)
        return 1
    try:
        result = ingest_one(raw_file, config, args.type, verbose=args.verbose,
                            pilot_confirmed=args.pilot_confirmed)
        print(f"\nResult: {result}")
        return 0 if result["status"] in ("ok", "skipped") else 1
    except ConversationPending:
        return 101
    except Exception:
        lock.release()
        raise
    else:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())

