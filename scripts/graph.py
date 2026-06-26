#!/usr/bin/env python3
"""graph.py — Knowledge graph builder (NashSU graph-relevance.ts parity).

Peer command of Ingest/Lint. Deterministic — no LLM, no embedding. Reads
``wiki/**/*.md`` frontmatter (title/type/sources) + ``[[wikilinks]]`` and
builds a four-signal weighted graph, then runs Louvain community detection.

Signals (per NashSU ``src/lib/graph-relevance.ts``):

  direct link    ×3.0  — [[wikilink]] or ``related:`` frontmatter between pages
  source overlap ×4.0  — share ≥1 raw source file (``sources:`` frontmatter)
  Adamic-Adar    ×1.5  — Σ 1/log(deg(c)) over common neighbors in the link graph
  type affinity  ×1.0  — page-type pair affinity (entity↔concept 1.2, …)

An edge is created only when a *structural* signal fires (direct link, source
overlap, or Adamic-Adar > 0). Type affinity is a boost on top — it never invents
an edge by itself, so two same-type pages with no other signal stay unconnected.

Outputs (build mode):
  <runtime>/graph.json           — full graph (nodes/edges/communities/gaps)
  <wiki>/knowledge-gaps.md       — isolated/bridge nodes + suggested links
  <wiki>/clusters/cluster-NNN.md — per-community hub page

Modes:
  build (default)              — rebuild graph + write outputs
  query --slug S               — read-only: top suggested wikilinks for page S
  --dry-run                    — stats only, no file writes

Usage:
  python3 graph.py
  python3 graph.py --wiki-root ~/Documents/知识库/HardwareWiki
  python3 graph.py --dry-run
  python3 graph.py --mode query --slug 风扇轴承类型
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import yaml
from networkx.algorithms.community import louvain_communities

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402

# --- Signal weights (NashSU-aligned, see references/nashsu-search-architecture.md) ---
W_DIRECT_LINK = 3.0
W_SOURCE_OVERLAP = 4.0
W_ADAMIC_ADAR = 1.5
W_TYPE_AFFINITY = 1.0

# Louvain seed — deterministic across runs.
LOUVAIN_SEED = 42

# Cohesion below this marks a low-quality community.
COHESION_LOW = 0.15

# Scalability caps (see references/nashsu-search-architecture.md for the signal
# model). NashSU computes relevance on-demand per pair; we materialize a graph
# for Louvain, so we bound the two signals that explode on large wikis:
#   - A book with N concept pages yields a N²/2 source-overlap clique. For
#     N > MAX_SOURCE_CLIQUE we drop the pairwise clique and connect each member
#     to the source-page hub instead (star, O(N) edges). The pages still cluster
#     via the hub; pairwise source-overlap for mega-books is sacrificed.
#   - Adamic-Adar via a high-degree hub is ~1/log(deg) → tiny and noisy. Drop AA
#     pairs below MIN_AA_SCORE (keeps common neighbors with deg < ~150).
MAX_SOURCE_CLIQUE = 100
MIN_AA_SCORE = 0.2

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# --- Type affinity matrix ---------------------------------------------------
# NashSU documents entity↔concept 1.2 and concept↔synthesis 1.2; the rest are
# reasonable extensions. ``type_affinity`` returns a small default for unlisted
# pairs and a lower floor for structural/navigational page types.
_AFFINITY: dict[tuple[str, str], float] = {
    ("entity", "concept"): 1.2,
    ("concept", "entity"): 1.2,
    ("concept", "synthesis"): 1.2,
    ("synthesis", "concept"): 1.2,
    ("concept", "finding"): 1.1,
    ("finding", "concept"): 1.1,
    ("synthesis", "finding"): 1.1,
    ("finding", "synthesis"): 1.1,
    ("synthesis", "thesis"): 1.0,
    ("thesis", "synthesis"): 1.0,
    ("concept", "thesis"): 1.0,
    ("thesis", "concept"): 1.0,
    ("entity", "entity"): 1.0,
    ("concept", "concept"): 1.0,
    ("comparison", "concept"): 1.0,
    ("concept", "comparison"): 1.0,
    ("comparison", "entity"): 1.0,
    ("entity", "comparison"): 1.0,
}
# Structural / navigational pages contribute little semantic affinity.
_LOW_AFFINITY_TYPES = {"index", "overview", "schema", "log", "disambiguation", "methodology"}


def type_affinity(t1: str, t2: str) -> float:
    """Affinity score for a page-type pair."""
    if t1 in _LOW_AFFINITY_TYPES or t2 in _LOW_AFFINITY_TYPES:
        return 0.2
    return _AFFINITY.get((t1, t2), 0.3)


# --- Page model -------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One wiki page parsed from disk."""

    node_id: str          # path relative to wiki_root, no '.md' (e.g. 'wiki/concepts/X')
    stem: str             # filename stem (e.g. 'X')
    title: str
    page_type: str        # frontmatter 'type:' lowercased; 'unknown' if missing
    domain: str
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


def load_pages(wiki_root: Path) -> dict[str, Page]:
    """Parse every ``*.md`` under ``<wiki_root>/wiki/`` into a Page."""
    wiki_dir = wiki_root / "wiki"
    if not wiki_dir.exists():
        return {}
    pages: dict[str, Page] = {}
    for md in sorted(wiki_dir.rglob("*.md")):
        rel = md.relative_to(wiki_root).with_suffix("")
        node_id = rel.as_posix()
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = _parse_frontmatter(text)
        page_type = str(fm.get("type", "unknown")).lower().strip()
        title = str(fm.get("title", md.stem))
        domain = str(fm.get("domain", "")).lower().strip()
        sources = tuple(s for s in _as_list(fm.get("sources")))
        # Direct links = [[wikilinks]] in body + `related:` frontmatter paths.
        targets: list[str] = []
        targets.extend(m.split("|")[0].split("#")[0].strip() for m in WIKILINK_RE.findall(body))
        targets.extend(_as_list(fm.get("related")))
        links = tuple(t for t in targets if t)
        pages[node_id] = Page(
            node_id=node_id, stem=md.stem, title=title, page_type=page_type,
            domain=domain, sources=sources, links=links, path=md,
        )
    return pages


# --- Link target resolution -------------------------------------------------


@dataclass
class LinkResolver:
    """Resolve a wikilink/related target string to a node id."""

    by_path: dict[str, str]            # 'wiki/concepts/X' -> node_id
    by_stem: dict[str, list[str]]      # stem -> [node_id, ...]

    def resolve(self, target: str) -> str | None:
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


# --- Signal computation -----------------------------------------------------


@dataclass
class Signals:
    """Precomputed pair signals. All keys are frozenset[{u, v}]."""

    direct: set[frozenset[str]] = field(default_factory=set)
    source_overlap: set[frozenset[str]] = field(default_factory=set)
    adamic_adar: dict[frozenset[str], float] = field(default_factory=dict)


def compute_signals(pages: dict[str, Page]) -> Signals:
    sig = Signals()
    resolver = build_resolver(pages)

    # 1) Direct links (symmetrized).
    for src, p in pages.items():
        for tgt_raw in p.links:
            dst = resolver.resolve(tgt_raw)
            if dst and dst in pages and dst != src:
                sig.direct.add(frozenset((src, dst)))

    # 2) Source overlap — group pages by each shared source string. A source with
    #    N pages yields a N²/2 clique; for N > MAX_SOURCE_CLIQUE use a star around
    #    the source-page hub (O(N) edges) to bound graph size on large wikis.
    by_source: dict[str, list[str]] = defaultdict(list)
    for nid, p in pages.items():
        for s in p.sources:
            by_source[s].append(nid)
    for members in by_source.values():
        if len(members) < 2:
            continue
        if len(members) <= MAX_SOURCE_CLIQUE:
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    sig.source_overlap.add(frozenset((members[i], members[j])))
        else:
            hub = next((m for m in members if pages[m].page_type == "source"), members[0])
            for m in members:
                if m != hub:
                    sig.source_overlap.add(frozenset((hub, m)))

    # 3) Adamic-Adar over the (sparse) direct-link graph only. Drop pairs below
    #    MIN_AA_SCORE — they are hub-mediated noise (1/log(deg) ≈ 0 for high-deg
    #    hubs) and explode in count without adding structure.
    g_direct = nx.Graph()
    g_direct.add_nodes_from(pages.keys())
    for pair in sig.direct:
        u, v = tuple(pair)
        g_direct.add_edge(u, v)
    # networkx yields (u, v, score) only for pairs sharing ≥1 common neighbor.
    for u, v, score in nx.adamic_adar_index(g_direct):
        if score >= MIN_AA_SCORE:
            sig.adamic_adar[frozenset((u, v))] = float(score)
    return sig


def edge_weight(u: str, v: str, sig: Signals, pages: dict[str, Page]) -> tuple[float, list[str]]:
    """Return (total weight, fired signal names) for a pair."""
    pair = frozenset((u, v))
    weight = 0.0
    fired: list[str] = []
    if pair in sig.direct:
        weight += W_DIRECT_LINK
        fired.append("direct_link")
    if pair in sig.source_overlap:
        weight += W_SOURCE_OVERLAP
        fired.append("source_overlap")
    aa = sig.adamic_adar.get(pair)
    if aa:
        weight += W_ADAMIC_ADAR * aa
        fired.append("adamic_adar")
    # Type affinity is a boost, never edge-creating on its own.
    if fired:
        aff = type_affinity(pages[u].page_type, pages[v].page_type)
        weight += W_TYPE_AFFINITY * aff
        fired.append(f"type_affinity({aff:.1f})")
    return weight, fired


def build_weighted_graph(pages: dict[str, Page], sig: Signals) -> nx.Graph:
    """Assemble the weighted graph from all structural-signal pairs."""
    g = nx.Graph()
    g.add_nodes_from(pages.keys())
    pairs: set[frozenset[str]] = set()
    pairs |= sig.direct
    pairs |= sig.source_overlap
    pairs |= set(sig.adamic_adar.keys())
    for pair in pairs:
        u, v = tuple(pair)
        if u not in pages or v not in pages:
            continue
        w, _ = edge_weight(u, v, sig, pages)
        if w > 0:
            g.add_edge(u, v, weight=round(w, 4))
    return g


# --- Communities & gaps -----------------------------------------------------


@dataclass
class Community:
    cid: int
    nodes: list[str]
    cohesion: float
    hub: str | None


def detect_communities(g: nx.Graph) -> list[Community]:
    if g.number_of_edges() == 0:
        # No links → each node its own singleton; skip Louvain.
        return [Community(cid=i, nodes=[n], cohesion=0.0, hub=n)
                for i, n in enumerate(sorted(g.nodes))]
    partition = louvain_communities(g, weight="weight", seed=LOUVAIN_SEED, resolution=1.0)
    communities: list[Community] = []
    for cid, members in enumerate(sorted(partition, key=len, reverse=True)):
        mset = set(members)
        intra = sum(1 for u, v in g.edges() if u in mset and v in mset)
        inter = sum(1 for u, v in g.edges() if (u in mset) != (v in mset))
        cohesion = intra / (intra + inter) if (intra + inter) else 0.0
        sub = g.subgraph(members)
        hub = max(sub.nodes, key=lambda n: sub.degree(n, weight="weight")) if sub.number_of_nodes() else None
        communities.append(Community(cid=cid, nodes=sorted(members), cohesion=round(cohesion, 4), hub=hub))
    return communities


def find_gaps(g: nx.Graph, pages: dict[str, Page], sig: Signals,
              top_n: int = 5) -> dict[str, list]:
    """Isolated nodes, bridge nodes, and top suggested (non-direct) links."""
    isolated = sorted(n for n in g.nodes if g.degree(n) == 0)
    # Bridge nodes via betweenness centrality (top-N). Exact betweenness is
    # O(VE) — for large graphs use the sampled approximation (fewer sources as
    # the graph grows, since cost scales with edges).
    bridges: list[str] = []
    if g.number_of_edges():
        n_nodes = g.number_of_nodes()
        if n_nodes > 1000:
            k = 500 if n_nodes <= 5000 else 200
            bc = nx.betweenness_centrality(g, weight="weight", k=k, seed=LOUVAIN_SEED)
        else:
            bc = nx.betweenness_centrality(g, weight="weight")
        bridges = [n for n, _ in sorted(bc.items(), key=lambda kv: kv[1], reverse=True)[:top_n] if bc[n] > 0]
    # Suggested missing links: high-weight non-direct pairs.
    suggestions: list[tuple[str, str, float, list[str]]] = []
    for pair in (sig.source_overlap | set(sig.adamic_adar.keys())):
        if pair in sig.direct:
            continue
        u, v = tuple(pair)
        if u not in pages or v not in pages:
            continue
        w, fired = edge_weight(u, v, sig, pages)
        if w > 0:
            suggestions.append((u, v, round(w, 4), fired))
    suggestions.sort(key=lambda x: x[2], reverse=True)
    return {
        "isolated": isolated,
        "bridges": bridges,
        "suggested_links": suggestions[:top_n],
    }


# --- Output writers ---------------------------------------------------------


def _node_payload(nid: str, pages: dict[str, Page]) -> dict:
    p = pages[nid]
    return {"id": nid, "stem": p.stem, "title": p.title, "type": p.page_type, "domain": p.domain}


def write_graph_json(out: Path, g: nx.Graph, pages: dict[str, Page],
                     communities: list[Community], gaps: dict, stats: dict) -> None:
    payload = {
        "stats": stats,
        "nodes": [_node_payload(n, pages) for n in sorted(g.nodes)],
        "edges": [
            {"source": u, "target": v, "weight": d["weight"]}
            for u, v, d in sorted(g.edges(data=True), key=lambda e: -e[2]["weight"])
        ],
        "communities": [
            {"id": c.cid, "nodes": c.nodes, "cohesion": c.cohesion, "hub": c.hub,
             "low_quality": c.cohesion < COHESION_LOW}
            for c in communities
        ],
        "gaps": {
            "isolated": gaps["isolated"],
            "bridges": gaps["bridges"],
            "suggested_links": [
                {"source": u, "target": v, "weight": w, "signals": f}
                for u, v, w, f in gaps["suggested_links"]
            ],
        },
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


_GRAPH_HTML_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]


def write_graph_html(out: Path, g: nx.Graph, pages: dict[str, Page],
                     communities: list[Community], gaps: dict) -> None:
    """Write an interactive D3.js force-directed HTML graph next to graph.json."""
    node_community: dict[str, int] = {}
    for c in communities:
        for n in c.nodes:
            node_community[n] = c.cid

    nodes_js = []
    for nid in sorted(g.nodes):
        p = pages.get(nid)
        title = p.title if p else nid.split("/")[-1]
        cid = node_community.get(nid, -1)
        color = _GRAPH_HTML_COLORS[cid % len(_GRAPH_HTML_COLORS)] if cid >= 0 else "#888"
        nodes_js.append({"id": nid, "label": title[:30], "color": color, "community": cid})

    edges_js = [
        {"source": u, "target": v, "weight": round(d["weight"], 2)}
        for u, v, d in sorted(g.edges(data=True), key=lambda e: -e[2]["weight"])
    ]

    legend_items = []
    for c in communities:
        if c.cohesion >= COHESION_LOW:
            p = pages.get(c.hub)
            hub_label = (p.title if p else c.hub.split("/")[-1])[:35]
            legend_items.append({
                "color": _GRAPH_HTML_COLORS[c.cid % len(_GRAPH_HTML_COLORS)],
                "label": f"C{c.cid}: {hub_label}",
                "size": len(c.nodes),
                "cohesion": round(c.cohesion, 3),
            })

    gaps_html = "".join(
        f'<div class="gap-item">&#9651; {u.split("/")[-1][:22]} &harr; {v.split("/")[-1][:22]}</div>\n'
        for u, v, _w, _f in gaps["suggested_links"][:5]
    )
    legend_html = "".join(
        f'<div class="legend-item" onclick="highlightCommunity(\'{item["color"]}\')"'
        f' style="border-left:3px solid {item["color"]}">'
        f'<div class="legend-dot" style="background:{item["color"]}"></div>'
        f'<div class="legend-text">{item["label"]}'
        f'<br><span class="legend-meta">{item["size"]} pages, cohesion {item["cohesion"]}</span>'
        f'</div></div>\n'
        for item in legend_items
    )

    nodes_json = json.dumps(nodes_js, ensure_ascii=False)
    edges_json = json.dumps(edges_js)
    total_pages = len(pages)
    total_edges = g.number_of_edges()
    total_communities = len(communities)

    html = (
        '<!DOCTYPE html>\n<html lang="zh">\n<head>\n<meta charset="utf-8">\n'
        '<title>Knowledge Graph</title>\n<style>\n'
        'body{margin:0;background:#111;color:#eee;font-family:sans-serif}\n'
        '#header{padding:12px 20px;background:#1a1a2e;border-bottom:1px solid #333;display:flex;align-items:center;gap:16px}\n'
        '#header h2{margin:0;font-size:16px;color:#7eb8f7}\n'
        '#hstats{font-size:12px;color:#aaa}\n'
        '#container{display:flex;height:calc(100vh - 52px)}\n'
        '#sidebar{width:240px;background:#161625;padding:12px;overflow-y:auto;border-right:1px solid #333;flex-shrink:0}\n'
        '#sidebar h3{margin:0 0 8px;font-size:12px;color:#aaa;text-transform:uppercase;letter-spacing:1px}\n'
        '.legend-item{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:11px;cursor:pointer;padding:4px 6px;border-radius:4px}\n'
        '.legend-item:hover{background:#222}\n'
        '.legend-dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}\n'
        '.legend-text{line-height:1.3}\n'
        '.legend-meta{color:#888;font-size:10px}\n'
        '#canvas{flex:1}\n'
        'svg{width:100%;height:100%}\n'
        '.node circle{stroke:#111;stroke-width:1px;cursor:pointer}\n'
        '.node circle:hover{stroke:#fff;stroke-width:2px}\n'
        '.node text{font-size:9px;fill:#ccc;pointer-events:none}\n'
        '.link{stroke:#444;stroke-opacity:0.4}\n'
        '#tooltip{position:fixed;background:#1e1e3a;border:1px solid #555;border-radius:6px;'
        'padding:8px 12px;font-size:12px;pointer-events:none;display:none;max-width:300px;line-height:1.5;z-index:100}\n'
        '#search{width:100%;box-sizing:border-box;background:#222;border:1px solid #444;color:#eee;'
        'padding:6px 8px;border-radius:4px;font-size:12px;margin-bottom:10px}\n'
        '.gap-item{font-size:10px;color:#f9a825;margin:4px 0;padding:3px 0;border-bottom:1px solid #222}\n'
        '</style>\n</head>\n<body>\n'
        '<div id="header">\n'
        '  <h2>&#128375; Knowledge Graph</h2>\n'
        f'  <div id="hstats">{total_pages} pages &nbsp;|&nbsp; {total_edges} edges &nbsp;|&nbsp; {total_communities} communities</div>\n'
        '</div>\n'
        '<div id="container">\n'
        '  <div id="sidebar">\n'
        '    <input id="search" type="text" placeholder="Search nodes..." oninput="filterNodes(this.value)">\n'
        '    <h3>Communities</h3>\n'
        f'    {legend_html}'
        '    <br>\n'
        '    <h3>Knowledge Gaps</h3>\n'
        f'    {gaps_html}'
        '  </div>\n'
        '  <div id="canvas"></div>\n'
        '</div>\n'
        '<div id="tooltip"></div>\n'
        '<script src="https://d3js.org/d3.v7.min.js"></script>\n'
        '<script>\n'
        f'const nodes={nodes_json};\n'
        f'const links={edges_json};\n'
        'const canvasEl=document.getElementById("canvas");\n'
        'const W=canvasEl.clientWidth||1200,H=canvasEl.clientHeight||800;\n'
        'const svg=d3.select("#canvas").append("svg").attr("viewBox",[0,0,W,H])\n'
        '  .call(d3.zoom().on("zoom",e=>gel.attr("transform",e.transform)));\n'
        'const gel=svg.append("g");\n'
        'const deg={};\n'
        'links.forEach(l=>{deg[l.source]=(deg[l.source]||0)+1;deg[l.target]=(deg[l.target]||0)+1;});\n'
        'function nodeR(d){return Math.min(3+Math.sqrt(deg[d.id]||0)*1.2,18);}\n'
        'const sim=d3.forceSimulation(nodes)\n'
        '  .force("link",d3.forceLink(links).id(d=>d.id).distance(30).strength(d=>Math.min(d.weight/15,0.5)))\n'
        '  .force("charge",d3.forceManyBody().strength(-60))\n'
        '  .force("center",d3.forceCenter(W/2,H/2))\n'
        '  .force("collision",d3.forceCollide().radius(d=>nodeR(d)+2));\n'
        'const link=gel.append("g").selectAll("line").data(links).join("line").attr("class","link")\n'
        '  .attr("stroke-width",d=>Math.min(d.weight/8,2));\n'
        'const node=gel.append("g").selectAll("g.node").data(nodes).join("g").attr("class","node")\n'
        '  .call(d3.drag()\n'
        '    .on("start",(e,d)=>{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;})\n'
        '    .on("drag", (e,d)=>{d.fx=e.x;d.fy=e.y;})\n'
        '    .on("end",  (e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));\n'
        'const tt=document.getElementById("tooltip");\n'
        'node.append("circle").attr("r",nodeR).attr("fill",d=>d.color)\n'
        '  .on("mouseover",(e,d)=>{\n'
        '    tt.style.display="block";tt.style.left=(e.clientX+12)+"px";tt.style.top=(e.clientY-20)+"px";\n'
        '    tt.innerHTML="<b>"+d.label+"</b><br><span style=\'color:#aaa;font-size:10px\'>"+d.id+"</span><br>Degree: "+(deg[d.id]||0);\n'
        '  })\n'
        '  .on("mousemove",e=>{tt.style.left=(e.clientX+12)+"px";tt.style.top=(e.clientY-20)+"px";})\n'
        '  .on("mouseout",()=>{tt.style.display="none";});\n'
        'node.filter(d=>(deg[d.id]||0)>15).append("text")\n'
        '  .attr("dy",d=>nodeR(d)+10).attr("text-anchor","middle").text(d=>d.label.substring(0,20));\n'
        'sim.on("tick",()=>{\n'
        '  link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y)\n'
        '      .attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);\n'
        '  node.attr("transform",d=>"translate("+d.x+","+d.y+")");\n'
        '});\n'
        'function filterNodes(q){\n'
        '  const lo=q.toLowerCase();\n'
        '  node.style("opacity",d=>(!lo||d.id.toLowerCase().includes(lo)||d.label.toLowerCase().includes(lo))?1:0.08);\n'
        '  link.style("opacity",()=>lo?0.05:0.4);\n'
        '}\n'
        'function highlightCommunity(color){\n'
        '  node.select("circle").attr("stroke",d=>d.color===color?"#fff":"#111")\n'
        '    .attr("stroke-width",d=>d.color===color?2:1);\n'
        '}\n'
        '</script>\n</body>\n</html>'
    )

    out.write_text(html, encoding="utf-8")


def write_knowledge_gaps(out: Path, gaps: dict, pages: dict[str, Page]) -> None:
    lines: list[str] = ["# Knowledge Gaps", ""]
    lines.append(f"- Isolated pages: **{len(gaps['isolated'])}**")
    lines.append(f"- Bridge pages: **{len(gaps['bridges'])}**")
    lines.append(f"- Suggested missing links: **{len(gaps['suggested_links'])}**")
    lines.append("")
    if gaps["isolated"]:
        lines.append("## Isolated pages (no links)")
        for n in gaps["isolated"][:50]:
            lines.append(f"- `[[{pages[n].stem}]]` — {pages[n].title} ({n})")
        if len(gaps["isolated"]) > 50:
            lines.append(f"- … and {len(gaps['isolated']) - 50} more")
        lines.append("")
    if gaps["bridges"]:
        lines.append("## Bridge pages (high betweenness — fragile if removed)")
        for n in gaps["bridges"]:
            lines.append(f"- `[[{pages[n].stem}]]` — {pages[n].title} ({n})")
        lines.append("")
    if gaps["suggested_links"]:
        lines.append("## Suggested missing links")
        lines.append("High-weight pairs with no direct wikilink yet — consider adding `[[…]]`.")
        lines.append("")
        for u, v, w, fired in gaps["suggested_links"]:
            lines.append(f"- `{pages[u].stem}` ↔ `{pages[v].stem}` (weight {w}; {', '.join(fired)})")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def write_clusters(clusters_dir: Path, communities: list[Community], pages: dict[str, Page]) -> None:
    clusters_dir.mkdir(parents=True, exist_ok=True)
    for c in communities:
        if len(c.nodes) < 2:
            continue
        hub_p = pages[c.hub] if c.hub and c.hub in pages else None
        lines = [
            "---",
            "type: index",
            f"title: \"Cluster {c.cid:03d}\"",
            "domain: graph",
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


# --- Query mode -------------------------------------------------------------


def query_suggestions(pages: dict[str, Page], sig: Signals,
                      slug: str, top_n: int) -> tuple[str | None, list[dict]]:
    """Top weighted, not-already-linked neighbors of the page matching ``slug``."""
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
        w, fired = edge_weight(node, other, sig, pages)
        if w > 0:
            scored.append({"target": other, "stem": pages[other].stem,
                           "title": pages[other].title, "weight": round(w, 4),
                           "signals": fired})
    scored.sort(key=lambda d: d["weight"], reverse=True)
    return node, scored[:top_n]


# --- Main -------------------------------------------------------------------


def _resolve_wiki_root(arg: Path | None) -> Path:
    return arg or Path.cwd()


def run_build(wiki_root: Path, output: Path | None, dry_run: bool) -> int:
    pages = load_pages(wiki_root)
    if not pages:
        print(f"❌ No wiki pages under {wiki_root / 'wiki'}")
        return 1
    sig = compute_signals(pages)
    g = build_weighted_graph(pages, sig)
    communities = detect_communities(g)
    gaps = find_gaps(g, pages, sig)
    low_q = sum(1 for c in communities if c.cohesion < COHESION_LOW)
    stats = {
        "total_pages": len(pages),
        "total_edges": g.number_of_edges(),
        "communities": len(communities),
        "low_quality_communities": low_q,
        "isolated_pages": len(gaps["isolated"]),
    }
    print("🕸️  Graph: Knowledge Graph Builder")
    print(f"  Wiki root: {wiki_root}")
    print(f"  Pages: {stats['total_pages']}")
    print(f"  Edges: {stats['total_edges']}")
    print(f"  Communities: {stats['communities']} ({low_q} low-cohesion)")
    print(f"  Isolated pages: {stats['isolated_pages']}")
    if dry_run:
        print("  (dry-run — no files written)")
        return 0
    runtime = detect_runtime_dir(wiki_root)
    graph_json = output or (runtime / "graph.json")
    write_graph_json(graph_json, g, pages, communities, gaps, stats)
    print(f"📁 Wrote {graph_json}")
    graph_html = graph_json.with_suffix(".html")
    write_graph_html(graph_html, g, pages, communities, gaps)
    print(f"🌐 Wrote {graph_html}")
    wiki_dir = wiki_root / "wiki"
    write_knowledge_gaps(wiki_dir / "knowledge-gaps.md", gaps, pages)
    print(f"📄 Wrote {wiki_dir / 'knowledge-gaps.md'}")
    write_clusters(wiki_dir / "clusters", communities, pages)
    written = sum(1 for c in communities if len(c.nodes) >= 2)
    print(f"📂 Wrote {written} cluster pages to {wiki_dir / 'clusters'}/")
    return 0


def run_query(wiki_root: Path, slug: str, top_n: int) -> int:
    pages = load_pages(wiki_root)
    if not pages:
        print(f"❌ No wiki pages under {wiki_root / 'wiki'}")
        return 1
    sig = compute_signals(pages)
    node, suggestions = query_suggestions(pages, sig, slug, top_n)
    if not node:
        print(f"❌ No page matches slug '{slug}'")
        return 1
    p = pages[node]
    print(f"🔍 Suggestions for `{p.stem}` — {p.title} ({node})")
    if not suggestions:
        print("  (none — page has no structural-signal neighbors beyond existing links)")
        return 0
    for i, s in enumerate(suggestions, 1):
        print(f"  {i}. {s['stem']} — {s['title']} ({s['target']})")
        print(f"     weight {s['weight']} · {', '.join(s['signals'])}")
        print(f"     → add [[{s['stem']}]]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build knowledge graph (NashSU graph-relevance parity)")
    parser.add_argument("--wiki-root", type=Path, help="Wiki project root (default: cwd)")
    parser.add_argument("--output", type=Path, help="graph.json output path (default: <runtime>/graph.json)")
    parser.add_argument("--mode", choices=["build", "query"], default="build",
                        help="build = rebuild graph + outputs; query = per-page suggestions")
    parser.add_argument("--slug", help="Page slug/path for --mode query")
    parser.add_argument("--max-suggestions", type=int, default=5,
                        help="Top-N for query mode and suggested-links (default: 5)")
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
    return run_build(wiki_root, args.output, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
