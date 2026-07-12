#!/usr/bin/env python3
"""wiki-lint-fix.py — apply structural-lint auto-fixes to a wiki/.

Runs ``_lint_suggest.run_structural_lint`` (with suggestions) over ``wiki/``
and applies the three fixes ported from NashSU ``lint-fixes.ts``:

  - broken-link + suggested_target   → rewrite [[broken]] → [[suggested]]
  - broken-link + no suggestion      → REVIEW/missing-page item (default since
                                       2026-07-12, NashSU parity: stubs need an
                                       explicit human choice — pass --stub to
                                       bulk-create ``type: query`` stub pages)
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
    STATE_FILES as _STATE_FILES,
    BROKEN_LINK_AUTO_REWRITE_MIN_SCORE,
)
from _lint_fixes import (  # noqa: E402
    append_wikilink,
    rewrite_wikilink_target,
    ensure_broken_link_stub,
    stub_relative_path_from_broken_target,
    build_deleted_keys,
    clean_index_listing,
    extract_title_anywhere,
    normalize_wiki_ref_key,
    strip_deleted_wikilinks,
)
from _frontmatter_array import (  # noqa: E402
    parse_frontmatter_array,
    write_frontmatter_array,
)
from _paths import iter_wiki_pages, atomic_write as _atomic_write  # noqa: E402

# Scan universe = NashSU {index, log} (overview/schema stay valid link targets,
# their outlinks count). The engine exempts aggregates from findings, so the
# fixer never gets an overview/schema finding to apply; _AGGREGATE_FILES is the
# extra write-guard used on the --from-cache path below. + state (shared
# _lint_suggest.STATE_FILES) + lint/REVIEW/media.
def _collect_pages(wiki_dir: Path) -> list[tuple[str, str]]:
    return list(iter_wiki_pages(
        wiki_dir, anchor_files=_ANCHOR_FILES, state_files=_STATE_FILES,
    ))


# BROKEN_LINK_AUTO_REWRITE_MIN_SCORE (imported from _lint_suggest, 2026-07-12):
# the headless auto-rewrite gate — only exact/same-basename tier suggestions
# are rewritten without a human; contains-tier and fuzzy matches go to
# REVIEW/suggestion instead (real incident class: the substring 脉冲压缩
# auto-linked across 10+ pages to the narrower 脉冲压缩与MTI组合 page).
# NashSU has no such gate because its Fix is human-clicked per item.


def plan_fixes(findings: list[dict]) -> list[dict]:
    """Turn lint findings into a list of fix actions.

    Action shapes:
      {kind: "rewrite", page, broken, suggested}
      {kind: "review-rewrite", page, broken, suggested, score}
                                       # suggestion below the auto-rewrite gate
                                       # → routed to REVIEW, never auto-applied
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
                score = fnd.get("suggested_score")
                # A missing score (stale cache from an older lint) is treated
                # conservatively: no headless rewrite, route to review.
                if score is not None and score >= BROKEN_LINK_AUTO_REWRITE_MIN_SCORE:
                    actions.append({"kind": "rewrite", "page": page,
                                    "broken": broken, "suggested": suggested})
                else:
                    actions.append({"kind": "review-rewrite", "page": page,
                                    "broken": broken, "suggested": suggested,
                                    "score": score})
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
            title = extract_title_anywhere(full.read_text(encoding="utf-8"))
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


def _emit_review_for_broken(
    project_root: Path,
    wiki_dir: Path,
    stub_actions: list[dict],
    *,
    dry_run: bool,
) -> None:
    """Write missing-page review items for broken links that have no suggestion.

    NashSU parity: when handleFix encounters a broken-link with no
    suggested_target, it falls back to the Review store instead of silently
    creating a stub. This function emits one review .md per unique broken
    target into ``wiki/REVIEW/missing-page/`` so the human can decide whether
    to create a real page, deep-research the concept, or ignore it.
    """
    if not stub_actions:
        return
    review_dir = wiki_dir / "REVIEW" / "missing-page"
    seen: set[str] = set()
    count = 0
    for act in stub_actions:
        broken = act.get("broken", "")
        if not broken or broken in seen:
            continue
        seen.add(broken)
        # Collect which pages reference this broken link
        ref_pages: list[str] = []
        for a in stub_actions:
            if a.get("broken") == broken and a.get("page"):
                ref_pages.append(a["page"])
        # Build review item
        slug = broken.replace("/", "-").replace("\\", "-")[:60]
        date_str = "2026-07-05"  # stable date for lint-generated items
        fname = f"{date_str}-lint-{slug}.md"
        fpath = review_dir / fname
        if dry_run:
            print(f"  [review]    would create {fpath.relative_to(wiki_dir)}")
            count += 1
            continue
        review_dir.mkdir(parents=True, exist_ok=True)
        ref_list = "\n".join(f"  - {p}" for p in ref_pages[:10])
        content = f"""---
type: review
review_type: missing-page
title: "Missing page: [[{broken}]]"
created: {date_str}
resolved: false
resolved_at: null
resolved_reason: null
affected_pages:
{ref_list}
search_queries:
  - "{broken}"
---

# Missing page: [[{broken}]]

This wikilink target does not exist in the wiki. It was detected by structural
lint during ``--fix-links`` (no-suggestion mode) and routed to review instead of
creating an empty stub page.

**Referenced by:**
{ref_list}

**Options:** Create Page | Deep Research | Skip
"""
        _atomic_write(fpath, content)
        print(f"  [review]    created {fpath.relative_to(wiki_dir)}")
        count += 1
    print(f"[lint-fix] emitted {count} missing-page review item(s)")


def _emit_review_for_unsuggestable(
    wiki_dir: Path,
    findings: list[dict],
    *,
    dry_run: bool,
) -> None:
    """Write review items for orphan / no-outlinks findings that have no
    suggestion and so produced no fix action.

    NashSU parity (audit M4, 2026-07-07): broken-link-no-suggestion already
    routes to review via ``--no-stub`` + ``_emit_review_for_broken``. But
    orphan (no ``suggested_source``) and no-outlinks (no ``suggested_target``)
    findings are dropped silently by ``plan_fixes`` — there is no stub/append
    action to take when no suggestion exists. Emit one review .md per affected
    page into ``wiki/REVIEW/suggestion/`` so the human can decide: link
    from/to where, deep-research, or ignore. Only called in ``--no-stub`` mode
    (the review-routing mode ``wiki-lint.sh`` uses by default for
    ``--fix-links``). Findings WITH a suggestion became append actions and were
    applied; they are not emitted here.
    """
    review_dir = wiki_dir / "REVIEW" / "suggestion"
    count = 0
    seen: set[str] = set()
    for fnd in findings:
        kind = fnd.get("type")
        if kind not in ("orphan", "no-outlinks"):
            continue
        # Skip findings that had a suggestion — those became append actions.
        if kind == "orphan" and fnd.get("suggested_source"):
            continue
        if kind == "no-outlinks" and fnd.get("suggested_target"):
            continue
        page = fnd.get("page")
        if not page or page in seen:
            continue
        seen.add(page)
        label = ("orphan (no inbound links, no suggested source)" if kind == "orphan"
                 else "no outbound links (no suggested target)")
        slug = page.removesuffix(".md").replace("/", "-").replace("\\", "-")[:60]
        date_str = "2026-07-07"  # stable date → idempotent re-runs (same filename)
        fname = f"{date_str}-lint-{kind}-{slug}.md"
        fpath = review_dir / fname
        if dry_run:
            print(f"  [review]    would create {fpath.relative_to(wiki_dir)}")
            count += 1
            continue
        review_dir.mkdir(parents=True, exist_ok=True)
        content = f"""---
type: review
review_type: suggestion
title: "Unsuggestable {kind}: {page}"
created: {date_str}
resolved: false
resolved_at: null
resolved_reason: null
affected_pages:
  - {page}
---

# Unsuggestable {kind}: {page}

This page is {label}. Structural lint's suggestion engine offered no link
target/source, so ``--fix-links`` could not auto-fix it. Routed to review
(``--no-stub`` mode) so a human can decide: add a link manually, deep-research
a related concept, or ignore.

**Options:** Add link manually | Deep Research | Skip
"""
        _atomic_write(fpath, content)
        print(f"  [review]    created {fpath.relative_to(wiki_dir)}")
        count += 1
    if count:
        print(f"[lint-fix] emitted {count} unsuggestable orphan/no-outlinks review item(s)")


def _emit_review_for_uncertain_rewrite(
    wiki_dir: Path,
    review_actions: list[dict],
    *,
    dry_run: bool,
) -> None:
    """Write review items for broken-link suggestions BELOW the auto-rewrite
    gate (contains-tier / fuzzy matches). Each item names the proposed target
    and its score so a human can approve the rewrite, pick another target, or
    ignore. Idempotent: stable filename per broken target."""
    if not review_actions:
        return
    review_dir = wiki_dir / "REVIEW" / "suggestion"
    seen: set[str] = set()
    count = 0
    for act in review_actions:
        broken = act.get("broken", "")
        if not broken or broken in seen:
            continue
        seen.add(broken)
        ref_pages = [a["page"] for a in review_actions
                     if a.get("broken") == broken and a.get("page")]
        suggested = act.get("suggested", "")
        score = act.get("score")
        score_str = f"{score}" if score is not None else "unknown (older cache)"
        slug = broken.replace("/", "-").replace("\\", "-")[:60]
        date_str = "2026-07-10"  # stable date → idempotent re-runs
        fname = f"{date_str}-lint-uncertain-rewrite-{slug}.md"
        fpath = review_dir / fname
        if dry_run:
            print(f"  [review]    would create {fpath.relative_to(wiki_dir)}")
            count += 1
            continue
        review_dir.mkdir(parents=True, exist_ok=True)
        ref_list = "\n".join(f"  - {p}" for p in ref_pages[:10])
        content = f"""---
type: review
review_type: suggestion
title: "Uncertain link rewrite: [[{broken}]] → [[{suggested}]]"
created: {date_str}
resolved: false
resolved_at: null
resolved_reason: null
affected_pages:
{ref_list}
---

# Uncertain link rewrite: [[{broken}]] → [[{suggested}]]

Structural lint suggests rewriting the broken link ``[[{broken}]]`` to
``[[{suggested}]]`` (similarity score {score_str}), but the score is below the
headless auto-rewrite gate ({BROKEN_LINK_AUTO_REWRITE_MIN_SCORE}) — the match
may be string-similar without being the right page. Routed to review so a
human decides.

**Referenced by:**
{ref_list}

**Options:** Approve rewrite | Pick a different target | Create the missing page | Skip
"""
        _atomic_write(fpath, content)
        print(f"  [review]    created {fpath.relative_to(wiki_dir)}")
        count += 1
    print(f"[lint-fix] emitted {count} uncertain-rewrite review item(s) "
          f"(score < {BROKEN_LINK_AUTO_REWRITE_MIN_SCORE} — not auto-applied)")


def _emit_review_for_orphan_delete(
    wiki_dir: Path,
    orphan_rels: list[str],
) -> None:
    """Write one review item per orphan page queued for deletion (2026-07-10,
    user-approved lint hardening: wiki-lint.sh's delete-orphans stage is now a
    preview + these review items; the actual cascade-delete requires an
    explicit ``wiki-lint-fix.py --delete-orphans --apply``).

    Always writes (this IS the preview's actionable output — an orphan that
    should genuinely go, like a stale placeholder stub, gets deleted after a
    human approves; a freshly-ingested page that simply has no inbound links
    yet gets spared). Idempotent: stable filename per page.

    Filed under its own ``review_type: orphan`` / ``wiki/REVIEW/orphan/``
    category (2026-07-12, user-requested) rather than lumped into
    ``suggestion`` — orphans are a distinct, high-volume review queue the
    user wants to page through on their own, separate from general
    suggestions. This is an improved-wiki-only category (no NashSU
    equivalent — the app has no persistent REVIEW store at all), so adding
    it doesn't break NashSU parity."""
    review_dir = wiki_dir / "REVIEW" / "orphan"
    count = 0
    for rel in orphan_rels:
        slug = rel.removesuffix(".md").replace("/", "-").replace("\\", "-")[:60]
        date_str = "2026-07-10"  # stable date → idempotent re-runs
        fname = f"{date_str}-lint-orphan-delete-{slug}.md"
        fpath = review_dir / fname
        if fpath.exists():
            continue
        review_dir.mkdir(parents=True, exist_ok=True)
        content = f"""---
type: review
review_type: orphan
title: "Orphan delete candidate: {rel}"
created: {date_str}
resolved: false
resolved_at: null
resolved_reason: null
human_gate: true
affected_pages:
  - {rel}
---

# Orphan delete candidate: {rel}

No other pages link to this page. It is a candidate for cascade deletion
(file + index listing + inbound ``[[wikilinks]]`` + ``related:`` refs), but
deletion is no longer automatic: an orphan may simply be a freshly-ingested
page that wikilink enrichment has not linked yet, or one only referenced from
REVIEW items. Note: if a ``--fix-links`` pass in the same lint run appended an
inbound link to this page, it may no longer be an orphan — re-run lint to
confirm before deleting.

**To delete after review:** ``wiki-lint-fix.py --delete-orphans --apply``

**Options:** Delete | Link it from a related page | Skip
"""
        _atomic_write(fpath, content)
        print(f"  [review]    created {fpath.relative_to(wiki_dir)}")
        count += 1
    print(f"[lint-fix] emitted {count} orphan-delete review item(s)")


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
    parser.add_argument("--emit-review", action="store_true",
                        help="With --delete-orphans and WITHOUT --apply: in addition "
                             "to the preview listing, write one REVIEW/orphan "
                             "item per orphan so a human can approve deletion. This "
                             "is what wiki-lint.sh's delete-orphans stage passes "
                             "(2026-07-10): preview + review by default, real delete "
                             "only via an explicit --delete-orphans --apply.")
    parser.add_argument("--stub", action="store_true",
                        help="Create empty stub pages (wiki/queries/) for broken links "
                             "that have no suggested target, and repoint the link at "
                             "the stub. Default OFF since 2026-07-12 (NashSU parity: "
                             "stubs come from a human-clicked per-item Fix, never a "
                             "headless bulk pass) — without this flag, unsuggestable "
                             "broken links become REVIEW/missing-page items instead.")
    parser.add_argument("--no-stub", action="store_true",
                        help="(Deprecated no-op — stub-off is the default since "
                             "2026-07-12. Kept so existing callers like wiki-lint.sh "
                             "don't break. An explicit --stub wins.)")
    parser.add_argument("--no-append", action="store_true",
                        help="Skip appending [[wikilinks]] to orphan / no-outlinks "
                             "pages. These come from the low-threshold (0.08) related "
                             "engine, far weaker than the 0.74 broken-link rewrite. "
                             "Combine with --no-stub for a rewrite-only pass that "
                             "touches only genuine typo/naming-drift links.")
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
        if args.from_cache and orphan_rels:
            # Re-verify orphanhood against the CURRENT wiki (2026-07-11): the
            # cache predates any --fix-links appends from the same lint run —
            # a page rescued by an appended inbound link would otherwise still
            # be previewed/deleted as an orphan. Detection-only scan is O(n).
            pages_now = _collect_pages(wiki_dir)
            current = run_structural_lint(pages_now, with_suggestions=False)
            still_orphan = {str(f.get("page", "")) for f in current
                            if f.get("type") == "orphan"}
            dropped = [r for r in orphan_rels if r not in still_orphan]
            if dropped:
                print(f"[lint-fix] {len(dropped)} cached orphan(s) no longer "
                      f"orphaned on current disk — dropped: {dropped[:5]}")
            orphan_rels = [r for r in orphan_rels if r in still_orphan]
        mode = "DRY-RUN" if not args.apply else "APPLY"
        print(f"[lint-fix] {mode} (delete-orphans): {len(orphan_rels)} orphan page(s)")
        if not orphan_rels:
            print("[lint-fix] no orphans to delete ✅")
            return 0
        if args.emit_review and not args.apply:
            _emit_review_for_orphan_delete(wiki_dir, orphan_rels)
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
    # Suggestions below the auto-rewrite gate are never applied headlessly —
    # they become review items carrying the proposed target for human approval.
    review_rewrites = [a for a in actions if a.get("kind") == "review-rewrite"]
    if review_rewrites:
        actions = [a for a in actions if a.get("kind") != "review-rewrite"]
        print(f"[lint-fix] {len(review_rewrites)} suggestion(s) below the "
              f"auto-rewrite gate ({BROKEN_LINK_AUTO_REWRITE_MIN_SCORE}) → review items")
        _emit_review_for_uncertain_rewrite(wiki_dir, review_rewrites,
                                           dry_run=not args.apply)
    # Stub-off is the DEFAULT (2026-07-12, NashSU parity: stubs only from an
    # explicit human choice). --stub restores the bulk stub-creation path;
    # --no-stub is a deprecated no-op kept for existing callers.
    if not args.stub:
        _before = len(actions)
        stub_actions = [a for a in actions if a.get("kind") == "stub"]
        actions = [a for a in actions if a.get("kind") != "stub"]
        _skipped = _before - len(actions)
        if _skipped:
            print(f"[lint-fix] skipping {_skipped} stub-creation "
                  f"action(s) — broken links with no suggestion → review items "
                  f"(pass --stub to bulk-create stubs instead)")
            # Generate review items for unsuggestable broken links (NashSU parity:
            # handleFix falls back to Review store when no suggestion exists).
            _emit_review_for_broken(project_root, wiki_dir, stub_actions, dry_run=not args.apply)
            # Orphan / no-outlinks findings with no suggestion are dropped by
            # plan_fixes (no stub/append action possible). Route them to review
            # too (audit M4) — fires on the default path.
            _emit_review_for_unsuggestable(wiki_dir, findings, dry_run=not args.apply)
    if args.no_append:
        _before = len(actions)
        actions = [a for a in actions if a.get("kind") != "append"]
        _skipped = _before - len(actions)
        if _skipped:
            print(f"[lint-fix] --no-append: skipping {_skipped} append "
                  f"action(s) (low-threshold related-link suggestions left out)")
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
