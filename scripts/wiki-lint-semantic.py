#!/usr/bin/env python3
"""
wiki-lint-semantic.py — LLM-driven semantic lint for a wiki/.

This is improved-wiki's port of NashSU's runSemanticLint() from
src/lib/lint.ts (latest version; v0.5.2 runSemanticLint ~L305, excludes
only log.md). It scans every page's first 500
chars + frontmatter, sends the concatenated summaries to an LLM, and
parses ---LINT:type|severity|title--- blocks back into findings.

Findings carry type="semantic" (matching NashSU's 4th structural-lint
type), with the raw type (contradiction / stale / missing-page /
suggestion) preserved in the detail string. affectedPages is parsed
from an optional "PAGES: a, b" line in the body.

Output schema (one item per finding):
  {
    "type": "semantic",
    "severity": "warning" | "info",
    "page": "<title from LINT header>",
    "detail": "[<rawType>] <body minus PAGES line>",
    "affectedPages": ["a.md", "b.md"] | undefined,
    "id": "lint-semantic-<n>",
    "createdAt": <epoch ms>
  }

Config:
  IMPROVED_WIKI_ROOT  project root (default: cwd)

LLM execution: conversation mode only. The semantic
lint is one LLM call; this script writes a prompt file under
<runtime>/conversation/semantic-lint/ and raises ConversationPending (exit
101). The calling agent answers with the current conversation's model, writes
the result, and re-invokes — the script reads the cached result and writes
lint-semantic.json. No external LLM API key is needed (text generation is
conversation-only).

Usage:
  ./wiki-lint-semantic.py              # scan and write lint-semantic.json
  ./wiki-lint-semantic.py --dry-run    # print prompt + summaries, no LLM call
  ./wiki-lint-semantic.py --limit 50   # cap pages sampled (for huge wikis)

Exit codes: 0 done; 101 conversation pending (agent answers + re-invokes);
2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# ── constants (verbatim from NashSU lint.ts L161-162) ────────────────────────
LINT_BLOCK_REGEX = re.compile(
    r"---LINT:\s*([^\n|]+?)\s*\|\s*([^\n|]+?)\s*\|\s*([^\n-]+?)\s*---\n"
    r"([\s\S]*?)---END LINT---"
)

# NashSU parity: only log.md excluded from semantic lint (lint.ts L188)
ANCHOR_FILES = {"log.md"}
STATE_FILES = {
    "lint-cache.json", "lint.json",
    "ingest-cache.json",
    "ingest-queue.json",
    "ingest-lock",
    "lint-lock", "lint.lock",
    "lint-semantic.json",  # don't lint our own output
}

# Per-page summary size (NashSU: 500 chars)
SUMMARY_CHARS = 500
# Concatenated sample for language detection (NashSU: 2000 chars)
LANG_SAMPLE_CHARS = 2000
# Batch size: pages per LLM call. A single concatenated call over a 7594-page
# wiki blows the conversation model's context, so summaries are split into
# batches. Each batch is one conversation handoff (exit 101 → agent answers →
# resume → next batch). The slug is content-hashed, so each batch resumes
# independently and the loop is idempotent across re-invokes.
SEMANTIC_BATCH_PAGES = 200


# ── language directive (NashSU parity: _language.detect_language port) ───────────
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
from _language import build_language_directive  # noqa: E402 (titles, descriptions, PAGES list) MUST be in English."
from _core import ConversationPending  # noqa: E402
from _llm_call import make_conversation_llm_call  # noqa: E402


# ── core scan ────────────────────────────────────────────────────────────────
def collect_summaries(wiki_dir: Path, limit: Optional[int] = None) -> list[tuple[str, str]]:
    """Returns [(short_path, summary_text), ...]. Excludes anchors + state files.
    Sorts by relative path for determinism (NashSU parity)."""
    out: list[tuple[str, str]] = []
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki_dir)
        if rel.name in STATE_FILES or rel.name in ANCHOR_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        preview = text[:SUMMARY_CHARS] + ("..." if len(text) > SUMMARY_CHARS else "")
        out.append((str(rel), preview))
        if limit and len(out) >= limit:
            break
    return out


def parse_lint_blocks(raw: str, now_ms: int) -> list[dict]:
    """Parse ---LINT:type|severity|title---\n<body>\n---END LINT--- blocks.
    Mirrors NashSU lint.ts L266-291."""
    results: list[dict] = []
    for n, m in enumerate(LINT_BLOCK_REGEX.finditer(raw)):
        raw_type = m.group(1).strip().lower()
        severity = m.group(2).strip().lower()
        title = m.group(3).strip()
        body = m.group(4).strip()

        # Affected pages (optional PAGES: line)
        pages_match = re.search(r"^PAGES:\s*(.+)$", body, re.MULTILINE)
        affected = (
            [p.strip() for p in pages_match.group(1).split(",")]
            if pages_match
            else None
        )
        detail = re.sub(r"^PAGES:.*$", "", body, flags=re.MULTILINE).strip()

        # Severity coercion (NashSU L286: only "warning" stays warning)
        sev = "warning" if severity == "warning" else "info"

        results.append({
            "type": "semantic",
            "severity": sev,
            "page": title,
            "detail": f"[{raw_type}] {detail}",
            "affectedPages": affected,
            "id": f"lint-semantic-{n}",
            "createdAt": now_ms,
        })
    return results


def build_prompt(summaries: list[tuple[str, str]]) -> tuple[str, str]:
    """Returns (system_prompt, user_content). The system prompt is the
    full task spec; the user content carries the wiki page summaries."""
    lang_directive = build_language_directive(
        "\n".join(p for _, p in summaries)[:LANG_SAMPLE_CHARS]
    )
    system_prompt = (
        "You are a wiki quality analyst. Review the following wiki page summaries and identify issues.\n"
        "\n"
        f"{lang_directive}\n"
        "\n"
        "For each issue, output exactly this format:\n"
        "\n"
        "---LINT: type | severity | Short title---\n"
        "Description of the issue.\n"
        "PAGES: page1.md, page2.md\n"
        "---END LINT---\n"
        "\n"
        "Types:\n"
        "- contradiction: two or more pages make conflicting claims\n"
        "- stale: information that appears outdated or superseded\n"
        "- missing-page: an important concept is heavily referenced but has no dedicated page\n"
        "- suggestion: a question or source worth adding to the wiki\n"
        "- cross-domain-ambiguity: same term (slug) used for different concepts in different domains but not disambiguated — e.g., 'switch' meaning both a mechanical switch (circuit-fundamentals) and a switching transistor (power-electronics) without domain-specific pages\n"
        "- wrong-domain: a page's frontmatter `domain` field does not match its actual content domain\n"
        "\n"
        "Severities:\n"
        "- warning: should be addressed\n"
        "- info: nice to have\n"
        "\n"
        "Only report genuine issues. Do not invent problems. Output ONLY the ---LINT--- blocks, no other text.\n"
        "\n"
        "## Wiki Pages\n"
    )
    user_content = "\n\n".join(
        f"### {path}\n{preview}" for path, preview in summaries
    )
    return system_prompt, user_content


def chunk_batches(summaries: list[tuple[str, str]],
                  batch_pages: int | None = None) -> list[list[tuple[str, str]]]:
    """Split summaries into batches of at most ``batch_pages`` pages each.

    Keeping each LLM call bounded lets the semantic lint scale to large wikis
    (a 7594-page HardwareWiki would otherwise produce a single multi-MB
    prompt). Returns ``[summaries]`` (one batch) when there are fewer pages
    than the batch size — the common small-wiki case.

    ``batch_pages`` defaults to the module global ``SEMANTIC_BATCH_PAGES``
    resolved at call time (not def time) so tests can monkeypatch it.
    """
    if batch_pages is None:
        batch_pages = SEMANTIC_BATCH_PAGES
    if batch_pages <= 0 or len(summaries) <= batch_pages:
        return [summaries]
    return [summaries[i:i + batch_pages]
            for i in range(0, len(summaries), batch_pages)]


def dedup_findings(findings: list[dict]) -> list[dict]:
    """Dedup semantic findings across batches.

    Batches are disjoint by page, but the LLM may emit the same issue keyed
    under different titles or repeat a cross-page contradiction from both
    ends. Dedup key: (lowercased page, raw_type, first 80 chars of detail).
    First occurrence wins.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for f in findings:
        detail = f.get("detail", "")
        raw_type = ""
        m = re.match(r"\[(\w+)\]", detail)
        if m:
            raw_type = m.group(1)
        key = (f.get("page", "").lower(), raw_type, detail[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Print prompt + summary stats, skip LLM call")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of pages sampled (for huge wikis)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output path (default: <state_dir>/lint-semantic.json)")
    args = parser.parse_args()

    root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_dir = root / "wiki"
    if not wiki_dir.is_dir():
        print(f"ERROR: wiki/ not found under {root}", file=sys.stderr)
        return 2

    # State dir resolution (matches ingest.py + validate_ingest.py)
    # Uses _paths.detect_runtime_dir() — .llm-wiki/ default, auto-migrates from .iwiki-runtime/
    _script_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(_script_root))
    from _paths import detect_runtime_dir  # noqa: E402

    state_dir = detect_runtime_dir(root)  # handles all fallback logic
    out_path = Path(args.output) if args.output else state_dir / "lint-semantic.json"

    summaries = collect_summaries(wiki_dir, limit=args.limit)
    if not summaries:
        print(f"[semantic-lint] No wiki pages found in {wiki_dir}", file=sys.stderr)
        # Still write an empty findings file so callers don't error
        out_path.write_text("[]", encoding="utf-8")
        return 0

    print(f"[semantic-lint] Collected {len(summaries)} page summaries")

    system_prompt, user_content = build_prompt(summaries)

    if args.dry_run:
        batches = chunk_batches(summaries)
        print(f"[semantic-lint] DRY-RUN: {len(summaries)} pages in {len(batches)} batch(es) "
              f"(batch size {SEMANTIC_BATCH_PAGES})")
        print(f"[semantic-lint] DRY-RUN: batch 1 would send {len(user_content):,} chars to LLM")
        print(f"  system_prompt: {len(system_prompt):,} chars")
        print(f"  first 500 chars of user_content:\n  {user_content[:500]!r}")
        return 0

    # Conversation mode, batched: each batch is one handoff (write prompt →
    # exit 101 → agent answers → re-invoke → cache hit → next batch). The
    # content-hashed slug makes each batch independently resumable, so the
    # loop is safe to re-enter after every 101.
    now_ms = int(time.time() * 1000)
    llm_call = make_conversation_llm_call(state_dir, stage_prefix="semantic-lint")
    batches = chunk_batches(summaries)
    findings: list[dict] = []
    for i, batch in enumerate(batches, 1):
        batch_system, batch_user = build_prompt(batch)
        try:
            raw = llm_call(batch_system, batch_user)
        except ConversationPending:
            print(f"[semantic-lint] Batch {i}/{len(batches)} pending "
                  f"({len(batch)} pages) — awaiting conversation answer", file=sys.stderr)
            return 101
        batch_findings = parse_lint_blocks(raw, now_ms)
        findings.extend(batch_findings)
        print(f"[semantic-lint] Batch {i}/{len(batches)}: {len(batch_findings)} finding(s) "
              f"from {len(batch)} pages ({len(raw):,} chars raw)")

    findings = dedup_findings(findings)
    # Renumber ids: parse_lint_blocks numbers per-batch from 0, so cross-batch
    # ids collide. Assign stable, unique ids after dedup.
    for n, f in enumerate(findings):
        f["id"] = f"lint-semantic-{n}"
    print(f"[semantic-lint] Parsed {len(findings)} semantic finding(s) "
          f"({len(batches)} batch(es), after dedup)")

    # Atomic write
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(findings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(out_path)
    print(f"[semantic-lint] Wrote {out_path}")

    # Summary
    if findings:
        from collections import Counter
        c = Counter(f["severity"] for f in findings)
        print(f"[semantic-lint] severity: warning={c.get('warning', 0)} info={c.get('info', 0)}")

    # ── Write lint pages to <state_dir>/lint/ (human-browsable, same format as structural) ──
    # Lives under the runtime dir (not wiki/) — derived diagnostic output, not
    # source knowledge; matches wiki-lint.sh's LINT_PAGES_DIR=$RUNTIME_DIR/lint.
    lint_dir = state_dir / "lint"
    lint_dir.mkdir(parents=True, exist_ok=True)
    date_str = time.strftime("%Y-%m-%d")
    severity_icon = {"warning": "⚠️", "info": "ℹ️"}

    written = 0
    fname_counts: dict[str, int] = {}
    for f in findings:
        detail = f.get("detail", "")
        raw_type = ""
        m = re.match(r"\[(\w+)\]\s*", detail)
        if m:
            raw_type = m.group(1)
        sev = f.get("severity", "info")
        icon = severity_icon.get(sev, "ℹ️")
        affected = f.get("affectedPages") or []
        page_ref = f.get("page", "semantic")

        # Safe filename
        safe_type = re.sub(r"[^\w-]", "", raw_type)[:20] if raw_type else "semantic"
        safe_title = re.sub(r"[^\w一-鿿\-]", "-", page_ref)[:50]
        base_name = f"semantic-{safe_type}-{safe_title}"
        base_name = re.sub(r"-{2,}", "-", base_name)
        n = fname_counts.get(base_name, 0) + 1
        fname_counts[base_name] = n
        filename = f"{base_name}-{n:02d}.md" if n > 1 else f"{base_name}.md"

        affected_links = "\n".join(f"- [[{p.replace('.md', '')}]]" for p in affected)

        md = f"""---
type: lint
lint_type: semantic
raw_type: {raw_type}
severity: {sev}
page: "{page_ref}"
affected_pages: [{', '.join(affected)}]
created: {date_str}
---

# {icon} [semantic/{raw_type}] {page_ref}

{detail}

{"## Affected Pages" if affected else ""}
{affected_links}
"""
        page_path = lint_dir / filename
        tmp = page_path.with_suffix(page_path.suffix + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.rename(page_path)
        written += 1

    if written > 0:
        print(f"[semantic-lint] {written} semantic lint pages → {lint_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
