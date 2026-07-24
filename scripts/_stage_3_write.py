"""Phase 3: Write pages to disk + aggregate repair.

This module holds Stage 3.1 (write, incl. three-layer page merge for
same-slug collisions) and 3.5 (aggregate repair + cache). Sibling modules:
_stage_3_2_inject_images.py (image injection), _stage_3_4_review.py (content
review), and _stage_3_7_embed.py (embeddings, runs from ingest.py post-ingest).

Extracted as separate module 2026-06-18. Refactored 2026-06-21 for explicit stage naming.
"""
from __future__ import annotations

import re, time
from pathlib import Path

from _paths import atomic_write, WIKI_ARTIFACT_DIRS
from _page_ref import PageRef
from _config import Config
from _core import canonical_source_path
from _schema import (
    is_safe_ingest_path, _ILLEGAL_CHARS_RE,
    source_slug_from_raw_path, schema_route_dir,
)
from _llm_api import call_anthropic_protocol
from _frontmatter_array import parse_frontmatter_array, write_frontmatter_array
from _stage_1_1_scanned import _decode_html_entities

__all__ = [
    "stage_3_1_write_wiki_file",       # Stage 3.1
    "stage_3_1_build_slug_dirs",       # Stage 3.1 link normalizer (universe)
    "stage_3_1_normalize_page_links",  # Stage 3.1 link normalizer (per page)
    "stage_3_5_aggregate_repair",      # Stage 3.5
    "rebuild_index_deterministic",     # standalone recovery tool (rebuild_index.py)
]

# Fallback per-side cap on body text shown to the LLM page-merge prompt, used
# only when config has no target_chars (e.g. minimal test configs). The real
# cap is config.target_chars (see llm_merger below) — the same live-probed,
# context-aware budget the rest of the pipeline uses for "how much text is
# safe in one prompt". A fixed 24K (raised from a hardcoded 3000 that
# truncated normal pages mid-content) was itself found too small 2026-07-09:
# a comprehensive source page's body (~67 claims, ~38 entities) runs to ~65K
# chars, well past 24K, silently truncating the "new content" side of the
# merge before the LLM ever saw the sections beyond Table of Contents — the
# merge then fell back to the old page's stale Key Entities/Claims/etc.
# sections for the part it never received.
MERGE_PROMPT_BODY_CAP = 24000

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


def _stage_3_1_auto_correct_wiki_path(rel_path: str, content: str, config: Config | None = None,
                                      quiet: bool = False) -> str | None:
    """Auto-correct malformed wiki paths from LLM output.

    LLM sometimes outputs:
      wiki/ConceptName        → concepts/ConceptName.md
      wiki/Book Title.md      → sources/Book Title.md
      wiki/Some Entity        → entities/Some Entity.md

    Same-slug collisions are not resolved here — when a path already exists
    on disk, Stage 3.1 write merges old + new (three-layer page merge, see
    `stage_3_1_write_wiki_file`).

    Returns corrected path (relative to wiki/ dir, NO "wiki/" prefix) or None if uncorrectable.
    """
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
            if not quiet:
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

    # NOTE: type↔directory schema-routing validation is NOT done here. Every
    # branch above returns before this point (this function only fires for paths
    # whose top dir is NOT a valid subdir), so a schema check here would be dead
    # code AND base-type-only. Schema routing runs at the write boundary via
    # _stage_3_1_schema_route(), which consults the project's schema typeDirs.
    return None


def _stage_3_1_schema_route(rel_path: str, content: str,
                            routing: dict[str, str]) -> str:
    """Route a page to the directory its frontmatter ``type`` declares (schema
    typeDirs first, then the fixed base types) — NashSU ``validateWikiPageRouting``
    applied at write time.

    Unlike NashSU (which DROPS a misrouted page), the writer auto-corrects by
    MOVING it: lossless and consistent with the writer's no-silent-fallback
    policy and its existing path auto-correct. The frontmatter ``type`` is the
    source of truth; the file is placed in that type's folder. Returns the
    (possibly rewritten) wiki-relative path (NO ``wiki/`` prefix).

    Left unchanged: unknown/unroutable types (don't guess), already-correct
    pages, and ``sources/`` pages (they keep their raw-mirroring subdirectories).
    """
    fm_type = _extract_fm_field(content, "type").strip().strip('"').strip("'")
    target = schema_route_dir(fm_type, routing)
    if target is None:                   # unknown/unroutable type — leave it
        return rel_path                  # (NB: "" is a valid target = wiki root)
    norm = rel_path[len("wiki/"):] if rel_path.startswith("wiki/") else rel_path
    top = norm.split("/", 1)[0] if "/" in norm else ""
    if top == target:                    # already correctly routed (incl. root: ""=="")
        return rel_path
    if target == "sources" or top == "sources":
        return rel_path                  # source pages keep their subdir layout
    basename = norm.rsplit("/", 1)[-1] if "/" in norm else norm
    return basename if target == "" else f"{target}/{basename}"


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

    Also decodes stray HTML entities (2026-07-04, NOT NashSU parity — NashSU
    only ever runs decodeHtmlEntities on HTML-table-cell text during OCR
    conversion, never on LLM-generated page content). Observed pattern: an LLM
    occasionally writes an inequality/symbol as a literal entity instead of
    the real character (e.g. "q&lt;3.15 kW/m^2") when drafting a generated
    page — this repairs it at write time regardless of which stage or which
    calling agent produced the content.
    """
    from _ingest_sanitize import sanitize_ingested_file_content
    content = sanitize_ingested_file_content(content)
    return _decode_html_entities(content)


def _stage_3_1_backup_existing_page(path: Path, config: Config) -> None:
    """NashSU parity (ingest.ts L2575-2584): snapshot existing page before overwrite."""
    if not path.exists():
        return
    history_dir = config.runtime_dir / "page-history"
    history_dir.mkdir(parents=True, exist_ok=True)
    safe_name = str(path.relative_to(config.wiki_dir)).replace("/", "_")
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = history_dir / f"{ts}_{safe_name}"
    # Sequence suffix: same-second backups of the same page (e.g. merge +
    # enrich passes) must not overwrite each other.
    seq = 1
    while backup_path.exists():
        backup_path = history_dir / f"{ts}-{seq}_{safe_name}"
        seq += 1
    atomic_write(backup_path, path.read_text(encoding="utf-8"))
    print(f"  [backup] {path.name} → page-history/{backup_path.name}")


# ── Frontmatter: delegate to canonical _frontmatter.py (NashSU frontmatter.ts + page-merge.ts pattern) ──
from _frontmatter import (
    parse_frontmatter,
    write_frontmatter,
    merge_page_content as _fm_merge_page_content,
    lock_fields,
    strip_embedded_images_section,
)


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
        # Show each body up to a generous cap, NOT a hardcoded constant. The
        # body-shrink threshold (_frontmatter.merge_page_content) rejects a
        # merge below 0.7 * max(full old, full new); truncating the prompt too
        # aggressively meant the LLM could not reproduce ≥70% of the full body
        # and the no-fallback policy raised RuntimeError on a legitimate merge
        # (2026-06-30 ohms-law, re-ingesting 无源器件篇). Use the live-probed,
        # context-aware target_chars budget already computed for this session's
        # model (same one chunking uses) rather than a stale fixed number — a
        # fixed 24K silently truncated large source pages (2026-07-09).
        cap = getattr(config, "target_chars", None) or MERGE_PROMPT_BODY_CAP
        prompt = f"""Merge two versions of a wiki page. Preserve ALL unique information from both.
Do NOT drop claims, entities, formulas, or references from either version.

# Existing page content
{old_body[:cap]}

# New content (from latest ingest)
{new_body[:cap]}

# Task
Output the merged page body (no frontmatter, no code fences).
The merged version should contain everything from both versions,
with duplicates consolidated and new information integrated.
"""
        # Give the merge the model's real output budget so it can reproduce both
        # bodies; the old hardcoded 4096 capped output at ~16K chars, itself
        # below-threshold for larger pages.
        response, _ = call_anthropic_protocol(prompt, config, max_tokens=config.max_tokens)
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
    replaced = False
    for line in lines:
        if line.strip().startswith("sources:"):
            new_lines.append(f"sources: [{items}]")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        # Frontmatter has no sources: line — append the computed list instead
        # of silently dropping it (the page would otherwise lose provenance).
        new_lines.append(f"sources: [{items}]")
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


# ---------- Stage 3.1 write-time link normalizer (audit 2026-07-02, A5/M6) ----------
# The generation prompts state link-format rules (related as prefixed bare
# slugs, STRICT [[dir/slug]] body links, no self-links) but nothing enforced
# them in code — three format diseases spread across page types (audit M6).
# This is the code backstop: ONE normalization pass applied to every
# non-listing FILE block right before stage_3_1_write_wiki_file. Loud
# per-page prints, never silent.

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
_H1_LINE_RE = re.compile(r"^#[ \t]")
_ANCHOR_STEMS = {"index", "log", "overview", "schema"}
_LISTING_BASENAMES = {"index.md", "log.md", "overview.md", "schema.md"}
_WARN_LIST_CAP = 6  # max offending entries shown per warn line

# Same-stem twin policy (fix 2026-07-02): a bare stem living in exactly
# concepts/ AND entities/ resolves to concepts/ — the concept page is the
# richer target (entity twins are typically chip stubs). Any OTHER ≥2-dir
# set is true ambiguity and keeps the leave-as-is warning.
_CONCEPT_ENTITY_PAIR = frozenset({"concepts", "entities"})

# D4 figure-ref backstop (fix 2026-07-02): the D4 prompt rule asked models to
# wrap bare 图X.X/表X.X/Fig X-X/Table X-X refs as source-page links but they
# applied it inconsistently — a pure deterministic transform, so enforce it
# here. Masked spans (existing wikilinks, markdown links/images whose media
# paths may embed 图X-X, inline code, math) pass through untouched; that also
# makes the transform idempotent — a wrapped ref sits inside [[...]] and is
# masked on the next pass.
_FIGREF_RE = re.compile(
    r"(?:图|表)\s?\d+[.．-]\d+"
    r"|\bFig\.?\s?\d+[-.]\d+"
    r"|\bTable\s?\d+[-.]\d+")
_FIGREF_MASK_RE = re.compile(
    r"\[\[[^\[\]]+\]\]"             # existing wikilinks (incl. |alias)
    r"|!?\[[^\]]*\]\([^)]*\)"       # markdown links/images
    r"|`[^`]+`"                     # inline code spans
    r"|\$\$[^$]+\$\$|\$[^$]+\$")    # math spans
_HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t]")
_CODE_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def _stage_3_1_wrap_figure_refs(body: str, source_page_slug: str) -> tuple[str, int]:
    """Wrap bare figure/table refs as ``[[<source-page>|据<ref>]]`` (D4 backstop).

    Line-by-line: heading lines and fenced code blocks are skipped whole;
    within a line, _FIGREF_MASK_RE spans pass through unchanged and only the
    gaps are transformed. Returns (new_body, wrap_count)."""
    count = 0

    def _wrap(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"[[{source_page_slug}|据{m.group(0)}]]"

    out_lines: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence or _HEADING_LINE_RE.match(line):
            out_lines.append(line)
            continue
        pieces: list[str] = []
        pos = 0
        for m in _FIGREF_MASK_RE.finditer(line):
            pieces.append(_FIGREF_RE.sub(_wrap, line[pos:m.start()]))
            pieces.append(m.group(0))
            pos = m.end()
        pieces.append(_FIGREF_RE.sub(_wrap, line[pos:]))
        out_lines.append("".join(pieces))
    return "\n".join(out_lines), count


def _stage_3_1_normalize_link_target(raw: str) -> str:
    """Reduce one link target to a bare (possibly dir-prefixed) slug.

    Strips ``[[..]]`` wrapping (incl. stray single brackets — a
    ``related: [[concepts/foo]]`` line parses to ``[concepts/foo]``),
    ``|alias`` and ``#anchor`` parts, a leading ``wiki/`` prefix, and a
    trailing ``.md`` suffix. Quotes are already stripped by
    parse_frontmatter_array for related entries. Slugs never contain
    brackets, so the bracket strip is lossless."""
    t = raw.strip().strip("[]").strip()
    t = t.split("|")[0].split("#")[0].strip()
    if t.startswith("wiki/"):
        t = t[len("wiki/"):]
    if t.endswith(".md"):
        t = t[:-3]
    return t.strip().strip("/")


def _stage_3_1_scan_wiki_slug_dirs(config: Config) -> dict[str, set[str]]:
    """stem → {wiki_dir-relative parent dir} for every knowledge page on disk.

    Mirrors list_existing_slugs' exclusions (artifact dirs, ``_``-prefixed
    stems, aggregate anchors) but keeps the parent dir — the normalizer needs
    to know WHICH folder a stem lives in, not just that it exists."""
    out: dict[str, set[str]] = {}
    if not config.wiki_dir.exists():
        return out
    for f in config.wiki_dir.rglob("*.md"):
        if WIKI_ARTIFACT_DIRS.intersection(f.parts):
            continue
        stem = f.stem
        if stem.startswith("_") or stem in _ANCHOR_STEMS:
            continue
        if f.parent == config.wiki_dir:
            continue  # root-level files are anchors/system pages, not link targets
        rel_dir = f.parent.relative_to(config.wiki_dir).as_posix()
        out.setdefault(stem, set()).add(rel_dir)
    return out


def stage_3_1_build_slug_dirs(
    file_blocks: list[tuple[str, str]],
    config: Config,
    valid_subdirs: set[str],
    routing: dict[str, str],
) -> dict[str, set[str]]:
    """Slug→dirs universe for the link normalizer: this batch ∪ on-disk wiki.

    Batch blocks are mapped through the SAME path-resolution chain the write
    loop applies (top-dir accept-list → auto-correct → ``.md`` suffix → schema
    route) so a block's universe entry matches where the loop will actually
    write it. Built once per book, before the write loop."""
    slug_dirs = _stage_3_1_scan_wiki_slug_dirs(config)
    for rel_path, content in file_blocks:
        if ".." in rel_path or rel_path.startswith("/"):
            continue
        if not is_safe_ingest_path(rel_path):
            continue
        if Path(rel_path).name in _LISTING_BASENAMES:
            continue
        top_dir = rel_path.split("/")[0] if "/" in rel_path else ""
        if top_dir not in valid_subdirs:
            corrected = _stage_3_1_auto_correct_wiki_path(rel_path, content, quiet=True)
            if not corrected:
                continue
            rel_path = corrected
        if not rel_path.endswith(".md"):
            rel_path += ".md"
        rel_path = _stage_3_1_schema_route(rel_path, content, routing)
        if "/" not in rel_path:
            continue
        rel_dir, name = rel_path.rsplit("/", 1)
        slug_dirs.setdefault(name[:-3], set()).add(rel_dir)
    return slug_dirs


def stage_3_1_normalize_page_links(
    rel_path: str, content: str, slug_dirs: dict[str, set[str]],
    source_page_slug: str | None = None,
) -> str:
    """A5 write-time link normalizer — one pass over a FILE block before write.

    Rules (audit M6 → fix A5):
      1. ``related:`` entries → prefixed bare slugs (``concepts/foo``): strips
         ``[[..]]`` wrapping / quotes / aliases / ``.md``; resolves the prefix
         by checking which dir the stem exists in (slug_dirs = batch ∪ disk);
         drops entries that resolve nowhere; collapses duplicates.
      2. Bare body wikilinks ``[[foo]]``: prefixed when the stem resolves to
         exactly ONE dir; ambiguous/missing are left as-is but warned — never
         de-linked automatically.
      3. H1 heading lines: embedded wikilinks stripped to plain text.
      4. Self-links (own slug in body or related) de-linked/removed.
      5. D4 backstop (when ``source_page_slug`` is given): bare figure/table
         refs (图X.X / 表X.X / Fig X-X / Table X-X) in body text wrapped as
         ``[[<source_page_slug>|据<ref>]]``. Skips headings, code/math spans,
         and refs already inside any ``[[..]]``; the source page itself
         (own slug == source_page_slug) is excluded. Idempotent.

    Ambiguous related stems (≥2 dirs, claimed prefix wrong or absent) are kept
    as the BARE stem + warned — usually a dedup failure (H1) where guessing a
    dir would pick the wrong twin; the entry becomes valid once the twins merge.
    Exception (fix 2026-07-02): the exact concepts/+entities/ pair resolves
    deterministically to concepts/ (see _CONCEPT_ENTITY_PAIR) — in related:
    AND in body links.

    Already-clean pages pass through byte-identical. All fixes print loud
    per-page ``[normalize]`` lines — never silent."""
    content = _stage_3_1_sanitize_ingested_content(content)
    norm_rel = rel_path[len("wiki/"):] if rel_path.startswith("wiki/") else rel_path
    own_prefixed = norm_rel[:-3] if norm_rel.endswith(".md") else norm_rel
    own_stem = own_prefixed.rsplit("/", 1)[-1]

    def _warn(msg: str) -> None:
        print(f"  [normalize] {norm_rel}: {msg}")

    def _fmt_list(items: list[str]) -> str:
        shown = ", ".join(items[:_WARN_LIST_CAP])
        extra = len(items) - _WARN_LIST_CAP
        return shown + (f" (+{extra} more)" if extra > 0 else "")

    # ── Rule 1 + 4a: related → prefixed bare slugs ──
    has_fm = content.startswith("---") and content.find("\n---", 3) != -1
    fm_end = content.find("\n---", 3) if has_fm else -1
    fm_text = content[4:fm_end] if has_fm else ""
    if has_fm and re.search(r"^related\s*:", fm_text, re.MULTILINE):
        orig_entries = parse_frontmatter_array(content, "related")
        kept: list[str] = []
        dropped: list[str] = []
        ambiguous: list[str] = []
        twins: list[str] = []
        self_removed: list[str] = []
        seen: set[str] = set()
        for entry in orig_entries:
            t = _stage_3_1_normalize_link_target(entry)
            if not t:
                dropped.append(entry)
                continue
            claimed_dir, stem = t.rsplit("/", 1) if "/" in t else ("", t)
            dirs = slug_dirs.get(stem)
            if not dirs:
                dropped.append(entry)
                continue
            is_ambiguous = False
            is_twin = False
            if claimed_dir and claimed_dir in dirs:
                resolved = t
            elif len(dirs) == 1:
                resolved = f"{next(iter(dirs))}/{stem}"
            elif dirs == _CONCEPT_ENTITY_PAIR:
                is_twin = True  # concepts/+entities/ pair — concepts wins
                resolved = f"concepts/{stem}"
            else:
                is_ambiguous = True  # ≥2 dirs, no (valid) claimed prefix — don't guess
                resolved = stem
            if resolved in (own_prefixed, own_stem):
                self_removed.append(entry)
                continue
            if is_ambiguous:
                ambiguous.append(entry)
            if is_twin:
                twins.append(entry)
            if resolved in seen:
                continue
            seen.add(resolved)
            kept.append(resolved)
        if dropped:
            _warn(f"related: dropped {len(dropped)} unresolvable — {_fmt_list(dropped)}")
        if self_removed:
            _warn(f"related: removed {len(self_removed)} self-link(s) — {_fmt_list(self_removed)}")
        if twins:
            _warn(f"related: resolved {len(twins)} same-stem ambiguity → concepts/ — {_fmt_list(twins)}")
        if ambiguous:
            _warn(f"related: ⚠️ {len(ambiguous)} ambiguous, kept bare — {_fmt_list(ambiguous)}")
        if kept != orig_entries:
            if not (dropped or self_removed or ambiguous or twins):
                _warn(f"related: normalized {len(kept)} entry(ies) to prefixed bare slugs")
            content = write_frontmatter_array(content, "related", kept)
            has_fm = content.startswith("---") and content.find("\n---", 3) != -1
            fm_end = content.find("\n---", 3) if has_fm else -1

    # ── Rules 2 + 3 + 4b: body wikilinks ──
    body_start = fm_end + 4 if has_fm else 0
    head, body = content[:body_start], content[body_start:]
    counts = {"h1": 0, "self": 0, "prefixed": 0, "twin": 0}
    unresolved: list[str] = []
    changed = False

    def _display_text(inner: str) -> str:
        target, _, alias = inner.partition("|")
        if alias.strip():
            return alias.strip()
        t = _stage_3_1_normalize_link_target(target)
        return t.rsplit("/", 1)[-1] if t else target.strip()

    def _delink_h1(m: re.Match) -> str:
        nonlocal changed
        counts["h1"] += 1
        changed = True
        return _display_text(m.group(1))

    def _fix_link(m: re.Match) -> str:
        nonlocal changed
        inner = m.group(1)
        target, _, alias = inner.partition("|")
        alias = alias.strip()
        anchor = target.split("#", 1)[1].strip() if "#" in target else ""
        t = _stage_3_1_normalize_link_target(target)
        if not t:
            return m.group(0)
        if t in (own_prefixed, own_stem):
            counts["self"] += 1
            changed = True
            return _display_text(inner)
        if "/" in t:
            return m.group(0)  # already prefixed — leave as-is
        dirs = slug_dirs.get(t)
        if dirs and len(dirs) == 1:
            counts["prefixed"] += 1
            changed = True
            d = next(iter(dirs))
            rebuilt = f"{d}/{t}" + (f"#{anchor}" if anchor else "") + (f"|{alias}" if alias else "")
            return f"[[{rebuilt}]]"
        if dirs == _CONCEPT_ENTITY_PAIR:
            counts["twin"] += 1  # concepts/+entities/ pair — concepts wins
            changed = True
            rebuilt = f"concepts/{t}" + (f"#{anchor}" if anchor else "") + (f"|{alias}" if alias else "")
            return f"[[{rebuilt}]]"
        unresolved.append(m.group(0))  # missing or truly ambiguous — never de-link (rule 2)
        return m.group(0)

    new_lines: list[str] = []
    for line in body.split("\n"):
        if "[[" not in line:
            new_lines.append(line)
        elif _H1_LINE_RE.match(line):
            new_lines.append(_WIKILINK_RE.sub(_delink_h1, line))
        else:
            new_lines.append(_WIKILINK_RE.sub(_fix_link, line))
    new_body = "\n".join(new_lines)

    # ── Rule 5: D4 figure-ref backstop (source page itself excluded) ──
    fig_count = 0
    if source_page_slug and own_prefixed != source_page_slug:
        new_body, fig_count = _stage_3_1_wrap_figure_refs(new_body, source_page_slug)
        if fig_count:
            changed = True
    if changed:
        content = head + new_body

    if counts["h1"]:
        _warn(f"H1: de-linked {counts['h1']} embedded wikilink(s)")
    if counts["self"]:
        _warn(f"body: de-linked {counts['self']} self-link(s)")
    if counts["prefixed"]:
        _warn(f"body: prefixed {counts['prefixed']} bare wikilink(s)")
    if counts["twin"]:
        _warn(f"body: resolved {counts['twin']} same-stem ambiguity → concepts/")
    if fig_count:
        _warn(f"body: wrapped {fig_count} figure/table ref(s) → [[{source_page_slug}]]")
    if unresolved:
        uniq = list(dict.fromkeys(unresolved))
        _warn(f"body: ⚠️ {len(unresolved)} bare wikilink(s) left as-is "
              f"(unresolved/ambiguous) — {_fmt_list(uniq)}")
    return content


def stage_3_1_write_wiki_file(path: Path, content: str, config: Config | None = None, merge: bool = False) -> None:
    content = _stage_3_1_sanitize_ingested_content(content)
    if config is not None:
        _stage_3_1_backup_existing_page(path, config)
        if merge and path.exists():
            existing = path.read_text(encoding="utf-8")
            content = _stage_3_1_merge_page_content(existing, content, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content)


# Category subdir → bilingual index.md section header (NashSU index parity).
_INDEX_CATEGORIES: list[tuple[str, str]] = [
    ("sources", "Sources（来源）"),
    ("concepts", "Concepts（概念）"),
    ("entities", "Entities（实体）"),
    ("queries", "Queries（查询）"),
    ("comparisons", "Comparisons（对比）"),
    ("synthesis", "Synthesis（综合）"),
    ("findings", "Findings（发现）"),
    ("thesis", "Thesis（论题）"),
    ("methodology", "Methodology（方法论）"),
]


def _scan_wiki_inventory(wiki_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Scan category subdirs for (stem, title) — authoritative on-disk page list.

    Used by Stage 3.5 index.md rewrite so the LLM gets the real page inventory
    instead of trusting the current index text (which drifts: only Sources was
    ever appended, Concepts/Entities/etc. went stale)."""
    inventory: dict[str, list[tuple[str, str]]] = {}
    for subdir, _header in _INDEX_CATEGORIES:
        d = wiki_dir / subdir
        if not d.is_dir():
            continue
        pages: list[tuple[str, str]] = []
        for f in sorted(d.rglob("*.md")):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                fm, _ = parse_frontmatter(content)
                title = ""
                if isinstance(fm, dict):
                    t = fm.get("title")
                    if isinstance(t, str):
                        title = t.strip().strip('"').strip("'")
                if not title:
                    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                    title = m.group(1).strip() if m else f.stem
                pages.append((f.stem, title))
            except (OSError, UnicodeDecodeError):
                pages.append((f.stem, f.stem))
        if pages:
            inventory[subdir] = pages
    return inventory


def rebuild_index_deterministic(wiki_dir: Path) -> str:
    """Full index.md rebuild from the on-disk page inventory — no LLM call.

    NashSU parity (llm_wiki 0.6.4 ``rebuild_wiki_index``): a pure
    frontmatter-scan recovery tool, independent of the ingest pipeline's LLM
    whole-page rewrite in ``stage_3_5_aggregate_repair`` above. That LLM
    rewrite only runs mid-ingest and is capped at ``INDEX_REWRITE_MAX_PAGES``
    (250) pages; this is for the "index.md looks corrupted/drifted but I
    don't want to trigger a full ingest re-run" case, at any wiki size.

    Same bullet format as the LLM rewrite (`- [[<stem>]] — <title>`, sorted
    alphabetically by stem within each section, bilingual headers in
    ``_INDEX_CATEGORIES`` order, empty categories omitted) so the two stay
    visually interchangeable. Entry point: ``rebuild_index.py``.
    """
    inventory = _scan_wiki_inventory(wiki_dir)
    lines = ["# Index", ""]
    for subdir, header in _INDEX_CATEGORIES:
        pages = inventory.get(subdir, [])
        if not pages:
            continue
        lines.append(f"## {header}")
        lines.append("")
        for stem, title in sorted(pages, key=lambda p: p[0]):
            lines.append(f"- [[{stem}]] — {title}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _log_contains_ingest_record(
    log_text: str,
    source_identity: str,
    source_hash: str,
) -> bool:
    """True when one INGEST block already binds this source and content hash."""
    source_line = f"- Source: `{source_identity}`"
    hash_line = f"- Hash: {source_hash[:16]}"
    blocks = re.split(
        r"(?m)(?=^## [^\n]+ — INGEST\s*$)",
        log_text,
    )
    return any(
        source_line in block and hash_line in block
        for block in blocks
    )


def _assert_aggregate_outputs(
    log_path: Path,
    index_path: Path,
    source_identity: str,
    source_hash: str,
    source_stem: str,
) -> None:
    """Hard Stage 3.5 postcondition used before its done marker is written."""
    if not log_path.is_file():
        raise RuntimeError(f"Stage 3.5 log artifact is missing: {log_path}")
    if not index_path.is_file():
        raise RuntimeError(f"Stage 3.5 index artifact is missing: {index_path}")
    log_text = log_path.read_text(encoding="utf-8")
    if not _log_contains_ingest_record(
        log_text, source_identity, source_hash
    ):
        raise RuntimeError(
            "Stage 3.5 log does not contain the source/hash INGEST record")
    index_text = index_path.read_text(encoding="utf-8")
    if f"[[{source_stem}]]" not in index_text:
        raise RuntimeError(
            f"Stage 3.5 index does not contain [[{source_stem}]]")


def stage_3_5_aggregate_repair(
    source_path: Path,
    raw_file: Path,
    analysis: dict,
    source_hash: str,
    extract_method: str,
    config: Config,
) -> list[str]:
    """NashSU Stage 2.6: log.md (deterministic append), index.md (LLM whole-page
    rewrite fed by on-disk inventory, append fallback), overview.md (LLM rewrite
    with structural validation + compress mode, keep-current fallback)."""
    files_written: list[str] = []
    source_identity = canonical_source_path(raw_file, config)

    # log.md
    log_path = config.wiki_dir / "log.md"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8")
    else:
        log_text = "# Log\n"
    source_rel = source_path.relative_to(config.wiki_dir)
    if _log_contains_ingest_record(log_text, source_identity, source_hash):
        print("[stage 3.5] Log already contains this source/hash — append skipped")
    else:
        entry = (
            f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')} — INGEST\n"
            f"- Source: `{source_identity}`\n"
            f"- Source page: `wiki/{source_rel}`\n"
            f"- Hash: {source_hash[:16]}\n"
            f"- Method: {extract_method}\n"
        )
        log_text += entry
        stage_3_1_write_wiki_file(log_path, log_text, config)
    files_written.append(PageRef.parse(
        log_path, config.wiki_root, config.wiki_dir).project_relative)

    # index.md — LLM whole-page rewrite (NashSU parity, option A: every ingest).
    # Fed by an authoritative on-disk page inventory so ALL categories stay in
    # sync, not just Sources. Deterministic single-line append is the hard
    # fallback when the LLM call fails or the index exceeds the size cap.
    index_path = config.wiki_dir / "index.md"
    current_index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    def _index_append_fallback() -> None:
        new_link = f"- [[{source_path.stem}]]"
        if not current_index:
            # Fresh wiki: write the skeleton WITH the new source link, not an
            # empty skeleton (the latter silently dropped the first ingest).
            stage_3_1_write_wiki_file(
                index_path, f"# Index\n\n## Sources（来源）\n\n{new_link}\n", config)
            files_written.append(str(index_path.relative_to(config.wiki_root)))
            return
        if f"[[{source_path.stem}]]" in current_index:
            return
        # Insert after the END of the Sources header line so a bilingual header
        # like "## Sources（来源）" isn't split mid-line.
        m = re.search(r"(?m)^##\s+Sources.*$", current_index)
        if m:
            insert_at = m.end()
            updated = current_index[:insert_at] + f"\n\n{new_link}" + current_index[insert_at:]
        else:
            print("[stage 3.5] ⚠️ index.md has no '## Sources' header — "
                  "appending a new Sources section at end of file")
            updated = current_index.rstrip("\n") + f"\n\n## Sources（来源）\n\n{new_link}\n"
        stage_3_1_write_wiki_file(index_path, updated, config)
        files_written.append(str(index_path.relative_to(config.wiki_root)))

    INDEX_MAX_CHARS = max(4096, int(config.source_budget * 0.12))
    # LLM whole-page rewrite can only produce ~250 bullets within the 4096-token
    # output cap. For larger wikis the LLM cannot emit a complete index, so we
    # fall back to the deterministic append (which at least keeps Sources fresh).
    # A deterministic full-rebuild for large wikis is future work.
    INDEX_REWRITE_MAX_PAGES = 250
    inventory = _scan_wiki_inventory(config.wiki_dir)
    total_pages = sum(len(v) for v in inventory.values())
    skip_reason = ""
    if len(current_index) > INDEX_MAX_CHARS:
        skip_reason = f"index too large ({len(current_index)} > {INDEX_MAX_CHARS})"
    elif total_pages > INDEX_REWRITE_MAX_PAGES:
        skip_reason = (f"wiki has {total_pages} pages (> {INDEX_REWRITE_MAX_PAGES}), "
                       f"LLM cannot emit a complete index")

    if skip_reason:
        print(f"[stage 3.5] {skip_reason} — LLM rewrite skipped, using append fallback")
        _index_append_fallback()
    else:
        inv_lines: list[str] = []
        for subdir, header in _INDEX_CATEGORIES:
            for stem, title in inventory.get(subdir, []):
                inv_lines.append(f"- [[{stem}]] — {title}")
        inventory_text = "\n".join(inv_lines) or "(no pages found)"

        prompt = f"""You maintain the index of a knowledge-base wiki. Below is the
CURRENT index.md, followed by the AUTHORITATIVE on-disk page inventory (scanned
from the filesystem — the ground truth of what pages exist now).

Rewrite the COMPLETE index.md so every category lists exactly its inventory
pages, under these bilingual section headers in this order (omit empty ones):
Sources（来源）, Concepts（概念）, Entities（实体）, Queries（查询）,
Comparisons（对比）, Synthesis（综合）, Findings（发现）, Thesis（论题）,
Methodology（方法论）.

Rules:
- Preserve existing entries' descriptions verbatim where the stem matches.
- For new entries (in inventory but not in current index), use the inventory
  "— title" as the description.
- One bullet per page: `- [[<stem>]] — <description>`. Sort within each section
  alphabetically by stem.
- Keep the existing frontmatter and top-level `# ` title unchanged.

# CURRENT index.md
{current_index or "(empty)"}

# ON-DISK INVENTORY (authoritative)
{inventory_text}

# Task
Output ONLY the complete new index.md. No commentary.
"""
        try:
            response, _ = call_anthropic_protocol(prompt, config, max_tokens=4096)
            if "---FILE:" in response:
                print("[stage 3.5] Index LLM response contained FILE blocks — falling back")
                _index_append_fallback()
            elif "## " in response and "[[" in response:
                stage_3_1_write_wiki_file(index_path, response.strip() + "\n", config)
                files_written.append(str(index_path.relative_to(config.wiki_root)))
                print(f"[stage 3.5] Index rewritten via LLM ({len(response)} chars)")
            else:
                print("[stage 3.5] Index LLM response missing sections/links — falling back")
                _index_append_fallback()
        except Exception as e:
            print(f"[stage 3.5] Index LLM rewrite failed ({e}) — using append fallback")
            _index_append_fallback()
    files_written.append(PageRef.parse(
        index_path, config.wiki_root, config.wiki_dir).project_relative)

    # overview.md — LLM rewrite with improved prompt (topic-synthesis, not
    # source-dump) + failure fallback. True NashSU aggregate-repair parity
    # (2026-07-03 correction): NashSU's own spec for overview.md is just "a
    # comprehensive 2-5 paragraph overview of ALL topics" (ingest.ts
    # buildGenerationPrompt / buildAggregateRepairPrompt) — it has no
    # Strong/Weak Claims or Open Questions sections (grep confirms zero
    # matches in NashSU source) and no forced-compress retry. NashSU's own
    # size handling is isAggregateRepairSafe: a PRE-check that SKIPS the
    # repair entirely (leaving the file untouched, just a warning) when
    # already over cap, rather than asking the LLM to compress it. The
    # earlier compress-mode design here (5 required sections + forced
    # tighter-rewrite retry) was an original addition, not NashSU parity —
    # aligned back to NashSU's simpler skip-if-unsafe behavior below.
    overview_path = config.wiki_dir / "overview.md"
    current_overview = overview_path.read_text(encoding="utf-8") if overview_path.exists() else ""
    # NashSU aggregateRepairSectionCap: proportional to context budget only,
    # no hard ceiling.
    OVERVIEW_MAX_CHARS = max(4096, int(config.source_budget * 0.12))
    if current_overview and len(current_overview) > OVERVIEW_MAX_CHARS:
        print(f"[stage 3.5] Overview too large ({len(current_overview)} > {OVERVIEW_MAX_CHARS}) — "
              f"skipping repair (NashSU isAggregateRepairSafe parity), leaving current overview untouched")
        _assert_aggregate_outputs(
            log_path,
            index_path,
            source_identity,
            source_hash,
            source_path.stem,
        )
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

    prompt = f"""You maintain the overview of a knowledge-base wiki. Below is the
CURRENT overview.md, followed by the newly ingested source page and recent
source pages for context. Rewrite the COMPLETE overview.md to incorporate the
new source.

CRITICAL — avoid source-listing dumps:
- Synthesize by knowledge area / topic, NOT enumerate books. Group sources
  under shared themes; cite a source inline only when it anchors a specific
  claim. Do NOT write "本次最新重新摄入…" source-inventory paragraphs or
  back-to-back book-by-book summaries.
- Each paragraph = one theme (e.g. power electronics, high-speed design, RF),
  covering what the wiki knows, key tensions, and gaps — not which books were read.

Keep the overview concise: 2-5 paragraphs synthesizing topics in common across
sources, not a per-source walkthrough.

# Current overview.md
{current_overview or "(empty)"}

# New source page: {source_path.stem}
{source_content[:3000]}

# Recent source pages (for context)
{chr(10).join(sources_lines[:8])}

# Task
Output ONLY the new overview.md (starting with "# Overview"): a comprehensive
2-5 paragraph overview of ALL topics in the wiki, updated to reflect the newly
ingested source — not just the new source.
"""
    try:
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=4096)
        if "---FILE:" in response:
            print("[stage 3.5] Overview LLM response contained FILE blocks — keeping current")
        else:
            body = response.strip()
            if not body.startswith("#"):
                print("[stage 3.5] Overview LLM response did not start with '#' — keeping current")
            else:
                stage_3_1_write_wiki_file(overview_path, body + "\n", config)
                files_written.append(PageRef.parse(
                    overview_path,
                    config.wiki_root,
                    config.wiki_dir,
                ).project_relative)
                print(f"[stage 3.5] Overview updated via LLM ({len(response)} chars, stop={stop_reason})")
    except Exception as e:
        print(f"[stage 3.5] Overview LLM update failed ({e}) — keeping current")

    _assert_aggregate_outputs(
        log_path,
        index_path,
        source_identity,
        source_hash,
        source_path.stem,
    )
    return list(dict.fromkeys(files_written))
