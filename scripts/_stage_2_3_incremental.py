"""Stage 2.3: Incremental Association Detection

Detects overlap between a new source\'s concepts/entities and existing wiki
pages, so downstream stages can avoid generating orphan/duplicate pages.
Deterministic: word-level title Jaccard + exact slug match. (LLM semantic
match is a future enhancement.)
"""
from pathlib import Path
import re

from _stage_2_base import _stage_2_frontmatter_title, _stage_2_title_words


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
    for page_dir in [concepts_dir, entities_dir]:
        if not page_dir.is_dir():
            continue
        for f in page_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                title = _stage_2_frontmatter_title(content)
                if title:
                    existing[f.stem] = _stage_2_title_words(title)
            except Exception:
                pass

    for name in found:
        name_words = _stage_2_title_words(name)
        matches = []
        slug_form = name.lower().replace(" ", "-")
        for slug, words in existing.items():
            if not words:
                continue
            if slug_form == slug.lower():
                matches.append(slug)
            elif name_words and len(name_words & words) / len(name_words | words) > 0.5:
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
            except Exception:
                pass

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


def _stage_2_3_verify_incremental_associations(checkpoint: dict, wiki_root=None) -> bool:
    if wiki_root and not (wiki_root / "concepts").is_dir():
        checkpoint["incremental_associations"] = {}
        return True
    return "incremental_associations" in checkpoint
