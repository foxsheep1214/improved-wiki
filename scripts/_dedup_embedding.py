#!/usr/bin/env python3
"""_dedup_embedding.py — vector-embedding candidate generation for dedup.

Faithful port of NashSU ``src/lib/dedup_embedding.ts`` (verified against v0.5.3). Pre-filters
pages by cosine similarity so the downstream LLM duplicate-detector only sees
a small candidate set instead of the whole wiki in one prompt (GAP-3).

Embeddings reuse improved-wiki's local Ollama bge-m3 stack via
``build_embeddings.embed_texts`` (OpenAI-compatible ``/v1/embeddings``) — no
external API key. If too few pages embed successfully, ``candidate_pairs``
raises so the caller falls back to the full LLM scan (NashSU parity).

Public API:
  - cosine_similarity(a, b)
  - page_to_embedding_text(page, budget=1500)
  - embed_pages(pages)              -> dict[id, list[float] | None]
  - candidate_pairs(pages, ...)     -> list[(id, id)]
  - cluster_by_pairs(page_ids, pairs) -> list[list[id]]
"""
from __future__ import annotations

import os
from typing import Optional

__all__ = [
    "cosine_similarity",
    "page_to_embedding_text",
    "embed_pages",
    "candidate_pairs",
    "cluster_by_pairs",
    "DuplicatePrefilterError",
]


class DuplicatePrefilterError(RuntimeError):
    """Raised when the embedding prefilter can't embed enough pages — caller
    falls back to the full LLM scan."""


def cosine_similarity(a: Optional[list[float]], b: Optional[list[float]]) -> float:
    """Cosine similarity between equal-length vectors. 0 if either is None,
    lengths differ, or either is zero-length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(len(a)):
        dot += a[i] * b[i]
        na += a[i] * a[i]
        nb += b[i] * b[i]
    denom = (na ** 0.5) * (nb ** 0.5)
    return dot / denom if denom else 0.0


def page_to_embedding_text(page: dict, budget: int = 1500) -> str:
    """Build the embedding input text from a page: slug + title + tags + body
    (truncated to ``budget``). Mirrors NashSU ``pageToEmbeddingText``."""
    pid = page.get("id", "")
    slug = os.path.basename(pid)
    if slug.endswith(".md"):
        slug = slug[:-3]
    title = page.get("title", "")
    tags = page.get("tags") or []
    tag_part = " ".join(tags)
    body = (page.get("body") or "")[:budget]
    return "\n".join(p for p in (slug, title, tag_part, body) if p)


def _embed_config() -> tuple[str, str, str]:
    """Resolve (base_url, model, api_key) from env, matching build_embeddings."""
    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    model = os.environ.get("EMBEDDING_MODEL", "bge-m3")
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    return base_url, model, api_key


def embed_pages(pages: list[dict]) -> dict[str, Optional[list[float]]]:
    """Embed pages via the local Ollama stack. Returns id → vector (or None on
    per-batch failure). Batches of 16; a failed batch is recorded as None for
    its members and the rest continue, so one bad page can't sink the scan."""
    from build_embeddings import embed_texts  # local import (heavy module)
    base_url, model, api_key = _embed_config()
    out: dict[str, Optional[list[float]]] = {}
    batch = 16
    keys = [p["id"] for p in pages]
    texts = [page_to_embedding_text(p) for p in pages]
    for i in range(0, len(texts), batch):
        chunk_keys = keys[i:i + batch]
        chunk_texts = texts[i:i + batch]
        try:
            vecs = embed_texts(chunk_texts, base_url, model, api_key)
            for k, v in zip(chunk_keys, vecs):
                out[k] = list(v) if v else None
        except Exception:
            for k in chunk_keys:
                out[k] = None
    return out


def candidate_pairs(
    pages: list[dict],
    *,
    top_k: int = 8,
    threshold: float = 0.82,
    max_pages: int = 5000,
    min_success_ratio: float = 0.8,
    embeddings: Optional[dict[str, Optional[list[float]]]] = None,
) -> list[tuple[str, str]]:
    """Generate candidate duplicate pairs: each page's top-K nearest neighbors
    above ``threshold``, self-excluded, symmetric-deduplicated.

    ``embeddings`` (id→vec) can be injected for tests; otherwise computed via
    ``embed_pages``. Raises DuplicatePrefilterError if too few pages embed."""
    if not pages:
        return []
    subset = pages[:max_pages]
    if len(pages) > len(subset):
        print(f"[dedup] embedding prefilter limited scan to "
              f"{len(subset)}/{len(pages)} pages")

    if embeddings is None:
        embeddings = embed_pages(subset)

    embedded = [v for v in embeddings.values() if v]
    if len(subset) >= 2 and len(embedded) < 2:
        raise DuplicatePrefilterError("could not embed enough pages")
    if subset and len(embedded) / len(subset) < min_success_ratio:
        raise DuplicatePrefilterError(
            f"embedded only {len(embedded)}/{len(subset)} pages")

    pair_set: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for i, pi in enumerate(subset):
        vi = embeddings.get(pi["id"])
        if not vi:
            continue
        scored: list[tuple[float, int]] = []
        for j, pj in enumerate(subset):
            if i == j:
                continue
            sim = cosine_similarity(vi, embeddings.get(pj["id"]))
            if sim >= threshold:
                scored.append((sim, j))
        scored.sort(key=lambda t: t[0], reverse=True)
        for k in range(min(top_k, len(scored))):
            a = pi["id"]
            b = subset[scored[k][1]]["id"]
            key = f"{a}\t{b}" if a < b else f"{b}\t{a}"
            if key not in pair_set:
                pair_set.add(key)
                pairs.append((a, b))
    return pairs


def cluster_by_pairs(page_ids: list[str], pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Union-find clustering of candidate pairs into groups of >1.
    Iterative find() with path compression (no recursion → no stack overflow)."""
    parent: dict[str, str] = {pid: pid for pid in page_ids}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        cur = x
        while parent[cur] != root:
            parent[cur], cur = root, parent[cur]
        return root

    for a, b in pairs:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for pid in page_ids:
        root = find(pid)
        groups.setdefault(root, []).append(pid)
    return [g for g in groups.values() if len(g) > 1]
