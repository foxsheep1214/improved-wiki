"""Stage 2.4 closing sub-step: 源内去重 (intra-source dedup) — concept AND
entity collapse within ONE source (entities added 2026-07-02, audit A1: entity
duplicates like `billingsley`×3 previously bypassed dedup entirely; the two
pools are deduped separately, never merged across the folder boundary).

Runs during ingest, BEFORE write, as a filter on the LLM's just-generated
file_blocks for this one book. Catches the case where the LLM names the same
concept twice within a single source (e.g. emits both `PAO` and `聚磷菌`
blocks). Candidate groups come from an embedding (cosine) semantic prefilter —
NOT word-Jaccard — so cross-language / synonym duplicates (傅里叶变换 vs Fourier
transform, word-overlap ≈ 0) are actually caught; each group is then confirmed
by an LLM (unconfirmed groups are left intact — conservative, never merge on
LLM failure). no-fallback: if the embedding stack is unavailable the prefilter
RAISES (pauses ingest) rather than silently degrading to Jaccard. Does NOT
rewrite cross-references (pages not written yet) and does NOT look at the
existing wiki (cross-source awareness is Stage 2.3's job). This is distinct
from the lint-time cross-source dedup (跨源去重, `cross_source_dedup.py`) which
merges across the whole wiki.

Refactored 2026-06-21 for explicit stage naming; embedding prefilter 2026-06-29
(folded into Stage 2.4, the 2.5 number retired).
"""
from pathlib import Path
import re
from _llm_api import call_anthropic_protocol
from _frontmatter import WIKILINK_RE as _WIKILINK_RE
from _stage_2_base import _stage_2_frontmatter_title
from _dedup_embedding import candidate_pairs, cluster_by_pairs

DEDUP_COSINE_THRESHOLD = 0.82


def _dedup_extract_concept_blocks(file_blocks, folder="concepts"):
    """Extract this ingest's just-generated page blocks for one folder.

    ``folder`` selects the pool ("concepts" or "entities"). A1 (audit
    2026-07-02, H1 layer 1): entities were never extracted, so same-ingest
    entity duplicates (`billingsley` / `j-b-billingsley` /
    `billingsley-j-b-billingsley`, Skolnik same night) sailed past dedup
    entirely. Each item carries its ``folder`` so merge rules stay
    pool-scoped (a concept never merges into an entity or vice versa).
    """
    prefix = f"{folder}/"
    marker = f"/{prefix}"
    concepts = []
    for idx, (path, content) in enumerate(file_blocks):
        if marker in path or path.startswith(prefix):
            title = _stage_2_frontmatter_title(content) or path.split("/")[-1]
            body_match = re.search(r"---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
            definition = (body_match.group(2) if body_match else content)[:300].lower()
            concepts.append({
                "slug": Path(path).stem,
                "title": title,
                "definition_snippet": definition,
                "block_index": idx,
                "full_content": content,
                "folder": folder,
            })
    return concepts


def _dedup_find_duplicate_concepts(concepts, *, embeddings=None):
    """Embedding (cosine) prefilter over this source's just-generated concepts.

    Returns candidate groups as lists of indices into ``concepts`` (groups of
    >1), to be confirmed by the LLM. ``embeddings`` (slug→vec) may be injected
    for tests; otherwise vectors are computed live via the Ollama stack. Lets
    DuplicatePrefilterError propagate (no-fallback) when too few concepts embed.
    """
    if len(concepts) < 2:
        return []
    pages = [{"id": c["slug"], "title": c["title"], "tags": [],
              "body": c["definition_snippet"]} for c in concepts]
    pairs = candidate_pairs(pages, threshold=DEDUP_COSINE_THRESHOLD, embeddings=embeddings)
    clusters = cluster_by_pairs([p["id"] for p in pages], pairs)
    slug_to_index = {c["slug"]: i for i, c in enumerate(concepts)}
    return [[slug_to_index[sid] for sid in cl] for cl in clusters]


def _dedup_confirm_prompt(groups_concepts):
    """Batched confirm prompt: ALL candidate groups in ONE call.

    Was one LLM call per group (Finding C: N groups → N conversation-mode
    handoffs). NashSU batches its dedup detector similarly. Each group is
    numbered; the model returns one verdict line per group.
    """
    sections = []
    for gi, group_concepts in enumerate(groups_concepts, 1):
        items = "\n".join(
            "  - {title} (slug: {slug}): {defn}".format(
                title=c["title"], slug=c["slug"], defn=c["definition_snippet"])
            for c in group_concepts
        )
        sections.append("## Group {gi}\n{items}".format(gi=gi, items=items))
    body = "\n\n".join(sections)
    return """You are reviewing groups of concept pages generated from the same source for duplicates.
Each group below is a set of candidate concepts that MIGHT be the same underlying concept.

{body}

For EACH group, decide whether its concepts describe the SAME underlying concept
(just named/worded differently). Reply with EXACTLY one line per group:
- If SAME: `GROUP <n>: MERGE yes | PRIMARY: <slug of the best canonical one>`
- If NOT:  `GROUP <n>: MERGE no`

When unsure, reply `GROUP <n>: MERGE no`.
""".format(body=body)


def _dedup_confirm_merges_with_llm(groups_concepts, config):
    """One LLM call confirming ALL candidate groups. Returns a list of
    (should_merge, primary_slug) aligned to ``groups_concepts``. Conservative:
    any group whose verdict is missing/unparseable/not-yes → (False, "")."""
    prompt = _dedup_confirm_prompt(groups_concepts)
    try:
        response, _ = call_anthropic_protocol(prompt, config, max_tokens=400, label="dedup-confirm")
    except Exception as e:
        print("  [stage 2.4] LLM confirm failed: {} — keeping all candidates".format(e))
        return [(False, "")] * len(groups_concepts)
    verdicts = []
    for gi in range(1, len(groups_concepts) + 1):
        m = re.search(r"GROUP\s+{}\s*:\s*MERGE\s*(yes|no)".format(gi), response, re.IGNORECASE)
        if not m or m.group(1).lower() != "yes":
            verdicts.append((False, ""))
            continue
        line = response[m.start():].split("\n", 1)[0]
        pm = re.search(r"PRIMARY:?\s*(\S+)", line)
        primary = pm.group(1).strip().strip("|").strip() if pm else ""
        verdicts.append((True, primary))
    return verdicts


def _dedup_generate_merge_rules(concepts, duplicate_groups, config=None):
    rules = []
    if not duplicate_groups:
        return rules
    groups_concepts = [[concepts[i] for i in group] for group in duplicate_groups]
    # Conservative default: never merge without an LLM confirmation — a missing
    # config must not silently merge every candidate group.
    if config is None:
        return rules
    verdicts = _dedup_confirm_merges_with_llm(groups_concepts, config)
    for group_concepts, (should_merge, primary_slug) in zip(groups_concepts, verdicts):
        if not should_merge:
            continue
        group_slugs = [c["slug"] for c in group_concepts]
        if not primary_slug or primary_slug not in group_slugs:
            primary = max(group_concepts, key=lambda c: len(c["definition_snippet"]))
            primary_slug = primary["slug"]
        duplicate_slugs = [c["slug"] for c in group_concepts if c["slug"] != primary_slug]
        if not duplicate_slugs:
            continue
        rules.append({
            "primary_slug": primary_slug,
            "primary_title": next(c["title"] for c in group_concepts if c["slug"] == primary_slug),
            "duplicate_slugs": duplicate_slugs,
            "merge_strategy": "union",
            "merge_reason": "LLM-confirmed duplicate ({} merged)".format(len(duplicate_slugs)),
            # Pool the group came from (groups never span pools) — apply uses
            # it so deleting an entity dup can't shadow a same-stem concept.
            "folder": group_concepts[0].get("folder", "concepts"),
        })
    return rules




def _dedup_rewrite_wikilinks(content, slug_map, current_slug=""):
    """Redirect [[target]] / [[target|text]] wikilinks pointing at a merged
    duplicate slug to the merge's primary slug instead. Handles both the bare
    stem and the `concepts/<slug>` path form (case-insensitive per
    naming-conventions.md), since merging deletes the duplicate's FILE block
    and any sibling block still pointing at it would otherwise become a
    permanently broken link the moment Stage 3.1 writes to disk.

    When the redirect target IS the current page (``current_slug``, i.e. the
    PRIMARY page linked to its own merged-away duplicate), the link is
    de-linked to plain text (display text if present, else the bare stem —
    same convention as _frontmatter's wikilink strip) instead of becoming a
    self-link (fix 2026-07-02).
    """
    def _sub(m):
        target = m.group(1)
        pipe = f"|{m.group(2)}" if m.group(2) else ""
        bare = target.rsplit("/", 1)[-1]
        new_slug = slug_map.get(bare.lower())
        if new_slug is None:
            return m.group(0)
        if current_slug and new_slug.lower() == current_slug.lower():
            return m.group(2) or bare
        new_target = target.rsplit("/", 1)[0] + "/" + new_slug if "/" in target else new_slug
        return f"[[{new_target}{pipe}]]"
    return _WIKILINK_RE.sub(_sub, content)


_STAGE_2_5_RELATED_LINE_RE = re.compile(r"^(related:[ \t]*\[)([^\]\r\n]*)(\][ \t]*)$", re.MULTILINE)


def _dedup_rewrite_related(content, slug_map, current_slug=""):
    """Rewrite frontmatter ``related:`` inline-array entries pointing at a
    merged duplicate slug to the primary slug. Entries are bare stems
    (optionally quoted and/or `concepts/`-prefixed) — the wikilink rewrite
    above never sees them, so without this the merged page's siblings keep a
    dangling related entry on disk (observed live 2026-07-02: 9+ broken links
    after 2.4-dedup merges). Entries that would now reference the page itself
    (on the PRIMARY page) are dropped; the rewritten list is de-duplicated.
    Lines with no rewritten entry are left byte-identical.
    """
    m = _STAGE_2_5_RELATED_LINE_RE.search(content)
    if not m or not m.group(2).strip():
        return content
    items, seen, changed = [], set(), False
    for raw in m.group(2).split(","):
        item = raw.strip().strip("'\"")
        if not item:
            continue
        bare = item.rsplit("/", 1)[-1]
        new_slug = slug_map.get(bare.lower())
        if new_slug is not None:
            changed = True
            if current_slug and new_slug.lower() == current_slug.lower():
                continue
            item = item.rsplit("/", 1)[0] + "/" + new_slug if "/" in item else new_slug
        if item.lower() in seen:
            continue
        seen.add(item.lower())
        items.append(item)
    if not changed:
        return content
    inner = ", ".join('"{}"'.format(i.replace('"', '\\"')) for i in items)
    return content[:m.start(2)] + inner + content[m.end(2):]


def _dedup_apply_merge_rules(file_blocks, merge_rules):
    if not merge_rules:
        return file_blocks
    # Deletions are (folder, slug)-keyed so an entity merge can never delete a
    # same-stem concept block (A1: entities now participate in dedup too).
    slugs_to_delete = set()
    slug_map = {}
    for rule in merge_rules:
        folder = rule.get("folder", "concepts")
        for dup_slug in rule["duplicate_slugs"]:
            slugs_to_delete.add((folder, dup_slug))
            slug_map[dup_slug.lower()] = rule["primary_slug"]
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        block_folder = next(
            (f for f in ("concepts", "entities")
             if f"/{f}/" in path or path.startswith(f"{f}/")),
            None,
        )
        if block_folder and (block_folder, slug) in slugs_to_delete:
            continue
        if slug_map:
            content = _dedup_rewrite_wikilinks(content, slug_map, current_slug=slug)
            content = _dedup_rewrite_related(content, slug_map, current_slug=slug)
        result.append((path, content))
    return result


def dedup_intra_source(file_blocks, chunk_analyses, config, *, verbose: bool = False) -> dict:
    """In-source concept dedup & merge (2.4 closing sub-step, ex-Stage 2.5; multi-chunk books only).

    Runs before the source page so the index lists de-duplicated concepts.
    Single-chunk sources skip dedup. Returns a dict with the new file_blocks,
    dedup_was_run flag, and before/after concept counts.
    """
    concept_count_before = sum(1 for p, _ in file_blocks if "/concepts/" in p)
    entity_count_before = sum(1 for p, _ in file_blocks if "/entities/" in p)
    dedup_was_run = len(chunk_analyses) > 1
    if not dedup_was_run:
        print(f"  [stage 2.4] Skipped (single chunk; {concept_count_before} concepts)")
        return {
            "file_blocks": file_blocks,
            "dedup_was_run": False,
            "concept_count_before": concept_count_before,
            "concept_count_after": concept_count_before,
        }

    # A1 (audit 2026-07-02): entities join the dedup — separate candidate pool
    # (never merged across the concepts/entities boundary), but ONE batched
    # confirm call for both pools (conversation-mode handoffs are expensive).
    concepts = _dedup_extract_concept_blocks(file_blocks)
    entities = _dedup_extract_concept_blocks(file_blocks, folder="entities")
    concept_groups = _dedup_find_duplicate_concepts(concepts)
    entity_groups = _dedup_find_duplicate_concepts(entities)
    items = concepts + entities
    offset = len(concepts)
    groups = concept_groups + [[i + offset for i in g] for g in entity_groups]
    merge_rules = _dedup_generate_merge_rules(items, groups, config=config)
    file_blocks = _dedup_apply_merge_rules(file_blocks, merge_rules)
    concept_count_after = sum(1 for p, _ in file_blocks if "/concepts/" in p)
    entity_count_after = sum(1 for p, _ in file_blocks if "/entities/" in p)
    if merge_rules:
        print(f"  [stage 2.4] Dedup: {concept_count_before} → {concept_count_after} "
              f"concepts, {entity_count_before} → {entity_count_after} entities "
              f"({len(merge_rules)} merge rule(s))")
    else:
        print(f"  [stage 2.4] No duplicate concepts/entities "
              f"({concept_count_after} concepts, {entity_count_after} entities)")
    return {
        "file_blocks": file_blocks,
        "dedup_was_run": True,
        "concept_count_before": concept_count_before,
        "concept_count_after": concept_count_after,
    }
