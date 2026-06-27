#!/usr/bin/env python3
"""wiki-lint-fix.py — apply structural-lint auto-fixes to a wiki/.

Runs ``_lint_suggest.run_structural_lint`` (with suggestions) over ``wiki/``
and applies the three fixes ported from NashSU ``lint-fixes.ts``:

  - broken-link + suggested_target   → rewrite [[broken]] → [[suggested]]
  - broken-link + no suggestion      → create a ``type: query`` stub page
  - orphan + suggested_source        → append [[orphan]] to the source page
  - no-outlinks + suggested_target   → append [[suggested]] to the page

Idempotent: re-running on a clean wiki is a no-op. ``--dry-run`` previews
without writing.

Usage:
  python3 wiki-lint-fix.py                                              # dry-run preview
  python3 wiki-lint-fix.py --apply                                      # write fixes
  python3 wiki-lint-fix.py --apply --wiki-root /path/to/project/wiki
  python3 wiki-lint-fix.py --apply --from-cache .llm-wiki/lint-cache.json  # skip rescan
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _lint_suggest import (  # noqa: E402
    run_structural_lint,
    ANCHOR_FILES as _ANCHOR_FILES,
    AGGREGATE_FILES as _AGGREGATE_FILES,
)
from _lint_fixes import (  # noqa: E402
    append_wikilink,
    rewrite_wikilink_target,
    ensure_broken_link_stub,
)

# Scan universe = NashSU {index, log} (overview/schema stay valid link targets,
# their outlinks count). The engine exempts aggregates from findings, so the
# fixer never gets an overview/schema finding to apply; _AGGREGATE_FILES is the
# extra write-guard used on the --from-cache path below. + state + lint/REVIEW/media.
_STATE_FILES = {
    "lint-cache.json", "ingest-cache.json", "ingest-queue.json",
    "review.json", "review-suggestions.json", "embed-cache.json",
    "lint-semantic.json", "dedup-report.json",
}
_SKIP_DIRS = {"lint", "REVIEW", "media"}


def _collect_pages(wiki_dir: Path) -> list[tuple[str, str]]:
    pages: list[tuple[str, str]] = []
    if not wiki_dir.is_dir():
        return pages
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki_dir)
        if rel.name in _ANCHOR_FILES or rel.name in _STATE_FILES:
            continue
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            continue
        try:
            pages.append((str(rel), path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return pages


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def plan_fixes(findings: list[dict]) -> list[dict]:
    """Turn lint findings into a list of fix actions.

    Action shapes:
      {kind: "rewrite", page, broken, suggested}
      {kind: "stub", broken}
      {kind: "append", page, target}   # append [[target]] to `page`
    """
    actions: list[dict] = []
    for fnd in findings:
        kind = fnd.get("type")
        if kind == "broken-link":
            broken = fnd.get("broken_target")
            suggested = fnd.get("suggested_target")
            page = fnd.get("page")
            if not broken or not page:
                continue
            if suggested:
                actions.append({"kind": "rewrite", "page": page,
                                "broken": broken, "suggested": suggested})
            else:
                actions.append({"kind": "stub", "broken": broken})
        elif kind == "orphan":
            source = fnd.get("suggested_source")
            page = fnd.get("page")
            if source and page:
                # The orphan is `page`; link TO it from the suggested source.
                actions.append({"kind": "append", "page": source, "target": page})
        elif kind == "no-outlinks":
            target = fnd.get("suggested_target")
            page = fnd.get("page")
            if target and page:
                actions.append({"kind": "append", "page": page, "target": target})
    return actions


def apply_fixes(
    project_root: Path,
    wiki_dir: Path,
    actions: list[dict],
    *,
    dry_run: bool,
) -> dict:
    """Apply actions. Returns a summary counter."""
    cache: dict[str, str] = {}
    dirty: set[str] = set()
    summary = {"rewrite": 0, "stub": 0, "append": 0, "skipped": 0}

    def load(rel: str) -> str | None:
        if rel in cache:
            return cache[rel]
        p = wiki_dir / rel
        if not p.exists():
            return None
        try:
            cache[rel] = p.read_text(encoding="utf-8")
            return cache[rel]
        except OSError:
            return None

    for act in actions:
        kind = act["kind"]
        if kind == "stub":
            if dry_run:
                print(f"  [stub]      create stub for [[{act['broken']}]]")
            else:
                _, rel, created = ensure_broken_link_stub(project_root, act["broken"])
                if created:
                    print(f"  [stub]      created {rel}")
            summary["stub"] += 1
            continue

        rel = act["page"]
        content = load(rel)
        if content is None:
            summary["skipped"] += 1
            continue
        if kind == "rewrite":
            new = rewrite_wikilink_target(content, act["broken"], act["suggested"])
        elif kind == "append":
            new = append_wikilink(content, act["target"])
        else:
            summary["skipped"] += 1
            continue
        if new == content:
            summary["skipped"] += 1
            continue
        cache[rel] = new
        dirty.add(rel)
        summary[kind] += 1
        verb = "rewrite" if kind == "rewrite" else "append"
        print(f"  [{verb:7}] {rel}")

    if not dry_run:
        for rel in sorted(dirty):
            _atomic_write(wiki_dir / rel, cache[rel])
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Write fixes (default: dry-run preview).")
    parser.add_argument("--wiki-root", type=Path, default=None,
                        help="Wiki dir (default: <project>/wiki).")
    parser.add_argument("--project-root", type=Path, default=None,
                        help="Project root (default: $IMPROVED_WIKI_ROOT or cwd).")
    parser.add_argument("--from-cache", type=Path, default=None,
                        help="Load findings from an existing lint-cache.json instead of rescanning.")
    args = parser.parse_args()

    project_root = args.project_root or Path(
        os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_dir = args.wiki_root or (project_root / "wiki")
    if not wiki_dir.is_dir():
        print(f"ERROR: wiki/ not found at {wiki_dir}", file=sys.stderr)
        return 2

    if args.from_cache:
        cache_path = args.from_cache
        if not cache_path.exists():
            print(f"ERROR: cache not found: {cache_path}", file=sys.stderr)
            return 2
        all_findings = json.loads(cache_path.read_text(encoding="utf-8"))
        # Drop any aggregate-file findings: a cache produced by a tool/version
        # without finding-suppression must not let us mutate index/log/overview/schema.
        findings = [f for f in all_findings
                    if f.get("type") in ("broken-link", "orphan", "no-outlinks")
                    and Path(str(f.get("page", ""))).name not in _AGGREGATE_FILES]
        broken  = [f for f in findings if f["type"] == "broken-link"]
        orphans = [f for f in findings if f["type"] == "orphan"]
        no_out  = [f for f in findings if f["type"] == "no-outlinks"]
        print(f"[lint-fix] from cache ({cache_path.name}): "
              f"broken-link={len(broken)} orphan={len(orphans)} no-outlinks={len(no_out)}")
    else:
        pages = _collect_pages(wiki_dir)
        if not pages:
            print(f"No wiki pages under {wiki_dir}")
            return 0
        print(f"[lint-fix] Scanning {len(pages)} pages under {wiki_dir}")
        findings = run_structural_lint(pages, with_suggestions=True)
        broken  = [f for f in findings if f["type"] == "broken-link"]
        orphans = [f for f in findings if f["type"] == "orphan"]
        no_out  = [f for f in findings if f["type"] == "no-outlinks"]
        print(f"[lint-fix] findings: broken-link={len(broken)} "
              f"orphan={len(orphans)} no-outlinks={len(no_out)}")

    actions = plan_fixes(findings)
    mode = "DRY-RUN" if not args.apply else "APPLY"
    print(f"[lint-fix] {mode}: {len(actions)} fix action(s) planned")
    if not actions:
        print("[lint-fix] nothing to fix ✅")
        return 0

    summary = apply_fixes(project_root, wiki_dir, actions, dry_run=not args.apply)
    print(f"[lint-fix] {mode} summary: "
          f"rewrite={summary['rewrite']} stub={summary['stub']} "
          f"append={summary['append']} skipped={summary['skipped']}")
    if not args.apply:
        print("[lint-fix] dry-run — no files changed. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
