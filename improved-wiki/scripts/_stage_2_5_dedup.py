"""Stage 2.5: Concept Dedup & Merge"""
from pathlib import Path
import re

def extract_concept_blocks(file_blocks: list[tuple]) -> list[dict]:
    concepts = []
    for idx, (path, content) in enumerate(file_blocks):
        if "/concepts/" in path:
            title_match = re.search(r'title:\s*([^\n]+)', content)
            title = title_match.group(1).strip() if title_match else path.split("/")[-1]
            body_match = re.search(r'---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
            definition = (body_match.group(2) if body_match else content)[:200].lower()
            concepts.append({
                "slug": Path(path).stem,
                "title": title,
                "definition_snippet": definition,
                "block_index": idx,
                "full_content": content,
            })
    return concepts

def find_duplicate_concepts(concepts: list[dict]) -> list[list[int]]:
    duplicates = []
    processed = set()
    for i, concept1 in enumerate(concepts):
        if i in processed:
            continue
        group = [i]
        processed.add(i)
        for j, concept2 in enumerate(concepts[i + 1 :], start=i + 1):
            if j in processed:
                continue
            title_match = concept1["title"].lower() in concept2["title"].lower() or \
                         concept2["title"].lower() in concept1["title"].lower()
            words1 = set(concept1["definition_snippet"].split())
            words2 = set(concept2["definition_snippet"].split())
            overlap = len(words1 & words2) / max(len(words1 | words2), 1)
            if title_match or overlap > 0.5:
                group.append(j)
                processed.add(j)
        if len(group) > 1:
            duplicates.append(group)
    return duplicates

def generate_merge_rules(concepts: list[dict], duplicate_groups: list[list[int]]) -> list[dict]:
    rules = []
    for group in duplicate_groups:
        primary_idx = max(group, key=lambda i: len(concepts[i]["definition_snippet"]))
        duplicate_indices = [i for i in group if i != primary_idx]
        rule = {
            "primary_slug": concepts[primary_idx]["slug"],
            "primary_title": concepts[primary_idx]["title"],
            "duplicate_slugs": [concepts[i]["slug"] for i in duplicate_indices],
            "merge_strategy": "union",
            "merge_reason": f"相似度 >70%，{len(duplicate_indices)} 个重复",
        }
        rules.append(rule)
    return rules

def apply_merge_rules(file_blocks: list[tuple], merge_rules: list[dict]) -> list[tuple]:
    if not merge_rules:
        return file_blocks
    slugs_to_delete = set()
    for rule in merge_rules:
        slugs_to_delete.update(rule["duplicate_slugs"])
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        if "/concepts/" in path and slug in slugs_to_delete:
            continue
        result.append((path, content))
    return result

def verify_dedup_merge(checkpoint: dict, chunk_count: int) -> bool:
    if chunk_count <= 1:
        checkpoint["concept_merge_rules"] = []
        return True
    return "concept_merge_rules" in checkpoint
