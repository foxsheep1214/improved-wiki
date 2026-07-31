#!/usr/bin/env python3
"""QC gate for Stage 2.2 chunk-analysis responses — detects malformed/placeholder analysis.

Generalized from the ad-hoc script that caught the Skolnik incident (2026-07-07):
a driving sub-agent chained past the L4 cap (delegate-mode.md, max 2 handoffs per
agent) without ever exiting, context accumulated past its practical ceiling, and
Stage 2.2 responses degraded into placeholder concepts (e.g. "Radar Handbook
Content" instead of real topic names). Run this after every Stage 2.2 response —
ideally before deciding whether to chain the next handoff or hand back to the
parent — to catch degradation at the cheapest point, before it propagates into
Stage 2.4's generated pages.

Checks: required structural fields, placeholder candidate names, and every
claim carrying a non-empty evidence anchor. There is deliberately no response
size, concept-count, claim-count, or source-quote quota: NashSU asks for key
items and says to be thorough but concise, so an honestly sparse chunk is valid.
A flagged chunk means delete the .txt and re-dispatch, never a pipeline abort.

Usage:
    python3 scripts/qc_stage22.py                       # scans IMPROVED_WIKI_ROOT (or cwd)
    python3 scripts/qc_stage22.py --conv e1aa860d       # only this book's conversation dir
    python3 scripts/qc_stage22.py --file /path/to/Stage-2-2-Chunk-1-abcd1234.txt
    IMPROVED_WIKI_ROOT=/path/to/project python3 scripts/qc_stage22.py

Use --file for the mandatory per-handoff gate: a conversation directory can
contain superseded responses from earlier prompt hashes, so --conv is useful
for an audit but can still drown the current response in stale failures.
Without either option the scan crosses every book ever ingested.
"""
import argparse
import os
import re
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir

PLACEHOLDER = re.compile(
    r"(?i)chunk \d|handbook content|reference material|technical content|"
    r"book content|comprehensive.*content"
)
CLAIM_LINE = re.compile(r"^\s*-\s*claim:", re.MULTILINE)
# A non-empty evidence value: optional quote, then a real character. Matches
# entries in both `claims:` and the digest's `key_claims:` — claim lines are
# counted from the same sections, so coverage compares like with like.
EVIDENCE_LINE = re.compile(r"^\s*evidence:\s*[\"']?[^\"'\s]", re.MULTILINE)
REQUIRED_TOP_LEVEL = (
    "entities_found",
    "concepts_found",
    "claims",
    "updated_global_digest",
)


def _chunk_num(p: Path):
    """Chunk number from a Stage-2-2-Chunk-*.txt filename, or None when the
    glob matched but the number part isn't numeric (e.g. a stray
    Stage-2-2-Chunk-copy.txt). Callers must tolerate None instead of crashing
    the whole QC scan on one oddly-named file."""
    m = re.search(r"Chunk-(\d+)", p.name)
    return int(m.group(1)) if m else None


def _indented_yaml_block(text: str, key: str) -> str:
    """Return an indented top-level YAML field body, preserving blank lines."""
    lines = text.splitlines()
    start = None
    key_line = re.compile(rf"^{re.escape(key)}:\s*(?:\[\])?\s*$")
    for index, line in enumerate(lines):
        if key_line.fullmatch(line):
            start = index + 1
            if line.rstrip().endswith("[]"):
                return ""
            break
    if start is None:
        return ""

    body = []
    for line in lines[start:]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def check(txt_file: Path) -> tuple[bool, str]:
    text = txt_file.read_text(encoding="utf-8", errors="replace")
    size = len(text)
    missing = [
        key for key in REQUIRED_TOP_LEVEL
        if not re.search(rf"^{re.escape(key)}\s*:", text, re.MULTILINE)
    ]
    if missing:
        return False, f"missing top-level field(s): {', '.join(missing)}"

    concepts_body = _indented_yaml_block(text, "concepts_found")
    concepts = re.findall(
        r"^\s*-\s*name:\s*[\"']?(.+?)[\"']?\s*$",
        concepts_body,
        re.MULTILINE,
    )
    entities_body = _indented_yaml_block(text, "entities_found")
    entities = re.findall(
        r"^\s*-\s*name:\s*[\"']?(.+?)[\"']?\s*$",
        entities_body,
        re.MULTILINE,
    )
    typed_body = _indented_yaml_block(text, "schema_typed_candidates")
    typed = re.findall(
        r"^\s+(?:-\s*)?name:\s*[\"']?(.+?)[\"']?\s*$",
        typed_body,
        re.MULTILINE,
    )
    placeholders = [
        name for name in concepts + entities + typed
        if PLACEHOLDER.search(name)
    ]
    if placeholders:
        return False, f"placeholder names: {placeholders[:3]}"
    n_claims = len(CLAIM_LINE.findall(text))
    n_evidence = len(EVIDENCE_LINE.findall(text))
    if n_evidence < n_claims:
        return False, (f"only {n_evidence}/{n_claims} claims carry a non-empty "
                       f"evidence anchor")
    return True, (
        f"OK ({len(concepts)} key concepts, {len(entities)} key entities, "
        f"{len(typed)} schema-typed candidates, {n_claims} claims, {size} bytes)"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--conv", default="",
                       help="scan this conversation prefix (e.g. e1aa860d), "
                            "including superseded responses; default: all books")
    scope.add_argument("--file", type=Path,
                       help="check exactly one Stage 2.2 response file "
                            "(recommended before re-invoking ingest.py)")
    args = ap.parse_args()

    project_root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    runtime = detect_runtime_dir(project_root)
    conv_root = runtime / "conversation"
    if args.file:
        target = args.file if args.file.is_absolute() else project_root / args.file
        if not target.is_file():
            print(f"Response file not found: {target}", file=sys.stderr)
            return 2
        if _chunk_num(target) is None:
            print(f"Not a Stage 2.2 chunk response: {target}", file=sys.stderr)
            return 2
        ok, msg = check(target)
        status = "✓" if ok else "✗"
        print(f"{target}: {status} {msg}")
        if not ok:
            print("Bad chunk (delete to force redo):")
            print(f"  rm {target}")
        return 0 if ok else 1

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
