"""Tests for the NashSU two-graph split in graph.py:

- RETRIEVAL graph (load_pages(include_hidden=True)) keeps query pages, so they
  count as Adamic-Adar common neighbors in calculate_relevance.
- DISPLAY graph (default) excludes query pages from nodes/edges/communities.
- is_structural_graph_node lowercases the stem before the STRUCTURAL_IDS test.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_scripts = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_scripts))

import graph  # noqa: E402


def _write(wiki_dir: Path, stem: str, type_: str, subdir: str, body_links=None):
    d = wiki_dir / subdir
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"See [[{l}]]." for l in (body_links or []))
    (d / f"{stem}.md").write_text(f"---\ntype: {type_}\n---\n\n{body}\n", encoding="utf-8")


def _id(pages, stem):
    return next(nid for nid, p in pages.items() if p.stem == stem)


def test_query_page_contributes_to_retrieval_aa_not_display(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    # a <-> b are linked concepts; query page q links to BOTH a and b.
    _write(wiki_dir, "a", "concept", "concepts", body_links=["b"])
    _write(wiki_dir, "b", "concept", "concepts", body_links=["a"])
    _write(wiki_dir, "q", "query", "queries", body_links=["a", "b"])

    display = graph.load_pages(tmp_path)
    retrieval = graph.load_pages(tmp_path, include_hidden=True)

    # Query page excluded from display, kept for retrieval.
    assert not any(p.stem == "q" for p in display.values())
    assert any(p.stem == "q" for p in retrieval.values())

    a, b = _id(display, "a"), _id(display, "b")
    dlg = graph.build_link_graph(display)
    rlg = graph.build_link_graph(retrieval)

    w_display, _ = graph.calculate_relevance(a, b, display, dlg)
    w_retrieval, _ = graph.calculate_relevance(a, b, retrieval, rlg, rlg)

    # q is a common neighbor of a and b only in the retrieval graph -> adds the
    # Adamic-Adar term 1/log(max(deg(q),2)) * W_COMMON_NEIGHBOR. deg(q)=2.
    assert w_retrieval > w_display
    assert w_retrieval - w_display == pytest.approx(
        graph.W_COMMON_NEIGHBOR / math.log(2), abs=1e-6)


def test_build_weighted_graph_excludes_query_nodes_but_weights_use_retrieval(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    _write(wiki_dir, "a", "concept", "concepts", body_links=["b"])
    _write(wiki_dir, "b", "concept", "concepts", body_links=["a"])
    _write(wiki_dir, "q", "query", "queries", body_links=["a", "b"])

    all_pages = graph.load_pages(tmp_path, include_hidden=True)
    display = {nid: p for nid, p in all_pages.items() if p.page_type not in graph.HIDDEN_TYPES}
    rlg = graph.build_link_graph(all_pages)
    dlg = graph.build_link_graph(display)
    g = graph.build_weighted_graph(display, dlg, rlg)

    # No query node in the display graph...
    assert all(g.nodes[n] is not None for n in g.nodes)
    assert not any(p.stem == "q" for nid, p in display.items() if nid in g.nodes)
    # ...but the a<->b edge weight includes the query-page AA contribution.
    a, b = _id(display, "a"), _id(display, "b")
    w_with_retrieval = g[a][b]["weight"]
    g_display_only = graph.build_weighted_graph(display, dlg)
    assert w_with_retrieval > g_display_only[a][b]["weight"]


def test_is_structural_graph_node_is_case_insensitive(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    _write(wiki_dir, "Index", "concept", "")  # capitalized stem
    pages = graph.load_pages(tmp_path)
    page = next(p for p in pages.values() if p.stem == "Index")
    assert graph.is_structural_graph_node(page) is True
