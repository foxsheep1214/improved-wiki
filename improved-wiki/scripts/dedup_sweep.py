#!/usr/bin/env python3
"""dedup_sweep.py — runnable entrypoint for the semantic dedup subsystem.

Wires `_dedup` (port of NashSU dedup.ts) into a real wiki/ sweep:

  1. Walk wiki/ content pages, build EntitySummary for each (skips pages
     with no frontmatter).
  2. LLM-driven detect_duplicate_groups (same-topic, different-name slugs:
     EN/中文, singular/plural, abbrev/full, synonyms). Not-duplicates
     whitelist honored.
  3. DRY-RUN (default): print candidate groups + write dedup-report.json.
     No writes.
  4. --apply: for each group, merge into the canonical slug — LLM body
     merge + deterministic frontmatter union + cross-ref rewrite — then
     backup every touched file, write canonical + rewrites, delete the
     merged-away pages, and prune index.md.

The LLM callable comes from `_llm_call.make_llm_callable` (env +
~/.agents/config.json). All merge I/O is reversible via the backup dir.

Usage:
  python3 dedup_sweep.py                            # dry-run: report only
  python3 dedup_sweep.py --apply                    # execute merges
  python3 dedup_sweep.py --project /path/to/wiki    # override root
  python3 dedup_sweep.py --whitelist whitelist.json # extra not-duplicates
  python3 dedup_sweep.py --max-tokens 8192

Env (for --apply / detect): LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
  LLM_PROTOCOL — or ~/.agents/config.json.
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
from _paths import detect_runtime_dir  # noqa: E402

# Files / dirs excluded from dedup scanning (anchors, state, self-output).
ANCHOR_FILES = {"index.md", "log.md", "overview.md"}
STATE_FILES = {
    "lint-cache.json", "lint.json", "ingest-cache.json", "ingest-queue.json",
    "ingest-lock", "lint-lock", "lint-semantic.json", "dedup-report.json",
    "dedup-whitelist.json", "review.json", "review-suggestions.json",
    "embed-cache.json",
}
SKIP_DIRS = {"lint", "REVIEW", "media"}


def collect_wiki_pages(wiki_dir: Path) -> list[tuple[str, str]]:
    """Return [(project_relative_path, content), ...] for every content page.

    project_relative_path includes the ``wiki/`` prefix (e.g.
    "wiki/entities/foo.md") so it can be joined to PROJECT_ROOT for I/O.
    Excludes anchors, state files, and lint/REVIEW/media dirs.
    """
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


def build_summaries(pages: list[tuple[str, str]]) -> list[_dedup.EntitySummary]:
    """Build EntitySummary for each page that has frontmatter."""
    summaries: list[_dedup.EntitySummary] = []
    for path, content in pages:
        s = _dedup.extract_entity_summary(path, content)
        if s is not None:
            summaries.append(s)
    return summaries


def load_whitelist(*paths: Path) -> list[list[str]]:
    """Load not-duplicates pairs from one or more JSON files.

    Each file: {"not_duplicates": [["a", "b"], ...]} or a bare list.
    """
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
    base = project_relative.split("/")[-1]
    return os.path.splitext(base)[0]


def run_sweep(
    project_root: Path,
    llm_call: Callable[[str, str], str],
    *,
    apply: bool = False,
    whitelist_pairs: list[list[str]] | None = None,
    today=None,
) -> dict:
    """Core sweep. Returns the report dict. Performs I/O only when apply=True.

    today: callable () -> str (ISO date) or a str; defaults to today.
    """
    wiki_dir = project_root / "wiki"
    runtime = detect_runtime_dir(project_root)
    pages = collect_wiki_pages(wiki_dir)
    summaries = build_summaries(pages)

    if len(summaries) < 2:
        print(f"[dedup] Fewer than 2 summarizable pages ({len(summaries)}); nothing to scan.")
        report = {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "apply": apply,
            "pagesScanned": len(summaries),
            "groups": [],
            "applied": [],
        }
        _write_report(runtime / "dedup-report.json", report)
        return report

    not_duplicates = list(whitelist_pairs or [])
    not_duplicates += load_whitelist(runtime / "dedup-whitelist.json")

    print(f"[dedup] Scanning {len(summaries)} pages for duplicate groups ...")
    groups = _dedup.detect_duplicate_groups(
        summaries, llm_call, not_duplicates=not_duplicates
    )

    print(f"[dedup] Detected {len(groups)} duplicate group(s).")
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
                    print(f"[dedup] WARNING: slug '{slug}' not found on disk; skipping group.", file=sys.stderr)
                    group_pages = []
                    break
                path, content = entry
                group_pages.append({"slug": slug, "path": path, "content": content})
            if len(group_pages) < 2:
                continue

            other_pages = [
                {"path": p, "content": c}
                for p, c in pages
                if _slug_from_path(p) not in {gp["slug"] for gp in group_pages}
            ]

            result = _dedup.merge_duplicate_group(
                group_pages, canonical_slug, other_pages, llm_call, today=today
            )
            _persist_merge(project_root, result, backup_dir)
            removed = {_slug_from_path(p) for p in result.pages_to_delete}
            applied.append({
                "canonical": canonical_slug,
                "canonical_path": result.canonical_path,
                "merged_away": sorted(removed),
                "rewrites": [r["path"] for r in result.rewrites],
                "backup_dir": str(backup_dir.relative_to(project_root))
                if _is_relative_to(backup_dir, project_root) else str(backup_dir),
            })
            print(f"[dedup] merged → {canonical_slug} "
                  f"(removed {sorted(removed)}, {len(result.rewrites)} cross-ref rewrite(s))")

    report = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "apply": apply,
        "pagesScanned": len(summaries),
        "groups": groups,
        "applied": applied,
    }
    _write_report(runtime / "dedup-report.json", report)
    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print(f"[dedup] {mode} — report → {runtime / 'dedup-report.json'}")
    return report


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _persist_merge(project_root: Path, result, backup_dir: Path) -> None:
    """Write backup, canonical, rewrites; delete merged-away pages; prune index."""
    # 1. Backup pre-merge state of every touched file.
    for b in result.backup:
        bpath = backup_dir / b["path"]
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text(b["content"], encoding="utf-8")

    # 2. Write canonical merged content.
    canon = project_root / result.canonical_path
    canon.parent.mkdir(parents=True, exist_ok=True)
    canon.write_text(result.canonical_content, encoding="utf-8")

    # 3. Write cross-reference rewrites.
    for r in result.rewrites:
        rpath = project_root / r["path"]
        rpath.write_text(r["new_content"], encoding="utf-8")

    # 4. Delete merged-away pages.
    for p in result.pages_to_delete:
        dpath = project_root / p
        if dpath.exists():
            dpath.unlink()

    # 5. Prune index.md lines referencing removed slugs.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Semantic duplicate-page detection + merge sweep."
    )
    parser.add_argument("--project", default=None,
                        help="Wiki project root (default: IMPROVED_WIKI_ROOT or cwd)")
    parser.add_argument("--apply", action="store_true",
                        help="Execute merges (default: dry-run, no writes)")
    parser.add_argument("--whitelist", action="append", default=[],
                        help="Extra not-duplicates JSON file (repeatable)")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="LLM max_tokens (default 4096)")
    args = parser.parse_args(argv)

    project_root = Path(args.project or os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    if not (project_root / "wiki").is_dir():
        print(f"ERROR: wiki/ not found under {project_root}", file=sys.stderr)
        return 2

    from _llm_call import make_llm_callable
    try:
        llm_call = make_llm_callable(max_tokens=args.max_tokens)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    whitelist_pairs = load_whitelist(*[Path(p) for p in args.whitelist])
    run_sweep(
        project_root, llm_call,
        apply=args.apply,
        whitelist_pairs=whitelist_pairs,
        today=lambda: date.today().isoformat(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
