"""_search_graph.py — one-hop graph expansion of search results.

Port of NashSU search.rs ``blend_graph_results`` / ``graph_result_quota``
(present since v0.6.6): the top min(limit, 20) ranked results seed a walk of
the wiki link graph, and 15–30 % of the result window goes to their direct
neighbours. The share is 30 % with no vector hits and shrinks linearly to
15 % as vector hits fill the window; unused graph slots fall back to the
ranked results. A neighbour scores Σ 1/(seed rank) over the seeds it touches.

Link resolution reuses graph.py (body [[wikilinks]] plus ``related:``;
path or unique-stem targets) so search and the Graph command agree.
Deliberate differences from search.rs:

* the page universe is the keyword walk's — no REVIEW/clusters/media/lint and
  no root index.md/log.md. search.rs also walks index.md, which links every
  page and so would be the top neighbour of any query;
* a ``type: redirect`` stub stands for its ``redirect:`` target, and a stub
  without a resolvable target is never a neighbour.
"""
from __future__ import annotations

import math
from pathlib import Path

from _frontmatter import parse_frontmatter
from _wiki_keyword import RRF_K, extract_title
from _wikilinks import WIKILINK_RE
from graph import Page, build_link_graph, build_resolver

MAX_GRAPH_SEEDS = 20
MIN_GRAPH_RESULT_RATIO = 0.15
MAX_GRAPH_RESULT_RATIO = 0.30


def _node_id(wiki_path: str) -> str:
    """'concepts/x.md' (search result path) → 'wiki/concepts/x' (graph node)."""
    return "wiki/" + (wiki_path[:-3] if wiki_path.endswith(".md") else wiki_path)


def _wiki_path(node_id: str) -> str:
    return node_id[len("wiki/"):] + ".md"


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


class GraphPages:
    """Pages collected during the keyword walk (``keyword_search(on_page=…)``)."""

    def __init__(self) -> None:
        self.pages: dict[str, Page] = {}
        self._redirects: dict[str, str] = {}

    def add(self, rel_path: str, content: str) -> None:
        fm, body = parse_frontmatter(content)
        node_id = _node_id(rel_path)
        page_type = str(fm.get("type", "other")).lower().strip()
        links = [m.group(1).split("#")[0].strip() for m in WIKILINK_RE.finditer(body)]
        links.extend(_as_list(fm.get("related")))
        self.pages[node_id] = Page(
            node_id=node_id,
            stem=Path(rel_path).stem,
            title=extract_title(content, Path(rel_path).name),
            page_type=page_type,
            sources=(),
            links=tuple(t for t in links if t),
            path=Path(rel_path),
        )
        target = str(fm.get("redirect") or "").strip()
        if page_type == "redirect" and target:
            self._redirects[node_id] = target

    def canonical(self) -> dict[str, str | None]:
        """Redirect stub → its target node, or None when unresolvable."""
        resolver = build_resolver(self.pages)
        out: dict[str, str | None] = {
            nid: None for nid, page in self.pages.items()
            if page.page_type == "redirect"
        }
        for nid, target in self._redirects.items():
            dst = resolver.resolve(target)
            out[nid] = dst if dst in self.pages and dst != nid else None
        return out


def graph_result_quota(limit: int, vector_hits: int) -> int:
    if limit < 2:
        return 0
    coverage = min(vector_hits, limit) / limit
    ratio = MAX_GRAPH_RESULT_RATIO - (
        MAX_GRAPH_RESULT_RATIO - MIN_GRAPH_RESULT_RATIO) * coverage
    return min(max(math.ceil(limit * ratio), 1), limit - 1)


def blend_graph_results(
    ranked: list[dict],
    graph: GraphPages,
    limit: int,
    vector_hits: int,
) -> tuple[list[dict], int]:
    """Return (final results, graph hit count) for a ranked result list.

    ``ranked`` is the full fused ranking (not yet cut to ``limit``). Graph
    picks go after the ranked results that remain, as in search.rs.
    """
    if not ranked or not graph.pages:
        return ranked[:limit], 0
    link_graph = build_link_graph(graph.pages)
    canonical = graph.canonical()

    seeds = [_node_id(r["path"]) for r in ranked[:min(limit, MAX_GRAPH_SEEDS)]]
    seed_set = set(seeds)
    scores: dict[str, float] = {}
    seed_titles: dict[str, set[str]] = {}
    for rank, seed in enumerate(seeds):
        if seed not in graph.pages:
            continue
        for neighbour in link_graph.neighbors(seed):
            if neighbour in canonical:
                neighbour = canonical[neighbour]
            if neighbour is None or neighbour in seed_set:
                continue
            scores[neighbour] = scores.get(neighbour, 0.0) + 1.0 / (rank + 1)
            seed_titles.setdefault(neighbour, set()).add(graph.pages[seed].title)

    candidates = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    candidates = candidates[:graph_result_quota(limit, vector_hits)]
    if not candidates:
        return ranked[:limit], 0

    selected = {_wiki_path(nid) for nid, _ in candidates}
    by_path = {r["path"]: r for r in ranked}
    results = [r for r in ranked if r["path"] not in selected]
    results = results[:limit - len(candidates)]
    for nid, graph_score in candidates:
        path = _wiki_path(nid)
        related = sorted(seed_titles[nid])
        if path in by_path:
            result = dict(by_path[path])
        else:
            result = {
                "path": path,
                "title": graph.pages[nid].title,
                "snippet": f"Graph neighbor of {', '.join(related)}",
                "title_match": False,
                "score": graph_score / (RRF_K + 1.0),
                "vector_score": None,
            }
        result["graph_related_to"] = related
        results.append(result)
    return results, len(candidates)
