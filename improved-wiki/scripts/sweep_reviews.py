#!/usr/bin/env python3
"""
sweep_reviews.py — Auto-resolve review items satisfied by subsequent ingests.

参考 NashSU sweep-reviews.ts: scans pending review items, applies
rule-based matching (Stage 1: missing-page now exists, duplicate's affected
page gone), then an optional LLM semantic judge (Stage 2) over what's left.

Conservative posture (NashSU): the rule-based stage only auto-resolves
``missing-page`` and ``duplicate``. ``contradiction`` / ``confirm`` /
``suggestion`` need human judgment and stay PENDING in the rule stage — the
LLM judge may resolve them, but defaults to keeping them.

LLM judge (Stage 2) runs in conversation mode (the only text-gen path): it
writes a prompt file under <runtime>/conversation/review-judge/ and returns
exit 101; the calling agent answers with the current conversation's model and
re-invokes. Use --no-llm for a pure rule-based run.

Usage:
  python3 sweep_reviews.py --project <wiki-root>           # dry-run (report only)
  python3 sweep_reviews.py --project <wiki-root> --apply   # auto-resolve + update files
  python3 sweep_reviews.py --project <wiki-root> --json    # machine-readable output
  python3 sweep_reviews.py --project <wiki-root> --no-llm  # skip LLM judge stage

Exit codes: 0 done; 101 conversation pending (agent answers + re-invokes).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _review_utils import (  # noqa: E402
    normalize_review_title,
    normalize_review_items,
)
from _frontmatter import (  # noqa: E402
    parse_frontmatter as _parse_frontmatter_shared,
    extract_frontmatter_title,
)

# ── LLM judge constants (verbatim from NashSU sweep-reviews.ts) ──────────────
JUDGE_BATCH_SIZE = 40
MAX_JUDGE_BATCHES = 5
MAX_PAGES_IN_PROMPT = 300


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse frontmatter dict (delegates to the shared _frontmatter parser)."""
    return _parse_frontmatter_shared(text)[0]


def _build_wiki_index(wiki_dir: Path) -> Dict[str, Set[str]]:
    """Port of NashSU sweep-reviews.ts ``buildWikiIndex``.

    Scan ALL of wiki/ RECURSIVELY (not a hardcoded folder list) and build an
    exact-match index:
      - by_id:    set of lowercased filename stems (id = filename without .md)
      - by_title: set of lowercased frontmatter titles

    The REVIEW/ subtree (and the runtime/state dirs) are excluded — those are
    review pages, not wiki knowledge pages.
    """
    by_id: Set[str] = set()
    by_title: Set[str] = set()
    for f in wiki_dir.rglob("*.md"):
        # Skip the REVIEW/ subtree. NOTE: this DIVERGES from NashSU's
        # buildWikiIndex, which indexes every .md (REVIEW included). We exclude
        # review pages on purpose so a review's own stem/title cannot false-match
        # a missing-page candidate and self-resolve. Deliberate, conservative.
        parts = {p.lower() for p in f.relative_to(wiki_dir).parts[:-1]}
        if "review" in parts:
            continue
        by_id.add(f.stem.lower())
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # First frontmatter `title:` value (shared extractor).
        t = extract_frontmatter_title(content)
        if t:
            by_title.add(t.lower())
    return {"by_id": by_id, "by_title": by_title}


def _wiki_page_summaries(wiki_dir: Path) -> List[Tuple[str, Optional[str]]]:
    """Collect (id, title) summaries for the LLM judge prompt (NashSU pages)."""
    pages: List[Tuple[str, Optional[str]]] = []
    for f in sorted(wiki_dir.rglob("*.md")):
        parts = {p.lower() for p in f.relative_to(wiki_dir).parts[:-1]}
        if "review" in parts:
            continue
        title: Optional[str] = None
        try:
            content = f.read_text(encoding="utf-8")
            title = extract_frontmatter_title(content) or None
        except Exception:
            pass
        pages.append((f.stem.lower(), title))
    return pages


def _scan_reviews(wiki_dir: Path) -> List[Dict]:
    """Scan wiki/REVIEW/ for unresolved items.

    Reads the category from ``review_type`` (NashSU field), falling back to the
    legacy ``type`` key only when ``review_type`` is absent (back-compat).
    """
    review_dir = wiki_dir / "REVIEW"
    if not review_dir.exists():
        return []
    items: List[Dict] = []
    for f in review_dir.rglob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(content)
        resolved = str(fm.get("resolved", "false")).lower() in ("true", "yes", "1")
        # NOTE: resolved items are KEPT here (not skipped) so the content-stable
        # dedup (normalize_review_items, resolved-wins) can let a resolved twin
        # suppress a freshly re-ingested pending duplicate. The caller filters
        # out resolved items AFTER dedup.
        # FIX: the category lives in `review_type:`; `type:` is always
        # "review". Read review_type first, fall back to type for old pages.
        rtype = fm.get("review_type")
        if not rtype:
            rtype = fm.get("type", "confirm")
        # Review pages carry the human title in the body H1 (`# [rtype] Title`),
        # not in frontmatter — recover it so dedup/judge see the real concept
        # name (else they'd key on the filename stem). Fall back to fm title /
        # stem for legacy pages.
        title = fm.get("title")
        if not title:
            h1 = re.search(r"^#\s*(?:\[[^\]]*\]\s*)?(.+?)\s*$", content, re.MULTILINE)
            title = h1.group(1).strip() if h1 else f.stem
        items.append({
            "file": str(f.relative_to(wiki_dir)),
            "path": f,
            "type": rtype,
            "title": title,
            "resolved": resolved,
            "review_id": fm.get("review_id", ""),
            "affected_pages": fm.get("affected_pages", []) if isinstance(fm.get("affected_pages"), list) else [],
            "search_queries": fm.get("search_queries", []) if isinstance(fm.get("search_queries"), list) else [],
            "description": fm.get("description", ""),
            "created": fm.get("created", ""),
            "frontmatter": fm,
        })
    return items


def _page_exists(name: str, index: Dict[str, Set[str]]) -> bool:
    """Port of NashSU sweep-reviews.ts ``pageExists`` — EXACT match only.

    A candidate matches when, lowercased and trimmed, it is:
      - an exact filename id, OR
      - an exact filename id after whitespace→hyphen (kebab) normalization, OR
      - an exact frontmatter title.

    No substring / partial matching (NashSU is exact: substring matching caused
    spurious auto-resolves where "attention" matched "attention-is-all").
    """
    normalized = name.strip().lower()
    if not normalized:
        return False
    if normalized in index["by_id"]:
        return True
    if re.sub(r"\s+", "-", normalized) in index["by_id"]:
        return True
    if normalized in index["by_title"]:
        return True
    return False


def _extract_candidate_names(review: Dict) -> List[str]:
    """Port of NashSU sweep-reviews.ts ``extractCandidateNames``.

    Conservative — only flags names we can confidently identify:
      - the normalized review title (the missing page name itself), capped at
        100 chars, and
      - each affected page's basename (filename stem).
    """
    names: List[str] = []
    seen: Set[str] = set()

    cleaned = normalize_review_title(review.get("title", ""))
    if cleaned and len(cleaned) <= 100 and cleaned not in seen:
        seen.add(cleaned)
        names.append(cleaned)

    for page in review.get("affected_pages") or []:
        base = str(page).strip().strip("[]").split("/")[-1]
        if base.endswith(".md"):
            base = base[:-3]
        base = base.lower()
        if base and base not in seen:
            seen.add(base)
            names.append(base)
    return names


# ── LLM judge (Stage 2) ──────────────────────────────────────────────────────

def extract_json_object(raw: str) -> str:
    """Port of NashSU sweep-reviews.ts ``extractJsonObject``.

    Extract the first balanced ``{...}`` object from an LLM response, handling
    bare JSON, ```json fences, and prose-wrapped objects via a brace-depth
    walk that respects strings/escapes. Returns "" if none is found.
    """
    text = raw.strip()
    # Strip an opening ```json or ``` fence (with or without newline).
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    # Strip a trailing ``` fence.
    text = re.sub(r"\s*```\s*$", "", text, flags=re.IGNORECASE)
    text = text.strip()

    start = text.find("{")
    if start == -1:
        return ""

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _build_judge_prompt(batch: List[Dict],
                        pages: List[Tuple[str, Optional[str]]]) -> Tuple[str, str]:
    """Build the (system, user) judge prompt — verbatim port of NashSU
    sweep-reviews.ts ``judgeBatch`` prompt text."""
    capped = pages[:MAX_PAGES_IN_PROMPT]
    page_list = "\n".join(
        f"- {pid}  (title: {title})" if title else f"- {pid}"
        for pid, title in capped
    )
    review_lines = []
    for r in batch:
        affected_list = r.get("affected_pages") or []
        affected = f" | affected: {', '.join(affected_list)}" if affected_list else ""
        desc_val = r.get("description") or ""
        desc = f" — {desc_val[:200]}" if desc_val else ""
        review_lines.append(
            f"- id={r['review_id']} [{r['type']}] \"{r['title']}\"{desc}{affected}"
        )
    review_list = "\n".join(review_lines)

    system_prompt = "You are cleaning up a stale review queue for a personal wiki."
    user_content = "\n".join([
        "After recent ingests, some review items may no longer be valid because the missing page now exists, the duplicate was resolved, or the referenced concept has been added.",
        "",
        "Current wiki pages (filename, optional title):",
        page_list or "(no pages yet)",
        "",
        "Pending review items to judge:",
        review_list,
        "",
        "For each review item, decide whether the underlying condition has been RESOLVED by the current wiki state.",
        "Be conservative: only mark as resolved if you are confident the concern no longer applies.",
        "For contradictions, confirmations, or human-judgment items, default to keeping them pending.",
        "",
        'Respond with ONLY a JSON object in this exact shape: {"resolved": ["id1", "id2"]}',
        'If none of the items are resolved, return exactly: {"resolved": []}',
        "Do not wrap in markdown fences. Do not add commentary.",
    ])
    return system_prompt, user_content


def parse_judge_response(raw: str, batch: List[Dict]) -> Set[str]:
    """Port of NashSU sweep-reviews.ts ``judgeBatch`` response handling.

    Parse a judge response into the set of resolved ids, restricted to ids
    actually in the batch. Conservative: returns empty set on any parse error.
    """
    if not raw.strip():
        return set()
    cleaned = extract_json_object(raw)
    if not cleaned:
        return set()
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return set()
    resolved_raw = parsed.get("resolved") if isinstance(parsed, dict) else None
    if not isinstance(resolved_raw, list):
        return set()
    valid_ids = {r["review_id"] for r in batch if r.get("review_id")}
    return {rid for rid in resolved_raw if isinstance(rid, str) and rid in valid_ids}


def _llm_judge_reviews(pending: List[Dict],
                       pages: List[Tuple[str, Optional[str]]],
                       runtime_dir: Path) -> Set[str]:
    """Port of NashSU sweep-reviews.ts ``llmJudgeReviews``.

    Judge still-pending items in batches of JUDGE_BATCH_SIZE, capped at
    MAX_JUDGE_BATCHES, breaking early if a batch resolves nothing.

    Conversation mode (the only text-gen path): each batch is one handoff —
    on cache miss, ``make_conversation_llm_call`` writes a prompt file and
    raises ConversationPending (propagated → exit 101). The content-hashed slug
    makes each batch independently resumable across re-invokes.

    Items without a stable ``review_id`` are skipped (the judge keys on id).
    """
    from _core import ConversationPending
    from _llm_call import make_conversation_llm_call

    resolved: Set[str] = set()
    judgeable = [r for r in pending if r.get("review_id")]
    if not judgeable:
        return resolved

    llm_call = make_conversation_llm_call(runtime_dir, stage_prefix="review-judge")
    remaining = list(judgeable)
    batches = 0
    while remaining and batches < MAX_JUDGE_BATCHES:
        batch = remaining[:JUDGE_BATCH_SIZE]
        remaining = remaining[JUDGE_BATCH_SIZE:]
        system, user = _build_judge_prompt(batch, pages)
        try:
            raw = llm_call(system, user)
        except ConversationPending:
            # Propagate so the CLI returns 101 and the agent answers + resumes.
            raise
        batch_resolved = parse_judge_response(raw, batch)
        batches += 1
        if not batch_resolved:
            # Nothing resolved — further batches likely the same. Stop early.
            break
        resolved |= batch_resolved
    return resolved


def _resolve_review(review: Dict, reason: str, dry_run: bool = True) -> bool:
    """Mark a review item as resolved by updating its frontmatter."""
    if dry_run:
        return True

    path = review["path"]
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        print(f"  x Cannot read {path}")
        return False

    today = time.strftime("%Y-%m-%d")
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm_text = content[3:end]
            body = content[end + 4:]
            fm_lines = fm_text.split("\n")
            new_lines = []
            has_resolved = False
            has_resolved_at = False
            for line in fm_lines:
                if line.strip().startswith("resolved:"):
                    new_lines.append(line.replace("false", "true").replace("False", "true"))
                    has_resolved = True
                elif line.strip().startswith("resolved_at:"):
                    new_lines.append(f"resolved_at: {today}")
                    has_resolved_at = True
                elif line.strip().startswith("resolved_reason:"):
                    new_lines.append(f'resolved_reason: "{reason}"')
                else:
                    new_lines.append(line)
            if not has_resolved:
                new_lines.append("resolved: true")
            if not has_resolved_at:
                new_lines.append(f"resolved_at: {today}")
            has_reason = any(l.strip().startswith("resolved_reason:") for l in new_lines)
            if not has_reason:
                new_lines.append(f'resolved_reason: "{reason}"')
            new_content = "---\n" + "\n".join(new_lines) + "\n---" + body
            try:
                path.write_text(new_content, encoding="utf-8")
                return True
            except Exception:
                return False
    return False


def _apply_rule_stage(reviews: List[Dict],
                      index: Dict[str, Set[str]]) -> Tuple[List[Dict], List[Dict]]:
    """Stage 1 (rule-based) dispatch — NashSU sweep-reviews.ts conservative rules.

    Only ``missing-page`` and ``duplicate`` are auto-resolvable by rules:
      - missing-page: resolves when ANY extracted candidate name now exists
        (exact match).
      - duplicate: resolves when ANY affected page no longer exists (including
        the all-deleted case) — NashSU: `!allStillExist`.

    contradiction / confirm / suggestion are LEFT PENDING for human judgment
    (the LLM judge may still resolve them in Stage 2).

    Returns (resolved, still_pending) where each resolved item carries a
    "reason" key.
    """
    resolved: List[Dict] = []
    still_pending: List[Dict] = []

    for review in reviews:
        rtype = review.get("type", "unknown")
        reason = None

        if rtype == "missing-page":
            names = _extract_candidate_names(review)
            if names and any(_page_exists(n, index) for n in names):
                reason = "missing page now exists"
        elif rtype == "duplicate":
            # NashSU: resolve when not every affected page still exists (a listed
            # page was merged/deleted). An EMPTY affected list is guarded out
            # below and stays PENDING — only a non-empty list with at least one
            # now-missing page resolves.
            affected = review.get("affected_pages") or []
            if affected:
                def _still(p: str) -> bool:
                    base = str(p).strip().strip("[]").split("/")[-1]
                    if base.endswith(".md"):
                        base = base[:-3]
                    return base.lower() in index["by_id"]
                if not all(_still(p) for p in affected):
                    reason = "duplicate's affected page no longer exists (merged/deleted)"

        if reason is not None:
            item = dict(review)
            item["reason"] = reason
            resolved.append(item)
        else:
            still_pending.append(review)

    return resolved, still_pending


def sweep_reviews(wiki_root: Path, dry_run: bool = True, use_llm: bool = True) -> Dict:
    """Main sweep logic. Returns results dict.

    Two-stage NashSU port:
      Stage 1 — rule-based (missing-page / duplicate only).
      Stage 2 — LLM semantic judge over what's left (conversation mode), unless
                ``use_llm`` is False.
    """
    wiki_dir = wiki_root / "wiki"
    if not wiki_dir.exists():
        return {"error": f"wiki/ not found in {wiki_root}"}

    print(f"=== Review Sweep: {wiki_root.name} ===")
    print(f"Mode: {'dry-run (report only)' if dry_run else 'apply (will modify files)'}")

    # Step 1: Build wiki index (recursive, exact-match)
    print("\n[1/3] Building wiki index...")
    index = _build_wiki_index(wiki_dir)
    print(f"  Indexed {len(index['by_id'])} pages, {len(index['by_title'])} titles")

    # Step 2: Scan pending reviews + dedup on content-stable id (resolved-wins)
    print("\n[2/3] Scanning pending reviews...")
    reviews = _scan_reviews(wiki_dir)
    # NashSU normalizeReviewItems parity: collapse same-content reviews so a
    # resolved twin survives re-ingest (resolved wins, fields unioned).
    reviews = normalize_review_items(reviews)
    reviews = [r for r in reviews if not r.get("resolved")]
    print(f"  Found {len(reviews)} unresolved review items (after content dedup)")

    if not reviews:
        print("\n[ok] No pending reviews — nothing to sweep.")
        return {"total": 0, "resolved": 0, "pending": 0, "details": []}

    # Step 3: Stage 1 rule-based matching
    print("\n[3/3] Applying rule-based matching...")
    rule_resolved, still_pending = _apply_rule_stage(reviews, index)

    # Stage 2: LLM semantic judge on what's left.
    # human_gate exclusion (2026-07-11): review items whose frontmatter sets
    # `human_gate: true` (e.g. orphan-delete candidates from wiki-lint-fix.py)
    # are NEVER sent to the judge. The judge's prompt only shows page ids +
    # titles — it cannot see inbound-link state, so any "resolved" verdict on
    # an orphan-delete item would be a guess; these gates exist precisely so a
    # human decides. Mechanical guarantee, not prompt-dependent.
    human_gated = [r for r in still_pending
                   if str(r.get("frontmatter", {}).get("human_gate", "")).lower()
                   in ("true", "yes", "1")]
    judge_pool = [r for r in still_pending if r not in human_gated]
    llm_resolved: List[Dict] = []
    if human_gated:
        print(f"\n[judge] {len(human_gated)} human-gated item(s) excluded from "
              f"the LLM judge (human_gate: true — human decision only)")
    if use_llm and judge_pool:
        print(f"\n[judge] LLM semantic judge over {len(judge_pool)} pending item(s)...")
        from _paths import detect_runtime_dir
        runtime_dir = detect_runtime_dir(wiki_root)
        pages = _wiki_page_summaries(wiki_dir)
        resolved_ids = _llm_judge_reviews(judge_pool, pages, runtime_dir)
        if resolved_ids:
            kept_pending: List[Dict] = []
            for review in judge_pool:
                if review.get("review_id") in resolved_ids:
                    item = dict(review)
                    item["reason"] = "LLM judged resolved by current wiki state"
                    llm_resolved.append(item)
                else:
                    kept_pending.append(review)
            still_pending = kept_pending + human_gated

    # Apply resolutions to disk (or dry-run accounting)
    all_resolved = rule_resolved + llm_resolved
    applied: List[Dict] = []
    for item in all_resolved:
        if not dry_run:
            if _resolve_review(item, item["reason"], dry_run=False):
                applied.append(item)
                print(f"  [ok] {item['title'][:60]}")
                print(f"     -> {item['reason']}")
            else:
                still_pending.append(item)
        else:
            applied.append(item)
            print(f"  [ok] [DRY RUN] {item['title'][:60]}")
            print(f"     -> {item['reason']}")

    # Report
    print(f"\n{'=' * 50}")
    print(f"Results: {len(reviews)} scanned, {len(applied)} auto-resolved "
          f"({len(rule_resolved)} by rules, {len(llm_resolved)} by LLM), "
          f"{len(still_pending)} pending")
    if still_pending:
        by_type_pending: Dict[str, int] = {}
        for p in still_pending:
            t = p.get("type", "unknown")
            by_type_pending[t] = by_type_pending.get(t, 0) + 1
        print(f"\n[pending] Still pending ({len(still_pending)}):")
        for t, n in sorted(by_type_pending.items()):
            print(f"  {t}: {n} items")
    else:
        by_type_pending = {}

    # NashSU parity: resolved review pages are KEPT (marked `resolved: true`),
    # never deleted. The resolved twin on disk is what lets normalize_review_items
    # apply "resolved wins" so the item stays resolved across re-ingest.

    return {
        "total": len(reviews),
        "resolved": len(applied),
        "rule_resolved": len(rule_resolved),
        "llm_resolved": len(llm_resolved),
        "pending": len(still_pending),
        "details": {
            "resolved": [{"title": r["title"], "reason": r["reason"]} for r in applied],
            "pending_types": by_type_pending,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="sweep_reviews.py — Auto-resolve wiki review items (NashSU parity)"
    )
    parser.add_argument("--project", required=True, help="Path to wiki project root")
    parser.add_argument("--apply", action="store_true", help="Actually resolve (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip the LLM semantic judge stage (pure rule-based)")
    args = parser.parse_args()

    wiki_root = Path(args.project).expanduser().resolve()
    if not wiki_root.exists():
        print(f"Error: project not found: {wiki_root}", file=sys.stderr)
        return 1

    try:
        result = sweep_reviews(wiki_root, dry_run=not args.apply, use_llm=not args.no_llm)
    except BaseException as exc:
        # ConversationPending (BaseException subclass) → exit 101 so the agent
        # answers the judge prompt and re-invokes (NashSU conversation handoff).
        if type(exc).__name__ == "ConversationPending":
            return 101
        raise

    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if "error" in result:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
