#!/usr/bin/env python3
"""cross_source_dedup.py — 跨源去重 (cross-source dedup): lint-time, whole-wiki.

Runs OFFLINE (user-invoked, not during ingest) across the ENTIRE wiki to merge
duplicates that accumulated across multiple ingests. Distinct from Stage 2.5
源内去重 (intra-source dedup, `_stage_2_5_dedup.py`) which is a conservative
inline filter on one source's blocks before write. This module is thorough:
two-phase, backs up, writes a report, and rewrites all `[[wikilinks]]` +
`related:` across the wiki so merges leave no broken links.

Phase 1 (deterministic, no LLM): merge pages sharing the same ``title:``
frontmatter — variant slugs (-zh, macOS " 2", case, parens). Seconds, no API
key.  (``_dedup_merge.run``)

Phase 2 (LLM semantic): detect same-topic different-name slugs (synonyms,
EN/中文, singular/plural, abbrev/full) via NashSU's ``_dedup`` engine, then
LLM body-merge each group.

LLM path (phase 2): **conversation-mode only** (``make_conversation_llm_call``) —
the same prompt-file handoff primitive ingest.py uses, so the calling agent's
model does the work. No direct HTTP API / ``LLM_API_KEY`` path (round v,
2026-06-23): text generation is conversation-mode everywhere, matching ingest.

Dedup is NOT run after ingest — it is a standalone lint-command action
(``wiki-lint.sh --dedup``). When invoked, it auto-applies (deletes files);
pass ``--dry-run`` to preview.

Usage:
  python3 cross_source_dedup.py                          # phase 1, auto-apply
  python3 cross_source_dedup.py --semantic               # phase 1 + phase 2 (LLM)
  python3 cross_source_dedup.py --dry-run                # preview only, no writes
  python3 cross_source_dedup.py --deterministic-only     # skip phase 2
  python3 cross_source_dedup.py --project /path/to/wiki
  python3 cross_source_dedup.py --whitelist whitelist.json

Exit codes: 0 done; 101 conversation pending (phase 2 only); 2 config error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import _dedup  # noqa: E402
import _dedup_merge  # noqa: E402
from _core import ConversationPending  # noqa: E402
from _paths import detect_runtime_dir  # noqa: E402
from _llm_call import make_conversation_llm_call  # noqa: E402

ANCHOR_FILES = {"index.md", "log.md", "overview.md"}
STATE_FILES = {
    "lint-cache.json", "lint.json", "ingest-cache.json", "ingest-queue.json",
    "ingest-lock", "lint-lock", "lint-semantic.json", "dedup-report.json",
    "dedup-whitelist.json", "review.json", "review-suggestions.json",
    "embed-cache.json",
}
SKIP_DIRS = {"lint", "REVIEW", "media"}


# ── LLM call: conversation-mode only ───────────────────────────────────────

def make_llm_call(project_root: Path):
    """Return (callable, runtime). Always conversation-mode — the calling
    agent's model does the work via the shared prompt-file handoff."""
    runtime = detect_runtime_dir(project_root)
    conv = make_conversation_llm_call(runtime, stage_prefix="dedup")
    print("[dedup] LLM path: conversation-mode (calling agent's model)")
    return conv, runtime


# ── phase 2: LLM semantic dedup (existing _dedup engine) ───────────────────

def collect_wiki_pages(wiki_dir: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not wiki_dir.is_dir():
        return out
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki_dir)
        if rel.name in ANCHOR_FILES or rel.name in STATE_FILES:
            continue
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append((f"wiki/{rel}", content))
    return out


def load_whitelist(*paths: Path) -> list[list[str]]:
    pairs: list[list[str]] = []
    for p in paths:
        if not p or not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        raw = data.get("not_duplicates", data) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            continue
        for pair in raw:
            if isinstance(pair, list) and len(pair) >= 2:
                pairs.append([str(x) for x in pair[:2]])
    return pairs


def _slug_from_path(project_relative: str) -> str:
    return os.path.splitext(project_relative.split("/")[-1])[0]


def run_phase2(project_root, llm_call, *, apply=True, whitelist_pairs=None, today=None) -> dict:
    wiki_dir = project_root / "wiki"
    runtime = detect_runtime_dir(project_root)
    pages = collect_wiki_pages(wiki_dir)
    summaries = [s for s in (_dedup.extract_entity_summary(p, c) for p, c in pages) if s is not None]
    if len(summaries) < 2:
        print("[dedup] phase 2: fewer than 2 summarizable pages; skipping.")
        return {"groups": 0, "applied": []}

    not_duplicates = list(whitelist_pairs or [])
    not_duplicates += load_whitelist(runtime / "dedup-whitelist.json")

    print(f"[dedup] phase 2: scanning {len(summaries)} pages for semantic duplicates ...")
    groups = _dedup.detect_duplicate_groups(summaries, llm_call, not_duplicates=not_duplicates)
    print(f"[dedup] phase 2: detected {len(groups)} duplicate group(s).")
    for i, g in enumerate(groups, 1):
        print(f"  group {i}: {g['slugs']}  ({g['confidence']}) — {g['reason']}")

    applied: list[dict] = []
    if apply and groups:
        backup_dir = runtime / f"dedup-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        pages_by_slug = {_slug_from_path(p): (p, c) for p, c in pages}
        for g in groups:
            canonical_slug = g["slugs"][0]
            group_pages = []
            for slug in g["slugs"]:
                entry = pages_by_slug.get(slug)
                if entry is None:
                    group_pages = []
                    break
                path, content = entry
                group_pages.append({"slug": slug, "path": path, "content": content})
            if len(group_pages) < 2:
                continue
            other_pages = [{"path": p, "content": c} for p, c in pages
                           if _slug_from_path(p) not in {gp["slug"] for gp in group_pages}]
            result = _dedup.merge_duplicate_group(
                group_pages, canonical_slug, other_pages, llm_call, today=today)
            _persist_merge(project_root, result, backup_dir)
            removed = {_slug_from_path(p) for p in result.pages_to_delete}
            applied.append({"canonical": canonical_slug, "merged_away": sorted(removed),
                            "rewrites": [r["path"] for r in result.rewrites]})
            print(f"[dedup] phase 2: merged → {canonical_slug} "
                  f"(removed {sorted(removed)}, {len(result.rewrites)} rewrite(s))")

    _write_report(runtime / "dedup-report.json", {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "apply": apply, "phase2": {"groups": groups, "applied": applied}})
    return {"groups": groups, "applied": applied}


def _persist_merge(project_root, result, backup_dir) -> None:
    for b in result.backup:
        bpath = backup_dir / b["path"]
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text(b["content"], encoding="utf-8")
    canon = project_root / result.canonical_path
    canon.parent.mkdir(parents=True, exist_ok=True)
    canon.write_text(result.canonical_content, encoding="utf-8")
    for r in result.rewrites:
        (project_root / r["path"]).write_text(r["new_content"], encoding="utf-8")
    for p in result.pages_to_delete:
        dpath = project_root / p
        if dpath.exists():
            dpath.unlink()
    removed_slugs = {_slug_from_path(p) for p in result.pages_to_delete}
    index_path = project_root / "wiki" / "index.md"
    if index_path.exists() and removed_slugs:
        idx = index_path.read_text(encoding="utf-8")
        pruned = _dedup.rewrite_index_md(idx, removed_slugs)
        if pruned != idx:
            index_path.write_text(pruned, encoding="utf-8")


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Two-phase dedup. Auto-applies by default.")
    parser.add_argument("--project", default=None,
                        help="Wiki project root (default: IMPROVED_WIKI_ROOT or cwd)")
    parser.add_argument("--semantic", action="store_true", help="Also run phase 2 (LLM semantic)")
    parser.add_argument("--deterministic-only", action="store_true",
                        help="Only run phase 1 (skip phase 2 even if --semantic)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no writes")
    parser.add_argument("--whitelist", action="append", default=[])
    args = parser.parse_args(argv)

    project_root = Path(args.project or os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    if not (project_root / "wiki").is_dir():
        print(f"ERROR: wiki/ not found under {project_root}", file=sys.stderr)
        return 2

    apply = not args.dry_run

    print(f"[dedup] phase 1: deterministic title-collision merge ({'APPLY' if apply else 'DRY-RUN'})")
    r1 = _dedup_merge.run(project_root, apply=apply)
    print(f"[dedup] phase 1: {r1['groups']} groups, {r1['redundant']} redundant")
    if apply:
        print(f"[dedup] phase 1: deleted {r1['deleted']}, rewrote {r1['rewrites']} refs "
              f"across {r1['files_touched']} files")
    elif r1["groups"]:
        for s in r1["samples"][:15]:
            print(f"  keep {s['keep']:55s} ← {', '.join(s['merge'])}")

    if args.semantic and not args.deterministic_only:
        llm_call, _ = make_llm_call(project_root)
        whitelist_pairs = load_whitelist(*[Path(p) for p in args.whitelist])
        try:
            run_phase2(project_root, llm_call, apply=apply,
                       whitelist_pairs=whitelist_pairs,
                       today=lambda: date.today().isoformat())
        except ConversationPending:
            print("[dedup] phase 2: conversation handoff — answer prompt under "
                  "<runtime>/conversation/dedup/ and re-invoke.", file=sys.stderr)
            return 101

    print("[dedup] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
