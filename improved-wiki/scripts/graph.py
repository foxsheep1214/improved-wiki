#!/usr/bin/env python3
"""graph.py — Knowledge graph builder (NashSU graph-view CLI).

Peer command of Ingest/Lint. Deterministic — no LLM, no embedding. Reads
``wiki/**/*.md`` frontmatter (title/type/sources) + ``[[wikilinks]]`` and
builds the wiki link graph, weights each edge with NashSU's four-signal
``calculateRelevance``, then runs Louvain community detection.

EDGE MODEL (NashSU ``src/lib/wiki-graph.ts`` buildWikiGraph): an edge exists
*iff* there is a resolved link between two pages. ``related:`` frontmatter is
treated as a link source (this wiki's link convention; equivalent to NashSU's
body ``[[wikilinks]]``). Source-overlap and Adamic-Adar are WEIGHT-ONLY — they
never create edges.

Edge weight = ``calculateRelevance(a, b)`` (NashSU ``src/lib/graph-relevance.ts``):

  direct link    — (forward + backward link count) × 3.0  (reciprocal = 6.0)
  source overlap — sharedSourceCount × 4.0
  Adamic-Adar    — Σ 1/log(max(deg(c), 2)) over union(in,out) common neighbors × 1.5
  type affinity  — TYPE_AFFINITY[a][b] (default 0.5) × 1.0  (added unconditionally)

Outputs (build mode):
  <runtime>/graph.json           — full graph (nodes/edges/communities/gaps/surprising)
  <runtime>/graph.html           — self-contained interactive force-directed graph
  <wiki>/REVIEW/knowledge-gaps.md — isolated/sparse-community/bridge gaps
  <wiki>/clusters/cluster-NNN.md — per-community hub page

The rendered graph (graph.json / graph.html) is emitted through NashSU's default
filters (applyGraphFilters: hideStructural=True). Pass ``--include-all`` to emit
the unfiltered graph.

Modes:
  build (default)              — rebuild graph + write outputs
  query --slug S               — read-only: top suggested NEW wikilinks for page S
  --dry-run                    — stats only, no file writes

Usage:
  python3 graph.py
  python3 graph.py --wiki-root ~/Documents/知识库/HardwareWiki
  python3 graph.py --dry-run
  python3 graph.py --include-all
  python3 graph.py --mode query --slug 风扇轴承类型
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import networkx as nx
import yaml
from networkx.algorithms.community import louvain_communities

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402

# --- Signal weights (NashSU graph-relevance.ts WEIGHTS) ---------------------
W_DIRECT_LINK = 3.0
W_SOURCE_OVERLAP = 4.0
W_COMMON_NEIGHBOR = 1.5   # Adamic-Adar multiplier
W_TYPE_AFFINITY = 1.0

# Louvain seed — deterministic across runs (CLI needs reproducible git output).
# NashSU is unseeded; this is an intentional CLI divergence.
LOUVAIN_SEED = 42

# Cohesion (intra-edge density) below this marks a low-quality community.
COHESION_LOW = 0.15

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Node types hidden at build (NashSU wiki-graph.ts HIDDEN_TYPES). Query pages are
# intermediate artifacts, not knowledge structure.
HIDDEN_TYPES = frozenset({"query"})

# Top-level wiki/ subdirs that hold DERIVED artifacts, not content pages — the
# graph must not ingest its OWN output (REVIEW/knowledge-gaps.md, clusters/*) or
# lint/media. Mirrors the lint engine's SKIP_DIRS.
GRAPH_SKIP_DIRS = frozenset({"REVIEW", "clusters", "media", "lint"})

# Structural pages (NashSU graph-filters.ts STRUCTURAL_IDS).
STRUCTURAL_IDS = frozenset({"index", "overview", "log", "schema", "purpose"})

# Structural ids used by the insight passes (NashSU graph-insights.ts uses the
# narrower {index, log, overview} set for gaps + surprising connections).
INSIGHT_STRUCTURAL_IDS = frozenset({"index", "log", "overview"})


# --- Type affinity matrix (NashSU graph-relevance.ts TYPE_AFFINITY) ---------
# Verbatim. Unlisted source-type or unlisted target-type pairs fall back to 0.5.
TYPE_AFFINITY: dict[str, dict[str, float]] = {
    "entity": {"concept": 1.2, "entity": 0.8, "source": 1.0, "synthesis": 1.0, "query": 0.8},
    "concept": {"entity": 1.2, "concept": 0.8, "source": 1.0, "synthesis": 1.2, "query": 1.0},
    "source": {"entity": 1.0, "concept": 1.0, "source": 0.5, "query": 0.8, "synthesis": 1.0},
    "query": {"concept": 1.0, "entity": 0.8, "synthesis": 1.0, "source": 0.8, "query": 0.5},
    "synthesis": {"concept": 1.2, "entity": 1.0, "source": 1.0, "query": 1.0, "synthesis": 0.8},
}

# Distant type pairs for surprising-connection scoring (graph-insights.ts).
DISTANT_TYPE_PAIRS = frozenset({
    "source-concept", "concept-source",
    "source-synthesis", "synthesis-source",
    "query-entity", "entity-query",
})


def type_affinity(t1: str, t2: str) -> float:
    """Affinity score for an ordered page-type pair (default 0.5)."""
    row = TYPE_AFFINITY.get(t1)
    if row is None:
        return 0.5
    return row.get(t2, 0.5)


# --- Page model -------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One wiki page parsed from disk."""

    node_id: str          # path relative to wiki_root, no '.md' (e.g. 'wiki/concepts/X')
    stem: str             # filename stem (e.g. 'X')
    title: str
    page_type: str        # frontmatter 'type:' lowercased; 'other' if missing
    sources: tuple[str, ...]
    links: tuple[str, ...]   # raw wikilink/related targets as written
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    try:
        fm = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, text[m.end():]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return [str(value)]


def load_pages(wiki_root: Path, include_hidden: bool = False) -> dict[str, Page]:
    """Parse every ``*.md`` under ``<wiki_root>/wiki/`` into a Page.

    Pages whose type is in HIDDEN_TYPES (query) are dropped by default — the
    DISPLAY graph (nodes/edges/communities) excludes them (NashSU buildWikiGraph).
    Pass ``include_hidden=True`` to keep them for the RETRIEVAL graph, which
    NashSU's calculateRelevance scores against (query pages count as Adamic-Adar
    common neighbors and in the degree denominator).
    """
    wiki_dir = wiki_root / "wiki"
    if not wiki_dir.exists():
        return {}
    pages: dict[str, Page] = {}
    for md in sorted(wiki_dir.rglob("*.md")):
        rel_to_wiki = md.relative_to(wiki_dir)
        if rel_to_wiki.parts and rel_to_wiki.parts[0] in GRAPH_SKIP_DIRS:
            continue  # derived artifacts (REVIEW/clusters/media/lint), not content
        rel = md.relative_to(wiki_root).with_suffix("")
        node_id = rel.as_posix()
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = _parse_frontmatter(text)
        page_type = str(fm.get("type", "other")).lower().strip()
        if page_type in HIDDEN_TYPES and not include_hidden:
            continue
        title = str(fm.get("title", md.stem))
        sources = tuple(s for s in _as_list(fm.get("sources")))
        # Direct links = [[wikilinks]] in body + `related:` frontmatter paths.
        targets: list[str] = []
        targets.extend(m.split("|")[0].split("#")[0].strip() for m in WIKILINK_RE.findall(body))
        targets.extend(_as_list(fm.get("related")))
        links = tuple(t for t in targets if t)
        pages[node_id] = Page(
            node_id=node_id, stem=md.stem, title=title, page_type=page_type,
            sources=sources, links=links, path=md,
        )
    return pages


# --- Link target resolution -------------------------------------------------


@dataclass
class LinkResolver:
    """Resolve a wikilink/related target string to a node id."""

    by_path: dict[str, str]            # 'wiki/concepts/X' -> node_id
    by_stem: dict[str, list[str]]      # stem -> [node_id, ...]

    def resolve(self, target: str) -> Optional[str]:
        t = target.split("|")[0].split("#")[0].strip()
        if not t:
            return None
        candidates: list[str] = []
        if t.startswith("wiki/"):
            candidates.append(t)
            candidates.append(t[len("wiki/"):])
        else:
            candidates.append(f"wiki/{t}")
            candidates.append(t)
        for cand in candidates:
            if cand in self.by_path:
                return self.by_path[cand]
        stem = t.split("/")[-1]
        ids = self.by_stem.get(stem)
        if ids and len(ids) == 1:
            return ids[0]
        return None


def build_resolver(pages: dict[str, Page]) -> LinkResolver:
    by_path = {nid: nid for nid in pages}
    by_stem: dict[str, list[str]] = defaultdict(list)
    for nid, p in pages.items():
        by_stem[p.stem].append(nid)
    return LinkResolver(by_path=by_path, by_stem=dict(by_stem))


# --- Link graph (NashSU buildWikiGraph link relations) ----------------------


@dataclass
class LinkGraph:
    """Directional link relations resolved between pages, plus per-page sources.

    out_links[a] = set of pages a links to; in_links[a] = set of pages linking
    to a. Edges are the deduplicated undirected link pairs.
    """

    out_links: dict[str, set[str]]
    in_links: dict[str, set[str]]
    edges: list[frozenset[str]]                 # deduplicated undirected link pairs
    link_counts: dict[str, int]                 # inbound + outbound (per directed link)

    def neighbors(self, nid: str) -> set[str]:
        return self.out_links.get(nid, set()) | self.in_links.get(nid, set())

    def degree(self, nid: str) -> int:
        return len(self.out_links.get(nid, set())) + len(self.in_links.get(nid, set()))


def build_link_graph(pages: dict[str, Page]) -> LinkGraph:
    """Resolve every link to a node id and build the directional link graph."""
    resolver = build_resolver(pages)
    out_links: dict[str, set[str]] = {nid: set() for nid in pages}
    in_links: dict[str, set[str]] = {nid: set() for nid in pages}
    link_counts: dict[str, int] = {nid: 0 for nid in pages}
    raw_edges: list[tuple[str, str]] = []

    for src, p in pages.items():
        for tgt_raw in p.links:
            dst = resolver.resolve(tgt_raw)
            if dst is None or dst not in pages or dst == src:
                continue
            out_links[src].add(dst)
            in_links[dst].add(src)
            raw_edges.append((src, dst))
            # NashSU increments both endpoints per directed link occurrence.
            link_counts[src] += 1
            link_counts[dst] += 1

    # Deduplicate edges (undirected).
    seen: set[frozenset[str]] = set()
    edges: list[frozenset[str]] = []
    for u, v in raw_edges:
        key = frozenset((u, v))
        if key not in seen:
            seen.add(key)
            edges.append(key)

    return LinkGraph(out_links=out_links, in_links=in_links, edges=edges,
                     link_counts=link_counts)


# --- Source overlap lookup (weight-only) ------------------------------------


def shared_source_count(a: str, b: str, pages: dict[str, Page]) -> int:
    sa = set(pages[a].sources)
    return sum(1 for s in pages[b].sources if s in sa)


# --- calculateRelevance port (graph-relevance.ts) ---------------------------


def calculate_relevance(a: str, b: str, pages: dict[str, Page],
                        lg: LinkGraph,
                        retrieval_lg: Optional["LinkGraph"] = None) -> tuple[float, list[str]]:
    """Port of NashSU ``calculateRelevance(nodeA, nodeB, graph)``.

    Returns (weight, fired signal names). Type affinity is added
    unconditionally to every scored pair. The Adamic-Adar term scores against
    ``retrieval_lg`` (the full link graph incl. query pages) when provided —
    NashSU computes relevance over the retrieval graph, not the display graph —
    falling back to ``lg`` for backward-compatible single-graph callers.
    """
    if a == b:
        return 0.0, []

    fired: list[str] = []

    # Signal 1: direct links — directional, (forward + backward) × 3.0.
    forward = 1 if b in lg.out_links.get(a, set()) else 0
    backward = 1 if a in lg.out_links.get(b, set()) else 0
    direct_score = (forward + backward) * W_DIRECT_LINK
    if direct_score:
        fired.append("direct_link" if (forward + backward) == 1 else "direct_link(reciprocal)")

    # Signal 2: source overlap — sharedSourceCount × 4.0.
    shared = shared_source_count(a, b, pages)
    source_score = shared * W_SOURCE_OVERLAP
    if source_score:
        fired.append("source_overlap")

    # Signal 3: Adamic-Adar over union(in, out) common neighbors, no threshold.
    # Scored against the retrieval graph (incl. query pages) like NashSU.
    rlg = retrieval_lg or lg
    neighbors_a = rlg.neighbors(a)
    neighbors_b = rlg.neighbors(b)
    adamic_adar = 0.0
    for c in neighbors_a:
        if c in neighbors_b:
            adamic_adar += 1.0 / math.log(max(rlg.degree(c), 2))
    common_score = adamic_adar * W_COMMON_NEIGHBOR
    if common_score:
        fired.append("adamic_adar")

    # Signal 4: type affinity — unconditional.
    aff = type_affinity(pages[a].page_type, pages[b].page_type)
    type_score = aff * W_TYPE_AFFINITY
    fired.append(f"type_affinity({aff:.1f})")

    return direct_score + source_score + common_score + type_score, fired


def build_weighted_graph(pages: dict[str, Page], lg: LinkGraph,
                         retrieval_lg: Optional["LinkGraph"] = None) -> nx.Graph:
    """Assemble the display link graph, weighting each edge by calculateRelevance.

    ``retrieval_lg`` (full graph incl. query pages) feeds the Adamic-Adar term;
    nodes/edges come from the display ``lg`` only.
    """
    g = nx.Graph()
    g.add_nodes_from(pages.keys())
    for pair in lg.edges:
        u, v = tuple(pair)
        w, _ = calculate_relevance(u, v, pages, lg, retrieval_lg)
        g.add_edge(u, v, weight=round(w, 4))
    return g


# --- Communities (NashSU detectCommunities) ---------------------------------


@dataclass
class Community:
    cid: int
    nodes: list[str]
    cohesion: float           # intra-edge density
    top_nodes: list[str]      # node ids, by unweighted link count desc
    hub: Optional[str]        # top node (highest link count)


def detect_communities(g: nx.Graph, lg: LinkGraph) -> list[Community]:
    """Louvain (unweighted, resolution 1) + density cohesion, renumbered by size.

    NashSU passes no edge-weight getter to louvain → unweighted partition.
    """
    if g.number_of_edges() == 0:
        # No links → each node its own singleton; skip Louvain.
        return [Community(cid=i, nodes=[n], cohesion=0.0, top_nodes=[n], hub=n)
                for i, n in enumerate(sorted(g.nodes))]

    partition = louvain_communities(g, weight=None, seed=LOUVAIN_SEED, resolution=1.0)

    edge_set = {frozenset(e) for e in g.edges()}

    communities: list[Community] = []
    for cid, members in enumerate(sorted(partition, key=len, reverse=True)):
        member_list = sorted(members)
        n = len(member_list)
        # Cohesion = intra-community edges / possible edges (density).
        intra = 0
        for i in range(n):
            for j in range(i + 1, n):
                if frozenset((member_list[i], member_list[j])) in edge_set:
                    intra += 1
        possible = (n * (n - 1) / 2) if n > 1 else 1
        cohesion = intra / possible
        # Top nodes by unweighted link count.
        by_links = sorted(member_list, key=lambda x: lg.link_counts.get(x, 0), reverse=True)
        top_nodes = by_links[:5]
        hub = by_links[0] if by_links else None
        communities.append(Community(cid=cid, nodes=member_list, cohesion=round(cohesion, 4),
                                     top_nodes=top_nodes, hub=hub))
    return communities


def community_assignments(communities: list[Community]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in communities:
        for n in c.nodes:
            out[n] = c.cid
    return out


# --- Knowledge gaps (NashSU detectKnowledgeGaps) ----------------------------


@dataclass
class KnowledgeGap:
    gap_type: str             # isolated-node | sparse-community | bridge-node
    title: str
    description: str
    node_ids: list[str]
    suggestion: str


def detect_knowledge_gaps(pages: dict[str, Page], lg: LinkGraph,
                          communities: list[Community], limit: int = 8) -> list[KnowledgeGap]:
    """Port of NashSU detectKnowledgeGaps (no betweenness)."""
    gaps: list[KnowledgeGap] = []
    assign = community_assignments(communities)

    # 1. Isolated nodes: linkCount <= 1, excluding overview/index/log.
    isolated = [
        nid for nid in pages
        if lg.link_counts.get(nid, 0) <= 1
        and pages[nid].page_type != "overview"
        and pages[nid].stem != "index"
        and pages[nid].stem != "log"
    ]
    isolated.sort()
    if isolated:
        top = isolated[:5]
        desc = ", ".join(pages[n].title for n in top)
        if len(isolated) > 5:
            desc += f" and {len(isolated) - 5} more"
        plural = "s" if len(isolated) > 1 else ""
        gaps.append(KnowledgeGap(
            gap_type="isolated-node",
            title=f"{len(isolated)} isolated page{plural}",
            description=desc,
            node_ids=isolated,
            suggestion="These pages have few or no connections. Consider adding [[wikilinks]] "
                       "to related pages, or research to expand their content.",
        ))

    # 2. Sparse communities: cohesion < 0.15 with >= 3 nodes.
    for comm in communities:
        if comm.cohesion < COHESION_LOW and len(comm.nodes) >= 3:
            entry = pages[comm.top_nodes[0]].title if comm.top_nodes else f"Community {comm.cid}"
            gaps.append(KnowledgeGap(
                gap_type="sparse-community",
                title=f"Sparse cluster: {entry}",
                description=f"{len(comm.nodes)} pages with cohesion {comm.cohesion:.2f} — "
                            "internal connections are weak.",
                node_ids=[n for n in pages if assign.get(n) == comm.cid],
                suggestion="This knowledge area lacks internal cross-references. Consider adding "
                           "links between these pages or researching to fill gaps.",
            ))

    # 3. Bridge nodes: neighbors spanning >= 3 distinct communities.
    community_neighbors: dict[str, set[int]] = {nid: set() for nid in pages}
    for pair in lg.edges:
        u, v = tuple(pair)
        cu, cv = assign.get(u), assign.get(v)
        if cv is not None:
            community_neighbors[u].add(cv)
        if cu is not None:
            community_neighbors[v].add(cu)

    bridge_candidates = [
        nid for nid in pages
        if pages[nid].stem not in INSIGHT_STRUCTURAL_IDS
        and len(community_neighbors.get(nid, set())) >= 3
    ]
    bridge_candidates.sort(key=lambda n: (-len(community_neighbors[n]), n))
    for nid in bridge_candidates[:3]:
        count = len(community_neighbors[nid])
        gaps.append(KnowledgeGap(
            gap_type="bridge-node",
            title=f"Key bridge: {pages[nid].title}",
            description=f"Connects {count} different knowledge clusters. This is a critical "
                        "junction in your wiki.",
            node_ids=[nid],
            suggestion="This page bridges multiple knowledge areas. Ensure it's well-maintained — "
                       "if it's thin, expanding it will strengthen your entire wiki.",
        ))

    return gaps[:limit]


# --- Surprising connections (NashSU findSurprisingConnections) --------------


@dataclass
class SurprisingConnection:
    source: str
    target: str
    score: int
    reasons: list[str]


def find_surprising_connections(g: nx.Graph, pages: dict[str, Page],
                                communities: list[Community],
                                limit: int = 5) -> list[SurprisingConnection]:
    """Port of NashSU findSurprisingConnections (threshold >= 3, top-5).

    Uses node.linkCount (inbound + outbound directed) stashed on g.nodes.
    """
    assign = community_assignments(communities)
    link_counts = {nid: int(g.nodes[nid].get("linkCount", 0)) for nid in g.nodes}
    max_degree = max([link_counts.get(n, 0) for n in g.nodes] + [1])

    scored: list[SurprisingConnection] = []
    for u, v, data in g.edges(data=True):
        if pages[u].stem in INSIGHT_STRUCTURAL_IDS or pages[v].stem in INSIGHT_STRUCTURAL_IDS:
            continue
        score = 0
        reasons: list[str] = []

        # Signal 1: cross-community (+3).
        if assign.get(u) != assign.get(v):
            score += 3
            reasons.append("crosses community boundary")

        # Signal 2: cross-type (+2 distant / +1 otherwise).
        tu, tv = pages[u].page_type, pages[v].page_type
        if tu != tv:
            if f"{tu}-{tv}" in DISTANT_TYPE_PAIRS:
                score += 2
                reasons.append(f"connects {tu} to {tv}")
            else:
                score += 1
                reasons.append("different types")

        # Signal 3: peripheral-to-hub (+2).
        du, dv = link_counts.get(u, 0), link_counts.get(v, 0)
        if min(du, dv) <= 2 and max(du, dv) >= max_degree * 0.5:
            score += 2
            reasons.append("peripheral node links to hub")

        # Signal 4: weak-but-present edge (+1).
        w = data.get("weight", 0)
        if 0 < w < 2:
            score += 1
            reasons.append("weak but present connection")

        if score >= 3 and reasons:
            scored.append(SurprisingConnection(source=u, target=v, score=score, reasons=reasons))

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:limit]


# --- Graph filters (NashSU applyGraphFilters / isStructuralGraphNode) -------


def is_structural_graph_node(page: Page) -> bool:
    """Port of NashSU isStructuralGraphNode."""
    if page.stem.lower() in STRUCTURAL_IDS:
        return True
    if page.page_type == "overview":
        return True
    norm = page.path.as_posix().lower()
    return (
        norm.endswith("/wiki/index.md")
        or norm.endswith("/wiki/overview.md")
        or norm.endswith("/wiki/log.md")
        or norm.endswith("/purpose.md")
        or norm.endswith("/schema.md")
    )


def apply_graph_filters(g: nx.Graph, pages: dict[str, Page], lg: LinkGraph,
                        hide_structural: bool = True) -> nx.Graph:
    """Port of NashSU applyGraphFilters with DEFAULT_GRAPH_FILTERS.

    Defaults: hideStructural=True, hideIsolated=False, no hiddenTypes/maxLinks.
    Returns a filtered copy of the graph.
    """
    hidden: set[str] = set()
    for nid in g.nodes:
        if hide_structural and is_structural_graph_node(pages[nid]):
            hidden.add(nid)
    visible = [n for n in g.nodes if n not in hidden]
    return g.subgraph(visible).copy()


# --- Output writers ---------------------------------------------------------


def _node_payload(nid: str, pages: dict[str, Page], lg: LinkGraph,
                  assign: dict[str, int]) -> dict:
    p = pages[nid]
    return {
        "id": nid, "stem": p.stem, "title": p.title, "type": p.page_type,
        "linkCount": lg.link_counts.get(nid, 0),
        "community": assign.get(nid, -1),
    }


def write_graph_json(out: Path, g: nx.Graph, pages: dict[str, Page], lg: LinkGraph,
                     communities: list[Community], gaps: list[KnowledgeGap],
                     surprising: list[SurprisingConnection], stats: dict) -> None:
    assign = community_assignments(communities)
    payload = {
        "stats": stats,
        "nodes": [_node_payload(n, pages, lg, assign) for n in sorted(g.nodes)],
        "edges": [
            {"source": u, "target": v, "weight": d["weight"]}
            for u, v, d in sorted(g.edges(data=True), key=lambda e: -e[2]["weight"])
        ],
        "communities": [
            {"id": c.cid, "nodes": c.nodes, "cohesion": c.cohesion,
             "topNodes": c.top_nodes, "hub": c.hub,
             "low_quality": c.cohesion < COHESION_LOW}
            for c in communities
        ],
        "gaps": [
            {"type": gp.gap_type, "title": gp.title, "description": gp.description,
             "nodeIds": gp.node_ids, "suggestion": gp.suggestion}
            for gp in gaps
        ],
        "surprisingConnections": [
            {"source": s.source, "target": s.target, "score": s.score, "reasons": s.reasons}
            for s in surprising
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_graph_html(out: Path, g: nx.Graph, pages: dict[str, Page], lg: LinkGraph,
                     communities: list[Community], gaps: list[KnowledgeGap]) -> None:
    """Write a self-contained D3.js + ForceAtlas2 force-directed HTML graph."""
    assign = community_assignments(communities)

    nodes_js = []
    for nid in sorted(g.nodes):
        p = pages[nid]
        nodes_js.append({
            "id": nid, "label": p.title[:30], "type": p.page_type,
            "community": assign.get(nid, -1),
        })

    edges_js = [
        {"source": u, "target": v, "weight": round(d["weight"], 2)}
        for u, v, d in sorted(g.edges(data=True), key=lambda e: -e[2]["weight"])
    ]

    type_colors = {
        "entity": "#60a5fa", "concept": "#c084fc", "source": "#fb923c",
        "query": "#4ade80", "synthesis": "#f87171", "overview": "#facc15",
        "comparison": "#2dd4bf", "finding": "#a855f7", "thesis": "#f43f5e",
        "methodology": "#14b8a6", "other": "#94a3b8",
    }
    type_labels = {
        "entity": "实体", "concept": "概念", "source": "来源", "query": "查询",
        "synthesis": "综合", "overview": "概览", "comparison": "对比",
        "finding": "发现", "thesis": "论点", "methodology": "方法论", "other": "其他",
    }
    community_colors = [
        "#60a5fa", "#4ade80", "#fb923c", "#c084fc", "#f87171", "#2dd4bf",
        "#facc15", "#f472b6", "#a78bfa", "#38bdf8", "#34d399", "#fbbf24",
    ]

    type_counts: dict[str, int] = {}
    for n in nodes_js:
        type_counts[n["type"]] = type_counts.get(n["type"], 0) + 1
    type_legend = "".join(
        f'<div class="legend-item"><span class="legend-dot" style="background:{type_colors.get(t, "#94a3b8")}"></span>'
        f'<span>{type_labels.get(t, t)} <span class="legend-meta">{cnt}</span></span></div>'
        for t, cnt in sorted(type_counts.items(), key=lambda x: -x[1])
    )

    community_legend = ""
    for c in communities:
        if c.cohesion >= COHESION_LOW and c.hub in pages:
            hub_label = pages[c.hub].title[:30]
            color = community_colors[c.cid % len(community_colors)]
            community_legend += (
                f'<div class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
                f'<span>C{c.cid}: {hub_label} <span class="legend-meta">{len(c.nodes)}页 coh {c.cohesion:.2f}</span></span></div>'
            )

    isolated_gap = next((gp for gp in gaps if gp.gap_type == "isolated-node"), None)
    gap_ids = isolated_gap.node_ids[:5] if isolated_gap else []
    gaps_html = "".join(
        f'<div class="gap-item">&#9651; {pages[n].title[:26] if n in pages else n}</div>'
        for n in gap_ids
    ) or '<div class="gap-item" style="color:#64748b">无明显空缺</div>'

    nodes_json = json.dumps(nodes_js, ensure_ascii=False)
    edges_json = json.dumps(edges_js)
    total_pages = g.number_of_nodes()
    total_edges = g.number_of_edges()
    total_communities = len(communities)

    template = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Knowledge Graph</title>
<style>
body{margin:0;background:#0f172a;color:#e2e8f0;font-family:sans-serif}
#header{padding:10px 20px;background:#1e293b;border-bottom:1px solid #334155;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
#header h2{margin:0;font-size:15px;color:#7eb8f7}
#hstats{font-size:12px;color:#94a3b8}
#controls{margin-left:auto;display:flex;gap:4px}
#controls button{background:#334155;border:1px solid #475569;color:#cbd5e1;padding:4px 12px;border-radius:4px;font-size:12px;cursor:pointer}
#controls button.active{background:#3b82f6;border-color:#60a5fa;color:#fff}
#container{display:flex;height:calc(100vh - 48px)}
#sidebar{width:220px;background:#1e293b;padding:12px;overflow-y:auto;border-right:1px solid #334155;flex-shrink:0}
#sidebar h3{margin:8px 0 6px;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px}
.legend-item{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:11px;padding:3px 4px;border-radius:3px}
.legend-item:hover{background:#334155}
.legend-dot{width:11px;height:11px;border-radius:50%;flex-shrink:0}
.legend-meta{color:#64748b;font-size:10px}
#canvas{flex:1;position:relative}
svg{width:100%;height:100%}
.node circle{stroke:#0f172a;stroke-width:1px;cursor:pointer;transition:stroke .1s}
.node circle:hover{stroke:#e2e8f0}
.nlabel{font-size:9px;fill:#cbd5e1;pointer-events:none;font-weight:600}
.link{stroke:#475569}
#tooltip{position:fixed;background:#1e1e3a;border:1px solid #555;border-radius:6px;padding:8px 12px;font-size:12px;pointer-events:none;display:none;max-width:320px;line-height:1.5;z-index:100;box-shadow:0 4px 12px rgba(0,0,0,.4)}
#search{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #475569;color:#e2e8f0;padding:6px 8px;border-radius:4px;font-size:12px;margin-bottom:6px}
.gap-item{font-size:10px;color:#fbbf24;margin:4px 0;padding:3px 0;border-bottom:1px solid #334155}
#loading{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#64748b;font-size:13px}
</style>
</head>
<body>
<div id="header">
  <h2>&#128375; Knowledge Graph</h2>
  <div id="hstats">%%TOTAL_PAGES%% pages &nbsp;|&nbsp; %%TOTAL_EDGES%% edges &nbsp;|&nbsp; %%TOTAL_COMMUNITIES%% communities</div>
  <div id="controls">
    <button id="btn-type" class="active">按类型</button>
    <button id="btn-community">按社区</button>
  </div>
</div>
<div id="container">
  <div id="sidebar">
    <input id="search" type="text" placeholder="搜索节点..." oninput="filterNodes(this.value)">
    <h3>图例</h3>
    <div id="legend-type">%%TYPE_LEGEND%%</div>
    <div id="legend-community" style="display:none">%%COMMUNITY_LEGEND%%</div>
    <h3>知识空缺</h3>
    %%GAPS%%
  </div>
  <div id="canvas"><div id="loading">布局中…</div></div>
</div>
<div id="tooltip"></div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script type="module">
const nodes = %%NODES%%;
const links = %%EDGES%%;

const TYPE_COLORS = {entity:"#60a5fa",concept:"#c084fc",source:"#fb923c",query:"#4ade80",synthesis:"#f87171",overview:"#facc15",comparison:"#2dd4bf",finding:"#a855f7",thesis:"#f43f5e",methodology:"#14b8a6",other:"#94a3b8"};
const COMMUNITY_COLORS = ["#60a5fa","#4ade80","#fb923c","#c084fc","#f87171","#2dd4bf","#facc15","#f472b6","#a78bfa","#38bdf8","#34d399","#fbbf24"];
let colorMode = "type";

const d3 = window.d3;
const canvasEl = document.getElementById("canvas");
const W = canvasEl.clientWidth || 1200;
const H = canvasEl.clientHeight || 800;
document.getElementById("loading").remove();

const svg = d3.select("#canvas").append("svg").attr("viewBox",[0,0,W,H])
  .call(d3.zoom().on("zoom", e => gel.attr("transform", e.transform)));
const gel = svg.append("g");

const nodeCount = nodes.length;
const deg = {};
links.forEach(l => { deg[l.source]=(deg[l.source]||0)+1; deg[l.target]=(deg[l.target]||0)+1; });
const maxLinks = Math.max(...nodes.map(n=>deg[n.id]||0), 1);
const densityScale = nodeCount <= 150 ? 1 : Math.max(0.35, Math.sqrt(150/nodeCount));

function nodeR(d) {
  const ratio = (deg[d.id]||0)/maxLinks;
  return Math.max(3, (4 + Math.sqrt(ratio)*12) * densityScale);
}
function colorOf(d) {
  if (colorMode === "community") return COMMUNITY_COLORS[d.community % COMMUNITY_COLORS.length] || "#888";
  return TYPE_COLORS[d.type] || TYPE_COLORS.other;
}

const nodeMap = {};
nodes.forEach(n => nodeMap[n.id] = n);

const maxWeight = Math.max(...links.map(l=>l.weight), 1);
const weakThreshold = nodeCount > 2500 ? 0.16 : nodeCount > 1200 ? 0.1 : nodeCount > 700 ? 0.05 : 0;
const weakHidden = new Set();
links.forEach(l => { if (weakThreshold > 0 && (l.weight/maxWeight) < weakThreshold) weakHidden.add(l); });
const labelThreshold = nodeCount > 2500 ? 18 : nodeCount > 1200 ? 14 : nodeCount > 600 ? 10 : 6;

const link = gel.append("g").selectAll("line").data(links).join("line").attr("class","link")
  .attr("stroke-width", l => Math.min(0.5 + (l.weight/maxWeight)*3, 3))
  .attr("opacity", l => weakHidden.has(l) ? 0 : 0.35);

const node = gel.append("g").selectAll("g.node").data(nodes).join("g").attr("class","node")
  .call(d3.drag()
    .on("start", (e,d) => { if (sim) { if(!e.active) sim.alphaTarget(0.3).restart(); } d.fx=d.x; d.fy=d.y; })
    .on("drag", function(e,d) {
      d.x=e.x; d.y=e.y; d.fx=e.x; d.fy=e.y;
      d3.select(this).attr("transform", `translate(${d.x},${d.y})`);
      link.filter(l => (l.s&&l.s.id===d.id) || (l.t&&l.t.id===d.id))
          .attr("x1", l => l.s.x).attr("y1", l => l.s.y).attr("x2", l => l.t.x).attr("y2", l => l.t.y);
    })
    .on("end", (e,d) => { if (sim) sim.alphaTarget(0); d.fx=null; d.fy=null; }));

node.append("circle").attr("r", nodeR).attr("fill", colorOf).attr("stroke","#0f172a").attr("stroke-width",1);
node.filter(d => (deg[d.id]||0) >= labelThreshold).append("text")
  .attr("dy", d => nodeR(d)+10).attr("text-anchor","middle").attr("class","nlabel").text(d=>d.label);

function renderPositions() {
  link.attr("x1", l => l.s.x).attr("y1", l => l.s.y).attr("x2", l => l.t.x).attr("y2", l => l.t.y);
  node.attr("transform", d => `translate(${d.x},${d.y})`);
}

const tt = document.getElementById("tooltip");
function showTooltip(e,d) {
  tt.style.display="block";
  tt.innerHTML = `<b>${d.label}</b><br><span style="color:#94a3b8;font-size:10px">${d.type} · C${d.community}</span><br><span style="color:#64748b;font-size:10px">${d.id}</span><br>Degree: ${deg[d.id]||0}`;
  moveTooltip(e);
}
function moveTooltip(e) { tt.style.left=(e.clientX+12)+"px"; tt.style.top=(e.clientY-20)+"px"; }
function hideTooltip() { tt.style.display="none"; }

function highlight(id) {
  const nb = new Set([id]);
  links.forEach(l => { if (l.s&&l.s.id===id) nb.add(l.t.id); if (l.t&&l.t.id===id) nb.add(l.s.id); });
  node.style("opacity", d => nb.has(d.id) ? 1 : 0.12);
  node.select("circle").attr("stroke", d => d.id===id ? "#fff" : (nb.has(d.id) ? "#94a3b8" : "#0f172a")).attr("stroke-width", d => d.id===id ? 2.5 : (nb.has(d.id) ? 1.5 : 1));
  link.attr("opacity", l => {
    if ((l.s&&l.s.id===id) || (l.t&&l.t.id===id)) return 0.85;
    if (weakHidden.has(l)) return 0;
    return 0.04;
  });
}
function clearHighlight() {
  node.style("opacity", 1);
  node.select("circle").attr("stroke","#0f172a").attr("stroke-width",1);
  link.attr("opacity", l => weakHidden.has(l) ? 0 : 0.35);
}

node.select("circle")
  .on("mouseover", (e,d) => { showTooltip(e,d); highlight(d.id); })
  .on("mousemove", moveTooltip)
  .on("mouseout", () => { hideTooltip(); clearHighlight(); });

window.filterNodes = function(q) {
  const lo = q.toLowerCase().trim();
  if (!lo) { clearHighlight(); return; }
  const match = d => d.id.toLowerCase().includes(lo) || d.label.toLowerCase().includes(lo);
  node.style("opacity", d => match(d) ? 1 : 0.1);
  node.select("circle").attr("stroke", d => match(d) ? "#fff" : "#0f172a").attr("stroke-width", d => match(d) ? 2 : 1);
  link.attr("opacity", 0.03);
};

document.getElementById("btn-type").onclick = () => setColorMode("type");
document.getElementById("btn-community").onclick = () => setColorMode("community");
function setColorMode(m) {
  colorMode = m;
  node.select("circle").attr("fill", colorOf);
  document.getElementById("btn-type").classList.toggle("active", m==="type");
  document.getElementById("btn-community").classList.toggle("active", m==="community");
  document.getElementById("legend-type").style.display = m==="type" ? "block" : "none";
  document.getElementById("legend-community").style.display = m==="community" ? "block" : "none";
}

let sim = null;

function withTimeout(p, ms) {
  return Promise.race([p, new Promise((_,rej) => setTimeout(() => rej(new Error("timeout")), ms))]);
}

async function runFA2() {
  const Gmod = await withTimeout(import("https://esm.run/graphology"), 10000);
  const Fmod = await withTimeout(import("https://esm.run/graphology-layout-forceatlas2"), 10000);
  const Graph = Gmod.default || Gmod.Graph;
  const fa2 = Fmod.default || Fmod;
  const gg = new Graph();
  nodes.forEach(n => { if (!gg.hasNode(n.id)) gg.addNode(n.id, { x: Math.random()*100, y: Math.random()*100 }); });
  links.forEach(l => { try { if (gg.hasNode(l.source) && gg.hasNode(l.target) && !gg.hasEdge(l.source,l.target)) gg.addEdge(l.source, l.target, { weight: l.weight }); } catch(e){} });
  const iters = nodeCount > 2500 ? 28 : nodeCount > 1200 ? 40 : nodeCount > 600 ? 65 : nodeCount > 250 ? 90 : 140;
  fa2.assign(gg, { iterations: iters, settings: { gravity: 1, scalingRatio: nodeCount > 400 ? 3 : 2, strongGravityMode: true, barnesHutOptimize: nodeCount > 50, slowDown: 10 } });
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  gg.forEachNode((id,a) => {
    const n = nodeMap[id]; n.x = a.x; n.y = a.y;
    minX=Math.min(minX,a.x); maxX=Math.max(maxX,a.x);
    minY=Math.min(minY,a.y); maxY=Math.max(maxY,a.y);
  });
  const sc = Math.min((W-80)/(maxX-minX||1), (H-80)/(maxY-minY||1));
  nodes.forEach(n => { n.x = (n.x - minX)*sc + 40; n.y = (n.y - minY)*sc + 40; });
  links.forEach(l => { l.s = nodeMap[l.source]; l.t = nodeMap[l.target]; });
  renderPositions();
}

function runFallback() {
  sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d=>d.id).distance(30).strength(l=>Math.min(l.weight/15,0.5)))
    .force("charge", d3.forceManyBody().strength(-60))
    .force("center", d3.forceCenter(W/2, H/2))
    .force("collision", d3.forceCollide().radius(d=>nodeR(d)+2));
  sim.on("tick", () => {
    links.forEach(l => { l.s = l.source; l.t = l.target; });
    renderPositions();
  });
}

try {
  await runFA2();
} catch(err) {
  console.warn("[graph] ForceAtlas2 unavailable, falling back to d3-force:", err);
  runFallback();
}
</script>
</body>
</html>"""

    html = (template
            .replace("%%NODES%%", nodes_json)
            .replace("%%EDGES%%", edges_json)
            .replace("%%TYPE_LEGEND%%", type_legend)
            .replace("%%COMMUNITY_LEGEND%%", community_legend)
            .replace("%%GAPS%%", gaps_html)
            .replace("%%TOTAL_PAGES%%", str(total_pages))
            .replace("%%TOTAL_EDGES%%", str(total_edges))
            .replace("%%TOTAL_COMMUNITIES%%", str(total_communities)))
    out.write_text(html, encoding="utf-8")


def write_knowledge_gaps(out: Path, gaps: list[KnowledgeGap], pages: dict[str, Page]) -> None:
    isolated = next((g for g in gaps if g.gap_type == "isolated-node"), None)
    sparse = [g for g in gaps if g.gap_type == "sparse-community"]
    bridges = [g for g in gaps if g.gap_type == "bridge-node"]

    lines: list[str] = ["# Knowledge Gaps", ""]
    lines.append(f"- Isolated pages: **{len(isolated.node_ids) if isolated else 0}**")
    lines.append(f"- Sparse clusters: **{len(sparse)}**")
    lines.append(f"- Bridge pages: **{len(bridges)}**")
    lines.append("")
    if isolated:
        lines.append(f"## {isolated.title}")
        lines.append(isolated.suggestion)
        lines.append("")
        for n in isolated.node_ids[:50]:
            lines.append(f"- `[[{pages[n].stem}]]` — {pages[n].title} ({n})")
        if len(isolated.node_ids) > 50:
            lines.append(f"- … and {len(isolated.node_ids) - 50} more")
        lines.append("")
    if sparse:
        lines.append("## Sparse clusters (cohesion < 0.15)")
        for gp in sparse:
            lines.append(f"### {gp.title}")
            lines.append(gp.description)
            lines.append("")
    if bridges:
        lines.append("## Bridge pages (span ≥ 3 communities)")
        for gp in bridges:
            n = gp.node_ids[0]
            lines.append(f"- `[[{pages[n].stem}]]` — {gp.title}: {gp.description}")
        lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def write_clusters(clusters_dir: Path, communities: list[Community],
                   pages: dict[str, Page]) -> None:
    clusters_dir.mkdir(parents=True, exist_ok=True)
    for c in communities:
        if len(c.nodes) < 2:
            continue
        hub_p = pages[c.hub] if c.hub and c.hub in pages else None
        lines = [
            "---",
            "type: index",
            f"title: \"Cluster {c.cid:03d}\"",
            "tags: [knowledge-graph, cluster]",
            "---",
            "",
            f"# Cluster {c.cid:03d}",
            "",
            f"- Members: **{len(c.nodes)}**",
            f"- Cohesion: **{c.cohesion}**{' ⚠️ low' if c.cohesion < COHESION_LOW else ''}",
            f"- Hub: {('`[[' + hub_p.stem + ']]` — ' + hub_p.title) if hub_p else '—'}",
            "",
            "## Members",
            "",
        ]
        for n in c.nodes:
            lines.append(f"- `[[{pages[n].stem}]]` — {pages[n].title} ({pages[n].page_type})")
        (clusters_dir / f"cluster-{c.cid:03d}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- Query mode (NashSU getRelatedNodes) ------------------------------------


def query_suggestions(pages: dict[str, Page], lg: LinkGraph,
                      slug: str, top_n: int,
                      retrieval_lg: Optional["LinkGraph"] = None) -> tuple[Optional[str], list[dict]]:
    """Top calculateRelevance neighbors of ``slug`` (NashSU getRelatedNodes).

    CLI choice: already-linked pages are excluded so this suggests NEW links.
    """
    resolver = build_resolver(pages)
    node = resolver.resolve(slug)
    if not node:
        node = slug if slug in pages else None
    if not node:
        return None, []
    p = pages[node]
    already = {resolver.resolve(t) for t in p.links}
    already.add(node)
    scored: list[dict] = []
    for other in pages:
        if other in already:
            continue
        w, fired = calculate_relevance(node, other, pages, lg, retrieval_lg)
        if w > 0:
            scored.append({"target": other, "stem": pages[other].stem,
                           "title": pages[other].title, "weight": round(w, 4),
                           "signals": fired})
    scored.sort(key=lambda d: d["weight"], reverse=True)
    return node, scored[:top_n]


# --- Main -------------------------------------------------------------------


def _resolve_wiki_root(arg: Optional[Path]) -> Path:
    return arg or Path.cwd()


def run_build(wiki_root: Path, output: Optional[Path], dry_run: bool,
              include_all: bool) -> int:
    all_pages = load_pages(wiki_root, include_hidden=True)   # retrieval graph (incl. query)
    pages = {nid: p for nid, p in all_pages.items() if p.page_type not in HIDDEN_TYPES}
    if not pages:
        print(f"❌ No wiki pages under {wiki_root / 'wiki'}")
        return 1
    retrieval_lg = build_link_graph(all_pages)
    lg = build_link_graph(pages)                              # display graph (excl. query)
    g = build_weighted_graph(pages, lg, retrieval_lg)
    # Stash linkCount on nodes so surprising-connection scoring matches NashSU.
    for nid in g.nodes:
        g.nodes[nid]["linkCount"] = lg.link_counts.get(nid, 0)

    communities = detect_communities(g, lg)
    gaps = detect_knowledge_gaps(pages, lg, communities)
    surprising = find_surprising_connections(g, pages, communities)

    low_q = sum(1 for c in communities if c.cohesion < COHESION_LOW)
    isolated_gap = next((gp for gp in gaps if gp.gap_type == "isolated-node"), None)
    isolated_count = len(isolated_gap.node_ids) if isolated_gap else 0
    stats = {
        "total_pages": len(pages),
        "total_edges": g.number_of_edges(),
        "communities": len(communities),
        "low_quality_communities": low_q,
        "isolated_pages": isolated_count,
        "surprising_connections": len(surprising),
    }
    print("🕸️  Graph: Knowledge Graph Builder")
    print(f"  Wiki root: {wiki_root}")
    print(f"  Pages: {stats['total_pages']}")
    print(f"  Edges: {stats['total_edges']}")
    print(f"  Communities: {stats['communities']} ({low_q} low-cohesion)")
    print(f"  Isolated pages: {stats['isolated_pages']}")
    print(f"  Surprising connections: {stats['surprising_connections']}")
    if dry_run:
        print("  (dry-run — no files written)")
        return 0

    # Rendered graph uses NashSU default filters unless --include-all.
    rendered = g if include_all else apply_graph_filters(g, pages, lg, hide_structural=True)

    runtime = detect_runtime_dir(wiki_root)
    graph_json = output or (runtime / "graph.json")
    write_graph_json(graph_json, rendered, pages, lg, communities, gaps, surprising, stats)
    print(f"📁 Wrote {graph_json}")
    graph_html = graph_json.with_suffix(".html")
    write_graph_html(graph_html, rendered, pages, lg, communities, gaps)
    print(f"🌐 Wrote {graph_html}")
    wiki_dir = wiki_root / "wiki"
    gaps_md = wiki_dir / "REVIEW" / "knowledge-gaps.md"
    write_knowledge_gaps(gaps_md, gaps, pages)
    print(f"📄 Wrote {gaps_md}")
    write_clusters(wiki_dir / "clusters", communities, pages)
    written = sum(1 for c in communities if len(c.nodes) >= 2)
    print(f"📂 Wrote {written} cluster pages to {wiki_dir / 'clusters'}/")
    return 0


def run_query(wiki_root: Path, slug: str, top_n: int) -> int:
    all_pages = load_pages(wiki_root, include_hidden=True)   # retrieval graph (incl. query)
    pages = {nid: p for nid, p in all_pages.items() if p.page_type not in HIDDEN_TYPES}
    if not pages:
        print(f"❌ No wiki pages under {wiki_root / 'wiki'}")
        return 1
    retrieval_lg = build_link_graph(all_pages)
    lg = build_link_graph(pages)                              # display graph (excl. query)
    node, suggestions = query_suggestions(pages, lg, slug, top_n, retrieval_lg)
    if not node:
        print(f"❌ No page matches slug '{slug}'")
        return 1
    p = pages[node]
    print(f"🔍 Suggestions for `{p.stem}` — {p.title} ({node})")
    if not suggestions:
        print("  (none)")
        return 0
    for i, s in enumerate(suggestions, 1):
        print(f"  {i}. {s['stem']} — {s['title']} ({s['target']})")
        print(f"     weight {s['weight']} · {', '.join(s['signals'])}")
        print(f"     → add [[{s['stem']}]]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build knowledge graph (NashSU parity)")
    parser.add_argument("--wiki-root", type=Path, help="Wiki project root (default: cwd)")
    parser.add_argument("--output", type=Path, help="graph.json output path (default: <runtime>/graph.json)")
    parser.add_argument("--mode", choices=["build", "query"], default="build",
                        help="build = rebuild graph + outputs; query = per-page suggestions")
    parser.add_argument("--slug", help="Page slug/path for --mode query")
    parser.add_argument("--max-suggestions", type=int, default=5,
                        help="Top-N for query mode (default: 5)")
    parser.add_argument("--include-all", action="store_true",
                        help="Emit unfiltered graph (skip NashSU default structural filter)")
    parser.add_argument("--dry-run", action="store_true", help="Stats only, no file writes")
    args = parser.parse_args()

    wiki_root = _resolve_wiki_root(args.wiki_root)
    if not wiki_root.exists():
        print(f"❌ Wiki root not found: {wiki_root}")
        return 1

    if args.mode == "query":
        if not args.slug:
            print("❌ --mode query requires --slug")
            return 1
        return run_query(wiki_root, args.slug, args.max_suggestions)
    return run_build(wiki_root, args.output, args.dry_run, args.include_all)


if __name__ == "__main__":
    sys.exit(main())
