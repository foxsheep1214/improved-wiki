"""Phase 3: Write pages to disk + aggregate repair.

This module holds Stage 3.1 (write, incl. NashSU three-layer page merge for
same-slug collisions) and 3.5 (aggregate repair + cache). Sibling modules:
_stage_3_2_inject_images.py (image injection), _stage_3_4_review.py (content
review), and _stage_3_7_embed.py (embeddings, runs from ingest.py post-ingest).
Stage 3.6 (quality scoring) was removed 2026-06-25 (NashSU alignment).
Stage 3.3 (cross-domain slug collision review) was removed 2026-06-26:
NashSU has no `domain` field and no disambiguation concept — same-slug
pages are merged at Stage 3.1 write time instead of renamed/warned.

Extracted as separate module 2026-06-18. Refactored 2026-06-21 for explicit stage naming.
"""
from __future__ import annotations

import json, os, re, sys, time
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import (
    Config,
    heartbeat as _heartbeat, file_tag as _file_tag,
    stage_begin as _stage_begin, stage_end as _stage_end,
    llm_call_progress as _llm_call_progress, llm_call_done as _llm_call_done,
    load_cache, save_cache, detect_domain, list_existing_slugs,
    parse_yaml_block, parse_file_blocks,
    is_safe_ingest_path, _WINDOWS_RESERVED, _ILLEGAL_CHARS_RE,
    source_slug_from_raw_path,
)
from _llm_api import call_anthropic_protocol
from _frontmatter_array import parse_frontmatter_array

__all__ = [
    "stage_3_1_write_wiki_file",  # Stage 3.1
    "stage_3_5_aggregate_repair", # Stage 3.5
]

# ---------- File writing ----------

def _extract_fm_field(content: str, field_name: str) -> str:
    """Extract a frontmatter YAML field from markdown content."""
    m = re.search(rf'^{field_name}:\s*(.+)$', content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _stage_3_1_contains_cjk(text: str) -> bool:
    """Check if text contains CJK characters (NashSU parity: containsCjk)."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
            0xF900 <= cp <= 0xFAFF or    # CJK Compatibility
            0x3040 <= cp <= 0x309F or    # Hiragana
            0x30A0 <= cp <= 0x30FF or    # Katakana
            0xAC00 <= cp <= 0xD7AF):     # Hangul
            return True
    return False


def _stage_3_1_make_cjk_slug(title: str) -> str:
    """Create a readable CJK slug from a page title.

    Rules (NashSU parity):
    - Keep CJK characters, alphanumeric, spaces, hyphens
    - Replace special chars with hyphens
    - Collapse multiple hyphens
    - Trim to 120 chars
    - Preserve proper nouns and technical identifiers in original form
    """
    # Keep CJK, alphanumeric, spaces, hyphens, parentheses (for units like "Cauer/Foster")
    slug = re.sub(r'[^\w\s\-\(\)一-鿿㐀-䶿豈-﫿぀-ゟ゠-ヿ가-힯]', '-', title, flags=re.UNICODE)
    # Collapse whitespace and hyphens
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-{2,}', '-', slug)
    slug = slug.strip('-')
    # Replace problematic chars for macOS filenames
    slug = slug.replace('/', '-').replace(':', '-').replace('\\', '-')
    if len(slug) > 120:
        slug = slug[:120].rstrip('-')
    return slug if slug else ""


def _stage_3_1_auto_correct_wiki_path(rel_path: str, content: str, config: Config | None = None) -> str | None:
    """Auto-correct malformed wiki paths from LLM output.

    LLM sometimes outputs:
      wiki/ConceptName        → concepts/ConceptName.md
      wiki/Book Title.md      → sources/Book Title.md
      wiki/Some Entity        → entities/Some Entity.md

    Same-slug collisions are NOT resolved here — NashSU has no `domain` field
    and no disambiguation concept; when a concept path already exists on disk,
    Stage 3.1 write merges old + new (three-layer page merge, see
    `stage_3_1_write_wiki_file`). Former "Plan B" cross-domain rename removed
    2026-06-26 (NashSU alignment).

    Returns corrected path (relative to wiki/ dir, NO "wiki/" prefix) or None if uncorrectable.
    """
    basename = Path(rel_path).name
    stem = Path(rel_path).stem

    # 2026-06-15: macOS/Linux 文件名不能含 /，LLM 可能在 slug 中输出 /
    # 例如 [[热仿真(Cauer/Foster模型)]] → slug "热仿真(Cauer/Foster模型)"
    stem = stem.replace("/", "_")

    # 2026-06-15: agent sometimes outputs paths without .md extension
    if not rel_path.endswith(".md"):
        rel_path += ".md"

    # Read frontmatter type from content (used by all cases below)
    fm_type = _extract_fm_field(content, "type") or None

    slug = stem

    # ── CJK slug rewriting (NashSU parity: rewriteIngestPathFromTitleForTargetLanguage) ──
    fm_title = _extract_fm_field(content, "title").strip("\"'") or None
    if fm_title and _stage_3_1_contains_cjk(fm_title) and not _stage_3_1_contains_cjk(slug):
        cjk_slug = _stage_3_1_make_cjk_slug(fm_title)
        if cjk_slug and _stage_3_1_contains_cjk(cjk_slug):
            print(f"  ⚠️  [cjk] Slug '{slug}' → '{cjk_slug}' (CJK title detected)")
            slug = cjk_slug

    # Case: bare filename (no path prefix) — LLM forgot wiki/concepts/ prefix
    # This is the most common correction: "ConceptName.md" → "concepts/ConceptName.md"
    if "/" not in rel_path:
        if fm_type == "source":
            return f"sources/{slug}.md"
        elif fm_type == "entity":
            return f"entities/{slug}.md"
        else:
            # Default: treat as concept (vast majority of pages)
            return f"concepts/{slug}.md"

    # Strip wiki/ prefix if present (from LLM or legacy format)
    if "/" in rel_path:
        if rel_path.startswith("wiki/"):
            rel_path = rel_path[len("wiki/"):]
        parts = rel_path.split("/")
        if len(parts) >= 2:
            # Case: 4+ part path — LLM added extra nesting
            # wiki/sources/Book/Title → sources/book/Title.md (keep type subdir, aligns with raw/)
            # wiki/concepts/topic/Title → concepts/Title.md (flatten — concepts have no subdirs)
            # wiki/entities/category/Name → entities/Name.md (flatten — entities have no subdirs)
            if len(parts) >= 4:
                dir_name = parts[1]  # "sources" or "concepts" or "entities"
                extra = parts[2]     # e.g., "book", "topic", "category"
                actual_slug = parts[-1].replace(".md", "")
                # Use frontmatter type if available, else infer from dir_name
                target_dir = dir_name if dir_name in ("sources", "concepts", "entities") else "concepts"
                if fm_type == "source":
                    target_dir = "sources"
                elif fm_type == "concept":
                    target_dir = "concepts"
                elif fm_type == "entity":
                    target_dir = "entities"
                # Source pages keep type subdirectory for raw/ alignment
                if target_dir == "sources":
                    return f"sources/{extra}/{actual_slug}.md"
                else:
                    # Concepts and entities: flatten — no subdirectories
                    return f"{target_dir}/{actual_slug}.md"

            # Case: 3-part path like wiki/sources/ConceptName
            # Check frontmatter type vs directory mismatch, then correct
            if len(parts) == 3:
                dir_name = parts[1]
                _type_to_dir = {"source": "sources", "concept": "concepts", "entity": "entities"}
                if fm_type and fm_type in _type_to_dir and _type_to_dir[fm_type] != dir_name:
                    return f"{_type_to_dir[fm_type]}/{slug}.md"
                # No frontmatter + in sources/ but not source-like → concepts
                if dir_name == "sources" and not ("## " in content and "sources:" in content.lower()):
                    return f"concepts/{slug}.md"

            # Use fm_type from outer scope (already parsed at top of function)
            if fm_type == "source":
                return f"sources/{slug}.md"
            elif fm_type == "concept":
                return f"concepts/{slug}.md"
            elif fm_type == "entity":
                return f"entities/{slug}.md"

            # Heuristic fallback: check content for source-like patterns
            if "## " in content and ("sources:" in content.lower() or "## Source" in content):
                return f"sources/{slug}.md"
            # Default: treat as concept (most common case for Chinese wiki)
            return f"concepts/{slug}.md"

    # ── Schema routing validation (NashSU parity: validateWikiPageRouting) ──
    # After all corrections, verify that frontmatter type matches directory.
    # This catches LLM writing type:concept to entities/ or vice versa.
    if rel_path and fm_type:
        _TYPE_TO_DIR = {
            "source": "sources", "concept": "concepts", "entity": "entities",
            "query": "queries", "comparison": "comparisons",
            "synthesis": "synthesis", "finding": "findings",
            "thesis": "thesis", "methodology": "methodology",
        }
        expected_dir = _TYPE_TO_DIR.get(fm_type)
        if expected_dir:
            actual_dir = rel_path.split("/")[0] if "/" in rel_path else ""
            if actual_dir and actual_dir != expected_dir:
                print(f"  ⚠️  [schema] Type '{fm_type}' in '{actual_dir}/' → routing to '{expected_dir}/'")
                if "/" in rel_path:
                    rel_path = f"{expected_dir}/{rel_path.split('/', 1)[1]}"
                else:
                    rel_path = f"{expected_dir}/{rel_path}"
            elif not actual_dir:
                rel_path = f"{expected_dir}/{rel_path}"

    return None


def _stage_3_1_wiki_path_for_source(raw_file: Path, config: Config) -> Path:
    """Return wiki/sources/<raw-rel-path>.md mirroring raw/ directory structure.

    Delegates to ``source_slug_from_raw_path()`` in _core.py for canonical
    derivation, falling back to the filename-only fallback for backward compat.
    """
    result = source_slug_from_raw_path(raw_file, config.wiki_root)
    if result is not None:
        return result
    # Fallback: file not under raw/ — use filename only (backward compat)
    return config.wiki_dir / "sources" / raw_file.with_suffix(".md").name


def _stage_3_1_sanitize_ingested_content(content: str) -> str:
    """NashSU parity (ingest-sanitize.ts): fix common LLM formatting errors.

    Delegates to _ingest_sanitize for the full 4-pattern port: outer ```yaml
    fence strip, ``frontmatter:`` prefix strip, missing opening fence repair,
    and wikilink-list-in-frontmatter repair. See _ingest_sanitize.py.
    """
    from _ingest_sanitize import sanitize_ingested_file_content
    return sanitize_ingested_file_content(content)


def _stage_3_1_backup_existing_page(path: Path, config: Config) -> None:
    """NashSU parity (ingest.ts L2575-2584): snapshot existing page before overwrite."""
    if not path.exists():
        return
    history_dir = config.runtime_dir / "page-history"
    history_dir.mkdir(parents=True, exist_ok=True)
    safe_name = str(path.relative_to(config.wiki_dir)).replace("/", "_")
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = history_dir / f"{ts}_{safe_name}"
    backup_path.write_text(path.read_text(encoding="utf-8"))
    print(f"  [backup] {path.name} → page-history/{backup_path.name}")


# ── Frontmatter: delegate to canonical _frontmatter.py (NashSU frontmatter.ts + page-merge.ts pattern) ──
from _frontmatter import (
    parse_frontmatter,
    write_frontmatter,
    union_arrays,
    merge_page_content as _fm_merge_page_content,
    lock_fields,
    strip_embedded_images_section,
)

# TODO: migrate callers and remove — backward-compat aliases (internal use; not exported)
_parse_frontmatter = parse_frontmatter
_merge_frontmatter_arrays = union_arrays
_fmt_frontmatter = write_frontmatter


def _stage_3_1_merge_page_content(existing_text: str, new_text: str, config: Config) -> str:
    """NashSU 3-layer merge: delegates to _frontmatter.merge_page_content.

    Layers: array-union → LLM body merge → lock fields.
    Fallback: if bodies don't need merging or LLM fails, returns array-merged result.
    """

    def llm_merger(prev_content: str, merged_content: str, source_file: str) -> str:
        """LLM merge callback — called by _frontmatter when bodies differ."""
        # Strip the auto-injected ## Embedded Images section before truncating
        # for the prompt: it can be 50K+ chars (457 images) and is re-injected
        # by Stage 3.2 after this merge. Sending it to the LLM both wastes the
        # 3K-per-side prompt budget on image-table rows and inflates the body
        # the LLM tries to reproduce (bug 2026-06-25).
        old_body = strip_embedded_images_section(parse_frontmatter(prev_content)[1])
        new_body = strip_embedded_images_section(parse_frontmatter(merged_content)[1])
        prompt = f"""Merge two versions of a wiki page. Preserve ALL unique information from both.
Do NOT drop claims, entities, formulas, or references from either version.

# Existing page content
{old_body[:3000]}

# New content (from latest ingest)
{new_body[:3000]}

# Task
Output the merged page body (no frontmatter, no code fences).
The merged version should contain everything from both versions,
with duplicates consolidated and new information integrated.
"""
        response, _ = call_anthropic_protocol(prompt, config, max_tokens=4096)
        merged_body = response.strip()
        if len(merged_body) < 100:
            # No fallback: an empty/tiny LLM merge response means the main path
            # is not working — pause rather than silently returning array-merge
            # (which drops the existing body). Policy 2026-06-24.
            raise RuntimeError(
                f"LLM page-merge returned {len(merged_body)} chars (too short) — "
                f"no fallback. Fix the LLM provider and re-run."
            )
        return write_frontmatter(parse_frontmatter(merged_content)[0], merged_body)

    return _fm_merge_page_content(
        new_content=new_text,
        existing_content=existing_text if existing_text else None,
        merger_fn=llm_merger,
    )


def _stage_3_1_canonicalize_sources_field(content: str, canonical_source: str) -> str:
    """NashSU parity (ingest.ts L1298-1324): union-merge sources[] with dedup.

    Preserves existing sources from prior ingests. Only adds the canonical
    source if it's not already present (matched by full path or basename).
    Removes duplicate entries.
    """
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    # content[3] is the '\n' after the opening '---'; slicing from 3 (not 4)
    # carries that newline into fm, and re-serializing below then produces
    # '---\n\ntype:...' — a blank line after the fence that breaks YAML
    # parsing. Mirrors the fix already applied to
    # _stage_3_1_stamp_frontmatter_dates for the same off-by-one.
    fm = content[4:end]
    body = content[end + 4:]

    # Parse existing sources (quote-aware: a filename containing commas must
    # stay one element, not split on every comma). The old naive src_text.split(",")
    # broke sources like "raw/Book/Flexible Electronics, Volume 1...pdf" into
    # fragments and then re-appended the full path → a 4-item corrupted array.
    existing_sources: list[str] = parse_frontmatter_array(content, "sources")

    # Normalize canonical source for comparison
    canon_norm = canonical_source.lower().replace("\\", "/").rstrip("/")
    canon_base = Path(canon_norm).name.lower()

    # Check if canonical source already present (full path or basename match)
    already_present = False
    for s in existing_sources:
        sn = s.lower().replace("\\", "/").rstrip("/")
        if sn == canon_norm or Path(sn).name == canon_base:
            already_present = True
            break

    if not already_present:
        existing_sources.append(canonical_source)

    # Dedup (keep order, remove case-duplicates)
    seen: set[str] = set()
    deduped: list[str] = []
    for s in existing_sources:
        key = s.lower().replace("\\", "/").rstrip("/")
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    # Rebuild sources line
    items = ", ".join(f'"{s}"' for s in deduped)
    lines = fm.split("\n")
    new_lines = []
    for line in lines:
        if line.strip().startswith("sources:"):
            new_lines.append(f"sources: [{items}]")
        else:
            new_lines.append(line)
    return "---\n" + "\n".join(new_lines) + "\n---" + body


def _stage_3_1_stamp_frontmatter_dates(content: str, today: str) -> str:
    """NashSU parity (ingest.ts L1440-1468): stamp created/updated dates."""
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    # content[3] is the '\n' after the opening '---'; the frontmatter body
    # starts at index 4. Slicing [3:end] carried that newline and re-serialization
    # produced '---\n\ntype:...' (a blank line after the fence), breaking YAML
    # parsing — and stacking a blank line each time stamp ran twice.
    fm = content[4:end]
    body = content[end + 4:]
    lines = fm.split("\n")
    new_lines = []
    has_created = False
    has_updated = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("created:"):
            new_lines.append(f"created: {today}")
            has_created = True
        elif stripped.startswith("updated:"):
            new_lines.append(f"updated: {today}")
            has_updated = True
        else:
            new_lines.append(line)
    if not has_created:
        new_lines.append(f"created: {today}")
    if not has_updated:
        new_lines.append(f"updated: {today}")
    return "---\n" + "\n".join(new_lines) + "\n---" + body


def stage_3_1_write_wiki_file(path: Path, content: str, config: Config | None = None, merge: bool = False) -> None:
    content = _stage_3_1_sanitize_ingested_content(content)
    if config is not None:
        _stage_3_1_backup_existing_page(path, config)
        if merge and path.exists():
            existing = path.read_text(encoding="utf-8")
            content = _stage_3_1_merge_page_content(existing, content, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)


def stage_3_5_aggregate_repair(
    source_path: Path,
    raw_file: Path,
    analysis: dict,
    source_hash: str,
    extract_method: str,
    config: Config,
) -> list[str]:
    """NashSU Stage 2.6: update index.md (append), log.md (append), overview.md (LLM rewrite)."""
    files_written: list[str] = []

    # log.md
    log_path = config.wiki_dir / "log.md"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8")
    else:
        log_text = "# Log\n"
    raw_rel = raw_file.relative_to(config.raw_root)
    source_rel = source_path.relative_to(config.wiki_dir)
    entry = (
        f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} — INGEST\n"
        f"- Source: `raw/{raw_rel}`\n"
        f"- Source page: `wiki/{source_rel}`\n"
        f"- Hash: {source_hash[:16]}\n"
        f"- Method: {extract_method}\n"
    )
    log_text += entry
    stage_3_1_write_wiki_file(log_path, log_text, config)
    files_written.append(str(log_path.relative_to(config.wiki_root)))

    # index.md — append link to new source page
    index_path = config.wiki_dir / "index.md"
    if index_path.exists():
        index_text = index_path.read_text(encoding="utf-8")
    else:
        index_text = "# Index\n\n## Sources\n\n"
    new_link = f"- [[{source_path.stem}]]\n"
    if "## Sources" in index_text and new_link not in index_text:
        index_text = index_text.replace("## Sources\n", f"## Sources\n\n{new_link}", 1)
        stage_3_1_write_wiki_file(index_path, index_text, config)
        files_written.append(str(index_path.relative_to(config.wiki_root)))

    # overview.md — NashSU aggregate repair: LLM rewrite with existing content as context.
    # Unlike the ADL8113 incident, the LLM SEES the current overview and preserves it.
    overview_path = config.wiki_dir / "overview.md"
    if overview_path.exists():
        current_overview = overview_path.read_text(encoding="utf-8")
        # NashSU parity (ingest.ts L1281-1296): proportional safety caps.
        # Section cap = max(4K, 12% of context window) for both index and overview.
        _AGGREGATE_CAP = max(4096, int(config.source_budget * 0.12))
        OVERVIEW_MAX_CHARS = min(24000, _AGGREGATE_CAP)
        INDEX_MAX_CHARS = _AGGREGATE_CAP
        if len(current_overview) > OVERVIEW_MAX_CHARS:
            print(f"[stage 3.5] Overview too large ({len(current_overview)} > {OVERVIEW_MAX_CHARS}) — "
                  f"skipping LLM rewrite to avoid truncation")
            return files_written

        # Index size check (NashSU parity: isAggregateRepairSafe)
        if index_path.exists():
            index_size = index_path.stat().st_size
            if index_size > INDEX_MAX_CHARS:
                print(f"[stage 3.5] Index too large ({index_size} > {INDEX_MAX_CHARS}) — "
                      f"skipping aggregate repair to avoid context overflow")
                return files_written
        source_content = source_path.read_text(encoding="utf-8") if source_path.exists() else ""

        sources_lines: list[str] = []
        sources_dir = config.wiki_dir / "sources"
        if sources_dir.is_dir():
            for f in sorted(sources_dir.rglob("*.md"))[-10:]:
                text = f.read_text(encoding="utf-8")
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    body = text[end + 4:] if end != -1 else text
                else:
                    body = text
                sources_lines.append(f"### {f.stem}\n{body[:800]}")

        prompt = f"""You maintain the overview of a hardware knowledge base wiki.
Below is the CURRENT overview.md, followed by the newly ingested source page.
Rewrite overview.md to incorporate the new source into a comprehensive 2-5
paragraph overview of ALL topics now in the wiki. Preserve all existing claims
and source references; only add or refine based on the new source.

# Current overview.md
{current_overview}

# New source page: {source_path.stem}
{source_content[:3000]}

# Recent source pages (for context)
{chr(10).join(sources_lines[:8])}

# Task
Rewrite the COMPLETE overview.md. Output ONLY the new overview.md content
(starting with \"# Overview\"). Preserve the structure:
- ## Where we are (2-5 paragraph comprehensive overview of ALL topics)
- ## Strong Claims (well-supported by multiple sources)
- ## Weak Claims (single-source or speculative)
- ## Open Questions
- ## Sources (auto-populated list — keep existing entries, add new source link)

Do NOT change or remove existing Strong Claims / Weak Claims / Open Questions
unless the new source directly contradicts or answers them.
"""
        try:
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=4096)
            # NashSU parity: filter aggregate repair output — reject FILE blocks (ingest.ts L1216-1235)
            if "---FILE:" in response:
                print(f"[stage 3.5] LLM response contained FILE blocks — discarding")
            elif response.strip().startswith("#"):
                stage_3_1_write_wiki_file(overview_path, response.strip() + "\n", config)
                files_written.append(str(overview_path.relative_to(config.wiki_root)))
                print(f"[stage 3.5] Overview updated via LLM ({len(response)} chars, stop={stop_reason})")
            else:
                print(f"[stage 3.5] LLM overview response did not start with '# Overview' — skipping")
        except Exception as e:
            print(f"[stage 3.5] Overview LLM update failed: {e}")

    return files_written
