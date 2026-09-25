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
import re
from pathlib import Path
import sys
from types import SimpleNamespace

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _evidence_ledger import evidence_items, load_ledgers  # noqa: E402
from _paths import detect_runtime_dir  # noqa: E402
from _frontmatter_array import parse_frontmatter_array
from _ingest_events import load_ingest_events
from _progress import file_sha256
from _source_identity import SourceResolver, raw_source_refs, normalize_source_ref, AmbiguousSourceError


def _page_sources(project: Path, page: str) -> list[str]:
    rel = page[len("wiki/"):] if page.startswith("wiki/") else page
    path = project / "wiki" / (rel if rel.endswith(".md") else f"{rel}.md")
    return parse_frontmatter_array(path.read_text(encoding='utf-8'), 'sources')


def _versioned_records(project: Path, runtime: Path) -> list[dict]:
    events = load_ingest_events(SimpleNamespace(runtime_dir=runtime))
    runs = {e['run_id']: e for e in events if e['event'] == 'ingest_completed'}
    records = []
    for original in load_ledgers(runtime):
        record = dict(original)
        sha, run_id = record.get('source_hash', ''), record.get('run_id', '')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError(f"Evidence has invalid source_hash: {record['source']}")
        if not isinstance(run_id, str):
            raise ValueError(f"Evidence has invalid run_id: {record['source']}")
        event = runs.get(run_id)
        source = normalize_source_ref(record['source'], project)
        record['source_at_evidence'] = record['source']
        if (event and event['source_hash'] == sha
                and source in {event['source'], event.get('source_at_event')}):
            source = event['source']
        else:
            event = None
        record['source'] = source
        record['run_id'] = run_id
        record['completed_at'] = event.get('completed_at') if event else None
        records.append(record)
    return records


def _classify_records(records: list[dict], project: Path, runtime: Path) -> None:
    # Hash only the requested sources, once per file, not the entire library.
    hashes, stages = {}, {}
    for record in records:
        source, sha, run_id = record['source'], record['source_hash'], record['run_id']
        path = (project / source).resolve()
        if not path.is_relative_to(project.resolve()):
            raise ValueError(f'Source escapes project: {source}')
        if source not in hashes:
            hashes[source] = file_sha256(path) if path.is_file() else None
        if sha not in stages:
            state_path = runtime / 'ingest-progress' / f'{sha[:16]}.stages.json'
            stages[sha] = (json.loads(state_path.read_text(encoding='utf-8'))
                           if state_path.exists() else {})
        state = stages[sha]
        marker_run = (state.get('ingested__payload') or {}).get('run_id')
        status = 'uncompleted'
        if hashes[source] is None:
            status = 'missing_source'
        elif hashes[source] != sha:
            status = 'stale'
        elif record['completed_at'] and state.get('ingested') and marker_run == run_id:
            status = 'current'
        elif record['completed_at']:
            status = 'historical'
        record['evidence_status'] = status



def lookup(project: Path, *, page: str = "", source: str = "",
           terms: list[str] | None = None, history: bool = False) -> dict:
    runtime = detect_runtime_dir(project)
    records = _versioned_records(project, runtime)
    ambiguous = {}
    if page:
        wanted = _page_sources(project, page)
        resolver = SourceResolver(project, raw_source_refs(project) |
                                  {r['source'] for r in records})
        resolved = {}
        for ref in wanted:
            try:
                resolved[ref] = resolver.resolve(ref)
            except AmbiguousSourceError as exc:
                resolved[ref] = None
                ambiguous[ref] = str(exc)
        selected = [r for r in records if r['source'] in resolved.values()]
    else:
        selected = [r for r in records if source.lower() in r["source"].lower()]
    _classify_records(selected, project, runtime)
    unavailable = [{'source': r['source'], 'source_hash': r['source_hash'],
                    'run_id': r['run_id'], 'evidence_status': r['evidence_status']}
                   for r in selected if r['evidence_status'] != 'current']
    if not history:
        selected = [r for r in selected if r['evidence_status'] == 'current']
    found = {r['source'] for r in selected}
    missing = [s for s in wanted if resolved[s] not in found] if page else []
    needles = [t.lower() for t in terms or [] if t.strip()]
    matches = []
    for record in selected:
        for item in evidence_items(record):
            haystack = " ".join(str(v) for v in item.values()).lower()
            if needles and not any(n in haystack for n in needles):
                continue
            matches.append({k: record.get(k) for k in (
                'source', 'source_at_evidence', 'source_hash', 'run_id',
                'completed_at', 'evidence_status')} | item)
    return {"sources": sorted(found), "missing_sources": missing,
            "ambiguous_sources": ambiguous, "unavailable_sources": unavailable,
            "matches": matches}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True, help="Wiki project root")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--page", help="Wiki page, e.g. concepts/x.md")
    target.add_argument("--source", help="Substring of the raw source path")
    parser.add_argument("--grep", nargs="+", default=[],
                        help="Keep items containing any of these terms")
    parser.add_argument("--json", action="store_true")
    parser.add_argument('--history', action='store_true',
                        help='Include old/uncompleted evidence with explicit version status')
    args = parser.parse_args(argv)

    try:
        result = lookup(Path(args.project).expanduser(), page=args.page or "",
                        source=args.source or "", terms=args.grep, history=args.history)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    for item in result["matches"]:
        anchor = item.get("evidence") or item.get("label") or item.get("meaning") or ""
        print(f"[{item['source']} · {item['evidence_status']} · "
              f"hash {item['source_hash']} · run {item['run_id'] or 'unknown'} · "
              f"chunk {item['chunk_index']} · {item['kind']}]"
              f"{' ' + anchor if anchor else ''}")
        print(f"  {item['text'][:600]}\n")
    if result["missing_sources"]:
        print("No matching evidence in the requested version scope: "
              + ", ".join(result["missing_sources"]))
    for reason in result['ambiguous_sources'].values():
        print(reason)
    if not result["matches"]:
        print("No matching evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
