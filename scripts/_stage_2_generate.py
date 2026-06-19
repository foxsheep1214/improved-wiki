from __future__ import annotations

import json, os, re, sys, time
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import (
    Config,
    heartbeat as _heartbeat, stage_begin as _stage_begin, stage_end as _stage_end,
    llm_call_progress as _llm_call_progress, llm_call_done as _llm_call_done,
    record_rate_limit as _record_rate_limit,
    load_template, load_progress, save_progress, clear_progress, progress_path,
    load_cache, save_cache,
    detect_domain, list_existing_slugs,
    str_distance as _str_distance, FOLDER_TO_TEMPLATE, detect_template_type,
    parse_yaml_block, parse_file_blocks, parse_simple_yaml,
)
from _llm_api import _retry_jitter, _is_retryable_exception, call_anthropic_protocol

__all__ = ["stage_2_5_review_suggestions", "build_per_chunk_gen_prompt", "_stage_2_per_concept_fallback", "stage_2_0_source_page", "build_query_generation_prompt", "stage_2_3_query_generation", "build_comparison_disambiguation_prompt", "build_comparison_in_source_prompt", "stage_2_5_comparison_generation"]

# ---------- Stage 2.5: Review suggestions ----------

def stage_2_5_review_suggestions(config: Config, file_blocks: list[tuple[str, str]],
                                  raw_file: Path, raw_response: str = "",
                                  verbose: bool = False) -> dict:
    """Run LLM review over newly generated wiki pages.

    NashSU trigger conditions (ingest.ts): any of —
      - >= 4 FILE blocks
      - >= 10K chars of generation output
      - Incomplete REVIEW block (opened but not closed)

    Output: wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md — human-browsable review pages.
    Each page has frontmatter `resolved: false`. When resolved, user changes to true.
    On next ingest, resolved pages are auto-cleaned.
    Also writes review-suggestions.json to runtime dir for tooling.
    """
    # NashSU 3-condition trigger (not just file block count)
    has_review_open = "---REVIEW:" in raw_response and not raw_response.rstrip().endswith("---END REVIEW---")
    if len(file_blocks) < 4 and len(raw_response) < 10000 and not has_review_open:
        print(f"[stage_2_5] Skipped — {len(file_blocks)} blocks, {len(raw_response)} chars, "
              f"no incomplete REVIEW (all below NashSU thresholds)")
        return {"skipped": True, "reason": "below-thresholds"}

    print(f"[stage_2_5] Running review over {len(file_blocks)} new pages + existing wiki...")

    # Collect new page contents
    new_pages: list[str] = []
    for path, content in file_blocks:
        new_pages.append(f"### {path}\n{content[:1500]}")

    # Sample existing wiki pages (up to 40)
    existing_pages: list[str] = []
    for sub in ["sources", "concepts", "entities", "comparisons", "findings"]:
        d = config.wiki_dir / sub
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix != ".md":
                continue
            content = f.read_text(encoding="utf-8")
            if content.startswith("---"):
                end = content.find("\n---", 3)
                body = content[end + 4:] if end != -1 else content
            else:
                body = content
            existing_pages.append(f"### {sub}/{f.name}\n{body[:1000]}")
            if len(existing_pages) >= 40:
                break
        if len(existing_pages) >= 40:
            break

    schema_text = ""
    schema_path = config.wiki_dir / "schema.md"
    if schema_path.exists():
        schema_text = schema_path.read_text(encoding="utf-8")[:2000]

    user_content = f"""# wiki/schema.md
{schema_text}

# Newly generated pages (from {raw_file.stem})
{chr(10).join(new_pages)}

# Existing wiki pages (sample of {len(existing_pages)})
{chr(10).join(existing_pages[:40])}
"""

    system_prompt = """你是 HardwareWiki 的 review agent。审阅当前 wiki 内容，找出 5 类可疑项：
1. confirm（需要人工确认）：数字、术语、矛盾点
2. suggestion（改进建议）：内容不完整、应补充、可加链接
3. missing-page（缺页）：[[wikilink]] 指向不存在的页面
4. contradiction（页面间矛盾）
5. duplicate（内容重复）

输出严格按 YAML 数组（只输出 YAML）：
```yaml
- id: 1
  type: confirm|suggestion|missing-page|contradiction|duplicate
  title: "一句话标题"
  description: "详细描述"
  affected_pages: ["sources/xxx.md", "concepts/yyy.md"]
  severity: high|medium|low
```
至少 5 个 items。数字、参数、公式要严格。"""

    prompt = f"{system_prompt}\n\n{user_content}"
    try:
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=8192)
    except RuntimeError as e:
        print(f"[stage_2_5] LLM call failed: {e}")
        return {"error": str(e)}

    if verbose:
        print(f"[stage_2_5] Response ({len(response)} chars, stop={stop_reason}):\n{response[:2000]}...\n")

    # Parse YAML
    text = response
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("yaml"):
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]

    try:
        import yaml
        items = yaml.safe_load(text.strip())
    except Exception:
        items = parse_simple_yaml(text.strip())
        if not isinstance(items, list):
            items = [items] if items else []

    if not isinstance(items, list):
        items = []

    # Write review pages to wiki/REVIEW/<review_type>/ (分子目录，一目了然)
    date_str = time.strftime("%Y-%m-%d")
    safe_source = re.sub(r'[^\w\s-]', '', raw_file.stem).strip()[:40]

    written = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        rtype = it.get("type", "suggestion")
        title = it.get("title", "Untitled")
        desc = it.get("description", "")
        affected = it.get("affected_pages", it.get("affected_pages", []))
        if isinstance(affected, str):
            affected = [affected]
        severity = it.get("severity", "medium")

        # Build short-slug from title (kebab-case, English only, max 40 chars)
        import unicodedata
        slug_raw = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode('ascii')
        short_slug = re.sub(r'[^\w\s-]', '', slug_raw).strip().lower()
        short_slug = re.sub(r'[-\s]+', '-', short_slug)[:50].strip('-')
        if not short_slug:
            short_slug = f"item-{written + 1}"

        reviews_dir = config.wiki_dir / "REVIEW" / rtype
        reviews_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-{safe_source}-{short_slug}.md"
        page_path = reviews_dir / filename

        # Build wikilinks for affected pages
        affected_links = "\n".join(f"- [[{p.replace('.md', '')}]]" for p in affected)

        md = f"""---
type: review
review_type: {rtype}
severity: {severity}
affected_pages: [{', '.join(affected)}]
resolved: false
created: {date_str}
source_ingest: "{raw_file.stem}"
---

# [{rtype}] {title}

{desc}

## Affected Pages
{affected_links}

## Resolution
_待审核。处理完成后将 frontmatter 中 `resolved: false` 改为 `resolved: true`，下次 ingest 时自动清理。_
"""
        tmp = page_path.with_suffix(page_path.suffix + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.rename(page_path)
        written += 1

    print(f"[stage_2_5] {written} review pages → wiki/REVIEW/")

    # Also write JSON for tooling (backward compat)
    runtime_dir = config.runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    sugg_path = runtime_dir / "review-suggestions.json"
    sugg_data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": config.llm_model,
        "stop_reason": stop_reason,
        "items": items,
    }
    tmp = sugg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sugg_data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(sugg_path)

    return {"items": written, "stop_reason": stop_reason}



# ---------- Stage 2: Per-Chunk Generation ----------


def build_per_chunk_gen_prompt(
    chunk_analysis: dict,
    chunk_text: str,
    chunk_index: int,
    file_path: Path,
    config: Config,
    template: str = "",
    generated_slugs: list[str] | None = None,
) -> str:
    """Build prompt to generate concept/entity pages from ONE chunk's analysis.

    Accepts generated_slugs from previously-processed chunks so the LLM can:
      - Skip concepts already covered by earlier chunks
      - Use [[wikilinks]] to reference existing pages
      - Avoid duplicate slug generation
    (NashSU parity: sequential, accumulating context.)
    """
    concepts = chunk_analysis.get("concepts_found", [])
    entities = chunk_analysis.get("entities_found", [])
    existing_slugs = list_existing_slugs(config)
    if generated_slugs is None:
        generated_slugs = []

    concept_lines = []
    concept_slugs: list[tuple[str, str]] = []  # (name, slug) for wikilink reference
    for c in concepts:
        if isinstance(c, dict):
            name = c.get("name", "")
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            slug = name.lower().replace(" ", "-").replace("/", "-")
            already = " [ALREADY COVERED — SKIP]" if slug in generated_slugs else ""
            concept_lines.append(
                f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn}{already}"
            )
            if not already:
                concept_slugs.append((name, f"concepts/{slug}"))
                for d in details[:3]:
                    concept_lines.append(f"      • {d}")

    entity_lines = []
    entity_slugs: list[tuple[str, str]] = []  # (name, slug) for wikilink reference
    for e in entities:
        if isinstance(e, dict):
            name = e.get("name", "")
            role = e.get("role", "")
            sig = e.get("significance", "")
            slug = name.lower().replace(" ", "-").replace("/", "-")
            already = " [ALREADY COVERED — SKIP]" if slug in generated_slugs else ""
            entity_lines.append(
                f"  - {name} (slug: entities/{slug}) ({role}): {sig}{already}"
            )
            if not already:
                entity_slugs.append((name, f"entities/{slug}"))

    concept_str = "\n".join(concept_lines[:100]) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines[:30]) if entity_lines else "(none)"

    generated_str = "\n".join(f"  - {s}" for s in generated_slugs) if generated_slugs else "(none yet — you are the first chunk)"

    # Build linkable slugs list: all slugs the LLM is allowed to wikilink to.
    linkable = set()
    for _, s in concept_slugs:
        linkable.add(s)
    for _, s in entity_slugs:
        linkable.add(s)
    for s in generated_slugs:
        if "/" in s:
            linkable.add(s)
        else:
            linkable.add(f"concepts/{s}")
            linkable.add(f"entities/{s}")
    for s in existing_slugs[:200]:
        linkable.add(s)
    linkable_list = sorted(linkable)
    if len(linkable_list) > 300:
        linkable_list = linkable_list[:300]
    linkable_str = "\n".join(f"  - {s}" for s in linkable_list) if linkable_list else "(none)"

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:1500]}\n</template>\n"

    return f"""# Role
You are generating wiki pages for ONE chunk of a book. Previous chunks have
already been processed — their pages are listed below. Do NOT regenerate them.

# Source
Book: {file_path.stem}
Chunk: {chunk_index + 1}

{template_section}
# Pages already generated by previous chunks (SKIP these):
{generated_str}

# Concepts found in this chunk (generate a page for each — skip ALREADY COVERED):
{concept_str}

# Entities found in this chunk (generate a page for key ones — skip ALREADY COVERED):
{entity_str}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/concepts/...`
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.

# [[wikilink]] Rules — STRICT
Each concept/entity above includes a slug like (slug: concepts/foo-bar).
This is the EXACT [[wikilink]] you must use — kebab-case with type prefix.

Correct format:
  [[concepts/natural-convection-heat-sink]]  ← kebab-case + concepts/ prefix
  [[entities/bell-labs]]                      ← kebab-case + entities/ prefix

WRONG formats (DO NOT use — these create broken links):
  [[Natural Convection Heat Sink]]  ← Title Case, no prefix = BROKEN
  [[convection]]                    ← missing prefix = BROKEN
  [[concepts/litz-wire.md]]         ← includes .md = BROKEN
  [[cooling technique]]             ← not in linkable list = BROKEN

# Linkable pages (ONLY these [[wikilinks]] are valid):
{linkable_str}

Rules:
1. ONLY use [[wikilinks]] from the "Linkable pages" list above.
2. Use the EXACT slug shown. Do not change case, add words, or invent new ones.
3. For concepts/entities IN THIS CHUNK: use the slug from its "(slug: ...)" label.
4. If no matching slug exists, write the term as PLAIN TEXT with NO [[]].
5. NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
6. Math: $inline$ $$display$$

# Output Format — EXACT
---FILE:wiki/concepts/<slug>.md---
---
type: concept
title: "..."
domain: general
tags: [...]
related: [...]
sources: ["raw/{file_path.relative_to(config.raw_root)}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# Title

(content)

---END FILE---
---FILE:wiki/entities/<slug>.md---
(frontmatter + content)
---END FILE---

Generate a page for EVERY concept listed above that is NOT marked [ALREADY COVERED]. Go!
"""


# ── Per-concept fallback (when per-chunk returns 0 blocks) ──

# Maximum concepts per LLM call in fallback mode.
# Above this, concepts are split into multiple calls.
PER_CONCEPT_BATCH_MAX = 4


def _stage_2_per_concept_fallback(
    chunk_analyses: list[dict],
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    pre_existing_slugs: list[str] | None = None,
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Generate each concept in its own small LLM call.

    Used when per-chunk generation returns 0 FILE blocks (e.g. chunk has
    too many concepts for a single call to complete within max_tokens/timeout).
    Each concept gets a focused prompt with just its definition + context from
    the chunk that found it.

    pre_existing_slugs: slugs already generated by prior per-chunk calls.
    When provided (barrier-free fallback), these concepts are skipped so the
    fallback only generates what was missed — avoids duplicate LLM calls.
    """
    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)
    all_file_blocks: list[tuple[str, str]] = []
    all_responses: list[str] = []
    generated_slugs: list[str] = list(pre_existing_slugs) if pre_existing_slugs else []
    gen_tokens = config.compute_max_tokens(8192)
    t0 = time.time()
    total = len(unique_concepts) + len(unique_entities)

    # Build concept→chunk_analysis map for targeted context
    concept_to_chunk: dict[str, int] = {}
    for idx, analysis in enumerate(chunk_analyses):
        for c in analysis.get("concepts_found", []):
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            concept_to_chunk[name] = idx

    print(f"[stage_2] Per-concept fallback: {len(unique_concepts)} concepts + "
          f"{len(unique_entities)} entities, {PER_CONCEPT_BATCH_MAX} per batch, "
          f"max_tokens={gen_tokens}")

    n = 0
    existing_slugs = list_existing_slugs(config)

    for concept_name in unique_concepts:
        chunk_idx = concept_to_chunk.get(concept_name, 0)
        analysis = chunk_analyses[chunk_idx] if chunk_idx < len(chunk_analyses) else chunk_analyses[0]

        # Extract concept details from the chunk analysis
        concept_info = None
        for c in analysis.get("concepts_found", []):
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            if name == concept_name:
                concept_info = c if isinstance(c, dict) else {"name": c}
                break

        slug = concept_name.lower().replace(" ", "-").replace("/", "-")
        if slug in generated_slugs:
            continue

        prompt = _build_per_concept_prompt(
            concept_info, slug, file_path, config, global_digest,
            analysis, generated_slugs, existing_slugs, template,
        )

        for attempt in range(3):
            try:
                t_call = time.time()
                response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
                all_responses.append(response)
                blocks = parse_file_blocks(response)
                all_file_blocks.extend(blocks)
                dt = time.time() - t_call
                n += 1
                pct = n * 100 // total
                tag = f" (retry #{attempt})" if attempt > 0 else ""
                print(f"  [concept {n}/{total}] {concept_name[:50]} → "
                      f"{len(blocks)} blocks ({len(response):,} chars, {stop_reason}) "
                      f"{dt:.0f}s [{pct}%]{tag}")
                for path, _content in blocks:
                    s = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
                    if s not in generated_slugs:
                        generated_slugs.append(s)
                break
            except Exception as e:
                if attempt < 2 and _is_retryable_exception(e):
                    wait = _retry_jitter(2.0, attempt)
                    print(f"  [concept {n+1}/{total}] {type(e).__name__} retry in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                print(f"  [concept {n+1}/{total}] ❌ {e}")
                break

    for entity_name in unique_entities[:min(len(unique_entities), 20)]:
        slug = entity_name.lower().replace(" ", "-").replace("/", "-")
        if slug in generated_slugs:
            continue
        prompt = _build_per_entity_prompt(
            entity_name, slug, file_path, config, global_digest,
            existing_slugs, template,
        )
        for attempt in range(3):
            try:
                response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
                all_responses.append(response)
                blocks = parse_file_blocks(response)
                all_file_blocks.extend(blocks)
                n += 1
                pct = n * 100 // total
                print(f"  [entity {n}/{total}] {entity_name[:50]} → "
                      f"{len(blocks)} blocks ({len(response):,} chars, {stop_reason}) [{pct}%]")
                for path, _content in blocks:
                    s = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
                    if s not in generated_slugs:
                        generated_slugs.append(s)
                break
            except Exception as e:
                if attempt < 2 and _is_retryable_exception(e):
                    time.sleep(_retry_jitter(2.0, attempt))
                    continue
                print(f"  [entity {n+1}/{total}] ❌ {e}")
                break

    # Generate source page from global digest (compact)
    try:
        source_rel = f"sources/{file_path.relative_to(config.raw_root).with_suffix('.md')}"
    except ValueError:
        source_rel = f"sources/{file_path.with_suffix('.md').name}"
    source_prompt = f"""# Role
Generate a source page for this document from the global digest.

# Global Digest
```yaml
{json.dumps(global_digest, ensure_ascii=False, indent=2)[:4000]}
```

# Concepts generated ({len(all_file_blocks)} pages)
{', '.join(Path(p).stem for p, _ in all_file_blocks[:80])}

# Output Format — EXACT
---FILE:wiki/{source_rel}---
(frontmatter type:source + content)
---END FILE---

START IMMEDIATELY with ---FILE:... No preamble.
"""
    try:
        src_response, _ = call_anthropic_protocol(
            source_prompt, config, max_tokens=config.compute_max_tokens(4096))
        all_responses.append(src_response)
        src_blocks = parse_file_blocks(src_response)
        all_file_blocks.extend(src_blocks)
    except Exception as e:
        print(f"  [stage_2] Source page generation failed: {e}")

    combined = "\n".join(all_responses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
    source_blocks = [b for b in all_file_blocks if "sources/" in b[0]]

    print(f"[stage_2] Per-concept fallback done — {time.time()-t0:.0f}s, "
          f"{len(all_file_blocks)} blocks ({len(concept_blocks)}c/{len(entity_blocks)}e/{len(source_blocks)}s)")

    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "source_generated": len(source_blocks) > 0,
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": len(chunk_analyses),
        "method": "per-concept-fallback",
    }
    return analysis, combined, all_file_blocks


def _build_per_concept_prompt(
    concept_info: dict,
    slug: str,
    file_path: Path,
    config: Config,
    global_digest: dict,
    chunk_analysis: dict,
    generated_slugs: list[str],
    existing_slugs: list[str],
    template: str = "",
) -> str:
    """Build a focused prompt for generating ONE concept page."""
    name = concept_info.get("name", slug)
    definition = concept_info.get("definition", "")
    importance = concept_info.get("importance", "core")
    details = concept_info.get("key_details", [])[:5]

    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    # Sibling concepts from same chunk (for wikilinks)
    siblings = []
    for c in chunk_analysis.get("concepts_found", []):
        cn = c.get("name", c) if isinstance(c, dict) else str(c)
        if cn != name:
            siblings.append(cn)

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:800]}\n</template>\n"

    return f"""# Role
Generate ONE wiki concept page. Output ONLY this one page, then stop.

{template_section}
# Concept to Generate
- Name: {name}
- Importance: {importance}
- Definition: {definition}
{f"- Key Details: " + "; ".join(details) if details else ""}

# Context from Source
Source: {file_path.stem}
{f"Sibling concepts in this section (use [[wikilinks]]): {', '.join(siblings[:10])}" if siblings else ""}

# Already Generated (skip these — use [[wikilinks]]):
{', '.join(generated_slugs[:50]) or "(none yet)"}

# Existing Wiki Pages (avoid duplicates):
{', '.join(existing_slugs[:50]) or "(none)"}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCK
Your FIRST line MUST be `---FILE:wiki/concepts/{slug}.md---`
Do NOT write preamble, analysis, or commentary. Parser IGNORES non-FILE text.

# Output Format — EXACT
---FILE:wiki/concepts/{slug}.md---
---
type: concept
domain: general
title: "{name}"
tags: [...]
related: []
sources: ["raw/{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# {name}

(Detailed content — explain the concept, include key details, use [[wikilinks]])

---END FILE---

Generate the page NOW. Start with ---FILE:...
"""


def _build_per_entity_prompt(
    entity_name: str,
    slug: str,
    file_path: Path,
    config: Config,
    global_digest: dict,
    existing_slugs: list[str],
    template: str = "",
) -> str:
    """Build a focused prompt for generating ONE entity page."""
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    return f"""# Role
Generate ONE wiki entity page. Output ONLY this one page, then stop.

# Entity to Generate
- Name: {entity_name}

# Source
Document: {file_path.stem}

# Existing Wiki Pages (avoid duplicates):
{', '.join(existing_slugs[:50]) or "(none)"}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCK
Your FIRST line MUST be `---FILE:wiki/entities/{slug}.md---`
Do NOT write preamble, analysis, or commentary.

# Output Format — EXACT
---FILE:wiki/entities/{slug}.md---
---
type: entity
domain: general
title: "{entity_name}"
tags: [...]
related: []
sources: ["raw/{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# {entity_name}

(Description, significance, key attributes, related concepts using [[wikilinks]])

---END FILE---

Generate the page NOW. Start with ---FILE:...
"""


def _generate_chunk(
    analysis: dict,
    chunk_idx: int,
    generated_slugs: list[str],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    chunk_text: str = "",
) -> list[tuple[str, str]]:
    """Generate FILE blocks for a single chunk (extracted from stage_2_per_chunk_generation).

    Used by the barrier-free pipeline in _do_prepare where each chunk is
    generated immediately after analysis, before moving to the next chunk.

    Returns list of (path, content) tuples.  Caller should append slugs to
    generated_slugs from the returned paths.
    """
    concepts_n = len(analysis.get("concepts_found", []))
    entities_n = len(analysis.get("entities_found", []))
    if concepts_n == 0 and entities_n == 0:
        print(f"  [chunk {chunk_idx+1}] (no concepts or entities — skipped)")
        return []

    prompt = build_per_chunk_gen_prompt(
        analysis, chunk_text, chunk_idx, file_path, config, template,
        generated_slugs=generated_slugs,
    )
    gen_tokens = config.compute_max_tokens(16384)

    for attempt in range(4):
        try:
            t0 = time.time()
            if attempt == 0:
                print(f"  [chunk {chunk_idx+1}] generating ({concepts_n}c/{entities_n}e)...",
                      flush=True)
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
            blocks = parse_file_blocks(response)
            dt = time.time() - t0
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [chunk {chunk_idx+1}] generate OK{tag} — "
                  f"{concepts_n}c/{entities_n}e → {len(blocks)} blocks "
                  f"({len(response):,} chars, {stop_reason}) {dt:.0f}s")
            if verbose:
                print(f"    response: {response[:500]}...")
            return blocks

        except Exception as e:
            if attempt < 3 and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                print(f"  [chunk {chunk_idx+1}] generate {err_label} retry {attempt+1}/4"
                      f" — {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [chunk {chunk_idx+1}] generate FAILED: {e}")
            return []



# ---------- Stage 2: Synthesis (legacy, for small books) ----------

def _build_image_reference_section(file_path: Path, config: Config) -> str:
    """Build a compact list of available images for the Stage 2 prompt."""
    slug = _media_slug(file_path, config)
    media_dir = config.wiki_dir / "media" / slug
    if not media_dir.exists():
        return "（本书无提取图片）\n"

    manifest_path = media_dir / "_manifest.json"
    captioned = 0
    total = 0
    sample_lines: list[str] = []

    if manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        images = m.get("images", [])
        # Legacy guard: skip full-page renders from old ingests (source:"page-render").
        # Current pipeline no longer produces these.
        images = [i for i in images if i.get("source") != "page-render"]
        total = len(images)
        for img in sorted(images, key=lambda x: (x["page"], x.get("img_idx_in_page", 0)))[:60]:
            cap_path = media_dir / (img["filename"] + ".caption.txt")
            cap = cap_path.read_text(encoding="utf-8").strip()[:70] if cap_path.exists() else ""
            sample_lines.append(f"  p{img['page']} `{img['filename']}`: {cap}")
            if cap:
                captioned += 1
    else:
        # Loose files (minerU, old cloud OCR without manifest)
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                # Legacy guard: skip old full-page renders (pNNNN.jpg without
                # -mineru_ / -fig suffix). Current pipeline no longer produces these.
                stem = f.stem
                if re.match(r"^p\d{4}$", stem) and "-mineru_" not in stem:
                    continue
                total += 1
                cap_path = media_dir / (f.name + ".caption.txt")
                cap = cap_path.read_text(encoding="utf-8").strip()[:70] if cap_path.exists() else ""
                if total <= 60:
                    sample_lines.append(f"  `{f.name}`: {cap}")
                if cap:
                    captioned += 1

    if total == 0:
        return "（本书无提取图片）\n"

    section = f"本书共 {total} 张图（{captioned} 有caption）。图片位于 wiki/media/{slug}/。\n"
    section += "在 concept/entity 页面中用 ![](media/{}/filename) 引用相关图片。\n".format(slug)
    section += "关键图片示例：\n"
    section += "\n".join(sample_lines[:60])
    if total > 60:
        section += f"\n  ... （共 {total} 张，仅列前 60）"
    return section + "\n"



def _extract_concept_entity_names(chunk_analyses: list[dict]) -> tuple[list[str], list[str]]:
    """Extract deduplicated concept and entity names from chunk analyses."""
    all_concepts: list[str] = []
    all_entities: list[str] = []
    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            all_concepts.append(name)
        for e in a.get("entities_found") or []:
            name = e.get("name", e) if isinstance(e, dict) else str(e)
            all_entities.append(name)
    seen_c: set[str] = set()
    unique_concepts = [x for x in all_concepts if not (x in seen_c or seen_c.add(x))]  # type: ignore[func-returns-value]
    seen_e: set[str] = set()
    unique_entities = [x for x in all_entities if not (x in seen_e or seen_e.add(x))]  # type: ignore[func-returns-value]
    return unique_concepts, unique_entities


def stage_2_0_source_page(
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    current_domain: str = "general",
    verbose: bool = False,
) -> tuple[str, str]:
    """Stage 2.0: Dedicated source page generation (NashSU two-step).

    Separated from concept/entity generation so the LLM can focus entirely
    on producing a high-quality source page from the global digest.
    This matches NashSU ingest.ts which generates the source page first,
    then concept/entity pages in a separate pass.
    """
    try:
        source_rel = str(file_path.relative_to(config.raw_root).with_suffix(""))
    except ValueError:
        source_rel = file_path.stem

    book_meta = global_digest.get("book_meta", {})
    if not isinstance(book_meta, dict):
        book_meta = {}
    title = book_meta.get("title", file_path.stem) if isinstance(book_meta, dict) else file_path.stem
    authors = book_meta.get("authors", []) if isinstance(book_meta, dict) else []
    year = book_meta.get("year", "") if isinstance(book_meta, dict) else ""
    publisher = book_meta.get("publisher", "") if isinstance(book_meta, dict) else ""

    digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
    if len(digest_str) > 8000:
        digest_str = digest_str[:8000] + "\n... (truncated)"

    outline = global_digest.get("outline", [])
    key_claims = global_digest.get("key_claims", [])
    key_concepts = global_digest.get("key_concepts", [])
    key_entities = global_digest.get("key_entities", [])

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:2000]}\n</template>\n"

    prompt = f"""# Role
You are writing a **source page** for a Karpathy-pattern wiki knowledge base.
This page will be the authoritative entry for a book in the wiki.
{template_section}
# Book Information (from Global Digest)
```yaml
{digest_str}
```

# Task
Write a comprehensive source page. Wrap it in FILE block format.

# ⚠️  CRITICAL — OUTPUT FORMAT
Your ENTIRE response MUST be wrapped in EXACTLY ONE file block:

---FILE:wiki/sources/{source_rel}.md---
---
type: source
title: "{title}"
domain: {current_domain}
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
tags: [tag1, tag2, tag3]
related: []
sources: ["raw/{source_rel}.pdf"]
---

## Book Summary

2-4 sentences summarizing what this book covers, its approach, and who it's for.

## Table of Contents & Key Concepts

For EACH chapter in the outline, write one comprehensive line:
1. **Chapter Title:** list ALL key topics — aim for 5-15 items, comma-separated.

Example:
1. **DC-DC Converters:** buck, boost, buck-boost, CCM vs DCM, voltage-mode control, PWM, synchronous rectification.

## Key Takeaways

5-10 most important claims, formulas, design rules, or conclusions. Each ONE sentence.
---END FILE---

# Instructions
- Your FIRST line MUST be `---FILE:wiki/sources/{source_rel}.md---`, immediately followed by `---` (frontmatter start) on the NEXT line with NO blank line in between
- Your LAST line MUST be `---END FILE---`
- The frontmatter MUST use real data from the digest. NO ``` fences. NO blank lines before frontmatter.
- Do NOT add extra sections beyond the 3 listed above. Link to concepts via [[wikilinks]].
- tags: 3-8 relevant tags (do NOT leave empty)
- related: 2-5 related wiki page slugs
- Math: $inline$ $$display$$
"""

    gen_tokens = config.compute_max_tokens(8192)
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
    if verbose:
        print(f"[stage_2_0] Source page generated ({len(response):,} chars, stop={stop_reason})")
    else:
        print(f"[stage_2_0] Source page ready ({len(response):,} chars)")

    return response, stop_reason


# ---------- Stage 2.3: Query generation ----------

def build_query_generation_prompt(
    global_digest: dict,
    concept_titles: list[str],
    entity_titles: list[str],
    key_claims: list[dict],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.3: generate open questions from single-source analysis."""
    digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
    if len(digest_str) > 3000:
        digest_str = digest_str[:3000] + "\n... (truncated)"

    concepts_str = '\n'.join(f"- {c}" for c in concept_titles[:80])
    entities_str = '\n'.join(f"- {e}" for e in entity_titles[:40])
    claims_str = '\n'.join(
        f"- {c.get('claim', str(c))}" if isinstance(c, dict)
        else f"- {c}"
        for c in (key_claims or [])[:30]
    )
    existing_slugs = list_existing_slugs(config)
    today_str = time.strftime("%Y-%m-%d")
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    return f"""# Role
You are maintaining a Karpathy-pattern knowledge base wiki. You have just finished generating source/concept/entity pages for a book.

# Current Domain
{current_domain}

# Book Context
- Title: {file_path.stem}
- Canonical source path: raw/{raw_rel}
- Global Digest (summary):
```yaml
{digest_str}
```

# Generated Concepts ({len(concept_titles)} total)
{concepts_str if concepts_str else '(none)'}

# Generated Entities ({len(entity_titles)} total)
{entities_str if entities_str else '(none)'}

# Key Claims from the Book
{claims_str if claims_str else '(none)'}

# Existing Wiki Pages (avoid referencing non-existent pages)
{', '.join(existing_slugs[:200])}

# Task
Identify **0-5 open questions** this book raises but does NOT fully answer.
A good query is:
1. Grounded — stems from specific content in the book
2. Explorable — can be advanced by reading more, experimenting, or deeper analysis
3. Bounded — specific enough to have a clear exploration direction

Bad examples (do NOT generate):
- "What is voltage?" — book already answers this
- "How to learn hardware design?" — too broad
- "Will AI replace hardware engineers?" — unrelated to this book

# Output Format
---FILE:wiki/queries/{{slug}}.md---
---
type: query
title: "{{question ending with ?}}"
domain: {current_domain}
tags: [{{2-4 tags}}]
related: [{{2-4 wikilink stems from generated concepts/entities}}]
sources: ["raw/{raw_rel}"]
created: {today_str}
updated: {today_str}
---

# {{question title}}

## Background
{{2-3 sentences: what specific content in the book prompted this question}}

## Clues from the Book
{{bullet points of partial answers/data/cases already in the book, each with chapter source}}

## To Explore
{{2-4 specific sub-questions the book left unanswered}}

## See Also
- [[{{related concept}}]] — {{one-line description}}
---END FILE---

If no worthwhile query exists, output exactly:
---QUERIES: 0---
(no open questions worth a standalone page)
---END QUERIES---

# Constraints
- slug: English kebab-case, 3-6 words
- title: complete question ending with ? or ？
- related: ONLY wikilink stems from THIS ingest (see Generated Concepts/Entities above)
- sources: ONLY this book
- Each query body ≥200 chars (excluding frontmatter)
- START IMMEDIATELY with ---FILE: or ---QUERIES: — no preamble
"""


def stage_2_3_query_generation(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_blocks: list[tuple[str, str]],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> tuple[list[tuple[str, str]], str]:
    """Stage 2.3: Generate query pages (open questions) from single-source analysis.

    Returns (new_query_blocks, raw_response).
    Skips for datasheet/standard source types.
    """
    # Skip for datasheet/standard — pure fact listing, no meaningful open questions
    try:
        from _paths import detect_template_type
        src_type = detect_template_type(file_path, config)
    except Exception:
        src_type = None
    if src_type in ("datasheet", "standard"):
        if verbose:
            print(f"[stage_2_3] Skipped — {src_type} source type (no meaningful open questions)")
        return [], ""

    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)

    # Collect key claims from chunk analyses
    key_claims = []
    for ca in chunk_analyses:
        claims = ca.get("claims", [])
        if isinstance(claims, list):
            key_claims.extend(claims)

    # Get concept/entity titles from generated file blocks
    concept_titles = []
    entity_titles = []
    for path, _ in file_blocks:
        if path.startswith("concepts/"):
            concept_titles.append(path.replace("concepts/", "").replace(".md", ""))
        elif path.startswith("entities/"):
            entity_titles.append(path.replace("entities/", "").replace(".md", ""))

    # If no concepts generated, skip
    if not concept_titles:
        if verbose:
            print("[stage_2_3] Skipped — no concepts generated")
        return [], ""

    # Detect domain
    current_domain = global_digest.get("book_meta", {}).get("domain", "general") if isinstance(global_digest.get("book_meta"), dict) else "general"

    prompt = build_query_generation_prompt(
        global_digest, concept_titles, entity_titles,
        key_claims, file_path, config, current_domain
    )

    query_tokens = config.compute_max_tokens(4096)
    if verbose:
        print(f"[stage_2_3] Query generation — {len(concept_titles)} concepts, "
              f"{len(key_claims)} claims, prompt {len(prompt):,} chars...")

    try:
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=query_tokens)
    except Exception as e:
        print(f"[stage_2_3] LLM call failed: {e}")
        return [], ""

    if verbose:
        print(f"[stage_2_3] Response ({len(response)} chars, stop={stop_reason}):\n{response[:2000]}...\n")

    # Parse query FILE blocks
    query_blocks = parse_file_blocks(response)
    if query_blocks:
        print(f"[stage_2_3] Generated {len(query_blocks)} query page(s)")
        for path, _ in query_blocks:
            print(f"  → {path}")
    elif "---QUERIES: 0---" in response or "QUERIES: 0" in response:
        print("[stage_2_3] No worthwhile queries (---QUERIES: 0---)")
    else:
        print("[stage_2_3] No query blocks parsed (may be implicit ---QUERIES: 0---)")

    return query_blocks, response


# ---------- Stage 2.5: Comparison generation ----------

def build_comparison_disambiguation_prompt(
    concept_titles: list[str],
    entity_titles: list[str],
    existing_slugs: list[str],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.5A: disambiguation comparisons."""
    new_titles = concept_titles + entity_titles
    new_str = '\n'.join(f"- {t} (domain: {current_domain})" for t in new_titles[:80])
    existing_str = ', '.join(existing_slugs[:300])
    today_str = time.strftime("%Y-%m-%d")

    return f"""# Role
You are maintaining a wiki knowledge base. You have just generated concept/entity pages for a book.

# Current Domain
{current_domain}

# New Pages from This Book
{new_str}

# Existing Wiki Pages
{existing_str}

# Task
Check if any NEW page title has an EXACT name match with an EXISTING wiki page from a DIFFERENT domain.
ONLY create a disambiguation page when there is a genuine naming collision across domains.
Do NOT create disambiguation for:
- Similar-but-different names (e.g., "8b/10b encoding" vs "8b10b encoding bypass")
- Terms that only exist in ONE domain
- Terms where the domain distinction is already clear from the page title
- Sub-topics or variations of the same concept

A genuine collision example: "Switch" exists in BOTH circuit-fundamentals AND power-electronics with different meanings.

# Output Format
---FILE:wiki/comparisons/{{term-slug}}.md---
---
type: comparison
title: "{{Term}} (disambiguation)"
domain: general
tags: [disambiguation]
related: [{{domain-specific page stems}}]
sources: []
created: {today_str}
updated: {today_str}
---

# {{Term}} (disambiguation)

The term "{{Term}}" has different meanings across HardwareWiki domains:

| Domain | Meaning | Page |
|--------|---------|------|
| {{domain-1}} | {{one-sentence definition}} | [[{{term}}-{{domain-1}}]] |
| {{domain-2}} | {{one-sentence definition}} | [[{{term}}-{{domain-2}}]] |

## How to Distinguish
{{1-2 sentences on how to tell which domain based on context}}

## See Also
- [[{{term}}-{{domain-1}}]] — {{description}}
- [[{{term}}-{{domain-2}}]] — {{description}}
---END FILE---

If no disambiguation is needed, output:
---COMPARISONS_DISAMBIGUATION: 0---
---END COMPARISONS_DISAMBIGUATION---

START IMMEDIATELY with ---FILE: or ---COMPARISONS_DISAMBIGUATION: — no preamble.
"""


def build_comparison_in_source_prompt(
    concept_titles: list[str],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.5B: in-source concept comparisons."""
    concepts_with_desc = '\n'.join(f"- {c}" for c in concept_titles[:60])
    today_str = time.strftime("%Y-%m-%d")
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    return f"""# Role
You are maintaining a wiki knowledge base. Review the concepts just generated for a book.

# Current Domain
{current_domain}

# Source
{file_path.stem} (raw/{raw_rel})

# Generated Concepts
{concepts_with_desc}

# Task
Identify pairs of concepts that are **naturally compared** — understanding one illuminates the other.
Good candidates:
- Two choices on the same dimension (CCM vs DCM, Buck vs Boost, Voltage Mode vs Current Mode)
- Commonly confused pairs (EMI vs EMC, SNR vs SINAD, PSRR vs CMRR)
- Explicitly contrasted in the book

Bad candidates:
- Upstream/downstream relationships (MOSFET → Gate Driver)
- Parent/child relationships (DC-DC Converter → Buck Converter)
- Three or more items → NOT a comparison

Generate at most 2 comparisons. Output 0 if no good pair exists.

# Output Format
---FILE:wiki/comparisons/{{slug}}.md---
---
type: comparison
title: "{{Concept A}} vs {{Concept B}}"
domain: {current_domain}
tags: [{{2-4 tags}}]
related: [{{concept-A-stem}}, {{concept-B-stem}}]
sources: ["raw/{raw_rel}"]
created: {today_str}
updated: {today_str}
---

# {{Concept A}} vs {{Concept B}}

## Why Compare
{{1-2 sentences: why these two benefit from side-by-side understanding}}

## Comparison Table
| Dimension | {{Concept A}} | {{Concept B}} |
|-----------|---------------|---------------|
| {{dim 1: e.g. operating principle}} | | |
| {{dim 2: e.g. key characteristic}} | | |
| {{dim 3: e.g. typical application}} | | |
| {{dim 4: e.g. advantages/disadvantages}} | | |

## Selection Guide
{{When to choose A vs B — 2-3 specific recommendations}}

## See Also
- [[{{Concept A}}]] — {{one-line description}}
- [[{{Concept B}}]] — {{one-line description}}
---END FILE---

If no good comparison pair exists, output:
---COMPARISONS_IN_SOURCE: 0---
---END COMPARISONS_IN_SOURCE---

START IMMEDIATELY with ---FILE: or ---COMPARISONS_IN_SOURCE: — no preamble.
"""


def stage_2_5_comparison_generation(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_blocks: list[tuple[str, str]],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> tuple[list[tuple[str, str]], str]:
    """Stage 2.5: Generate comparison pages (disambiguation + in-source contrast).

    Returns (new_comparison_blocks, raw_response).
    Skips when no concepts were generated.
    """
    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)

    # Get concept/entity titles from generated file blocks
    concept_titles = []
    entity_titles = []
    for path, _ in file_blocks:
        if path.startswith("concepts/"):
            concept_titles.append(path.replace("concepts/", "").replace(".md", ""))
        elif path.startswith("entities/"):
            entity_titles.append(path.replace("entities/", "").replace(".md", ""))

    if not concept_titles and not entity_titles:
        if verbose:
            print("[stage_2_5_comp] Skipped — no concepts/entities generated")
        return [], ""

    current_domain = global_digest.get("book_meta", {}).get("domain", "general") if isinstance(global_digest.get("book_meta"), dict) else "general"
    existing_slugs = list_existing_slugs(config)
    comp_tokens = config.compute_max_tokens(4096)
    all_blocks: list[tuple[str, str]] = []

    # 2.5A: Disambiguation
    if verbose:
        print(f"[stage_2_5_comp] 2.5A Disambiguation check — {len(concept_titles)} concepts vs {len(existing_slugs)} existing...")
    prompt_25a = build_comparison_disambiguation_prompt(
        concept_titles, entity_titles, existing_slugs, file_path, config, current_domain
    )
    try:
        response_25a, stop_25a = call_anthropic_protocol(prompt_25a, config, max_tokens=comp_tokens)
    except Exception as e:
        print(f"[stage_2_5_comp] 2.5A LLM call failed: {e}")
        response_25a = ""
    if response_25a:
        blocks_25a = parse_file_blocks(response_25a)
        if blocks_25a:
            print(f"[stage_2_5_comp] 2.5A: {len(blocks_25a)} disambiguation page(s)")
            all_blocks.extend(blocks_25a)
        else:
            print("[stage_2_5_comp] 2.5A: no disambiguation needed")

    # 2.5B: In-source concept comparison
    response_25b = ""
    if len(concept_titles) >= 2:
        if verbose:
            print(f"[stage_2_5_comp] 2.5B In-source comparison — {len(concept_titles)} concepts...")
        prompt_25b = build_comparison_in_source_prompt(
            concept_titles, file_path, config, current_domain
        )
        try:
            response_25b, stop_25b = call_anthropic_protocol(prompt_25b, config, max_tokens=comp_tokens)
        except Exception as e:
            print(f"[stage_2_5_comp] 2.5B LLM call failed: {e}")
            response_25b = ""
        if response_25b:
            blocks_25b = parse_file_blocks(response_25b)
            if blocks_25b:
                print(f"[stage_2_5_comp] 2.5B: {len(blocks_25b)} comparison page(s)")
                for path, _ in blocks_25b:
                    print(f"  → {path}")
                all_blocks.extend(blocks_25b)
            else:
                print("[stage_2_5_comp] 2.5B: no comparison pairs found")
    else:
        if verbose:
            print("[stage_2_5_comp] 2.5B skipped — fewer than 2 concepts")

    if all_blocks:
        print(f"[stage_2_5_comp] Total: {len(all_blocks)} comparison page(s)")
    else:
        print("[stage_2_5_comp] No comparisons generated (---COMPARISONS: 0---)")

    combined_response = response_25a
    if response_25a and response_25b:
        combined_response = response_25a + "\n" + response_25b
    elif response_25b:
        combined_response = response_25b

    return all_blocks, combined_response



