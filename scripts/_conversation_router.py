"""Conversation-mode LLM router and task manifest.

This is the single text-generation path: ``call_anthropic_protocol`` writes
the prompt to a file and raises
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
import time
from pathlib import Path

from _config import Config
from _llm_api import conversation_handoff, set_conversation_router
from _paths import atomic_write


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
    # Stage-name slug + content-hash suffix gives human-readable grouping;
    # the 8-char content hash guarantees distinct prompts get distinct cache
    # files. Without the hash, every call that falls through _infer_stage to
    # 'LLM-task' otherwise shares one
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
    #   1. Inline single-line: "- Existing wiki pages: a, b, c"
    #   2. Heading + multi-line list:
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
    # this, a prompt's extracted_text block changes hash whenever captions are
    # added/removed, thrashing the slug and re-prompting the stage on every
    # resume (observed: 497f2b16 -> e20e22a4 for the same paper's digest).
    # Only the cache KEY is stabilized; the full prompt is still written to the
    # .md for the LLM.
    stable_prompt = re.sub(r'!\[[^\]]*\]\(', '![](', stable_prompt)
    content_hash = hashlib.sha256(stable_prompt.encode("utf-8")).hexdigest()[:8]
    prefix = config.conversation_prefix or "00000000"
    conv_dir = config.runtime_dir / "conversation" / prefix
    slug = _compatible_conversation_slug(conv_dir, stage, content_hash)

    response = conversation_handoff(
        conv_dir, slug, prompt,
        label=slug,
        stale_check=_is_stale_result,
        on_cached=lambda _response: _mark_task_done(
            config, slug, prompt, _response),
        on_prompt_written=lambda: _mark_task_pending(
            config, slug, prompt, max_tokens),
    )
    return response, "end_turn"


def _compatible_conversation_slug(
    conv_dir: Path,
    stage: str,
    content_hash: str,
) -> str:
    """Reuse the pre-correction review slug while its handoff is in flight."""
    current = f"{stage}-{content_hash}"
    if stage != "Stage-3-1-Review":
        return current

    legacy = f"Stage-3-4-Review-{content_hash}"
    suffixes = (".md", ".txt")
    if any((conv_dir / f"{current}{suffix}").exists() for suffix in suffixes):
        return current
    if any((conv_dir / f"{legacy}{suffix}").exists() for suffix in suffixes):
        return legacy
    return current


def _task_manifest_path(config: Config) -> Path:
    return config.runtime_dir / "conversation" / config.conversation_prefix / "tasks.json"


_TASK_MANIFEST_SCHEMA_VERSION = 2


def _empty_task_manifest(config: Config) -> dict:
    return {
        "schema_version": _TASK_MANIFEST_SCHEMA_VERSION,
        "conversation_prefix": config.conversation_prefix,
        "tasks": {},
        "pending": [],
        "completed": [],
    }


def _normalize_task_manifest(config: Config, manifest: dict) -> dict:
    """Migrate v1 arrays and derive unique compatibility lists from records."""
    if manifest.get("schema_version") != _TASK_MANIFEST_SCHEMA_VERSION:
        migrated = _empty_task_manifest(config)
        pending = list(dict.fromkeys(manifest.get("pending", [])))
        completed = list(dict.fromkeys(manifest.get("completed", [])))
        completed_set = set(completed)
        for slug in pending + completed:
            migrated["tasks"][slug] = {
                "slug": slug,
                "status": "completed" if slug in completed_set else "pending",
                "prompt_file": f"{slug}.md",
                "result_file": f"{slug}.txt",
                "attempts": 0,
                "migrated_from_v1": True,
            }
        manifest = migrated
    manifest["schema_version"] = _TASK_MANIFEST_SCHEMA_VERSION
    manifest["conversation_prefix"] = config.conversation_prefix
    tasks = manifest.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
        manifest["tasks"] = tasks

    # Prompt-policy/code changes intentionally produce a new content-hash slug.
    # Preserve the old task and files for audit, but once the newer prompt for
    # the same logical stage/chunk has completed, stop reporting the older task
    # as an actionable handoff forever. Logical keys are deliberately limited
    # to stage shapes that have one task per source or an explicit chunk number;
    # generic/dedup/merge prompts may have several legitimate siblings.
    conv_dir = _task_manifest_path(config).parent
    for task in tasks.values():
        if not isinstance(task, dict) or task.get("logical_key"):
            continue
        prompt_name = task.get("prompt_file")
        if not prompt_name:
            continue
        try:
            prompt = (conv_dir / str(prompt_name)).read_text(encoding="utf-8")
        except OSError:
            continue
        logical_key = _conversation_task_logical_key(prompt)
        if logical_key:
            task["logical_key"] = logical_key

    task_order = {slug: index for index, slug in enumerate(tasks)}
    completed_by_key: dict[str, tuple[str, dict]] = {}
    for slug, task in tasks.items():
        if not isinstance(task, dict) or task.get("status") != "completed":
            continue
        logical_key = str(task.get("logical_key") or "")
        if not logical_key:
            continue
        prior = completed_by_key.get(logical_key)
        candidate_order = (
            int(task.get("created_at", 0) or 0),
            task_order.get(slug, -1),
        )
        prior_order = (
            int(prior[1].get("created_at", 0) or 0),
            task_order.get(prior[0], -1),
        ) if prior is not None else (-1, -1)
        if candidate_order > prior_order:
            completed_by_key[logical_key] = (slug, task)

    for slug, task in tasks.items():
        if not isinstance(task, dict) or task.get("status") != "pending":
            continue
        logical_key = str(task.get("logical_key") or "")
        newer = completed_by_key.get(logical_key)
        if not logical_key or newer is None:
            continue
        newer_slug, newer_task = newer
        newer_order = (
            int(newer_task.get("created_at", 0) or 0),
            task_order.get(newer_slug, -1),
        )
        pending_order = (
            int(task.get("created_at", 0) or 0),
            task_order.get(slug, -1),
        )
        if newer_slug != slug and newer_order > pending_order:
            task["status"] = "superseded"
            task["superseded_by"] = newer_slug
            task["superseded_at"] = int(
                newer_task.get("completed_at")
                or newer_task.get("updated_at")
                or time.time() * 1000
            )

    manifest["pending"] = [
        slug for slug, task in tasks.items()
        if isinstance(task, dict) and task.get("status") == "pending"
    ]
    manifest["completed"] = [
        slug for slug, task in tasks.items()
        if isinstance(task, dict) and task.get("status") == "completed"
    ]
    return manifest


def _load_task_manifest(config: Config) -> dict:
    p = _task_manifest_path(config)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(
                    f"expected JSON object, got {type(raw).__name__}")
            return _normalize_task_manifest(config, raw)
        except Exception as e:
            # Corrupted manifest is not a silent reset — warn loudly so the
            # user knows why pending-task reporting restarted (policy 2026-06-24).
            print(f"⚠️  [conversation] {p} corrupted ({type(e).__name__}: {e}) "
                  f"— resetting task manifest.", flush=True)
    return _empty_task_manifest(config)


def _save_task_manifest(config: Config, manifest: dict) -> None:
    p = _task_manifest_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_task_manifest(config, manifest)
    atomic_write(p, json.dumps(normalized, ensure_ascii=False, indent=2))


def _mark_task_pending(
    config: Config,
    slug: str,
    prompt: str,
    max_tokens: int | None,
) -> None:
    m = _load_task_manifest(config)
    now = int(time.time() * 1000)
    prior = m.setdefault("tasks", {}).get(slug, {})
    attempts = int(prior.get("attempts", 0) or 0) + 1
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    created_at = prior.get("created_at", now)
    m["tasks"][slug] = {
        **prior,
        "slug": slug,
        "status": "pending",
        "prompt_file": f"{slug}.md",
        "result_file": f"{slug}.txt",
        "prompt_sha256": prompt_hash,
        "prompt_chars": len(prompt),
        "max_tokens": max_tokens,
        "attempts": attempts,
        "created_at": created_at,
        "updated_at": now,
    }
    logical_key = _conversation_task_logical_key(prompt)
    if logical_key:
        m["tasks"][slug]["logical_key"] = logical_key
    _save_task_manifest(config, m)


def _mark_task_done(
    config: Config,
    slug: str,
    prompt: str,
    response: str,
) -> None:
    m = _load_task_manifest(config)
    now = int(time.time() * 1000)
    prior = m.setdefault("tasks", {}).get(slug, {})
    m["tasks"][slug] = {
        **prior,
        "slug": slug,
        "status": "completed",
        "prompt_file": f"{slug}.md",
        "result_file": f"{slug}.txt",
        "prompt_sha256": prior.get(
            "prompt_sha256",
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        ),
        "prompt_chars": prior.get("prompt_chars", len(prompt)),
        "response_sha256": hashlib.sha256(
            response.encode("utf-8")).hexdigest(),
        "response_chars": len(response),
        "attempts": max(1, int(prior.get("attempts", 0) or 0)),
        "created_at": prior.get("created_at", now),
        "updated_at": now,
        "completed_at": prior.get("completed_at", now),
    }
    logical_key = _conversation_task_logical_key(prompt)
    if logical_key:
        m["tasks"][slug]["logical_key"] = logical_key
    _save_task_manifest(config, m)


def _is_stale_result(response: str, prompt: str) -> bool:
    """Detect if agent just copied the prompt instead of generating output."""
    has_yaml = "```yaml" in response or "entities_found" in response or "concepts_found" in response
    has_files = "---FILE:" in response or "### File" in response
    if has_yaml or has_files:
        return False
    # Match prompt-instruction markers only at their real line boundaries.
    # A substring check for "# Role" also matches the legitimate wiki heading
    # "## Role", causing a valid merge result to be deleted and regenerated
    # forever (observed while merging the NVIC page).
    copied_instruction = bool(
        re.search(r"(?m)^# Role(?:[ \t]*$|[ \t]+)", response)
        or re.search(r"(?m)^You are(?:[ \t]+|$)", response)
    )
    return copied_instruction and len(response) < len(prompt) * 0.8


def _conversation_task_logical_key(prompt: str) -> str:
    """Return a safe supersession key for one-task-per-source/stage shapes."""
    stage = _infer_stage(prompt)
    if stage.startswith("Stage-2-2-Chunk-"):
        return stage
    if stage == "Stage-2-4-Generation":
        chunk = re.search(r"^Chunk:\s*(\d+)\s*$", prompt, flags=re.MULTILINE)
        return f"{stage}:chunk-{chunk.group(1)}" if chunk else f"{stage}:all"
    if stage == "Stage-3-1-Review":
        return stage
    return ""


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
    for that book misreport itself as the Stage 3.1 review step).
    """
    # The output-language directive (commit c359232) is prepended to every
    # generation/analysis prompt and runs ~890 chars — it would push the
    # distinctive stage marker past this 500-char window and collapse every
    # generation stage to the generic "LLM-task" label (observed live on the
    # Printed Circuits Handbook ingest: several Stage 2 generation prompts were
    # all mislabeled,
    # which also defeats the per-stage cache-file grouping). When the prompt
    # literally opens with the directive, skip that block and scan the
    # instruction text that follows. Prompts that don't open with it (e.g. the
    # cached Stage 2.2 chunk-analysis prompts, whose marker is already in the
    # first 500 chars) are untouched, so their slug/cache key is unchanged.
    scan = prompt
    if prompt.lstrip().startswith("## ⚠️ MANDATORY OUTPUT LANGUAGE"):
        stripped = prompt.lstrip()
        idx = stripped.find("\n# ")
        if idx != -1:
            scan = stripped[idx + 1:]
    head = scan[:500]
    if "repairing truncated wiki FILE blocks" in head:
        return "Stage-2-TruncatedFileRepair"
    if "generating wiki pages" in head.lower() or ("Synthesis" in head and "FILE blocks" in head):
        return "Stage-2-4-Generation"
    if "review agent" in head or "可疑项" in head:
        return "Stage-3-1-Review"
    if "Chunk Analysis" in head:
        m = re.search(r"chunk (\d+)/(\d+)", prompt)
        if m:
            return f"Stage-2-2-Chunk-{m.group(1)}"
    if "reviewing concept pages generated from the same source for duplicates" in head:
        # In-source dedup is Stage 2.4's closing sub-step; use that stage in
        # cache labels as well.
        return "Stage-2-4-DedupConfirm"
    return "LLM-task"
