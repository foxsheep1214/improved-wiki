"""Stage 2.8: Cross-source Query Resolution

For each generated query, search existing wiki pages and use an LLM judge to
decide: closed (answer exists), incomplete (-> comparison), or kept (open).
Defaults to "kept" on any uncertainty or LLM failure — never auto-deletes
a query without explicit LLM confirmation.

Refactored 2026-06-21 for explicit stage naming.
"""
from pathlib import Path
import re
from _llm_api import call_anthropic_protocol
from _stage_2_base import _stage_2_frontmatter_title, _stage_2_title_words


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


def _stage_2_8_find_related_wiki_pages(wiki_root, query_title, threshold=0.6):
    if not wiki_root.is_dir():
        return []
    related = []
    q_words = _stage_2_title_words(query_title)
    if not q_words:
        return related
    for page_dir in [wiki_root / "concepts", wiki_root / "entities"]:
        if not page_dir.is_dir():
            continue
        for page_file in page_dir.glob("*.md"):
            try:
                content = page_file.read_text(encoding="utf-8", errors="ignore")
                title = _stage_2_frontmatter_title(content)
                if not title:
                    continue
                p_words = _stage_2_title_words(title)
                if not p_words:
                    continue
                if len(q_words & p_words) / len(q_words | p_words) >= threshold:
                    related.append((page_file.stem, title))
            except Exception:
                pass
    return related


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
- "incomplete": the existing pages partially address it -> better expressed as a comparison page.
- "kept": the existing pages do NOT answer it -> keep as an open query.

When unsure, choose "kept" — never close a question without clear evidence.

Reply with exactly one line: STATUS: <closed|incomplete|kept> | REASON: <one sentence>
""".format(title=query["title"], body=query["body"], pages=pages)


def _stage_2_8_judge_query_resolution(query, related, config):
    if not related:
        return "kept", "no related wiki pages"
    prompt = _stage_2_8_judge_prompt(query, related)
    try:
        response, _ = call_anthropic_protocol(prompt, config, max_tokens=200, label="query-resolve")
    except Exception as e:
        print("  [stage 2.8] LLM judge failed for '{}': {} — defaulting to kept".format(query["slug"], e))
        return "kept", "llm-unavailable"
    m = re.search(r"STATUS:\s*(closed|incomplete|kept)", response, re.IGNORECASE)
    if not m:
        print("  [stage 2.8] Could not parse judge response for '{}' — defaulting to kept".format(query["slug"]))
        return "kept", "unparseable"
    status = m.group(1).lower()
    reason = ""
    rm = re.search(r"REASON:\s*(.+)", response)
    if rm:
        reason = rm.group(1).strip()
    return status, reason


def stage_2_8_resolve_queries(file_blocks, wiki_root, config):
    resolutions = {}
    queries = _stage_2_8_extract_query_blocks(file_blocks)
    for query in queries:
        related = _stage_2_8_find_related_wiki_pages(wiki_root, query["title"])
        status, reason = _stage_2_8_judge_query_resolution(query, related, config)
        resolutions[query["slug"]] = {
            "status": status,
            "resolution_pages": [s for s, _ in related],
            "reason": reason,
        }
        print("  [stage 2.8] query '{}' -> {} ({})".format(query["slug"], status, reason))
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
