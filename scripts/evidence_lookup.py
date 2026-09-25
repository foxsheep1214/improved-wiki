#!/usr/bin/env python3
"""evidence_lookup.py — find the anchored evidence behind a wiki page.

Reads the per-source ledgers ingest writes to ``<runtime>/evidence/`` (see
``_evidence_ledger``) and prints the claims (with their "§/Figure/Table"
evidence anchor and confidence), anchored quotes, formulas and verbatim
tables of the page's ``sources:`` — or of any source whose raw path contains
``--source``. ``--grep`` keeps items containing any of the terms
(case-insensitive). Sources ingested before the ledger existed are listed
under ``missing_sources``.

Usage:
  evidence_lookup.py --project <root> --page concepts/x.md --grep 0.2 dB
  evidence_lookup.py --project <root> --source "Wu" --json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _evidence_ledger import evidence_items, load_ledgers  # noqa: E402
from _frontmatter import parse_frontmatter  # noqa: E402
from _paths import detect_runtime_dir  # noqa: E402


def _page_sources(project: Path, page: str) -> list[str]:
    rel = page[len("wiki/"):] if page.startswith("wiki/") else page
    path = project / "wiki" / (rel if rel.endswith(".md") else f"{rel}.md")
    fm, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    sources = fm.get("sources") or []
    return [str(s) for s in (sources if isinstance(sources, list) else [sources])]


def lookup(project: Path, *, page: str = "", source: str = "",
           terms: list[str] | None = None) -> dict:
    records = load_ledgers(detect_runtime_dir(project))
    if page:
        # Match by file name: older pages may carry a differently prefixed
        # path for the same raw file.
        wanted = _page_sources(project, page)
        names = {Path(s).name for s in wanted}
        selected = [r for r in records if Path(r["source"]).name in names]
        found = {Path(r["source"]).name for r in selected}
        missing = [s for s in wanted if Path(s).name not in found]
    else:
        selected = [r for r in records if source.lower() in r["source"].lower()]
        missing = []
    needles = [t.lower() for t in terms or [] if t.strip()]
    matches = []
    for record in selected:
        for item in evidence_items(record):
            haystack = " ".join(str(v) for v in item.values()).lower()
            if needles and not any(n in haystack for n in needles):
                continue
            matches.append({"source": record["source"], **item})
    return {"sources": [r["source"] for r in selected],
            "missing_sources": missing, "matches": matches}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True, help="Wiki project root")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--page", help="Wiki page, e.g. concepts/x.md")
    target.add_argument("--source", help="Substring of the raw source path")
    parser.add_argument("--grep", nargs="+", default=[],
                        help="Keep items containing any of these terms")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = lookup(Path(args.project).expanduser(), page=args.page or "",
                    source=args.source or "", terms=args.grep)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    for item in result["matches"]:
        anchor = item.get("evidence") or item.get("label") or item.get("meaning") or ""
        print(f"[{item['source']} · chunk {item['chunk_index']} · {item['kind']}]"
              f"{' ' + anchor if anchor else ''}")
        print(f"  {item['text'][:600]}\n")
    if result["missing_sources"]:
        print("No evidence ledger (ingested before ledgers existed): "
              + ", ".join(result["missing_sources"]))
    if not result["matches"]:
        print("No matching evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
