
from _stage_2_base import *
from _language import build_language_directive

# Digest packing (fix 2026-07-02): the old `digest_str[:12000] + truncated`
# cut mid-JSON (observed truncating mid-claim). Pack whole keys by priority
# instead — never cut mid-structure.
_STAGE_2_7_DIGEST_KEY_PRIORITY = ("book_meta", "outline", "key_claims", "key_entities", "key_concepts")
_STAGE_2_7_DIGEST_CHAR_BUDGET = 24000


def _stage_2_7_pack_digest(global_digest: dict, budget: int = _STAGE_2_7_DIGEST_CHAR_BUDGET) -> str:
    """Serialize the global digest for the prompt, whole keys only.

    Keys are considered in priority order (then any remaining keys in digest
    order); each key is included WHOLE while the running total stays within
    ``budget`` chars, otherwise skipped and listed in a one-line trailing
    note. A key is never cut mid-structure.
    """
    keys = [k for k in _STAGE_2_7_DIGEST_KEY_PRIORITY if k in global_digest]
    keys += [k for k in global_digest if k not in _STAGE_2_7_DIGEST_KEY_PRIORITY]
    included, omitted, total = {}, [], 2  # 2 = outer braces
    for key in keys:
        piece_len = len(json.dumps({key: global_digest[key]}, ensure_ascii=False, indent=2))
        if total + piece_len > budget:
            value = global_digest[key]
            n_items = len(value) if isinstance(value, (list, dict, str)) else 1
            omitted.append(f"{key} ({n_items} items)")
            continue
        included[key] = global_digest[key]
        total += piece_len
    digest_str = json.dumps(included, ensure_ascii=False, indent=2)
    if omitted:
        digest_str += "\n...(omitted keys: " + ", ".join(omitted) + ")"
    return digest_str


def _stage_2_7_build_prompt(
    global_digest: dict,
    concept_titles: list[str],
    entity_titles: list[str],
    key_claims: list[dict],
    file_path: Path,
    config: Config,
    source_context: str = "",
) -> str:
    """Build prompt for Stage 2.7: generate open questions from single-source analysis."""
    digest_str = _stage_2_7_pack_digest(global_digest)

    # P1 parity with Stage 2.4 (2026-06-27): ground questions in the raw source so
    # the LLM raises the questions the SOURCE actually leaves open, instead of
    # inventing generic open questions from training memory.
    if source_context.strip():
        source_section = (
            "\n# Source Text (ground questions in THIS — do not invent from memory)\n"
            "Base open questions on what the source ACTUALLY says and leaves unresolved:\n"
            "use its own framing, numbers, and the gaps IT exposes. Do not fabricate\n"
            "questions from generic domain knowledge.\n"
            "<source>\n"
            f"{source_context}\n"
            "</source>\n"
        )
    else:
        source_section = ""

    # Generous caps (2026-07-02): [:80]/[:40] silently hid 20% of a mid-size
    # book's generated pages from the related-link candidate lists (observed
    # live: "101 declared, 80 listed"). Titles are ~30 chars each — even 600
    # is <20K chars in the prompt.
    concepts_str = '\n'.join(f"- {c}" for c in concept_titles[:600])
    entities_str = '\n'.join(f"- {e}" for e in entity_titles[:300])
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

    language_directive = build_language_directive(source_context or digest_str)
    return f"""{language_directive}

# Role
You are maintaining a Karpathy-pattern knowledge base wiki. You have just finished generating source/concept/entity pages for a book.

# Book Context
- Title: {file_path.stem}
- Canonical source path: raw/{raw_rel}
- Global Digest (summary):
```yaml
{digest_str}
```
{source_section}
# Generated Concepts ({len(concept_titles)} total)
{concepts_str if concepts_str else '(none)'}

# Generated Entities ({len(entity_titles)} total)
{entities_str if entities_str else '(none)'}

# Key Claims from the Book
{claims_str if claims_str else '(none)'}

# Existing Wiki Pages (avoid referencing non-existent pages)
{', '.join(existing_slugs)}

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
- Each query body >=200 chars (excluding frontmatter)
- START IMMEDIATELY with ---FILE: or ---QUERIES: — no preamble
"""


def stage_2_7_query_generation(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_blocks: list[tuple[str, str]],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    source_context: str = "",
) -> tuple[list[tuple[str, str]], str]:
    """Stage 2.7: Generate query pages (open questions) from single-source analysis.

    Returns (new_query_blocks, raw_response).
    Skips for datasheet/standard source types.
    """
    # Skip for datasheet/standard — pure fact listing, no meaningful open questions
    try:
        src_type = detect_template_type(file_path, config)
    except Exception:
        src_type = None
    if src_type in ("datasheet", "standard"):
        if verbose:
            print(f"[stage 2.7] Skipped — {src_type} source type (no meaningful open questions)")
        return [], ""

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
            print("[stage 2.7] Skipped — no concepts generated")
        return [], ""

    prompt = _stage_2_7_build_prompt(
        global_digest, concept_titles, entity_titles,
        key_claims, file_path, config,
        source_context=source_context,
    )

    query_tokens = config.compute_max_tokens(4096)
    if verbose:
        print(f"[stage 2.7] Query generation — {len(concept_titles)} concepts, "
              f"{len(key_claims)} claims, prompt {len(prompt):,} chars...")

    try:
        response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=query_tokens)
    except Exception as e:
        print(f"[stage 2.7] LLM call failed: {e}")
        return [], ""

    if verbose:
        print(f"[stage 2.7] Response ({len(response)} chars, stop={stop_reason}):\n{response[:2000]}...\n")

    # Parse query FILE blocks
    query_blocks = parse_file_blocks(response)
    if query_blocks:
        print(f"[stage 2.7] Generated {len(query_blocks)} query page(s)")
        for path, _ in query_blocks:
            print(f"  → {path}")
    elif "---QUERIES: 0---" in response or "QUERIES: 0" in response:
        print("[stage 2.7] No worthwhile queries (---QUERIES: 0---)")
    else:
        print("[stage 2.7] No query blocks parsed (may be implicit ---QUERIES: 0---)")

    return query_blocks, response
