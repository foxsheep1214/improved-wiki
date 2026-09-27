"""Shared wiki link resolution for structural lint and the graph.

Both tools read a link or ``related:`` entry the same way (``link_target``)
and look it up in the same ``PageIndex``. They differ only where their NashSU
originals differ:

* lint (lint-structural-core.ts normalizeTarget + slugMap): case-insensitive
  path, then basename; a basename several pages share resolves to the last
  page indexed.
* graph (wiki-graph.ts buildTargetIndex/targetAliases): exact id, then the
  aliases lowercase and whitespace -> hyphen, first page wins. NashSU's graph
  ids are file stems; improved-wiki keeps path ids, so a stem several pages
  share resolves only when exactly one of them is not a redirect stub.

Both strip a ``#heading`` anchor (NashSU keeps it and reports the link as
broken).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable, Optional

from _wikilinks import split_wikilink_inner

__all__ = ["link_target", "PageIndex"]


def link_target(raw: str) -> str:
    """The page id a link or related: entry names (case kept).

    Accepts ``x``, ``[[x|alias]]``, ``x#heading``, ``wiki/x.md``. Quotes are
    kept: a quote that leaked into a link is a defect lint reports and fixes.
    """
    t = raw.strip()
    if t.startswith("[[") and t.endswith("]]"):
        t = t[2:-2]
    t = split_wikilink_inner(t)[0].split("#", 1)[0]
    t = t.replace("\\", "/").strip()
    t = re.sub(r"^wiki/", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\.md$", "", t, flags=re.IGNORECASE)
    return t.strip()


def _aliases(value: str) -> tuple[str, ...]:
    lower = value.lower()
    return lower, re.sub(r"\s+", "-", lower)


class PageIndex:
    """Page ids (wiki-relative, no ``.md``) in scan order."""

    def __init__(self, page_ids: Iterable[str]):
        self.ids = list(page_ids)
        self._exact = set(self.ids)
        self._path: dict[str, list[str]] = defaultdict(list)
        self._stem: dict[str, list[str]] = defaultdict(list)
        self._stem_folded: dict[str, list[str]] = defaultdict(list)
        for pid in self.ids:
            stem = pid.rsplit("/", 1)[-1]
            self._path[pid.lower()].append(pid)
            self._stem[stem].append(pid)
            self._stem_folded[stem.lower()].append(pid)

    def lint_resolve(self, raw: str) -> Optional[str]:
        t = link_target(raw).lower()
        if not t:
            return None
        ids = self._path.get(t) or self._stem_folded.get(t.rsplit("/", 1)[-1])
        return ids[-1] if ids else None

    def graph_resolve(self, raw: str, stubs: frozenset[str] = frozenset()) -> Optional[str]:
        t = link_target(raw)
        if not t:
            return None
        if t in self._exact:
            return t
        for alias in _aliases(t):
            if alias in self._path:
                return self._path[alias][0]
        stem = t.rsplit("/", 1)[-1]
        for ids in (self._stem.get(stem), *(self._stem_folded.get(a) for a in _aliases(stem))):
            if ids:
                return self._one(ids, stubs)
        return None

    @staticmethod
    def _one(ids: list[str], stubs: frozenset[str]) -> Optional[str]:
        if len(ids) == 1:
            return ids[0]
        real = [pid for pid in ids if pid not in stubs]
        return real[0] if len(real) == 1 else None
