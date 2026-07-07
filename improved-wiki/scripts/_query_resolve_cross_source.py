"""Stage 2.7 closing sub-step: Cross-source Query Resolution

For each generated query, find related existing wiki pages via an embedding
(cosine) semantic prefilter — NOT title word-Jaccard — so a natural-language
question ("什么是傅里叶变换") actually matches a noun-titled concept page
("Fourier Transform") it shares few literal words with. An LLM judge then
decides: closed (answer already exists) or kept (still open) — ALL queries are
judged in ONE batched handoff (one verdict line per query), not N sequential
calls. Defaults to "kept" on any uncertainty or LLM failure — never
auto-deletes a query without explicit LLM confirmation.

no-fallback: if the embedding stack is unavailable the prefilter RAISES (pauses
ingest) rather than degrading to Jaccard. Empty wiki (nothing to resolve
against) short-circuits to "kept" for every query WITHOUT embedding — that is a
genuine no-op, not a fallback.

Refactored 2026-06-21 for explicit stage naming; embedding prefilter 2026-06-29
(folded into Stage 2.7, the 2.8 number retired).
"""
from pathlib import Path
import re
from _frontmatter_array import write_frontmatter_array
from _llm_api import call_anthropic_protocol
from _stage_2_base import _stage_2_frontmatter_title
from _dedup_embedding import cosine_similarity, embed_pages, DuplicatePrefilterError

# Audit A3 (2026-07-02): 0.82 was borrowed from symmetric page-page dedup;
# question-phrased query titles vs noun-phrased concept titles cap cosine
# around 0.75-0.79 in production (bge-m3), so the LLM judge historically NEVER
# fired. 0.70 admits real matches; candidates are no longer gated by this
# threshold at all (top-k all go to the judge, default kept) — it now only
# marks which candidates are recorded as resolve conclusions (cross_refs).
RESOLVE_COSINE_THRESHOLD = 0.70


def _query_resolve_extract_query_blocks(file_blocks):
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


def _query_resolve_load_existing_pages(wiki_root):
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


def _query_resolve_query_id(query):
    return "__query__" + query["slug"]


def _query_resolve_embed_existing_and_queries(existing_pages, queries, *, min_success_ratio=0.8):
    """Embed existing pages + query pages in one batched call. no-fallback:
    raises DuplicatePrefilterError if too few embed (mirrors candidate_pairs)."""
    query_pages = [{"id": _query_resolve_query_id(qy), "title": qy["title"],
                    "tags": [], "body": qy["body"]} for qy in queries]
    all_pages = existing_pages + query_pages
    embeddings = embed_pages(all_pages)
    embedded = [v for v in embeddings.values() if v]
    if all_pages and len(embedded) / len(all_pages) < min_success_ratio:
        raise DuplicatePrefilterError(
            f"embedded only {len(embedded)}/{len(all_pages)} query-resolution pages")
    return embeddings


def _query_resolve_find_related_wiki_pages(query, existing_pages, embeddings, top_k=8):
    """Cosine-rank existing pages against one query; return the top_k as
    (page_id, title, similarity) triples — NO threshold gate (audit A3: the
    old >=0.82 gate filtered out even the best real matches, so the judge
    never fired). The judge sees every top-k candidate and decides, defaulting
    to kept. Empty when the query failed to embed."""
    qvec = embeddings.get(_query_resolve_query_id(query))
    if not qvec:
        return []
    scored = []
    for page in existing_pages:
        sim = cosine_similarity(qvec, embeddings.get(page["id"]))
        scored.append((sim, page["id"], page["title"]))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(pid, title, sim) for sim, pid, title in scored[:top_k]]


def _query_resolve_batch_judge_prompt(judged):
    """ONE prompt covering every query that has candidate pages (2026-07-02:
    N queries previously meant N sequential single-line judge handoffs, but the
    resolve step knows all queries upfront). One verdict line per query in a
    fixed parseable format. A single-query book produces the same batch prompt
    with one entry — no special case."""
    entries = []
    for query, related in judged:
        pages = "\n".join("- [[{}]]: {}".format(pid, title) for pid, title, _sim in related[:8])
        entries.append(
            "### Query slug: {slug}\nTitle: {title}\nBody:\n{body}\n"
            "Existing wiki pages that may relate:\n{pages}".format(
                slug=query["slug"], title=query["title"],
                body=query["body"], pages=pages))
    return """You are judging whether open questions from a newly-ingested source are already answered by existing wiki pages.

For EACH query below, decide:
- "closed": the existing pages fully answer this question -> the query can be removed.
- "kept": the existing pages do NOT (or only partially) answer it -> keep as an open query.

When unsure, choose "kept" — never close a question without clear evidence.

{entries}

Reply with EXACTLY one line per query, in the same order, no other text.
Line format (slug first, verbatim from "Query slug:"):
<slug>: STATUS: <closed|kept> | REASON: <one sentence>
""".format(entries="\n\n".join(entries))


_STAGE_2_8_VERDICT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<slug>\S+?)\s*[:：]\s*STATUS\s*[:：]?\s*(?P<status>closed|kept)\b"
    r"(?:\s*\|\s*REASON\s*[:：]?\s*(?P<reason>.*\S))?",
    re.IGNORECASE)


def _query_resolve_parse_batch_verdicts(response, expected_slugs):
    """Parse per-query verdict lines from the single batch response into
    {slug: (status, reason)}. Lenient on decoration (bullets, backticks,
    [[ ]], a queries/ prefix) but strict on the STATUS keyword; unknown slugs
    are ignored and the first verdict per slug wins. Missing slugs are the
    caller's job (default kept, loud warn)."""
    verdicts = {}
    expected = set(expected_slugs)
    for line in response.splitlines():
        m = _STAGE_2_8_VERDICT_RE.match(line)
        if not m:
            continue
        slug = m.group("slug").strip("`[]").rsplit("/", 1)[-1]
        if slug not in expected or slug in verdicts:
            continue
        verdicts[slug] = (m.group("status").lower(),
                          (m.group("reason") or "").strip())
    return verdicts


def _query_resolve_judge_queries_batch(judged, config):
    """Judge all candidate-bearing queries in ONE conversation handoff.

    Returns {slug: (status, reason)} covering every judged query. Defaults to
    "kept" on any failure — whole-call LLM error, or a missing/unparseable
    verdict line (loud warn) — never closes a query without explicit LLM
    confirmation."""
    if not judged:
        return {}
    slugs = [query["slug"] for query, _related in judged]
    prompt = _query_resolve_batch_judge_prompt(judged)
    try:
        response, _ = call_anthropic_protocol(
            prompt, config, max_tokens=max(400, 120 * len(judged)),
            label="query-resolve")
    except Exception as e:
        print("  [stage 2.7] LLM batch judge failed ({} queries): {} — "
              "defaulting ALL to kept".format(len(judged), e))
        return {slug: ("kept", "llm-unavailable") for slug in slugs}
    verdicts = _query_resolve_parse_batch_verdicts(response, slugs)
    results = {}
    for slug in slugs:
        if slug in verdicts:
            results[slug] = verdicts[slug]
        else:
            print("  [stage 2.7][WARN] batch judge response has no parseable "
                  "verdict for '{}' — defaulting to kept".format(slug))
            results[slug] = ("kept", "unparseable")
    return results


def query_resolve_cross_source(file_blocks, wiki_root, config, *, embeddings=None):
    resolutions = {}
    queries = _query_resolve_extract_query_blocks(file_blocks)
    if not queries:
        return resolutions

    existing_pages = _query_resolve_load_existing_pages(wiki_root)
    if not existing_pages:
        # Empty wiki: nothing to resolve against. Keep every query without
        # embedding (genuine no-op, not a fallback) — avoids a spurious raise
        # on the very first ingest into an empty wiki.
        for query in queries:
            resolutions[query["slug"]] = {
                "status": "kept", "resolution_pages": [], "reason": "no existing wiki pages"}
        return resolutions

    if embeddings is None:
        embeddings = _query_resolve_embed_existing_and_queries(existing_pages, queries)

    # Batch judge (2026-07-02): every candidate-bearing query goes into ONE
    # handoff instead of N sequential single-line judge calls. Queries with no
    # candidates (failed embed) keep the kept short-circuit — nothing to judge.
    judged = []
    for query in queries:
        related = _query_resolve_find_related_wiki_pages(query, existing_pages, embeddings)
        if related:
            judged.append((query, related))
        else:
            resolutions[query["slug"]] = {
                "status": "kept", "resolution_pages": [],
                "reason": "no related wiki pages"}
            print("  [stage 2.7] query '{}' -> kept (no related wiki pages)".format(
                query["slug"]))
    verdicts = _query_resolve_judge_queries_batch(judged, config)
    for query, related in judged:
        status, reason = verdicts[query["slug"]]
        resolutions[query["slug"]] = {
            "status": status,
            # Top-k candidates ALL go to the judge; only >=threshold ones are
            # recorded as resolve conclusions (and written back as cross_refs).
            "resolution_pages": [pid for pid, _t, sim in related
                                 if sim >= RESOLVE_COSINE_THRESHOLD],
            "reason": reason,
        }
        print("  [stage 2.7] query '{}' -> {} ({})".format(query["slug"], status, reason))
    return resolutions


def _query_resolve_update_file_blocks_after_resolution(file_blocks, resolutions):
    closed_slugs = {slug for slug, res in resolutions.items() if res["status"] == "closed"}
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        if ("/queries/" in path or path.startswith("queries/")) and slug in closed_slugs:
            continue
        result.append((path, content))
    return result


def _query_resolve_apply_cross_refs(file_blocks, resolutions):
    """Write resolve conclusions back into kept query pages' frontmatter as a
    `cross_refs:` list (audit A3/H3: resolution_pages previously lived only in
    the progress cache — the on-disk query page carried no trace of the
    resolve step). Closed queries are already dropped by
    _query_resolve_update_file_blocks_after_resolution; queries with no
    above-threshold pages are left untouched (no empty cross_refs field).
    Returns a new list — never mutates the input blocks."""
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        res = resolutions.get(slug)
        if (("/queries/" in path or path.startswith("queries/"))
                and res and res.get("status") == "kept"
                and res.get("resolution_pages")):
            content = write_frontmatter_array(content, "cross_refs", res["resolution_pages"])
        result.append((path, content))
    return result
