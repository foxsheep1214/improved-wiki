"""Tests for graph.py — NashSU graph-view.

In-memory page fixtures (no network). Pages are written to a temp wiki dir so
the real load_pages/build pipeline runs end to end.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_scripts = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_scripts))

import graph  # noqa: E402


# --- fixture helpers --------------------------------------------------------


def _write_page(wiki_dir: Path, stem: str, *, type_: str = "concept",
                title: str = "", sources=None, related=None, body_links=None,
                subdir: str = "") -> None:
    fm_lines = ["---", f"type: {type_}"]
    if title:
        fm_lines.append(f"title: {title}")
    if sources:
        fm_lines.append("sources:")
        for s in sources:
            fm_lines.append(f"  - {s}")
    if related:
        fm_lines.append("related:")
        for r in related:
            fm_lines.append(f"  - {r}")
    fm_lines.append("---")
    body = ""
    if body_links:
        body = "\n".join(f"See [[{l}]]." for l in body_links)
    target_dir = wiki_dir / subdir if subdir else wiki_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{stem}.md").write_text("\n".join(fm_lines) + "\n\n" + body + "\n",
                                            encoding="utf-8")


@pytest.fixture
def wiki(tmp_path):
    """Returns (wiki_root, wiki_dir)."""
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    return tmp_path, wiki_dir


def _build(wiki_root):
    pages = graph.load_pages(wiki_root)
    lg = graph.build_link_graph(pages)
    g = graph.build_weighted_graph(pages, lg)
    for nid in g.nodes:
        g.nodes[nid]["linkCount"] = lg.link_counts.get(nid, 0)
    return pages, lg, g


# --- 1. edges come only from links, not source-overlap / AA -----------------


def test_edges_from_links_only_not_source_overlap(wiki):
    root, wiki_dir = wiki
    # A and B share a source but have NO link between them.
    _write_page(wiki_dir, "a", sources=["paper.pdf"])
    _write_page(wiki_dir, "b", sources=["paper.pdf"])
    pages, lg, g = _build(root)
    assert not g.has_edge("wiki/a", "wiki/b"), "source overlap must not create an edge"
    assert g.number_of_edges() == 0


def test_edges_from_links_only_not_adamic_adar(wiki):
    root, wiki_dir = wiki
    # hub links to a and b; a and b share neighbor hub (AA>0) but no direct link.
    _write_page(wiki_dir, "hub", body_links=["a", "b"])
    _write_page(wiki_dir, "a")
    _write_page(wiki_dir, "b")
    pages, lg, g = _build(root)
    assert g.has_edge("wiki/hub", "wiki/a")
    assert g.has_edge("wiki/hub", "wiki/b")
    assert not g.has_edge("wiki/a", "wiki/b"), "common-neighbor (AA) must not create an edge"


def test_related_frontmatter_creates_edge(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "a", related=["b"])
    _write_page(wiki_dir, "b")
    pages, lg, g = _build(root)
    assert g.has_edge("wiki/a", "wiki/b"), "related: frontmatter is a link source"


# --- 2. weights via calculateRelevance --------------------------------------


def test_reciprocal_link_weight_includes_6(wiki):
    root, wiki_dir = wiki
    # a -> b and b -> a, both type 'source' so type-affinity is small & known.
    _write_page(wiki_dir, "a", type_="source", body_links=["b"])
    _write_page(wiki_dir, "b", type_="source", body_links=["a"])
    pages, lg, g = _build(root)
    w = g["wiki/a"]["wiki/b"]["weight"]
    # reciprocal directLink = (1+1)*3 = 6.0; source-source affinity = 0.5*1.0;
    # no shared sources, no common neighbors.
    assert w == pytest.approx(6.0 + 0.5), w


def test_one_way_link_direct_score_is_3(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "a", type_="source", body_links=["b"])
    _write_page(wiki_dir, "b", type_="source")
    pages, lg, g = _build(root)
    score, _ = graph.calculate_relevance("wiki/a", "wiki/b", pages, lg)
    # one-way directLink = (1+0)*3 = 3.0 ; source-source affinity 0.5
    assert score == pytest.approx(3.0 + 0.5)


def test_source_overlap_is_multiplicative(wiki):
    root, wiki_dir = wiki
    # two shared sources -> sharedSourceCount=2 -> 2*4.0 = 8.0
    _write_page(wiki_dir, "a", type_="source", sources=["p1.pdf", "p2.pdf"], body_links=["b"])
    _write_page(wiki_dir, "b", type_="source", sources=["p1.pdf", "p2.pdf"])
    pages, lg, g = _build(root)
    score, fired = graph.calculate_relevance("wiki/a", "wiki/b", pages, lg)
    # directLink 3.0 + sourceOverlap 2*4=8.0 + affinity(source,source)=0.5
    assert score == pytest.approx(3.0 + 8.0 + 0.5)
    assert "source_overlap" in fired


def test_shared_source_count_helper(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "a", sources=["x", "y", "z"])
    _write_page(wiki_dir, "b", sources=["y", "z", "w"])
    pages = graph.load_pages(root)
    assert graph.shared_source_count("wiki/a", "wiki/b", pages) == 2


def test_adamic_adar_over_in_out_union(wiki):
    root, wiki_dir = wiki
    # c links to a and b; a links to b directly. a and b share neighbor c.
    _write_page(wiki_dir, "c", type_="source", body_links=["a", "b"])
    _write_page(wiki_dir, "a", type_="source", body_links=["b"])
    _write_page(wiki_dir, "b", type_="source")
    pages, lg, g = _build(root)
    score, fired = graph.calculate_relevance("wiki/a", "wiki/b", pages, lg)
    # c degree: out={a,b} (2), in={} -> degree 2 -> 1/log(2)
    aa = 1.0 / math.log(2)
    expected = 3.0 + graph.W_COMMON_NEIGHBOR * aa + 0.5  # direct + AA + affinity
    assert score == pytest.approx(expected)
    assert "adamic_adar" in fired


# --- 3. type affinity matrix matches NashSU verbatim ------------------------


def test_type_affinity_matrix_values():
    # exact values from graph-relevance.ts TYPE_AFFINITY
    assert graph.type_affinity("entity", "concept") == 1.2
    assert graph.type_affinity("entity", "entity") == 0.8
    assert graph.type_affinity("concept", "synthesis") == 1.2
    assert graph.type_affinity("concept", "concept") == 0.8
    assert graph.type_affinity("source", "source") == 0.5
    assert graph.type_affinity("query", "query") == 0.5
    assert graph.type_affinity("synthesis", "synthesis") == 0.8
    assert graph.type_affinity("source", "entity") == 1.0


def test_type_affinity_default_is_half():
    # unlisted source type -> default 0.5
    assert graph.type_affinity("comparison", "concept") == 0.5
    # listed source row, unlisted target -> default 0.5
    assert graph.type_affinity("entity", "thesis") == 0.5


def test_type_affinity_added_unconditionally(wiki):
    root, wiki_dir = wiki
    # linked pair entity->concept: affinity 1.2 present on top of directLink
    _write_page(wiki_dir, "a", type_="entity", body_links=["b"])
    _write_page(wiki_dir, "b", type_="concept")
    pages, lg, g = _build(root)
    score, fired = graph.calculate_relevance("wiki/a", "wiki/b", pages, lg)
    assert score == pytest.approx(3.0 + 1.2)
    assert any(f.startswith("type_affinity") for f in fired)


# --- 4. cohesion = density formula ------------------------------------------


def test_cohesion_density_full_triangle(wiki):
    root, wiki_dir = wiki
    # triangle a-b-c: 3 edges, n=3, possible=3 -> cohesion 1.0
    _write_page(wiki_dir, "a", body_links=["b", "c"])
    _write_page(wiki_dir, "b", body_links=["c"])
    _write_page(wiki_dir, "c")
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    main = max(comms, key=lambda c: len(c.nodes))
    assert main.cohesion == pytest.approx(1.0)


def test_cohesion_density_path(wiki):
    root, wiki_dir = wiki
    # path a-b-c: 2 edges, n=3, possible=3 -> cohesion 2/3
    _write_page(wiki_dir, "a", body_links=["b"])
    _write_page(wiki_dir, "b", body_links=["c"])
    _write_page(wiki_dir, "c")
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    main = max(comms, key=lambda c: len(c.nodes))
    assert main.cohesion == pytest.approx(2.0 / 3.0, abs=1e-4)


def test_top_nodes_by_unweighted_link_count(wiki):
    root, wiki_dir = wiki
    # hub linked by/to many; should be top node of its community.
    _write_page(wiki_dir, "hub", body_links=["a", "b", "c"])
    _write_page(wiki_dir, "a", body_links=["hub"])
    _write_page(wiki_dir, "b")
    _write_page(wiki_dir, "c")
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    main = max(comms, key=lambda c: len(c.nodes))
    assert main.top_nodes[0] == "wiki/hub"


# --- 5. knowledge gaps ------------------------------------------------------


def test_isolated_linkcount_le_1_with_structural_exclusion(wiki):
    root, wiki_dir = wiki
    # lonely: no links (linkCount 0) -> isolated
    _write_page(wiki_dir, "lonely", title="Lonely")
    # index is structural (stem index) -> excluded from isolated even if 0 links
    _write_page(wiki_dir, "index", type_="index", title="Index")
    # overview type excluded
    _write_page(wiki_dir, "ov", type_="overview", title="Overview")
    # log excluded
    _write_page(wiki_dir, "log", type_="index", title="Log")
    # a connected pair so they are not isolated
    _write_page(wiki_dir, "x", body_links=["y"])
    _write_page(wiki_dir, "y", body_links=["x"])
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    gaps = graph.detect_knowledge_gaps(pages, lg, comms)
    iso = next((gp for gp in gaps if gp.gap_type == "isolated-node"), None)
    assert iso is not None
    assert "wiki/lonely" in iso.node_ids
    assert "wiki/index" not in iso.node_ids
    assert "wiki/ov" not in iso.node_ids
    assert "wiki/log" not in iso.node_ids
    # x and y each have linkCount 2 -> not isolated
    assert "wiki/x" not in iso.node_ids


def test_bridge_node_spans_three_communities(wiki):
    root, wiki_dir = wiki
    # Build 3 dense triangles, each its own community, plus a bridge node
    # that links to one member of each triangle.
    for grp in ("p", "q", "r"):
        _write_page(wiki_dir, f"{grp}1", body_links=[f"{grp}2", f"{grp}3"])
        _write_page(wiki_dir, f"{grp}2", body_links=[f"{grp}3"])
        _write_page(wiki_dir, f"{grp}3")
    _write_page(wiki_dir, "bridge", title="Bridge", body_links=["p1", "q1", "r1"])
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    gaps = graph.detect_knowledge_gaps(pages, lg, comms)
    bridges = [gp for gp in gaps if gp.gap_type == "bridge-node"]
    bridge_ids = {nid for gp in bridges for nid in gp.node_ids}
    assert "wiki/bridge" in bridge_ids


def test_no_betweenness_call():
    # betweenness centrality must be gone — verify networkx call is not used.
    src = (_scripts / "graph.py").read_text(encoding="utf-8")
    assert "betweenness_centrality" not in src
    assert "nx.betweenness" not in src


# --- 6. query node exclusion ------------------------------------------------


def test_query_nodes_excluded(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "q", type_="query", title="A Query", body_links=["real"])
    _write_page(wiki_dir, "real", type_="concept")
    pages = graph.load_pages(root)
    assert "wiki/q" not in pages
    assert "wiki/real" in pages


# --- 7. structural filter ---------------------------------------------------


def test_structural_pages_hidden_by_default_filter(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "index", type_="index", body_links=["a"])
    _write_page(wiki_dir, "a", body_links=["index"])
    _write_page(wiki_dir, "schema", type_="other", body_links=["a"])
    pages, lg, g = _build(root)
    filtered = graph.apply_graph_filters(g, pages, lg, hide_structural=True)
    assert "wiki/index" not in filtered.nodes
    assert "wiki/schema" not in filtered.nodes
    assert "wiki/a" in filtered.nodes


def test_include_all_keeps_structural(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "index", type_="index", body_links=["a"])
    _write_page(wiki_dir, "a", body_links=["index"])
    pages, lg, g = _build(root)
    # include_all path = no filter applied -> index present
    assert "wiki/index" in g.nodes


def test_is_structural_graph_node(wiki):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "purpose", type_="other")
    _write_page(wiki_dir, "regular", type_="concept")
    _write_page(wiki_dir, "ov", type_="overview")
    pages = graph.load_pages(root)
    assert graph.is_structural_graph_node(pages["wiki/purpose"])
    assert graph.is_structural_graph_node(pages["wiki/ov"])  # overview type
    assert not graph.is_structural_graph_node(pages["wiki/regular"])


# --- 8. surprising connections present --------------------------------------


def test_find_surprising_connections_present(wiki):
    root, wiki_dir = wiki
    # cross-community edge: two dense triangles bridged by a single weak link
    for grp in ("p", "q"):
        _write_page(wiki_dir, f"{grp}1", type_="concept", body_links=[f"{grp}2", f"{grp}3"])
        _write_page(wiki_dir, f"{grp}2", type_="concept", body_links=[f"{grp}3"])
        _write_page(wiki_dir, f"{grp}3", type_="concept")
    # bridge across communities + across distant types (source<->concept)
    _write_page(wiki_dir, "src", type_="source", body_links=["p1", "q1"])
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    surprising = graph.find_surprising_connections(g, pages, comms)
    assert len(surprising) >= 1
    keys = {(s.source, s.target) for s in surprising} | {(s.target, s.source) for s in surprising}
    assert ("wiki/src", "wiki/p1") in keys or ("wiki/src", "wiki/q1") in keys


def test_surprising_excludes_structural(wiki):
    root, wiki_dir = wiki
    # index bridges two communities but is structural -> excluded
    for grp in ("p", "q"):
        _write_page(wiki_dir, f"{grp}1", body_links=[f"{grp}2", f"{grp}3"])
        _write_page(wiki_dir, f"{grp}2", body_links=[f"{grp}3"])
        _write_page(wiki_dir, f"{grp}3")
    _write_page(wiki_dir, "index", type_="index", body_links=["p1", "q1"])
    pages, lg, g = _build(root)
    comms = graph.detect_communities(g, lg)
    surprising = graph.find_surprising_connections(g, pages, comms)
    for s in surprising:
        assert pages[s.source].stem != "index"
        assert pages[s.target].stem != "index"


# --- 9. end-to-end build smoke ----------------------------------------------


def test_build_writes_outputs(wiki, tmp_path):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "a", body_links=["b"])
    _write_page(wiki_dir, "b", body_links=["a"])
    out = tmp_path / "graph.json"
    rc = graph.run_build(root, out, dry_run=False, include_all=False)
    assert rc == 0
    assert out.exists()
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "surprisingConnections" in data
    assert "gaps" in data
    assert (wiki_dir / "REVIEW" / "knowledge-gaps.md").exists()


# --- write_clusters: stale cluster files cleared (2026-07-12) ----------------


def test_write_clusters_removes_stale_cluster_files(wiki, tmp_path):
    root, wiki_dir = wiki
    _write_page(wiki_dir, "a", body_links=["b"])
    _write_page(wiki_dir, "b", body_links=["a"])
    pages, lg, g = _build(root)
    communities = graph.detect_communities(g, lg)
    clusters_dir = wiki_dir / "clusters"
    clusters_dir.mkdir()
    # A leftover from a previous, larger run — and a non-cluster file that
    # must be preserved (only cluster-NNN.md files are cleared).
    (clusters_dir / "cluster-099.md").write_text("stale\n", encoding="utf-8")
    (clusters_dir / "notes.md").write_text("keep me\n", encoding="utf-8")
    graph.write_clusters(clusters_dir, communities, pages)
    assert not (clusters_dir / "cluster-099.md").exists(), "stale cluster page must be removed"
    assert (clusters_dir / "notes.md").exists(), "non-cluster files must survive"
    written = sorted(p.name for p in clusters_dir.glob("cluster-*.md"))
    assert len(written) == len([c for c in communities if len(c.nodes) >= 2])
