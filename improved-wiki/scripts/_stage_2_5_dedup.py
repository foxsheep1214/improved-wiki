"""Stage 2.5: Concept Dedup & Merge (deterministic candidate + LLM confirm)

Deterministic phase finds candidate duplicate groups (title word-overlap or
definition Jaccard >= 0.6). LLM phase confirms each group and picks the
canonical primary; unconfirmed groups are left intact (conservative — never
merge on LLM failure).

Refactored 2026-06-21 for explicit stage naming.
"""
from pathlib import Path
import re
from _llm_api import call_anthropic_protocol

DEDUP_JACCARD_THRESHOLD = 0.6


def _stage_2_5_extract_concept_blocks(file_blocks):
    concepts = []
    for idx, (path, content) in enumerate(file_blocks):
        if "/concepts/" in path or path.startswith("concepts/"):
            title_match = re.search(r"title:\s*([^\n]+)", content)
            title = title_match.group(1).strip() if title_match else path.split("/")[-1]
            body_match = re.search(r"---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
            definition = (body_match.group(2) if body_match else content)[:300].lower()
            concepts.append({
                "slug": Path(path).stem,
                "title": title,
                "definition_snippet": definition,
                "block_index": idx,
                "full_content": content,
            })
    return concepts


_STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "and", "or", "to",
              "with", "by", "is", "are", "be", "as", "at", "from", "that", "this",
              "it", "its", "into", "using", "use", "used", "via", "per", "than"}


def _stage_2_5_word_set(s):
    return set(w for w in re.split(r"[\s,，。.;:、]+", s.lower())
               if len(w) > 1 and w not in _STOPWORDS)


def _stage_2_5_find_duplicate_concepts(concepts):
    duplicates = []
    processed = set()
    for i, c1 in enumerate(concepts):
        if i in processed:
            continue
        group = [i]
        processed.add(i)
        w1 = _stage_2_5_word_set(c1["title"] + " " + c1["definition_snippet"])
        for j in range(i + 1, len(concepts)):
            if j in processed:
                continue
            c2 = concepts[j]
            t_overlap = (c1["title"].lower() in c2["title"].lower() or
                         c2["title"].lower() in c1["title"].lower())
            w2 = _stage_2_5_word_set(c2["title"] + " " + c2["definition_snippet"])
            overlap = len(w1 & w2) / max(len(w1 | w2), 1)
            if (t_overlap and overlap >= 0.4) or overlap >= DEDUP_JACCARD_THRESHOLD:
                group.append(j)
                processed.add(j)
        if len(group) > 1:
            duplicates.append(group)
    return duplicates


def _stage_2_5_confirm_prompt(group_concepts):
    items = "\n\n".join(
        "### Concept {n}: {title}\nslug: {slug}\n{defn}".format(
            n=i + 1, title=c["title"], slug=c["slug"], defn=c["definition_snippet"])
        for i, c in enumerate(group_concepts)
    )
    return """You are reviewing concept pages generated from the same source for duplicates.

{items}

Are these concepts describing the SAME underlying concept (just named/worded differently)?
- If YES: reply `MERGE: yes | PRIMARY: <slug of the best canonical one> | REASON: <one sentence>`
- If NO:  reply `MERGE: no | REASON: <one sentence>`

When unsure, reply `MERGE: no`.
""".format(items=items)


def _stage_2_5_confirm_merge_with_llm(group_concepts, config):
    prompt = _stage_2_5_confirm_prompt(group_concepts)
    try:
        response, _ = call_anthropic_protocol(prompt, config, max_tokens=200, label="dedup-confirm")
    except Exception as e:
        print("  [stage 2.5] LLM confirm failed: {} — keeping all candidates".format(e))
        return False, ""
    m = re.search(r"MERGE:\s*(yes|no)", response, re.IGNORECASE)
    if not m or m.group(1).lower() != "yes":
        return False, ""
    pm = re.search(r"PRIMARY:\s*(\S+)", response)
    primary = pm.group(1).strip() if pm else ""
    return True, primary


def _stage_2_5_generate_merge_rules(concepts, duplicate_groups, config=None):
    rules = []
    for group in duplicate_groups:
        group_concepts = [concepts[i] for i in group]
        primary_slug = ""
        should_merge = True
        if config is not None:
            should_merge, primary_slug = _stage_2_5_confirm_merge_with_llm(group_concepts, config)
        if not should_merge:
            continue
        group_slugs = [c["slug"] for c in group_concepts]
        if not primary_slug or primary_slug not in group_slugs:
            primary_idx = max(group, key=lambda i: len(concepts[i]["definition_snippet"]))
            primary_slug = concepts[primary_idx]["slug"]
        duplicate_slugs = [c["slug"] for c in group_concepts if c["slug"] != primary_slug]
        if not duplicate_slugs:
            continue
        rules.append({
            "primary_slug": primary_slug,
            "primary_title": next(c["title"] for c in group_concepts if c["slug"] == primary_slug),
            "duplicate_slugs": duplicate_slugs,
            "merge_strategy": "union",
            "merge_reason": "LLM-confirmed duplicate ({} merged)".format(len(duplicate_slugs)),
        })
    return rules


def _stage_2_5_apply_merge_rules(file_blocks, merge_rules):
    if not merge_rules:
        return file_blocks
    slugs_to_delete = set()
    for rule in merge_rules:
        slugs_to_delete.update(rule["duplicate_slugs"])
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        if ("/concepts/" in path or path.startswith("concepts/")) and slug in slugs_to_delete:
            continue
        result.append((path, content))
    return result


def _stage_2_5_verify_dedup_merge(checkpoint, chunk_count):
    if chunk_count <= 1:
        checkpoint["concept_merge_rules"] = []
        return True
    return "concept_merge_rules" in checkpoint


# ── Backward-compat aliases ──
extract_concept_blocks = _stage_2_5_extract_concept_blocks
find_duplicate_concepts = _stage_2_5_find_duplicate_concepts
confirm_merge_with_llm = _stage_2_5_confirm_merge_with_llm
generate_merge_rules = _stage_2_5_generate_merge_rules
apply_merge_rules = _stage_2_5_apply_merge_rules
verify_dedup_merge = _stage_2_5_verify_dedup_merge
