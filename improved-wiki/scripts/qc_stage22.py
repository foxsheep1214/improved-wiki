#!/usr/bin/env python3
"""QC gate for Stage 2.2 chunk-analysis responses — detects placeholder/thin analysis.

Generalized from the ad-hoc script that caught the Skolnik incident (2026-07-07):
a driving sub-agent chained past the L4 cap (delegate-mode.md, max 2 handoffs per
agent) without ever exiting, context accumulated past its practical ceiling, and
Stage 2.2 responses degraded into placeholder concepts (e.g. "Radar Handbook
Content" instead of real topic names). Run this after every Stage 2.2 response —
ideally before deciding whether to chain the next handoff or hand back to the
parent — to catch degradation at the cheapest point, before it propagates into
Stage 2.4's generated pages.

Checks: response size, real-concept count, placeholder names, source_quotes
present + non-empty, and every claim carrying a non-empty evidence anchor
(the last two migrated from the C1 hard gate removed 2026-07-08 — advisory
here: a flagged chunk means delete the .txt and re-dispatch, never a
pipeline abort).

Usage:
    python3 scripts/qc_stage22.py                       # scans IMPROVED_WIKI_ROOT (or cwd)
    python3 scripts/qc_stage22.py --conv e1aa860d       # only this book's conversation dir
    IMPROVED_WIKI_ROOT=/path/to/project python3 scripts/qc_stage22.py

--conv scopes the scan to one conversation prefix (the current book). Without
it the scan crosses every book ever ingested and drowns the signal in stale
historical answers (observed live 2026-07-09: hundreds of flags from
already-superseded runs).
"""
import argparse
import os
import re
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir

MIN_CONCEPTS = 5
MIN_BYTES = 3000
PLACEHOLDER = re.compile(
    r"(?i)chunk \d|handbook content|reference material|technical content|"
    r"book content|comprehensive.*content"
)
# source_quotes / evidence coverage (migrated from the removed C1 hard gate,
# 2026-07-08 d28ae85 — advisory here, never a pipeline abort). The block-scalar
# body is the indented lines following "source_quotes: |".
SOURCE_QUOTES_BLOCK = re.compile(
    r"^source_quotes:\s*\|[^\n]*\n((?:[ \t]+\S[^\n]*\n?)*)", re.MULTILINE)
CLAIM_LINE = re.compile(r"^\s*-\s*claim:", re.MULTILINE)
# A non-empty evidence value: optional quote, then a real character. Matches
# entries in both `claims:` and the digest's `key_claims:` — claim lines are
# counted from the same sections, so coverage compares like with like.
EVIDENCE_LINE = re.compile(r"^\s*evidence:\s*[\"']?[^\"'\s]", re.MULTILINE)


def _chunk_num(p: Path):
    """Chunk number from a Stage-2-2-Chunk-*.txt filename, or None when the
    glob matched but the number part isn't numeric (e.g. a stray
    Stage-2-2-Chunk-copy.txt). Callers must tolerate None instead of crashing
    the whole QC scan on one oddly-named file."""
    m = re.search(r"Chunk-(\d+)", p.name)
    return int(m.group(1)) if m else None


def check(txt_file: Path) -> tuple[bool, str]:
    text = txt_file.read_text(encoding="utf-8", errors="replace")
    size = len(text)
    concepts = re.findall(r"^\s*-\s*name:\s*[\"']?(.+?)[\"']?\s*$", text, re.MULTILINE)
    placeholders = [c for c in concepts if PLACEHOLDER.search(c)]
    if size < MIN_BYTES:
        return False, f"size {size} < {MIN_BYTES}"
    if len(concepts) < MIN_CONCEPTS:
        return False, f"only {len(concepts)} concepts (< {MIN_CONCEPTS})"
    if placeholders:
        return False, f"placeholder names: {placeholders[:3]}"
    quotes = SOURCE_QUOTES_BLOCK.search(text)
    if not quotes or not quotes.group(1).strip():
        return False, ("source_quotes missing or empty (2-3 verbatim sentences "
                       "with section/equation anchors required)")
    n_claims = len(CLAIM_LINE.findall(text))
    n_evidence = len(EVIDENCE_LINE.findall(text))
    if n_evidence < n_claims:
        return False, (f"only {n_evidence}/{n_claims} claims carry a non-empty "
                       f"evidence anchor")
    return True, f"OK ({len(concepts)} concepts, {n_claims} claims, {size} bytes)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conv", default="",
                    help="only scan this conversation prefix (e.g. e1aa860d); "
                         "default: all books")
    args = ap.parse_args()

    project_root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    runtime = detect_runtime_dir(project_root)
    conv_root = runtime / "conversation"
    if not conv_root.is_dir():
        print(f"No conversation dir at {conv_root}")
        return 0

    bad = []
    total = 0
    for conv_dir in sorted(conv_root.iterdir()):
        if args.conv and conv_dir.name != args.conv:
            continue
        # Tolerant sort key: files whose chunk number can't be parsed sort
        # last (by name) with a warning, instead of AttributeError-ing the run.
        candidates = sorted(
            conv_dir.glob("Stage-2-2-Chunk-*.txt"),
            key=lambda p: (_chunk_num(p) is None, _chunk_num(p) or 0, p.name),
        )
        targets = []
        for p in candidates:
            if _chunk_num(p) is None:
                print(f"  ⚠ skipping {p.name}: no numeric chunk index in name",
                      file=sys.stderr)
                continue
            targets.append(p)
        if not targets:
            continue
        print(f"=== {conv_dir.name} ===")
        for f in targets:
            total += 1
            n = _chunk_num(f)
            ok, msg = check(f)
            status = "✓" if ok else "✗"
            print(f"  chunk {n}: {status} {msg}")
            if not ok:
                bad.append((conv_dir.name, f, msg))

    print(f"\n{total} responses, {len(bad)} bad")
    if bad:
        print("Bad chunks (delete to force redo):")
        for conv_name, f, msg in bad:
            print(f"  rm {f}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
