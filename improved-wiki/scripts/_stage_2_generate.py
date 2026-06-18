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

__all__ = ["stage_2_5_review_suggestions", "build_per_chunk_gen_prompt", "stage_2_per_chunk_generation", "stage_2_0_source_page", "stage_2_synthesis", "build_synthesis_prompt", "build_query_generation_prompt", "stage_2_3_query_generation", "build_comparison_disambiguation_prompt", "build_comparison_in_source_prompt", "stage_2_5_comparison_generation"]

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
    for c in concepts:
        if isinstance(c, dict):
            name = c.get("name", "")
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            # Mark if this concept was already covered by a prior chunk
            slug = name.lower().replace(" ", "-").replace("/", "-")
            already = " [ALREADY COVERED — SKIP]" if slug in generated_slugs else ""
            concept_lines.append(f"  - {name} [{imp}]: {defn}{already}")
            if not already:
                for d in details[:3]:
                    concept_lines.append(f"      • {d}")

    entity_lines = []
    for e in entities:
        if isinstance(e, dict):
            name = e.get("name", "")
            role = e.get("role", "")
            sig = e.get("significance", "")
            slug = name.lower().replace(" ", "-").replace("/", "-")
            already = " [ALREADY COVERED — SKIP]" if slug in generated_slugs else ""
            entity_lines.append(f"  - {name} ({role}): {sig}{already}")

    concept_str = "\n".join(concept_lines[:100]) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines[:30]) if entity_lines else "(none)"

    generated_str = "\n".join(f"  - {s}" for s in generated_slugs) if generated_slugs else "(none yet — you are the first chunk)"

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

# Existing wiki pages (avoid duplicate slugs):
{', '.join(existing_slugs[:100])}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/concepts/...`
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.
- Use [[wikilink]] with FULL filename stem to link to pages from previous chunks
- ⚠️ NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
- Math: $inline$ $$display$$

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


def stage_2_per_chunk_generation(
    chunk_analyses: list[dict],
    chunks: list[str],
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    max_chunk_concurrent: int = 4,
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Stage 2 (per-chunk): Generate FILE blocks SEQUENTIALLY.

    NashSU parity: each chunk builds on pages already generated by previous
    chunks.  Later chunks know which concepts have already been covered, so
    they skip duplicates and use [[wikilinks]] to reference existing pages.

    The old dedup step is no longer needed — sequential execution with
    accumulated slug awareness prevents duplicates at the source.
    """
    chunk_total = len(chunk_analyses)
    print(f"[stage_2] Per-chunk generation: {chunk_total} chunks, sequential NashSU mode")

    all_file_blocks: list[tuple[str, str]] = []
    all_responses: list[str] = []
    generated_slugs: list[str] = []  # accumulates as chunks are processed
    gen_tokens = config.compute_max_tokens(8192)

    t0 = time.time()
    for idx in range(chunk_total):
        analysis = chunk_analyses[idx]
        chunk_text = chunks[idx] if idx < len(chunks) else ""
        concepts_n = len(analysis.get("concepts_found", []))
        entities_n = len(analysis.get("entities_found", []))
        if concepts_n == 0 and entities_n == 0:
            print(f"  [chunk {idx+1}/{chunk_total}] (no concepts or entities — skipped)")
            continue

        prompt = build_per_chunk_gen_prompt(
            analysis, chunk_text, idx, file_path, config, template,
            generated_slugs=generated_slugs,
        )
        chunk_ok = False
        for attempt in range(4):  # up to 4 attempts per chunk
            try:
                t_chunk = time.time()
                response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
                all_responses.append(response)
                blocks = parse_file_blocks(response)
                all_file_blocks.extend(blocks)
                dt = time.time() - t_chunk
                elapsed = time.time() - t0
                done_count = idx + 1
                eta = (elapsed / done_count) * (chunk_total - done_count) if done_count > 0 else 0
                pct = done_count * 100 // chunk_total
                tag = f" (retry #{attempt})" if attempt > 0 else ""
                print(f"  [chunk {idx+1}/{chunk_total}] {concepts_n}c/{entities_n}e → "
                      f"{len(blocks)} blocks ({len(response):,} chars, {stop_reason}) "
                      f"{dt:.0f}s [{pct}% ETA {eta:.0f}s]{tag}")
                # Extract slugs from generated blocks so the NEXT chunk knows
                for path, _content in blocks:
                    slug = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
                    if slug not in generated_slugs:
                        generated_slugs.append(slug)
                chunk_ok = True
                break
            except Exception as e:
                if attempt < 3 and _is_retryable_exception(e):
                    wait = _retry_jitter(2.0, attempt)
                    err_label = type(e).__name__
                    print(f"  [chunk {idx+1}/{chunk_total}] {err_label} on attempt {attempt+1}/4"
                          f" — retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                print(f"  [chunk {idx+1}/{chunk_total}] ❌ {e}")
                break  # non-retryable or exhausted — skip this chunk

    # Generate source page from global digest
    source_rel = f"sources/{file_path.relative_to(config.raw_root).with_suffix('.md')}"
    source_prompt = f"""# Role
Generate a source page for this book from the global digest.

# Global Digest
```yaml
{json.dumps(global_digest, ensure_ascii=False, indent=2)[:5000]}
```

# Concepts generated ({len(all_file_blocks)} pages)
{', '.join(Path(p).stem for p, _ in all_file_blocks[:60])}

# Output Format — EXACT
---FILE:wiki/{source_rel}---
(frontmatter type:source + content)
---END FILE---

START IMMEDIATELY with ---FILE:... No preamble.
"""
    try:
        src_response, _ = call_anthropic_protocol(source_prompt, config, max_tokens=8192)
        all_responses.append(src_response)
        src_blocks = parse_file_blocks(src_response)
        all_file_blocks.extend(src_blocks)
    except Exception as e:
        print(f"  [stage_2] Source page generation failed: {e}")

    combined = "\n".join(all_responses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
    source_blocks = [b for b in all_file_blocks if "sources/" in b[0]]

    print(f"[stage_2] Done — {chunk_total} chunks in {time.time()-t0:.0f}s, "
          f"{len(all_file_blocks)} file blocks ({len(concept_blocks)} concepts, "
          f"{len(entity_blocks)} entities, {len(source_blocks)} source)")

    # Build analysis for cache
    unique_concepts, _ = _extract_concept_entity_names(chunk_analyses)
    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "source_generated": len(source_blocks) > 0,
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": chunk_total,
        "method": "per-chunk-sequential",
    }
    return analysis, combined, all_file_blocks



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
        total = len(images)
        for img in sorted(images, key=lambda x: (x["page"], x.get("img_idx_in_page", 0)))[:60]:
            cap_path = media_dir / (img["filename"] + ".caption.txt")
            cap = cap_path.read_text(encoding="utf-8").strip()[:70] if cap_path.exists() else ""
            sample_lines.append(f"  p{img['page']} `{img['filename']}`: {cap}")
            if cap:
                captioned += 1
    else:
        # Loose files (minerU)
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
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


def _classify_concepts_by_importance(chunk_analyses: list[dict]) -> dict[str, list[str]]:
    """Classify deduplicated concepts by importance: core / supporting / mentioned.

    Returns dict with keys 'core', 'supporting', 'mentioned', each a list of names.
    A concept appearing in multiple chunks takes the highest importance seen.
    """
    seen: dict[str, str] = {}  # name → importance (highest wins)
    importance_rank = {"core": 3, "supporting": 2, "mentioned": 1}

    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            imp = c.get("importance", "mentioned")
            if name not in seen or importance_rank.get(imp, 0) > importance_rank.get(seen[name], 0):
                seen[name] = imp

    # Normalize importance to handle LLM typos (e.g., "supported" → "supporting")
    _imp_norm: dict[str, str] = {}
    for raw in ["core", "supporting", "mentioned"]:
        _imp_norm[raw] = raw
    # Map common LLM typos
    _imp_norm["supported"] = "supporting"
    _imp_norm["major"] = "core"
    _imp_norm["primary"] = "core"
    _imp_norm["minor"] = "mentioned"
    _imp_norm["reference"] = "mentioned"

    result: dict[str, list[str]] = {"core": [], "supporting": [], "mentioned": []}
    for name, imp in seen.items():
        imp_normalized = _imp_norm.get(imp, "mentioned")  # default to mentioned
        result[imp_normalized].append(name)
    # Sort each list alphabetically
    for imp in result:
        result[imp].sort()
    return result


# Coverage targets by importance level (NashSU-aligned: not every concept needs a page).
# "mentioned" concepts are typically covered inline in other pages.
COVERAGE_TARGETS = {
    "core": 0.80,        # Core concepts should have dedicated pages
    "supporting": 0.50,  # Supporting concepts should mostly be covered
    "mentioned": 0.20,   # Mentioned can be inline — low bar to catch egregious gaps
}


def _normalize_for_matching(s: str) -> str:
    """Normalize a string for fuzzy concept-to-page matching.

    Strips common prefixes, removes punctuation, and collapses whitespace
    so that "Buck Converter" matches "buck-converter-power-electronics".
    """
    import re as _re
    # Remove wiki/ path prefix and common subdirs
    s = _re.sub(r'^(wiki/)?(concepts|sources|entities)/', '', s)
    s = s.replace('.md', '')
    # Replace delimiters with spaces, then collapse
    s = s.replace('_', ' ').replace('-', ' ').replace('/', ' ')
    # Lowercase and remove all non-alphanumeric except spaces
    s = _re.sub(r'[^a-z0-9一-鿿 ]', '', s.lower())
    # Collapse multiple spaces
    s = _re.sub(r'\s+', ' ', s).strip()
    return s


def _concept_matches_page(concept_name: str, page_path: str) -> bool:
    """Check if a concept name matches a generated page path.

    Uses token-level matching: the concept name's tokens should all appear
    in the page path, in order (though not necessarily adjacent).
    """
    c_tokens = _normalize_for_matching(concept_name).split()
    p_norm = _normalize_for_matching(page_path)
    if not c_tokens:
        return False
    # All concept tokens must appear in the page path in order
    pos = 0
    for token in c_tokens:
        idx = p_norm.find(token, pos)
        if idx == -1:
            return False
        pos = idx + len(token)
    return True


def _compute_uncovered_concepts(
    unique_concepts: list[str], file_blocks: list[tuple[str, str]],
) -> list[str]:
    """Return concepts from the master list that have no corresponding FILE block."""
    uncovered = []
    for c in unique_concepts:
        if not any(_concept_matches_page(c, path) for path, _ in file_blocks):
            uncovered.append(c)
    return uncovered


def build_synthesis_prompt(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    round_num: int = 1,
    prior_response_tail: str = "",
    uncovered_concepts: list[str] | None = None,
    collision_warning: str = "",
    current_domain: str = "general",
) -> str:
    """Build the prompt for Stage 2: Synthesis.

    Round 1: full context with mandatory coverage targets.
    Round 2+: gap-aware continuation listing remaining uncovered concepts.
    """
    unique_concepts, unique_entities = _extract_concept_entity_names(chunk_analyses)
    classified = _classify_concepts_by_importance(chunk_analyses)
    core_concepts = classified.get("core", [])
    supporting_concepts = classified.get("supporting", [])
    mentioned_concepts = classified.get("mentioned", [])

    if round_num == 1:
        # Round 1: full context
        digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
        if len(digest_str) > 5000:
            digest_str = digest_str[:5000] + "\n... (truncated)"

        existing_slugs = list_existing_slugs(config)

        # source_rel mirrors raw/ directory structure (e.g. "book/High Speed Digital Design")
        try:
            source_rel = str(file_path.relative_to(config.raw_root).with_suffix(""))
        except ValueError:
            source_rel = file_path.stem

        template_section = ""
        if template:
            template_trimmed = template[:2500]
            template_section = f"""
# Document Type Instructions
<template>
{template_trimmed}
</template>
"""

        # Show concepts by importance tier
        core_str = ', '.join(core_concepts[:50])
        supp_str = ', '.join(supporting_concepts[:50])
        ment_str = ', '.join(mentioned_concepts[:30])
        concept_list_str = (
            f"**CORE concepts ({len(core_concepts)} — MUST generate ALL):**\n{core_str}\n\n"
            f"**SUPPORTING concepts ({len(supporting_concepts)} — generate at least 60%):**\n{supp_str}"
        )
        if mentioned_concepts:
            concept_list_str += f"\n\n**MENTIONED concepts ({len(mentioned_concepts)} — can cover inline):**\n{ment_str}"

        entity_list_str = ', '.join(unique_entities[:60])
        if len(unique_entities) > 60:
            entity_list_str += f"\n... and {len(unique_entities) - 60} more"

        return f"""# Role
You are maintaining a Karpathy-pattern knowledge base wiki.
{template_section}
# Current Domain
This source belongs to the **{current_domain}** domain. Tag all generated concept pages with `domain: {current_domain}` in their frontmatter.

{collision_warning}
# Global Digest
```yaml
{digest_str}
```

# Concepts to cover ({len(unique_concepts)} total — ALL must be generated)
{concept_list_str}

# Entities to cover ({len(unique_entities)} total)
{entity_list_str}

# Existing wiki pages (avoid duplicates)
{', '.join(existing_slugs[:200])}

# Source
- Book: {file_path.stem}

# Extracted Images
{_build_image_reference_section(file_path, config)}
# Task
The source page has already been generated separately. Now create:
1. Concept pages at wiki/concepts/<slug>.md for EVERY concept in the list above
2. Entity pages at wiki/entities/<slug>.md for key entities

**Every concept page frontmatter MUST include: `domain: {current_domain}`**

Include relevant images in pages using Markdown syntax: ![](media/<stem>/<filename>)

# Output Format — EXACT
Every page MUST be wrapped in delimiters:
```
---FILE:wiki/sources/{source_rel}.md---
(frontmatter + content)
---END FILE---
---FILE:wiki/concepts/<slug>.md---
(frontmatter + content)
---END FILE---
```

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/sources/...`
- Do NOT write any preamble, introduction, analysis, table of contents, or
  commentary before the first FILE block. The parser IGNORES everything outside
  ---FILE:...---END FILE--- blocks. Every token before the first FILE block is WASTED.
- Use [[wikilink]] with FULL filename stem
- ⚠️ NEVER use `/` in filenames (macOS rejects it). Replace "/" with "-" in slugs
- Math: $inline$ $$display$$
- **MANDATORY COVERAGE (importance-weighted)**:
  - CORE concepts ({len(core_concepts)}): Generate a dedicated page for EVERY one. Target: {int(COVERAGE_TARGETS['core']*100)}%.
  - SUPPORTING concepts ({len(supporting_concepts)}): Generate pages for at least {int(COVERAGE_TARGETS['supporting']*100)}%.
  - MENTIONED concepts ({len(mentioned_concepts)}): Can be covered inline in other pages.
  - Focus your effort on depth for CORE, breadth for SUPPORTING.
- Do NOT stop after a few pages — continuation rounds will let you finish.
- If you reach the token limit, you'll be continued. Do not stop early.
"""
    else:
        # Round 2+: gap-aware continuation
        uncovered = uncovered_concepts or []
        uncovered_str = ', '.join(uncovered[:80]) if uncovered else '(all concepts from previous rounds covered — continue with entities and remaining details)'
        # Stronger directive if previous round produced 0 blocks (likely all preamble)
        zero_block_warning = ""
        if prior_response_tail and "---FILE:" not in prior_response_tail[-5000:]:
            zero_block_warning = (
                "\n# ⚠️  PREVIOUS ROUND HAD ZERO FILE BLOCKS\n"
                "Your last response contained NO ---FILE:...---END FILE--- blocks. "
                "The parser IGNORES all text outside these delimiters. "
                "START IMMEDIATELY with ---FILE:wiki/concepts/<slug>.md---. "
                "Do NOT write any preamble, analysis, or commentary.\n"
            )
        return f"""# Continue Generation (Round {round_num})
{zero_block_warning}
# Remaining concepts that STILL need pages ({len(uncovered)} remaining of {len(unique_concepts)} total):
{uncovered_str}

# Your previous output ended with:
```
{prior_response_tail[-2000:]}
```

Generate wiki pages using the EXACT format: ---FILE:wiki/<path>.md---...---END FILE---
START IMMEDIATELY with the first FILE block. No preamble.
Focus on the UNCOVERED concepts listed above. Do NOT repeat previous pages.
"""


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
    title = book_meta.get("title", file_path.stem)
    authors = book_meta.get("authors", [])
    year = book_meta.get("year", "")
    publisher = book_meta.get("publisher", "")

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
Write a comprehensive source page at wiki/sources/{source_rel}.md.

**Required structure:**

```
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
1. **Chapter Title:** list ALL key topics covered — aim for 5-15 items, comma-separated. Include specific component names, formulas, design methods, and techniques.

Example:
1. **DC-DC Converters:** buck (step-down), boost (step-up), buck-boost, continuous vs discontinuous conduction mode, voltage-mode control, efficiency analysis, PWM, synchronous rectification.

## Key Takeaways

The 5-10 most important claims, formulas, design rules, or conclusions. Each ONE sentence, actionable.
```

# Instructions
- The frontmatter MUST be exactly as shown above with real data from the digest. Do NOT duplicate the title as an H1 heading.
- ⚠️ CRITICAL: DO NOT wrap the YAML frontmatter in ```yaml fences. The first line MUST be `---`, the frontmatter ends with `---`, then the body follows immediately. No code blocks anywhere.
- ⚠️ The source page MUST contain ONLY these 3 sections: ## Book Summary, ## Table of Contents & Key Concepts, ## Key Takeaways. Do NOT add extra sections (no 核心概念 list, no 关键实体 list, no 相关器件, no 关联知识点, no 来源说明). Link to concept/entity pages with [[wikilinks]] instead.
- Chapter outline: list ALL key topics per chapter (aim for 5-15 items). Be comprehensive — this is the wiki's authoritative reference for what the book covers.
- Key Takeaways: extract the most impactful claims from the digest's key_claims
- tags: Generate 3-8 relevant tags from the book's content (e.g. [dc-dc-converter, power-electronics, magnetics]). Do NOT leave tags: [] empty.
- related: Link to 2-5 related wiki pages by slug (e.g. [power-electronics, buck-converter])
- Use [[wikilink]] syntax to link to concept pages (slugs should be concept-name-slug format)
- The response MUST start with `---` (three dashes on the first line) — NO preamble, NO ``` fences, NO commentary
- Math: $inline$ $$display$$
"""

    gen_tokens = config.compute_max_tokens(8192)
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
    if verbose:
        print(f"[stage_2_0] Source page generated ({len(response):,} chars, stop={stop_reason})")
    else:
        print(f"[stage_2_0] Source page ready ({len(response):,} chars)")

    return response, stop_reason


def stage_2_synthesis(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Stage 2: Multi-round generation with coverage enforcement.

    Round 1: full context with mandatory coverage target.
    Rounds 2-8: gap-aware continuation listing uncovered concepts.
    After all rounds: gap-fill check — if < 90% coverage, do targeted rounds.
    """
    max_rounds = 8
    gen_tokens = config.compute_max_tokens(16384)
    all_responses: list[str] = []
    prior_tail = ""

    unique_concepts, _ = _extract_concept_entity_names(chunk_analyses)
    classified = _classify_concepts_by_importance(chunk_analyses)
    core_concepts = classified.get("core", [])
    supporting_concepts = classified.get("supporting", [])
    mentioned_concepts = classified.get("mentioned", [])
    target_count = len(core_concepts) + len(supporting_concepts)  # primary coverage target

    # Domain detection & slug collision check (Plan B: disambiguation)
    current_domain = _detect_domain(file_path, template, global_digest)
    existing_domains = _list_existing_concepts_with_domains(config)
    # Generate tentative slugs for all concepts to find collisions
    all_concept_names = unique_concepts
    collisions = _find_slug_collisions(all_concept_names, existing_domains, current_domain)
    collision_warning = _build_collision_warning(collisions, existing_domains)
    if collisions:
        print(f"[stage_2] ⚠️  Domain: {current_domain} — {len(collisions)} slug collisions across domains: "
              f"{', '.join(s for s, _, _ in collisions[:8])}{'...' if len(collisions) > 8 else ''}")
    else:
        print(f"[stage_2] Domain: {current_domain} — no cross-domain slug collisions detected")

    print(f"[stage_2] Concept importance: {len(core_concepts)} core, "
          f"{len(supporting_concepts)} supporting, {len(mentioned_concepts)} mentioned "
          f"(coverage targets: core≥{COVERAGE_TARGETS['core']:.0%}, "
          f"supporting≥{COVERAGE_TARGETS['supporting']:.0%}, mentioned≥{COVERAGE_TARGETS['mentioned']:.0%})")
    uncovered: list[str] = []

    for round_num in range(1, max_rounds + 1):
        # Compute uncovered concepts from what's been generated so far
        if round_num > 1:
            combined_so_far = "\n".join(all_responses)
            blocks_so_far = parse_file_blocks(combined_so_far)
            uncovered = _compute_uncovered_concepts(unique_concepts, blocks_so_far)

        print(f"[stage_2] Round {round_num}/{max_rounds} — building prompt...", flush=True)
        prompt = build_synthesis_prompt(
            global_digest, chunk_analyses, file_path, config, template,
            round_num=round_num, prior_response_tail=prior_tail,
            uncovered_concepts=uncovered,
            collision_warning=collision_warning if round_num == 1 else "",
            current_domain=current_domain,
        )
        prompt_len = len(prompt)
        print(f"[stage_2] Round {round_num} — prompt {prompt_len:,} chars, "
              f"{len(uncovered)} uncovered concepts, calling LLM (max_tokens={gen_tokens})...", flush=True)
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens)
        all_responses.append(response)
        blocks_this_round = len(parse_file_blocks(response))
        print(f"[stage_2] Round {round_num} — {len(response):,} chars, "
              f"{blocks_this_round} blocks, stop_reason={stop_reason}", flush=True)

        # Detect: LLM generated preamble instead of FILE blocks (wasted round)
        if blocks_this_round == 0 and len(response) > 5000:
            # LLM produced substantial text but no FILE blocks — likely preamble
            preamble_len = len(response.split("---FILE:")[0]) if "---FILE:" in response else len(response)
            print(f"[stage_2] Round {round_num} — ⚠️  {preamble_len:,} chars of preamble with 0 FILE blocks. "
                  f"Adding stronger directive for next round.")

        prior_tail = response[-3000:]

        if stop_reason == "end_turn":
            # Check importance-weighted coverage before accepting end_turn
            combined = "\n".join(all_responses)
            current_blocks = parse_file_blocks(combined)
            concept_paths = [p for p, _ in current_blocks if "concepts/" in p]

            # Compute coverage by importance tier (using shared matching function)
            core_covered = len([c for c in core_concepts if any(
                _concept_matches_page(c, p) for p in concept_paths)])
            supp_covered = len([c for c in supporting_concepts if any(
                _concept_matches_page(c, p) for p in concept_paths)])
            ment_covered = len([c for c in mentioned_concepts if any(
                _concept_matches_page(c, p) for p in concept_paths)])

            core_pct = core_covered / max(len(core_concepts), 1)
            supp_pct = supp_covered / max(len(supporting_concepts), 1)
            ment_pct = ment_covered / max(len(mentioned_concepts), 1)

            core_ok = core_pct >= COVERAGE_TARGETS["core"]
            supp_ok = supp_pct >= COVERAGE_TARGETS["supporting"]
            ment_ok = ment_pct >= COVERAGE_TARGETS["mentioned"]

            if core_ok and supp_ok:
                print(f"[stage_2] Round {round_num} — end_turn, coverage met: "
                      f"core={core_pct:.0%} supp={supp_pct:.0%} ment={ment_pct:.0%}. Done.")
                break
            else:
                missing = []
                if not core_ok: missing.append(f"core={core_pct:.0%} (need {COVERAGE_TARGETS['core']:.0%})")
                if not supp_ok: missing.append(f"supp={supp_pct:.0%} (need {COVERAGE_TARGETS['supporting']:.0%})")
                print(f"[stage_2] Round {round_num} — end_turn but coverage insufficient: {', '.join(missing)}. Continuing...")
        elif stop_reason == "max_tokens":
            print(f"[stage_2] Round {round_num} hit max_tokens — continuing...")
        else:
            print(f"[stage_2] Round {round_num} stop_reason={stop_reason}, continuing...")

    combined = "\n".join(all_responses)
    file_blocks = parse_file_blocks(combined)
    concept_blocks = [b for b in file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in file_blocks if "entities/" in b[0]]
    source_blocks = [b for b in file_blocks if "sources/" in b[0]]
    overall_pct = len(concept_blocks) / max(len(unique_concepts), 1)

    # Final importance-weighted coverage
    concept_paths = [p for p, _ in concept_blocks]
    core_final = len([c for c in core_concepts if any(
        _concept_matches_page(c, p) for p in concept_paths)])
    supp_final = len([c for c in supporting_concepts if any(
        _concept_matches_page(c, p) for p in concept_paths)])

    print(f"[stage_2] Done — {len(all_responses)} rounds, {len(combined):,} chars total, "
          f"{len(file_blocks)} file blocks ({len(concept_blocks)} concepts, {len(entity_blocks)} entities, "
          f"{len(source_blocks)} source), "
          f"coverage: core={core_final}/{len(core_concepts)} "
          f"supp={supp_final}/{len(supporting_concepts)} "
          f"overall={overall_pct:.0%}")

    if verbose and file_blocks:
        for p, content in file_blocks:
            print(f"  block: {p} ({len(content)} chars)")

    # Build analysis from global_digest + chunk summaries (for cache/logging)
    all_concepts: list[str] = []
    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            all_concepts.append(name)

    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_core": len(core_concepts),
        "concepts_supporting": len(supporting_concepts),
        "concepts_mentioned": len(mentioned_concepts),
        "concepts_generated": len(concept_blocks),
        "coverage_core": round(core_final / max(len(core_concepts), 1), 2),
        "coverage_supporting": round(supp_final / max(len(supporting_concepts), 1), 2),
        "coverage_pct": round(overall_pct, 2),
        "entities_generated": len(entity_blocks),
        "source_generated": len(source_blocks) > 0,
        "total_rounds": len(all_responses),
        "stop_reason": stop_reason,
    }
    return analysis, combined, file_blocks


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



