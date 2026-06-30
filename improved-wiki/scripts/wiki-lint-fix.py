#!/usr/bin/env python3
"""wiki-lint-fix.py — apply structural-lint auto-fixes to a wiki/.

Runs ``_lint_suggest.run_structural_lint`` (with suggestions) over ``wiki/``
and applies the three fixes ported from NashSU ``lint-fixes.ts``:

  - broken-link + suggested_target   → rewrite [[broken]] → [[suggested]]
  - broken-link + no suggestion      → create a ``type: query`` stub page
  - orphan + suggested_source        → append [[orphan]] to the source page
  - no-outlinks + suggested_target   → append [[suggested]] to the page

It also ports NashSU lint-view.tsx:handleDeleteOrphan as a separate, opt-in,
DESTRUCTIVE action (``--delete-orphans``): cascade-delete every orphan page
(file + index.md listing + body [[wikilinks]] + related: frontmatter refs).

Idempotent: re-running on a clean wiki is a no-op. Default mode is a dry-run
preview; pass ``--apply`` to write.

Usage:
  python3 wiki-lint-fix.py                                              # dry-run preview
  python3 wiki-lint-fix.py --apply                                      # write fixes
  python3 wiki-lint-fix.py --apply --wiki-root /path/to/project/wiki
  python3 wiki-lint-fix.py --apply --from-cache .llm-wiki/lint-cache.json  # skip rescan
  python3 wiki-lint-fix.py --delete-orphans                            # preview orphan cascade
  python3 wiki-lint-fix.py --delete-orphans --apply                    # cascade-delete orphans
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
    stub_relative_path_from_broken_target,
    build_deleted_keys,
    clean_index_listing,
    extract_frontmatter_title,
    normalize_wiki_ref_key,
    strip_deleted_wikilinks,
)
from _frontmatter_array import (  # noqa: E402
    parse_frontmatter_array,
    write_frontmatter_array,
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
_SKIP_DIRS = {"lint", "REVIEW", "clusters", "media"}  # clusters/ = graph-generated (match semantic lint + graph.py)


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
      {kind: "stub", broken, page}     # create stub AND rewrite [[broken]] in `page`
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
                # Carry `page` so apply_fixes can repoint [[broken]] at the new
                # stub — otherwise the link stays dangling and the next lint run
                # re-reports it (non-idempotent). NashSU handleFix does both.
                actions.append({"kind": "stub", "broken": broken, "page": page})
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
    # Order matters: a 'rewrite' (typo [[broken]] -> [[canonical]]) or 'stub'
    # can create the very link an 'append' wants to add to the same page. If the
    # append runs first it checks the still-broken content, its dedup
    # (has_wikilink_to_target) misses, and it appends a duplicate of the link the
    # rewrite then produces. Process link-FIXING actions (rewrite/stub) before
    # link-ADDING ones (append) so append sees the final canonical link and
    # correctly skips. Stable sort preserves original order within each group.
    _KIND_ORDER = {"rewrite": 0, "stub": 0, "append": 1}
    actions = sorted(actions, key=lambda a: _KIND_ORDER.get(a.get("kind"), 0))

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
            page_rel = act.get("page")
            if dry_run:
                stub_rel = stub_relative_path_from_broken_target(act["broken"])
                print(f"  [stub]      create stub {stub_rel} for [[{act['broken']}]]")
                if page_rel:
                    print(f"  [stub]      + rewrite [[{act['broken']}]] -> stub in {page_rel}")
                summary["stub"] += 1
                continue
            _, rel, created = ensure_broken_link_stub(project_root, act["broken"])
            if created:
                print(f"  [stub]      created {rel}")
            # Repoint the source page's [[broken]] at the freshly-created stub so
            # the link resolves and a re-lint finds nothing (idempotent). NashSU
            # handleFix parity: ensureBrokenLinkStub THEN rewriteWikilinkTarget.
            if page_rel:
                content = load(page_rel)
                if content is not None:
                    new = rewrite_wikilink_target(content, act["broken"], rel)
                    if new != content:
                        cache[page_rel] = new
                        dirty.add(page_rel)
                        print(f"  [stub]      rewrote [[{act['broken']}]] in {page_rel}")
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


def _all_wiki_md(wiki_dir: Path) -> list[Path]:
    """Every ``*.md`` under wiki/ (incl. aggregates) — the sweep universe for
    the cascade. Mirrors NashSU flattenMd(listDirectory(wiki))."""
    if not wiki_dir.is_dir():
        return []
    return sorted(p for p in wiki_dir.rglob("*.md"))


def cascade_delete_orphans(
    wiki_dir: Path,
    orphan_rels: list[str],
    *,
    dry_run: bool,
) -> dict:
    """Cascade-delete orphan pages and every reference to them across the wiki.

    Faithful port of NashSU lint-view.tsx:handleDeleteOrphan ->
    wiki-page-delete.ts:cascadeDeleteWikiPagesWithRefs. Steps:

      1. Read each target's title (for index/related matching) + snapshot its
         slug (file stem). Captured BEFORE deletion.
      2. Delete each target file from disk.
      3. Sweep all surviving wiki/*.md and rewrite them:
         - index.md listing entries whose primary [[target]] points at a
           deleted page  → line dropped (clean_index_listing)
         - any body [[deleted]] / [[deleted|alias]]  → wikilink replaced with
           plain text, alias preserved (strip_deleted_wikilinks)
         - any frontmatter `related:` array entry pointing at a deleted slug or
           its title-form  → filtered out, array rewritten

    Atomic + idempotent: writes go through _atomic_write; a second run finds the
    files already gone and no surviving refs, so it is a no-op. Aggregate files
    (index/log/overview/schema) are protected as DELETE TARGETS via the
    AGGREGATE_FILES guard, but are still SWEPT for stale refs (index.md listing
    cleanup specifically depends on sweeping index.md).

    EMBEDDING-CHUNK DELETION IS NOT PERFORMED: NashSU cascadeDeleteWikiPage also
    calls removePageEmbedding to drop LanceDB vector chunks, but this CLI has no
    access to the desktop app's LanceDB instance. The chunks for a deleted page
    become phantom hits until the next full re-embed. This divergence is
    intentional and reported; we do NOT fake the drop.

    Returns a summary counter.
    """
    summary = {"deleted": 0, "rewritten": 0, "skipped": 0, "missing": 0}

    # Resolve + filter targets. Never delete an aggregate file even if a stale
    # cache somehow surfaced one as an orphan.
    targets: list[Path] = []
    infos: list[tuple] = []  # (slug, title)
    for rel in orphan_rels:
        if Path(rel).name in _AGGREGATE_FILES:
            summary["skipped"] += 1
            continue
        full = wiki_dir / rel
        if not full.exists():
            summary["missing"] += 1
            continue
        title = ""
        try:
            title = extract_frontmatter_title(full.read_text(encoding="utf-8"))
        except OSError:
            pass
        slug = full.stem  # getFileStem equivalent
        if slug:
            infos.append((slug, title))
            targets.append(full)

    if not targets:
        return summary

    # Step 2: delete target files.
    deleted_paths: set[Path] = set()
    for full in targets:
        if dry_run:
            print(f"  [delete]   {full.relative_to(wiki_dir)}")
            deleted_paths.add(full)
            summary["deleted"] += 1
            continue
        try:
            full.unlink()
            deleted_paths.add(full)
            summary["deleted"] += 1
            print(f"  [delete]   {full.relative_to(wiki_dir)}")
        except OSError as exc:
            print(f"  [warn] failed to delete {full}: {exc}", file=sys.stderr)

    # Step 3: sweep surviving pages for stale refs.
    deleted_keys = build_deleted_keys(infos)
    if not deleted_keys:
        return summary

    for path in _all_wiki_md(wiki_dir):
        if path in deleted_paths:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue

        updated = content
        if path.name == "index.md":
            updated = clean_index_listing(updated, deleted_keys)
        updated = strip_deleted_wikilinks(updated, deleted_keys)

        related = parse_frontmatter_array(updated, "related")
        if related:
            filtered = [
                s for s in related
                if normalize_wiki_ref_key(s) not in deleted_keys
            ]
            if len(filtered) != len(related):
                updated = write_frontmatter_array(updated, "related", filtered)

        if updated != content:
            verb = "would-edit" if dry_run else "rewrite"
            print(f"  [{verb}] {path.relative_to(wiki_dir)}")
            if not dry_run:
                _atomic_write(path, updated)
            summary["rewritten"] += 1

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
    parser.add_argument("--delete-orphans", action="store_true",
                        help="DESTRUCTIVE: cascade-delete every orphan page (file + "
                             "index.md listing + body [[wikilinks]] + related: refs). "
                             "Default OFF. Honors --apply (omit for dry-run preview). "
                             "Does NOT run the link auto-fixes.")
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

    if args.delete_orphans:
        orphan_rels = [str(f.get("page", "")) for f in orphans if f.get("page")]
        mode = "DRY-RUN" if not args.apply else "APPLY"
        print(f"[lint-fix] {mode} (delete-orphans): {len(orphan_rels)} orphan page(s)")
        if not orphan_rels:
            print("[lint-fix] no orphans to delete ✅")
            return 0
        dsummary = cascade_delete_orphans(
            wiki_dir, orphan_rels, dry_run=not args.apply)
        print(f"[lint-fix] {mode} (delete-orphans) summary: "
              f"deleted={dsummary['deleted']} rewritten={dsummary['rewritten']} "
              f"skipped={dsummary['skipped']} missing={dsummary['missing']}")
        print("[lint-fix] NOTE: embedding chunks for deleted pages are NOT dropped "
              "(no LanceDB access from CLI); they clear on the next full re-embed.")
        if not args.apply:
            print("[lint-fix] dry-run — no files changed. Re-run with --apply to delete.")
        return 0

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
