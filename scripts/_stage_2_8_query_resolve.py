"""Stage 2.7 closing sub-step: Cross-source Query Resolution

For each generated query, find related existing wiki pages via an embedding
(cosine) semantic prefilter — NOT title word-Jaccard — so a natural-language
question ("什么是傅里叶变换") actually matches a noun-titled concept page
("Fourier Transform") it shares few literal words with. An LLM judge then
decides: closed (answer already exists) or kept (still open). Defaults to
"kept" on any uncertainty or LLM failure — never auto-deletes a query without
explicit LLM confirmation.

no-fallback: if the embedding stack is unavailable the prefilter RAISES (pauses
ingest) rather than degrading to Jaccard. Empty wiki (nothing to resolve
against) short-circuits to "kept" for every query WITHOUT embedding — that is a
genuine no-op, not a fallback.

Refactored 2026-06-21 for explicit stage naming; embedding prefilter 2026-06-29
(folded into Stage 2.7, the 2.8 number retired).
"""
from pathlib import Path
import re
from _llm_api import call_anthropic_protocol
from _stage_2_base import _stage_2_frontmatter_title
from _dedup_embedding import cosine_similarity, embed_pages, DuplicatePrefilterError

RESOLVE_COSINE_THRESHOLD = 0.82


def _stage_2_8_extract_query_blocks(file_blocks):
    queries = []
    for idx, (path, content) in enumerate(file_blocks):
        if "/queries/" in path or path.startswith("queries/"):
            title = _stage_2_frontmatter_title(content) or path.split("/")[-1]
            body = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
            queries.append({
                "slug": Path(path).stem,
                "title": title,
                "block_index": idx,
                "path": path,
                "body": body[:1500],
                "full_content": content,
            })
    return queries


def _stage_2_8_load_existing_pages(wiki_root):
    """Load existing concept/entity pages once (id namespaced by folder so a
    concept and entity sharing a stem don't collide in the embeddings dict)."""
    pages = []
    if not wiki_root.is_dir():
        return pages
    for sub in ("concepts", "entities"):
        page_dir = wiki_root / sub
        if not page_dir.is_dir():
            continue
        for page_file in page_dir.glob("*.md"):
            try:
                content = page_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            title = _stage_2_frontmatter_title(content)
            if not title:
                continue
            body = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL)
            pages.append({
                "id": f"{sub}/{page_file.stem}",
                "stem": page_file.stem,
                "title": title,
                "tags": [],
                "body": body[:1500],
            })
    return pages


def _stage_2_8_query_id(query):
    return "__query__" + query["slug"]


def _stage_2_8_embed_existing_and_queries(existing_pages, queries, *, min_success_ratio=0.8):
    """Embed existing pages + query pages in one batched call. no-fallback:
    raises DuplicatePrefilterError if too few embed (mirrors candidate_pairs)."""
    query_pages = [{"id": _stage_2_8_query_id(qy), "title": qy["title"],
                    "tags": [], "body": qy["body"]} for qy in queries]
    all_pages = existing_pages + query_pages
    embeddings = embed_pages(all_pages)
    embedded = [v for v in embeddings.values() if v]
    if all_pages and len(embedded) / len(all_pages) < min_success_ratio:
        raise DuplicatePrefilterError(
            f"embedded only {len(embedded)}/{len(all_pages)} query-resolution pages")
    return embeddings


def _stage_2_8_find_related_wiki_pages(query, existing_pages, embeddings,
                                       threshold=RESOLVE_COSINE_THRESHOLD, top_k=8):
    """Cosine-rank existing pages against one query; return the top_k above
    threshold as (stem, title). Empty when the query failed to embed."""
    qvec = embeddings.get(_stage_2_8_query_id(query))
    if not qvec:
        return []
    scored = []
    for page in existing_pages:
        sim = cosine_similarity(qvec, embeddings.get(page["id"]))
        if sim >= threshold:
            scored.append((sim, page["stem"], page["title"]))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(stem, title) for _, stem, title in scored[:top_k]]


def _stage_2_8_judge_prompt(query, related):
    pages = "\n".join("- [[{}]]: {}".format(slug, title) for slug, title in related[:8])
    if not pages:
        pages = "(none found)"
    return """You are judging whether an open question from a newly-ingested source is already answered by existing wiki pages.

Query title: {title}
Query body:
{body}

Existing wiki pages that may relate:
{pages}

Decide:
- "closed": the existing pages fully answer this question -> the query can be removed.
- "kept": the existing pages do NOT (or only partially) answer it -> keep as an open query.

When unsure, choose "kept" — never close a question without clear evidence.

Reply with exactly one line: STATUS: <closed|kept> | REASON: <one sentence>
""".format(title=query["title"], body=query["body"], pages=pages)


def _stage_2_8_judge_query_resolution(query, related, config):
    if not related:
        return "kept", "no related wiki pages"
    prompt = _stage_2_8_judge_prompt(query, related)
    try:
        response, _ = call_anthropic_protocol(prompt, config, max_tokens=200, label="query-resolve")
    except Exception as e:
        print("  [stage 2.7] LLM judge failed for '{}': {} — defaulting to kept".format(query["slug"], e))
        return "kept", "llm-unavailable"
    m = re.search(r"STATUS:\s*(closed|kept)", response, re.IGNORECASE)
    if not m:
        print("  [stage 2.7] Could not parse judge response for '{}' — defaulting to kept".format(query["slug"]))
        return "kept", "unparseable"
    status = m.group(1).lower()
    reason = ""
    rm = re.search(r"REASON:\s*(.+)", response)
    if rm:
        reason = rm.group(1).strip()
    return status, reason


def stage_2_8_resolve_queries(file_blocks, wiki_root, config, *, embeddings=None):
    resolutions = {}
    queries = _stage_2_8_extract_query_blocks(file_blocks)
    if not queries:
        return resolutions

    existing_pages = _stage_2_8_load_existing_pages(wiki_root)
    if not existing_pages:
        # Empty wiki: nothing to resolve against. Keep every query without
        # embedding (genuine no-op, not a fallback) — avoids a spurious raise
        # on the very first ingest into an empty wiki.
        for query in queries:
            resolutions[query["slug"]] = {
                "status": "kept", "resolution_pages": [], "reason": "no existing wiki pages"}
        return resolutions

    if embeddings is None:
        embeddings = _stage_2_8_embed_existing_and_queries(existing_pages, queries)

    for query in queries:
        related = _stage_2_8_find_related_wiki_pages(query, existing_pages, embeddings)
        status, reason = _stage_2_8_judge_query_resolution(query, related, config)
        resolutions[query["slug"]] = {
            "status": status,
            "resolution_pages": [s for s, _ in related],
            "reason": reason,
        }
        print("  [stage 2.7] query '{}' -> {} ({})".format(query["slug"], status, reason))
    return resolutions


def _stage_2_8_update_file_blocks_after_resolution(file_blocks, resolutions):
    closed_slugs = {slug for slug, res in resolutions.items() if res["status"] == "closed"}
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        if ("/queries/" in path or path.startswith("queries/")) and slug in closed_slugs:
            continue
        result.append((path, content))
    return result


def _stage_2_8_verify_query_resolution(checkpoint):
    return "query_resolutions" in checkpoint
