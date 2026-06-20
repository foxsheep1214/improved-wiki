"""Stage 2.3: Incremental Association Detection"""
from pathlib import Path
import re

def detect_incremental_associations(wiki_root: Path, chunk_analyses: list[dict]) -> dict:
    associations = {}
    concepts_dir = wiki_root / "concepts"
    if not concepts_dir.is_dir() or not list(concepts_dir.glob("*.md")):
        return {}

    found_concepts = set()
    for chunk in chunk_analyses:
        for concept in chunk.get("concepts_found", []):
            name = concept.get("name", "").lower().strip() if isinstance(concept, dict) else concept.lower().strip()
            if name:
                found_concepts.add(name)

    for concept_name in found_concepts:
        matching_pages = []
        for concept_file in concepts_dir.glob("*.md"):
            try:
                content = concept_file.read_text(encoding="utf-8", errors="ignore")
                title_match = re.search(r'title:\s*([^\n]+)', content)
                if title_match:
                    title = title_match.group(1).strip().lower()
                    if concept_name in title or title in concept_name:
                        matching_pages.append(concept_file.stem)
            except:
                pass
        if matching_pages:
            associations[concept_name] = matching_pages

    return associations

def verify_incremental_associations(checkpoint: dict, wiki_root=None) -> bool:
    if wiki_root and not (wiki_root / "concepts").is_dir():
        checkpoint["incremental_associations"] = {}
        return True
    return "incremental_associations" in checkpoint
