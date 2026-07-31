#!/usr/bin/env python3
"""_wiki_keyword.py — keyword search over wiki/, ported from NashSU
``src-tauri/src/commands/search.rs``.

Pure-Python keyword retrieval with CJK bigram tokenization, stopword
filtering, and the same weighted scoring table as NashSU's Rust backend:

  filename exact match     +200
  title contains phrase     +50
  body phrase per occ       +20 (cap 10)
  title token hit           ×5 / token
  body  token hit           ×1 / token

Plus RRF (Reciprocal Rank Fusion, K=60) to merge keyword + vector rankings.

No LLM, no embeddings, no external deps — works offline without Ollama.
Public API:
  - tokenize_query(query)
  - keyword_search(wiki_dir, query, max_results=20)
  - rrf_merge(keyword_results, vector_results, top=20)
  - extract_title(content, file_name)
  - build_snippet(content, anchor)
"""
from __future__ import annotations

import re
from pathlib import Path

from _frontmatter import TITLE_LINE_RE as _FM_TITLE_RE
from typing import Optional

__all__ = [
    "tokenize_query",
    "keyword_search",
    "rrf_merge",
    "extract_title",
    "build_snippet",
    "score_file",
    "RRF_K",
]

# ── constants (verbatim from search.rs) ─────────────────────────────────────
FILENAME_EXACT_BONUS = 200.0
PHRASE_IN_TITLE_BONUS = 50.0
PHRASE_IN_CONTENT_PER_OCC = 20.0
MAX_PHRASE_OCC_COUNTED = 10
TITLE_TOKEN_WEIGHT = 5.0
CONTENT_TOKEN_WEIGHT = 1.0
SNIPPET_CONTEXT = 80
RRF_K = 60.0
MAX_SEARCH_FILES = 10_000

_CJK_RE = re.compile(r"[㐀-鿿]")
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

_CJK_SEPARATORS = set("，。！？、；：“”‘’（）·～…")
_STOP_WORDS = {
    "的", "是", "了", "什么", "在", "有", "和", "与", "对", "从",
    "the", "is", "a", "an", "what", "how", "are", "was", "were", "do",
    "does", "did", "be", "been", "being", "have", "has", "had", "it",
    "its", "in", "on", "at", "to", "for", "of", "with", "by", "this",
    "that", "these", "those",
}


def _is_query_separator(c: str) -> bool:
    if c.isspace() or (c.isascii() and not c.isalnum()):
        return True
    return c in _CJK_SEPARATORS


def is_stop_word(token: str) -> bool:
    return token in _STOP_WORDS


def _split_on_separators(token: str) -> list[str]:
    parts: list[str] = []
    cur = ""
    for c in token:
        if _is_query_separator(c):
            if cur:
                parts.append(cur)
                cur = ""
        else:
            cur += c
    if cur:
        parts.append(cur)
    return parts


def tokenize_query(query: str) -> list[str]:
    """Lowercase, split on separators, drop len<=1 + stopwords, expand CJK
    tokens >2 chars into bigrams + single chars + original (dedup, sorted)."""
    raw: list[str] = []
    for tok in query.lower().split():
        for sub in _split_on_separators(tok):
            if len(sub) > 1 and not is_stop_word(sub):
                raw.append(sub)

    out: set[str] = set()
    for token in raw:
        chars = list(token)
        has_cjk = any(_CJK_RE.match(ch) for ch in chars)
        if has_cjk and len(chars) > 2:
            for i in range(len(chars) - 1):
                out.add(chars[i] + chars[i + 1])
            for ch in chars:
                if not is_stop_word(ch):
                    out.add(ch)
            out.add(token)
        else:
            out.add(token)
    return sorted(out)


def extract_title(content: str, file_name: str) -> str:
    """Frontmatter `title:` → first `# heading` → filename stem (spaces)."""
    m = _FM_TITLE_RE.search(content[:2000])
    if m and m.group(1).strip():
        return m.group(1).strip()
    h = _HEADING_RE.search(content)
    if h and h.group(1).strip():
        return h.group(1).strip()
    stem = re.sub(r"\.md$", "", file_name, flags=re.IGNORECASE)
    return re.sub(r"[-_]+", " ", stem).strip()


def _count_occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


def _token_match_score(text: str, tokens: list[str]) -> int:
    lower = text.lower()
    return sum(1 for t in tokens if t and t in lower)


def build_snippet(content: str, anchor: str) -> str:
    """SNIPPET_CONTEXT*2 chars around the first occurrence of `anchor`."""
    if not anchor:
        return content[: SNIPPET_CONTEXT * 2]
    idx = content.lower().find(anchor.lower())
    if idx < 0:
        return content[: SNIPPET_CONTEXT * 2]
    chars = list(content)
    start = max(0, idx - SNIPPET_CONTEXT)
    end = min(len(chars), idx + len(anchor) + SNIPPET_CONTEXT)
    snippet = "".join(chars[start:end])
    if start > 0:
        snippet = "..." + snippet
    if end < len(chars):
        snippet = snippet + "..."
    return snippet


def score_file(
    rel_path: str,
    file_name: str,
    content: str,
    tokens: list[str],
    query_phrase: str,
    query: str,
) -> Optional[dict]:
    """Score one file against the query. Returns None if no signal."""
    title = extract_title(content, file_name)
    title_text = f"{title} {file_name}"
    title_lower = title_text.lower()
    content_lower = content.lower()
    stem = re.sub(r"\.md$", "", file_name, flags=re.IGNORECASE).lower()

    filename_exact = bool(query_phrase) and stem == query_phrase
    title_has_phrase = bool(query_phrase) and query_phrase in title_lower
    content_phrase_occ = (min(_count_occurrences(content_lower, query_phrase),
                              MAX_PHRASE_OCC_COUNTED)
                          if query_phrase else 0)
    title_token_score = _token_match_score(title_text, tokens)
    content_token_score = _token_match_score(content, tokens)

    if not (filename_exact or title_has_phrase or content_phrase_occ
            or title_token_score or content_token_score):
        return None

    score = ((FILENAME_EXACT_BONUS if filename_exact else 0.0)
             + (PHRASE_IN_TITLE_BONUS if title_has_phrase else 0.0)
             + content_phrase_occ * PHRASE_IN_CONTENT_PER_OCC
             + title_token_score * TITLE_TOKEN_WEIGHT
             + content_token_score * CONTENT_TOKEN_WEIGHT)

    if content_phrase_occ > 0:
        anchor = query_phrase
    else:
        anchor = next((t for t in tokens if t in content_lower), query)

    return {
        "path": rel_path,
        "title": title,
        "snippet": build_snippet(content, anchor),
        "title_match": title_token_score > 0 or title_has_phrase,
        "score": score,
        "vector_score": None,
    }


def _walk_md_files(wiki_dir: Path) -> list[Path]:
    files = sorted(wiki_dir.rglob("*.md"))
    return files[:MAX_SEARCH_FILES]


def keyword_search(
    wiki_dir: Path,
    query: str,
    max_results: int = 20,
    *,
    skip_dirs: tuple[str, ...] = ("lint", "REVIEW", "media"),
) -> list[dict]:
    """Walk wiki/*.md, score each, return top-N by keyword score."""
    query_phrase = query.strip().lower()
    tokens = tokenize_query(query)
    if not query_phrase and not tokens:
        return []

    results: list[dict] = []
    if not wiki_dir.is_dir():
        return results
    for path in _walk_md_files(wiki_dir):
        rel = path.relative_to(wiki_dir)
        if rel.parts and rel.parts[0] in skip_dirs:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        hit = score_file(str(rel), path.name, content, tokens, query_phrase, query)
        if hit is not None:
            results.append(hit)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]


def rrf_merge(
    keyword_results: list[dict],
    vector_results: list[dict],
    top: int = 20,
) -> list[dict]:
    """Reciprocal Rank Fusion (K=60) of keyword + vector rankings.

    Each result dict must have a `path` key. Fused `score` = RRF sum.
    Keyword dict wins on overlap (richer snippet); vector_score carried
    through when present."""
    krank = {r["path"]: i for i, r in enumerate(keyword_results)}
    vrank = {r["path"]: i for i, r in enumerate(vector_results)}
    all_paths = list(krank.keys()) + [p for p in vrank if p not in krank]

    by_path: dict[str, dict] = {}
    for r in vector_results:
        by_path[r["path"]] = dict(r)
    for r in keyword_results:
        by_path[r["path"]] = dict(r)

    fused: list[dict] = []
    for p in all_paths:
        rrf = 0.0
        if p in krank:
            rrf += 1.0 / (RRF_K + krank[p])
        if p in vrank:
            rrf += 1.0 / (RRF_K + vrank[p])
            vr = next((v for v in vector_results if v["path"] == p), None)
            if vr and vr.get("vector_score") is not None:
                by_path[p]["vector_score"] = vr["vector_score"]
        by_path[p]["score"] = rrf
        fused.append(by_path[p])
    fused.sort(key=lambda r: r["score"], reverse=True)
    return fused[:top]
