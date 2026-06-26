"""Conversation-mode LLM router + task manifest.

Extracted from ingest.py on 2026-06-23. This is the single text-generation
path: ``call_anthropic_protocol`` writes the prompt to a file and raises
``ConversationPending`` so the driving agent answers with the current
conversation's model; on re-invoke the cached result is read and returned.

The router is registered on ``_llm_api`` at import time (via
``set_conversation_router``) so the stage modules that call
``_llm_api.call_anthropic_protocol`` route here automatically.

``tasks.json`` tracks pending vs completed conversation prompts so
``ingest_one`` can report resume state on re-invoke.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from _core import Config
from _llm_api import conversation_handoff, set_conversation_router


def call_anthropic_protocol(prompt: str, config: Config, max_tokens: int | None = None) -> tuple[str, str]:
    """Text-generation LLM call — conversation mode only.

    This skill is only ever driven from a CLI session with an agent present
    to answer prompts, so there is no separate paid text-gen API key. The
    prompt is written to a file and ``ConversationPending`` is raised so the
    calling agent can answer with the current conversation's model; on
    re-invoke the cached result is read and returned.

    This function is registered as the conversation router on ``_llm_api`` so
    that the stage modules (which call ``_llm_api.call_anthropic_protocol``)
    route here automatically.

    Returns (text_content, stop_reason).
    """
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
    #
    # Two prompt shapes carry the list, both must be redacted:
    #   1. Inline single-line (Stage 2.1/2.8): "- Existing wiki pages: a, b, c"
    #   2. Heading + multi-line list (Stage 2.4/2.7/2.9/3.4):
    #        "# Existing wiki pages ..." followed by indented dash items or a
    #        bare comma-separated line, terminated by a blank line or the next
    #        "#" heading. The old single-line regex only matched shape 1, so
    #        Stage 2.4's slug changed every re-invoke as the wiki grew,
    #        thrashing the cache and blocking the ingest from reaching Phase 3.
    stable_prompt = re.sub(
        r"(#+[ \t]*(?:Existing [Ww]iki [Pp]ages|Linkable pages)[^\n]*\n)"  # group 1: heading line
        r"(?:(?!#+[ \t])[ \t]*[^\n]+\n)*"                                 # following list lines
        r"|(Existing wiki pages:[^\n]*)",                                  # group 2: inline "...:" single-line
        lambda m: (m.group(1) + "<redacted>\n") if m.group(1)
                   else "Existing wiki pages: <redacted>",
        prompt)
    # Redact volatile image alt-text captions. The image filename (a content
    # hash) is stable across runs, but the VLM/minerU alt-text caption may be
    # present or absent depending on the Stage 1.3 caption-cache state. Without
    # this, Stage 2.1's extracted_text block changes hash whenever captions are
    # added/removed, thrashing the 2.1 digest slug and re-prompting Stage 2.1
    # on every resume (observed: 497f2b16 -> e20e22a4 for the same paper).
    # Only the cache KEY is stabilized; the full prompt is still written to the
    # .md for the LLM.
    stable_prompt = re.sub(r'!\[[^\]]*\]\(', '![](', stable_prompt)
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
    """Best-effort stage label for the CONVERSATION banner / cache-file prefix.

    Cosmetic only — conversation_handoff()'s actual cache key also includes a
    content hash of the prompt, so a wrong label here can't cause two distinct
    prompts to collide. But every check here must stay anchored to a prefix
    slice of distinctive *instruction* text, never an unbounded scan of the
    full prompt — a digest/chunk-analysis prompt embeds up to 200K chars of
    the source's own prose, and generic words like "review"/"suggestions"
    routinely appear somewhere in a real book by coincidence (confirmed live:
    Plett's BMS Vol.2 preface contains "send me corrections and suggestions
    for improvements", which previously made every digest/chunk-analysis call
    for that book misreport itself as the Stage 3.4 review step).
    """
    head = prompt[:500]
    if "generating wiki pages" in head.lower() or ("Synthesis" in head and "FILE blocks" in head):
        return "Stage-2-4-Generation"
    if "review agent" in head or "可疑项" in head:
        return "Stage-3-4-Review"
    if "Chunk Analysis" in head:
        m = re.search(r"chunk (\d+)/(\d+)", prompt)
        if m:
            return f"Stage-2-2-Chunk-{m.group(1)}"
    if "writing a **source page**" in head:
        return "Stage-2-6-SourcePage"
    if "finished generating source/concept/entity pages" in head:
        return "Stage-2-7-QueryGeneration"
    if "reviewing concept pages generated from the same source for duplicates" in head:
        return "Stage-2-5-DedupConfirm"
    if "just generated concept/entity pages for a book" in head:
        return "Stage-2-9-Comparison"
    if "review the concepts just generated for a book" in head.lower():
        return "Stage-2-9-ComparisonReview"
    if "performing **Stage 1: Global Digest**" in head or "produce a **high-level structural summary**" in head:
        return "Stage-2-1-Global-Digest"
    return "LLM-task"
