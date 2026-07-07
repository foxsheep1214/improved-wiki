"""Stage 2.3: Incremental Association Detection

Detects overlap between a new source\'s concepts/entities and existing wiki
pages, so downstream stages can avoid generating orphan/duplicate pages.
Deterministic: word-level title Jaccard + exact slug match. (LLM semantic
match is a future enhancement.)
"""
from pathlib import Path
import re

from _stage_2_base import (
    _stage_2_frontmatter_title,
    _stage_2_title_words,
    _stage_2_title_cjk_bigrams,
)

# Cross-domain acronym guard: shared tokens no longer than this are treated as
# bare acronyms ("ram", "mti") rather than full words.
_STAGE_2_3_ACRONYM_MAX_LEN = 4
_STAGE_2_3_CJK_RE = re.compile("[\\u3400-\\u4dbf\\u4e00-\\u9fff]")


def _stage_2_3_acronym_only_mismatch(name: str, slug: str, shared_tokens: set) -> bool:
    """True when a title-Jaccard match rests solely on short ASCII tokens
    (<=4 chars, i.e. bare acronyms) while the two names carry disjoint CJK
    parts — a cross-domain acronym collision, not a real association.

    Live failure (2026-07-02, 《直升机多普勒导航雷达原理》): _stage_2_title_words
    strips CJK characters entirely, so "RAM 片选信号软件控制" (computer memory)
    and the existing page 雷达吸波材料-ram (radar absorbing material) both
    tokenized to {"ram"} → Jaccard 1.0 → the new concept was flagged ALREADY
    COVERED and generation linked memory pages to the radar page. Exact
    slug-form matches, matches carrying at least one longer shared token, and
    names without CJK on both sides are unaffected.
    """
    if not shared_tokens:
        return False
    if any(len(tok) > _STAGE_2_3_ACRONYM_MAX_LEN for tok in shared_tokens):
        return False
    name_cjk = set(_STAGE_2_3_CJK_RE.findall(name))
    slug_cjk = set(_STAGE_2_3_CJK_RE.findall(slug))
    return bool(name_cjk) and bool(slug_cjk) and not (name_cjk & slug_cjk)


def stage_2_3_detect_incremental_associations(wiki_root: Path, chunk_analyses: list[dict]) -> dict:
    associations = {}
    concepts_dir = wiki_root / "concepts"
    entities_dir = wiki_root / "entities"
    if not concepts_dir.is_dir() or not list(concepts_dir.glob("*.md")):
        return {}

    found = set()
    for chunk in chunk_analyses:
        for concept in chunk.get("concepts_found", []):
            name = concept.get("name", "").strip() if isinstance(concept, dict) else str(concept).strip()
            if name:
                found.add(name)
        for ent in chunk.get("entities_found", []):
            name = ent.get("name", "").strip() if isinstance(ent, dict) else str(ent).strip()
            if name:
                found.add(name)

    existing = {}
    existing_cjk = {}
    for page_dir in [concepts_dir, entities_dir]:
        if not page_dir.is_dir():
            continue
        for f in page_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                title = _stage_2_frontmatter_title(content)
                if title:
                    existing[f.stem] = _stage_2_title_words(title)
                    existing_cjk[f.stem] = _stage_2_title_cjk_bigrams(title)
            except Exception as e:
                print(f"[2.3] warn: skip {f}: {type(e).__name__}: {e}")

    for name in found:
        name_words = _stage_2_title_words(name)
        name_cjk = _stage_2_title_cjk_bigrams(name)
        matches = []
        slug_form = name.lower().replace(" ", "-")
        for slug, words in existing.items():
            # Exact slug-form match first: a pure-CJK title tokenizes to an
            # empty ASCII word set, and skipping on empty words BEFORE this
            # check made exact CJK name↔slug matches undetectable (fix
            # 2026-07-02). Only the Jaccard branch needs non-empty words.
            cjk = existing_cjk.get(slug, set())
            if slug_form == slug.lower():
                matches.append(slug)
            elif (words and name_words
                  and len(name_words & words) / len(name_words | words) > 0.5
                  and not _stage_2_3_acronym_only_mismatch(name, slug, name_words & words)):
                matches.append(slug)
            # CJK bigram Jaccard branch (A4, audit 2026-07-02): pure/mostly-CJK
            # titles previously had no non-exact match path at all. Separate
            # from the ASCII branch so mixed titles don't dilute either side.
            # A shared CJK bigram implies shared CJK characters between the
            # TITLES; the acronym guard (name-vs-slug CJK disjointness) is
            # still applied for symmetry with the ASCII branch.
            elif (cjk and name_cjk
                  and len(name_cjk & cjk) / len(name_cjk | cjk) > 0.5
                  and not _stage_2_3_acronym_only_mismatch(name, slug, name_cjk & cjk)):
                matches.append(slug)
        if matches:
            associations[name] = matches
    return associations


def stage_2_3_resolve_proposed_connections(wiki_root: Path, chunk_analyses: list[dict]) -> list[dict]:
    """Resolve each chunk's self-reported ``connections_to_existing_wiki``
    entries against real wiki pages.

    Stage 2.2 asks the LLM to propose relationships (extends/applies/cites/
    contrasts) to existing pages, but nothing downstream ever read this field
    — it was silently discarded. This validates each proposed page actually
    exists (exact slug or title-Jaccard >=0.5, same method as
    ``stage_2_3_detect_incremental_associations``) and resolves it to a
    type-prefixed slug, so Stage 2.4 can wikilink new pages to genuinely
    related (not duplicate) existing pages instead of dropping the field.
    """
    proposed: list[tuple[str, str]] = []
    for chunk in chunk_analyses:
        for conn in chunk.get("connections_to_existing_wiki", []) or []:
            if not isinstance(conn, dict):
                continue
            page = (conn.get("existing_page") or "").strip()
            rel = (conn.get("relationship") or "related").strip()
            if page:
                proposed.append((page, rel))
    if not proposed:
        return []

    existing: dict[str, tuple[str, set]] = {}
    for type_dir in ("concepts", "entities", "sources", "queries", "comparisons"):
        page_dir = wiki_root / type_dir
        if not page_dir.is_dir():
            continue
        for f in page_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                title = _stage_2_frontmatter_title(content)
                existing[f.stem] = (type_dir, _stage_2_title_words(title) if title else set())
            except Exception as e:
                print(f"[2.3] warn: skip {f}: {type(e).__name__}: {e}")

    resolved = []
    seen = set()
    for page, rel in proposed:
        slug_form = page.lower().replace(" ", "-")
        match = slug_form if slug_form in existing else None
        if not match:
            page_words = _stage_2_title_words(page)
            best_ratio, best_slug = 0.0, None
            for stem, (_, words) in existing.items():
                if not words or not page_words:
                    continue
                if _stage_2_3_acronym_only_mismatch(page, stem, page_words & words):
                    continue
                ratio = len(page_words & words) / len(page_words | words)
                if ratio > best_ratio:
                    best_ratio, best_slug = ratio, stem
            if best_ratio > 0.5:
                match = best_slug
        if match and match not in seen:
            seen.add(match)
            type_dir = existing[match][0]
            resolved.append({"slug": f"{type_dir}/{match}", "relationship": rel})
    return resolved
