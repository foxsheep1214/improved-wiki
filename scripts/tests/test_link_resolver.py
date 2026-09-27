"""Shared link resolution used by structural lint and graph.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _link_resolver import PageIndex, link_target  # noqa: E402


def test_link_target_reads_every_spelling():
    assert link_target("[[concepts/x|X]]") == "concepts/x"
    assert link_target("wiki/concepts/x.md") == "concepts/x"
    assert link_target("concepts/x#Details") == "concepts/x"
    assert link_target('concepts/x"') == 'concepts/x"'  # leaked quote stays visible


def test_lint_policy_is_case_insensitive_and_last_basename_wins():
    index = PageIndex(["concepts/x", "methodology/x", "concepts/Loop-Gain"])
    assert index.lint_resolve("CONCEPTS/X") == "concepts/x"
    assert index.lint_resolve("x") == "methodology/x"
    assert index.lint_resolve("loop-gain") == "concepts/Loop-Gain"
    assert index.lint_resolve("Loop gain") is None  # NashSU lint: no space->hyphen
    assert index.lint_resolve("concepts/x#sec") == "concepts/x"


def test_graph_policy_uses_nashsu_aliases_and_skips_stubs():
    index = PageIndex(["concepts/loop-gain", "concepts/x", "methodology/x",
                       "concepts/y", "methodology/y"])
    assert index.graph_resolve("Loop gain") == "concepts/loop-gain"
    assert index.graph_resolve("x") is None  # two real pages share the stem
    assert index.graph_resolve("y", frozenset({"concepts/y"})) == "methodology/y"
    assert index.graph_resolve("concepts/x") == "concepts/x"
