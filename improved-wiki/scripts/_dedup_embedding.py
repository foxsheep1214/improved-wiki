#!/usr/bin/env python3
"""_dedup_embedding.py — vector-embedding candidate generation for dedup.

Faithful port of NashSU ``src/lib/dedup_embedding.ts``. Pre-filters
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
from operator import mul
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


def _normalize(vec: list[float]) -> Optional[list[float]]:
    """L2-normalize a vector so its cosine similarity against any other
    normalized vector reduces to a plain dot product. Used by
    ``candidate_pairs`` to avoid recomputing each vector's own norm on every
    pairwise comparison (see the 2026-07-10 comment there). Returns ``None``
    for a zero (or all-zero) vector, matching ``cosine_similarity``'s
    zero-denominator → 0.0 behavior (a zero vector never clears a positive
    threshold, so excluding it from comparisons entirely is equivalent)."""
    norm_sq = sum(x * x for x in vec)
    if norm_sq <= 0:
        return None
    norm = norm_sq ** 0.5
    return [x / norm for x in vec]


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


# Bounded embedding access (2026-07-12, aligned with cross_source_dedup's
# bounded path): a hard per-request timeout — embed_texts' historical default
# was an unbounded-in-practice 120s×3-retries per batch — plus a consecutive-
# failure circuit breaker so a dead endpoint fails the prefilter fast instead
# of grinding through every batch.
EMBED_TIMEOUT_S = 60
EMBED_MAX_CONSECUTIVE_FAILURES = 3


def embed_pages(pages: list[dict]) -> dict[str, Optional[list[float]]]:
    """Embed pages via the local Ollama stack. Returns id → vector (or None on
    per-batch failure). Batches of 16; a failed batch is recorded as None for
    its members and the rest continue, so one bad page can't sink the scan.
    EMBED_MAX_CONSECUTIVE_FAILURES failed batches in a row raise
    DuplicatePrefilterError (endpoint is dead — callers already handle the
    prefilter-error fallback)."""
    from build_embeddings import embed_texts  # local import (heavy module)
    base_url, model, api_key = _embed_config()
    out: dict[str, Optional[list[float]]] = {}
    batch = 16
    keys = [p["id"] for p in pages]
    texts = [page_to_embedding_text(p) for p in pages]
    consecutive_failures = 0
    for i in range(0, len(texts), batch):
        chunk_keys = keys[i:i + batch]
        chunk_texts = texts[i:i + batch]
        try:
            vecs = embed_texts(chunk_texts, base_url, model, api_key,
                               timeout=EMBED_TIMEOUT_S)
            for k, v in zip(chunk_keys, vecs):
                out[k] = list(v) if v else None
            consecutive_failures = 0
        except Exception as e:
            # Single-batch failure is non-fatal (the rest continue), but must
            # be visible: these pages get None and are silently skipped in
            # candidate_pairs, so an unreported batch failure hides dedup
            # gaps. Overall success ratio <min_success_ratio still raises.
            print(f"[dedup] warn: embedding batch failed ({type(e).__name__}: {e}) — "
                  f"{len(chunk_keys)} pages marked None and will be skipped")
            for k in chunk_keys:
                out[k] = None
            consecutive_failures += 1
            if consecutive_failures >= EMBED_MAX_CONSECUTIVE_FAILURES:
                raise DuplicatePrefilterError(
                    f"aborting embed after {consecutive_failures} consecutive "
                    f"batch failures (endpoint down/stalled?)") from e
    return out


def candidate_pairs(
    pages: list[dict],
    *,
    top_k: int = 8,
    threshold: float = 0.82,
    max_pages: int = 5000,
    min_success_ratio: float = 0.8,
    embeddings: Optional[dict[str, Optional[list[float]]]] = None,
    _force_pure: bool = False,
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

    # 2026-07-10: normalize each vector ONCE instead of letting
    # cosine_similarity() recompute both vectors' own norms on every single
    # pairwise comparison — confirmed live as the dominant cost of this O(N^2)
    # sweep (~40 CPU-minutes on a ~7500-page wiki, pure-Python, no network
    # wait). A normalized cosine similarity is a plain dot product, cutting
    # the per-pair work from three passes over the vector (dot, |a|, |b|) to
    # one. ``sum(map(mul, ...))`` is also faster in CPython than an indexed
    # for-loop. Memory profile is unchanged from before (still O(N) per row,
    # discarded after each outer iteration) — only the per-pair cost drops.
    normalized: dict[str, list[float]] = {}
    for pg in subset:
        vec = embeddings.get(pg["id"])
        if vec:
            nv = _normalize(vec)
            if nv is not None:
                normalized[pg["id"]] = nv

    # 2026-07-11 (#7): numpy fast path when available — the pairwise sweep
    # becomes blocked matrix multiplication (BLAS), turning ~10 CPU-minutes
    # at 7.5K pages into seconds. numpy is NOT a required dependency: the
    # pure-Python loop below remains the fallback (stdlib-only convention),
    # and both paths produce identical pair sets (same threshold semantics,
    # same stable tie-breaking by ascending index; regression-tested for
    # equivalence in tests/test_dedup_embedding.py).
    try:
        import numpy as _np
    except ImportError:
        _np = None
    if _np is not None and len(normalized) >= 2 and not _force_pure:
        return _candidate_pairs_numpy(_np, subset, normalized, threshold, top_k)

    pair_set: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for i, pi in enumerate(subset):
        vi = normalized.get(pi["id"])
        if not vi:
            continue
        scored: list[tuple[float, int]] = []
        for j, pj in enumerate(subset):
            if i == j:
                continue
            vj = normalized.get(pj["id"])
            if not vj:
                continue
            sim = sum(map(mul, vi, vj))
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


_NUMPY_BLOCK_ROWS = 512  # rows per matmul block: 512×N similarity slab keeps
                         # peak memory ~30MB at N=7500 instead of an N×N matrix


def _candidate_pairs_numpy(np, subset: list[dict],
                           normalized: dict[str, list[float]],
                           threshold: float, top_k: int) -> list[tuple[str, str]]:
    """numpy implementation of the top-K nearest-neighbor pair sweep.

    Semantics mirror the pure-Python loop exactly: for each page (in subset
    order), score every OTHER embedded page by dot product of the normalized
    vectors, keep those >= threshold, take the top_k by score with ties broken
    by ascending candidate index (the pure path's stable sort preserves the
    ascending-j scan order), symmetric-dedup across the whole sweep.
    float64 matches CPython float arithmetic; tiny summation-order differences
    vs sum(map(mul, ...)) are ~1e-16 and only matter for exact-boundary ties.
    """
    ids = [pg["id"] for pg in subset if pg["id"] in normalized]
    if len(ids) < 2:
        return []
    mat = np.array([normalized[pid] for pid in ids], dtype=np.float64)
    n = len(ids)
    pair_set: set = set()
    pairs: list[tuple[str, str]] = []
    for start in range(0, n, _NUMPY_BLOCK_ROWS):
        stop = min(start + _NUMPY_BLOCK_ROWS, n)
        # errstate: macOS Accelerate BLAS emits spurious divide-by-zero /
        # overflow RuntimeWarnings from matmul even on verified-finite inputs
        # (reproduced on clean normalized float64). Inputs here are normalized
        # finite vectors by construction, so the suppression cannot mask a
        # real numerical problem in this call.
        with np.errstate(all="ignore"):
            sims = mat[start:stop] @ mat.T  # (block, n)
        for r in range(stop - start):
            i = start + r
            row = sims[r]
            row[i] = -np.inf  # self-excluded
            cand = np.flatnonzero(row >= threshold)
            if cand.size == 0:
                continue
            order = np.argsort(-row[cand], kind="stable")
            for j in cand[order][:top_k]:
                a, b = ids[i], ids[int(j)]
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
