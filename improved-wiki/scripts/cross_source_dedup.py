#!/usr/bin/env python3
"""cross_source_dedup.py — 跨源去重 (cross-source dedup): lint-time, whole-wiki.

Runs OFFLINE (user-invoked, not during ingest) across the ENTIRE wiki to merge
duplicates that accumulated across multiple ingests. Distinct from Stage 2.5
源内去重 (intra-source dedup, `_stage_2_5_dedup.py`) which is a conservative
inline filter on one source's blocks before write. This module is thorough:
backs up, writes a report, and rewrites all `[[wikilinks]]` + `related:`
across the wiki so merges leave no broken links.

LLM semantic detection (NashSU `dedup.ts` + `dedup-runner.ts` parity): detects same-topic
different-name slugs (synonyms, EN/中文, singular/plural, abbrev/full) via
LLM-driven self-check, then LLM body-merge each group.

LLM path: **conversation-mode only** (``make_conversation_llm_call``) — the
same prompt-file handoff primitive ingest.py uses, so the calling agent's
model does the work. No direct HTTP API / ``LLM_API_KEY`` path (round v,
2026-06-23): text generation is conversation-mode everywhere, matching ingest.

Dedup is NOT run after ingest — it is a standalone lint-command action
(``wiki-lint.sh --dedup``). When invoked, it auto-applies (deletes files);
pass ``--dry-run`` to preview.

Usage:
  python3 cross_source_dedup.py                          # LLM semantic dedup, auto-apply
  python3 cross_source_dedup.py --dry-run                # preview only, no writes
  python3 cross_source_dedup.py --project /path/to/wiki
  python3 cross_source_dedup.py --whitelist whitelist.json

Exit codes: 0 done; 101 conversation pending; 2 config error.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import _dedup  # noqa: E402
# Cross-source dedup is pure LLM semantic (no deterministic prefilter) — NashSU
# dedup.ts parity.
from _core import ConversationPending  # noqa: E402
from _paths import detect_runtime_dir, iter_wiki_pages, atomic_write  # noqa: E402
from _llm_call import make_conversation_llm_call  # noqa: E402
from _dedup_embedding import (  # noqa: E402
    candidate_pairs,
    cluster_by_pairs,
    DuplicatePrefilterError,
)
from _dedup_storage import add_not_duplicate, load_not_duplicates  # noqa: E402

# ── Prefilter / detector tuning (NashSU dedup-runner.ts parity) ──────────────
# The cross-source prefilter threshold is deliberately BELOW the intra-source
# module default (0.82) — NashSU dedup-runner overrides candidate_pairs with
# DEDUP_PREFILTER_THRESHOLD=0.68 so weaker/non-multilingual embedders still
# surface cross-language and abbrev/full aliases. (NashSU dedup-runner.ts)
DEDUP_PREFILTER_THRESHOLD = 0.68
# NashSU packs candidate clusters into <=80-summary batches per LLM detector
# call (DEDUP_DETECTOR_BATCH_SUMMARIES). (NashSU dedup-runner.ts)
DEDUP_DETECTOR_BATCH_SUMMARIES = 80
# NashSU only full-scans on zero candidate pairs when summaries<=250
# (DEDUP_EMPTY_PREFILTER_FULL_SCAN_LIMIT). (NashSU dedup-runner.ts)
DEDUP_EMPTY_PREFILTER_FULL_SCAN_LIMIT = 250

# Aggregate files excluded from dedup candidates (NashSU embedding/graph parity:
# aggregates aren't dedup'd). Keep in sync with _lint_suggest.AGGREGATE_FILES.
ANCHOR_FILES = {"index.md", "log.md", "overview.md", "schema.md"}
STATE_FILES = {
    "lint-cache.json", "lint.json", "ingest-cache.json", "ingest-queue.json",
    "ingest-lock", "lint-lock", "lint-semantic.json", "dedup-report.json",
    "dedup-whitelist.json", "review.json", "review-suggestions.json",
    "embed-cache.json",
}
# Artifact dirs (lint/REVIEW/clusters/media) come from the shared
# _paths.WIKI_ARTIFACT_DIRS via iter_wiki_pages — the local copy here had
# drifted (missing `clusters`, so graph-generated hub pages leaked into dedup).


# ── LLM call: conversation-mode only ───────────────────────────────────────

def make_llm_call(project_root: Path):
    """Return (callable, runtime). Always conversation-mode — the calling
    agent's model does the work via the shared prompt-file handoff."""
    runtime = detect_runtime_dir(project_root)
    conv = make_conversation_llm_call(runtime, stage_prefix="dedup")
    print("[dedup] LLM path: conversation-mode (calling agent's model)")
    return conv, runtime


# ── LLM semantic dedup (existing _dedup engine) ───────────────────

def collect_wiki_pages(wiki_dir: Path) -> list[tuple[str, str]]:
    return [
        (f"wiki/{rel}", content)
        for rel, content in iter_wiki_pages(
            wiki_dir, anchor_files=ANCHOR_FILES, state_files=STATE_FILES,
        )
    ]


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


_slug_from_path = _dedup._slug_from_path


def _is_embedding_coverage_error(ex: Exception) -> bool:
    """True for the DuplicatePrefilterError variants that mean "couldn't embed
    enough pages". Mirrors NashSU isEmbeddingCoverageError. (NashSU dedup-runner.ts)"""
    msg = str(ex).lower()
    return ("could not embed enough pages" in msg
            or "embedded only" in msg)


# Order-independent, case-insensitive key — shared with the whitelist storage
# layer (was a local copy with a "\t" separator; keys are only ever compared
# against keys built by the same function, so unifying on "," is safe and
# removes the cross-module drift risk).
from _dedup_storage import canonical_key as _normalize_slug_group_key  # noqa: E402


def _filter_whitelisted_pairs(pairs, not_duplicates):
    """Drop candidate pairs that are on the not-duplicates whitelist BEFORE
    clustering, so a whitelisted pair can't drag a cluster together. Pair ids
    are slugs (emb_pages use slug as id). Mirrors NashSU filterWhitelistedPairs.
    (NashSU dedup-runner.ts)"""
    if not not_duplicates:
        return pairs
    not_dup_set = {_normalize_slug_group_key(g) for g in not_duplicates if len(g) >= 2}
    return [(a, b) for a, b in pairs
            if _normalize_slug_group_key([a, b]) not in not_dup_set]


def _batch_candidate_clusters(clusters, summary_by_slug):
    """Pack candidate clusters into <=DEDUP_DETECTOR_BATCH_SUMMARIES-summary
    batches so a very large cluster doesn't blow up one LLM call. Mirrors
    NashSU batchCandidateClusters. (NashSU dedup-runner.ts)"""
    batches: List[list] = []
    current: list = []
    for cluster in clusters:
        cluster_summaries = [summary_by_slug[sid] for sid in cluster
                             if sid in summary_by_slug]
        if len(cluster_summaries) < 2:
            continue
        if (current
                and len(current) + len(cluster_summaries) > DEDUP_DETECTOR_BATCH_SUMMARIES):
            batches.append(current)
            current = []
        current.extend(cluster_summaries)
        if len(current) >= DEDUP_DETECTOR_BATCH_SUMMARIES:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


def _unique_duplicate_groups(groups):
    """Dedup identical detected groups across batches/clusters (order- and
    case-insensitive on the slug set). Mirrors NashSU uniqueDuplicateGroups.
    (NashSU dedup-runner.ts)"""
    seen: set = set()
    out: List[dict] = []
    for g in groups:
        key = _normalize_slug_group_key(g["slugs"])
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out


def _detect_groups(summaries, pages, llm_call, not_duplicates, embedding_prefilter):
    """Run the LLM duplicate detector, optionally pre-clustered by embeddings.

    With ``embedding_prefilter`` (GAP-3): embed every page's short description,
    cluster by cosine similarity, drop whitelisted pairs, then run the LLM
    detector per size-bounded batch — so each LLM call sees a small candidate
    set instead of the whole wiki in one prompt.

    Empty-prefilter / coverage handling follows NashSU dedup-runner:
      - zero candidate pairs: full-scan only when summaries<=250 (recall for
        small/medium wikis); large wikis return [] (avoids the #359 hang).
      - coverage error: fall back to a full LLM scan only for small/medium
        wikis; large wikis skip (return []) rather than hang.
    """
    if not embedding_prefilter:
        return _dedup.detect_duplicate_groups(summaries, llm_call, not_duplicates=not_duplicates)

    summary_by_slug = {s.slug: s for s in summaries}
    emb_pages: List[dict] = []
    for path, content in pages:
        slug = _slug_from_path(path)
        if slug not in summary_by_slug:
            continue
        s = summary_by_slug[slug]
        # NashSU vectors summary.description (the short blurb), not the full
        # body, for candidate generation. (NashSU summaryToEmbeddingPage)
        emb_pages.append({"id": slug, "title": s.title, "tags": s.tags,
                          "body": s.description or ""})

    try:
        # NashSU dedup-runner overrides the module default (0.82, the
        # intra-source value) with DEDUP_PREFILTER_THRESHOLD=0.68 so
        # cross-language/abbrev aliases aren't missed. (NashSU dedup-runner.ts)
        pairs = candidate_pairs(emb_pages, threshold=DEDUP_PREFILTER_THRESHOLD)
    except DuplicatePrefilterError as ex:
        if (len(summaries) > DEDUP_EMPTY_PREFILTER_FULL_SCAN_LIMIT
                and _is_embedding_coverage_error(ex)):
            print(f"[dedup] embedding prefilter coverage too low ({ex}); "
                  f"skipping full fallback for large wiki "
                  f"({len(summaries)} > {DEDUP_EMPTY_PREFILTER_FULL_SCAN_LIMIT}).")
            return []
        print(f"[dedup] embedding prefilter failed ({ex}); "
              f"falling back to full scan.")
        return _dedup.detect_duplicate_groups(summaries, llm_call, not_duplicates=not_duplicates)

    if not pairs:
        # Preserve recall for small/medium wikis: a weak or non-multilingual
        # embedder can miss exactly the cross-language aliases the detector is
        # meant to find. Large wikis return [] — the old full scan is what
        # caused #359 hangs. (NashSU dedup-runner.ts)
        if len(summaries) <= DEDUP_EMPTY_PREFILTER_FULL_SCAN_LIMIT:
            return _dedup.detect_duplicate_groups(
                summaries, llm_call, not_duplicates=not_duplicates)
        print(f"[dedup] no candidate pairs and large wiki "
              f"({len(summaries)} > {DEDUP_EMPTY_PREFILTER_FULL_SCAN_LIMIT}); "
              f"skipping detector.")
        return []

    # Drop whitelisted pairs BEFORE clustering. (NashSU dedup-runner.ts)
    filtered_pairs = _filter_whitelisted_pairs(pairs, not_duplicates)
    if not filtered_pairs:
        return []

    clusters = cluster_by_pairs([pg["id"] for pg in emb_pages], filtered_pairs)
    if not clusters:
        return []

    batches = _batch_candidate_clusters(clusters, summary_by_slug)
    print(f"[dedup] embedding prefilter → {len(filtered_pairs)} candidate pair(s), "
          f"{len(clusters)} cluster(s), {len(batches)} detector batch(es).")
    groups: List[dict] = []
    for batch in batches:
        sub_groups = _dedup.detect_duplicate_groups(
            batch, llm_call, not_duplicates=not_duplicates)
        groups.extend(sub_groups)
    return _unique_duplicate_groups(groups)


def run_phase2(project_root, llm_call, *, apply=True, whitelist_pairs=None,
               today=None, apply_low_confidence=False,
               embedding_prefilter=True) -> dict:
    wiki_dir = project_root / "wiki"
    runtime = detect_runtime_dir(project_root)
    pages = collect_wiki_pages(wiki_dir)
    summaries = [s for s in (_dedup.extract_entity_summary(p, c) for p, c in pages) if s is not None]
    if len(summaries) < 2:
        print("[dedup] fewer than 2 summarizable pages; skipping.")
        return {"groups": 0, "applied": []}

    not_duplicates = list(whitelist_pairs or [])
    # Runtime whitelist read goes through the ported _dedup_storage reader so
    # read and the --mark-not-duplicate write share one file + format.
    not_duplicates += load_not_duplicates(runtime)

    print(f"[dedup] scanning {len(summaries)} pages for semantic duplicates ...")
    groups = _detect_groups(summaries, pages, llm_call, not_duplicates, embedding_prefilter)
    print(f"[dedup] detected {len(groups)} duplicate group(s).")
    for i, g in enumerate(groups, 1):
        print(f"  group {i}: {g['slugs']}  ({g['confidence']}) — {g['reason']}")

    # GAP-2: low-confidence LLM groups are often false positives — auto-merging
    # them deletes pages that may not be duplicates. Skip them by default; require
    # an explicit --apply-low-confidence to merge. NashSU parity: the desktop UI
    # requires per-group user confirmation before any merge.
    if apply and not apply_low_confidence:
        skipped = [g for g in groups if g.get("confidence") == "low"]
        if skipped:
            print(f"[dedup] skipping {len(skipped)} low-confidence group(s) "
                  f"(re-run with --apply-low-confidence to merge them).")

    applied: list[dict] = []
    if apply and groups:
        # Serialize the merge+persist phase with a file lock so two concurrent
        # cross_source_dedup invocations can't interleave cross-reference
        # rewrites (last-write-wins data loss). This is the one-shot-CLI
        # equivalent of NashSU's persistent dedup-queue.ts: we port the
        # serialization GUARANTEE, not the persistent Zustand task queue (YAGNI).
        with _merge_lock(runtime):
            applied = _apply_merges(project_root, runtime, groups, pages,
                                    llm_call, today, apply_low_confidence)

    _write_report(runtime / "dedup-report.json", {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "apply": apply, "phase2": {"groups": groups, "applied": applied}})
    return {"groups": groups, "applied": applied}


@contextlib.contextmanager
def _merge_lock(runtime: Path):
    """Exclusive file lock (fcntl.flock) around the merge+persist phase. The
    CLI equivalent of dedup-queue.ts serialization — see _apply_merges caller."""
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / "dedup-merge.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _apply_merges(project_root, runtime, groups, pages, llm_call, today,
                  apply_low_confidence) -> list:
    """Merge each detected group and persist. Must run under _merge_lock."""
    applied: list = []
    backup_dir = runtime / f"dedup-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    pages_by_slug = {_slug_from_path(p): (p, c) for p, c in pages}
    for g in groups:
        if g.get("confidence") == "low" and not apply_low_confidence:
            continue
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
        print(f"[dedup] merged → {canonical_slug} "
              f"(removed {sorted(removed)}, {len(result.rewrites)} rewrite(s))")
    return applied


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
            # Atomic write so a crash mid-write can't corrupt index.md.
            atomic_write(index_path, pruned)


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2))


# ── main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM semantic dedup (NashSU dedup.ts parity). Auto-applies by default.")
    parser.add_argument("--project", default=None,
                        help="Wiki project root (default: IMPROVED_WIKI_ROOT or cwd)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — no writes")
    parser.add_argument("--apply-low-confidence", action="store_true",
                        help="Also merge low-confidence groups (skipped by default)")
    # NashSU dedup-runner ALWAYS prefilters by embedding before the LLM detector
    # — it is not optional there. Default ON to match: without it the detector
    # gets the WHOLE wiki in one prompt (the #359 hang on large wikis). The
    # prefilter degrades safely when local embeddings are unavailable (small
    # wikis fall back to a full scan; large wikis skip rather than hang — see
    # _detect_groups). --no-embedding-prefilter forces the old full-scan path.
    parser.add_argument("--embedding-prefilter", dest="embedding_prefilter",
                        action="store_true", default=True,
                        help="Pre-cluster pages by embedding similarity before the LLM "
                             "detector (default: ON; needs local Ollama)")
    parser.add_argument("--no-embedding-prefilter", dest="embedding_prefilter",
                        action="store_false",
                        help="Disable the embedding prefilter; run a single full-wiki "
                             "LLM detector scan (small wikis only — can hang on large ones)")
    parser.add_argument("--whitelist", action="append", default=[])
    parser.add_argument("--mark-not-duplicate", nargs=2, metavar=("SLUG_A", "SLUG_B"),
                        default=None,
                        help="Record a not-duplicate pair to dedup-whitelist.json "
                             "(idempotent) so the detector won't re-suggest it, then exit.")
    args = parser.parse_args(argv)

    project_root = Path(args.project or os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    if not (project_root / "wiki").is_dir():
        print(f"ERROR: wiki/ not found under {project_root}", file=sys.stderr)
        return 2

    # Whitelist WRITE action: record a not-duplicate pair and exit (no LLM,
    # no handoff). NashSU dedup-storage.addNotDuplicate parity.
    if args.mark_not_duplicate is not None:
        runtime = detect_runtime_dir(project_root)
        added = add_not_duplicate(runtime, list(args.mark_not_duplicate))
        pair = ", ".join(args.mark_not_duplicate)
        if added:
            print(f"[dedup] recorded not-duplicate pair: [{pair}]")
        else:
            print(f"[dedup] not-duplicate pair already recorded: [{pair}]")
        return 0

    apply = not args.dry_run

    llm_call, _ = make_llm_call(project_root)
    whitelist_pairs = load_whitelist(*[Path(p) for p in args.whitelist])
    try:
        run_phase2(project_root, llm_call, apply=apply,
                   whitelist_pairs=whitelist_pairs,
                   today=lambda: date.today().isoformat(),
                   apply_low_confidence=args.apply_low_confidence,
                   embedding_prefilter=args.embedding_prefilter)
    except ConversationPending:
        print("[dedup] conversation handoff — answer prompt under "
              "<runtime>/conversation/dedup/ and re-invoke.", file=sys.stderr)
        return 101

    print("[dedup] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
