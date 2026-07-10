#!/usr/bin/env python3
"""lint_verify_semantic.py — adversarially re-verify high-severity
semantic-lint findings against FULL page content, before they're trusted.

Why: wiki-lint-semantic.py's LLM judge only ever sees a 500-char preview per
page, batched blind — 200 pages per call, no memory across batches. That's
enough to flag a *candidate* issue but not enough to be sure it's real (the
same reason ingest's Stage 2.4 dedup groups get a fresh subagent's yes/no
before two pages are merged, rather than trusting the first LLM's guess).
This is a standalone follow-up step porting that same discipline to lint
output — like sweep_reviews.py / cross_source_dedup.py, it is NOT wired into
wiki-lint.sh's default chain; run it manually after a lint pass.

Scope: only severity=="warning" findings without a "verified" key yet
(idempotent — safe to re-run, already-verified findings are skipped).
info-severity findings ("nice to have") aren't worth the round-trip.

For each unverified warning, this fetches the FULL content of every page in
its affectedPages list (not the summary the semantic pass saw), batches
several findings into one LLM call, and asks for a confirmed / refuted /
uncertain verdict + one-line reason per finding. Results are written back
into lint-semantic.json (`verified`, `verify_reason` keys added in place) and,
where a matching human-browsable .llm-wiki/lint/*.md page exists, a verdict
note is appended to it.

Conversation mode only (make_conversation_llm_call) — same handoff protocol
(exit 101 / write prompt / agent answers / re-invoke) as the rest of the
pipeline.

Usage:
  python3 lint_verify_semantic.py             # verify all unverified warnings
  python3 lint_verify_semantic.py --project /path/to/wiki
  python3 lint_verify_semantic.py --dry-run   # print what would be sent, no LLM call

Exit codes: 0 done (or nothing to verify); 101 conversation pending; 2 config error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _core import ConversationPending  # noqa: E402
from _paths import detect_runtime_dir, atomic_write  # noqa: E402
from _llm_call import make_conversation_llm_call  # noqa: E402

VERIFY_BATCH_FINDINGS = 8  # full page content is much heavier than a 500-char
                           # summary, so batches are smaller than semantic-lint's 200

# id=<token>--- rather than a |-delimited field: finding ids look like
# "lint-semantic-12" (hyphens are the norm, not the exception). Mirrors the
# 2026-07-10 fix to wiki-lint-semantic.py's LINT_BLOCK_REGEX, which silently
# dropped any title containing a hyphen — don't reintroduce that trap here.
VERIFY_BLOCK_REGEX = re.compile(
    r"---VERIFY id=(\S+?)---\n(.*?)---END VERIFY---", re.DOTALL
)

_VALID_VERDICTS = {"confirmed", "refuted", "uncertain"}


def findings_to_verify(findings: list[dict]) -> list[dict]:
    return [
        f for f in findings
        if f.get("severity") == "warning" and "verified" not in f
    ]


def read_affected_pages(wiki_dir: Path, affected: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for rel in affected:
        p = wiki_dir / rel
        try:
            out[rel] = p.read_text(encoding="utf-8")
        except OSError:
            out[rel] = None
    return out


def _finding_raw_type(finding: dict) -> str:
    m = re.match(r"\[([\w-]+)\]\s*", finding.get("detail", ""))
    return m.group(1) if m else ""


def build_verify_prompt(batch: list[dict], pages_by_rel: dict[str, str | None]) -> tuple[str, str]:
    system = (
        "You are a strict fact-checker for a wiki lint tool. Each FINDING below "
        "was raised by an earlier pass that only saw a short preview of each "
        "page. You are shown the FULL content of every page it cited. Decide "
        "whether the finding actually holds up.\n\n"
        "For each finding, output exactly this format:\n\n"
        "---VERIFY id=<finding id>---\n"
        "VERDICT: confirmed | refuted | uncertain\n"
        "REASON: one sentence, grounded in the page content actually shown above, "
        "in Chinese (中文)\n"
        "---END VERIFY---\n\n"
        "confirmed = the finding is accurate given the full content.\n"
        "refuted = the full content contradicts or does not support the finding "
        "(e.g. the pages turn out to cover genuinely different things).\n"
        "uncertain = the shown content is not enough to decide either way.\n"
        "One block per finding id shown below. Output ONLY the ---VERIFY--- "
        "blocks, no other text."
    )
    parts = []
    for f in batch:
        raw_type = _finding_raw_type(f)
        detail = re.sub(r"^\[[\w-]+\]\s*", "", f.get("detail", ""))
        pages_text = "\n\n".join(
            f"#### {rel}\n{content if content is not None else '(page not found on disk)'}"
            for rel in f.get("affectedPages", [])
            for content in [pages_by_rel.get(rel)]
        )
        parts.append(
            f"### FINDING id={f['id']}\n"
            f"Type: {raw_type}\n"
            f"Claim: {detail}\n\n"
            f"{pages_text}"
        )
    user = "\n\n---\n\n".join(parts)
    return system, user


def parse_verify_blocks(raw: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in VERIFY_BLOCK_REGEX.finditer(raw):
        finding_id = m.group(1).strip()
        body = m.group(2)
        verdict_m = re.search(r"VERDICT:\s*(\S+)", body)
        reason_m = re.search(r"REASON:\s*(.+)", body)
        verdict = verdict_m.group(1).strip().lower() if verdict_m else "uncertain"
        if verdict not in _VALID_VERDICTS:
            verdict = "uncertain"
        reason = reason_m.group(1).strip() if reason_m else ""
        out[finding_id] = {"verdict": verdict, "reason": reason}
    return out


def _annotate_lint_page(lint_dir: Path, finding: dict, verdict: str, reason: str) -> None:
    """Best-effort: append a verification note to the matching human-browsable
    .llm-wiki/lint/*.md page, matched by (raw_type, page) frontmatter — same
    fields wiki-lint-semantic.py stamps when it first writes the page. Silently
    no-ops if no exact match is found (the JSON record is the source of truth;
    this is just a visibility nicety for anyone browsing lint/ directly)."""
    if not lint_dir.is_dir():
        return
    raw_type = _finding_raw_type(finding)
    page_ref = finding.get("page", "")
    icon = {"confirmed": "✅", "refuted": "❌", "uncertain": "❓"}.get(verdict, "❓")
    for md_path in lint_dir.glob("semantic-*.md"):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if not fm_match:
            continue
        fm = fm_match.group(1)
        if (f'raw_type: {raw_type}' not in fm) or (f'page: "{page_ref}"' not in fm):
            continue
        if "## 核验结果" in text:
            return  # already annotated
        note = f"\n## 核验结果\n\n{icon} **{verdict}** — {reason}\n"
        atomic_write(md_path, text.rstrip("\n") + "\n" + note)
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Adversarially re-verify severity=='warning' semantic-lint "
                    "findings against full page content."
    )
    parser.add_argument("--project", default=None,
                        help="Wiki project root (default: IMPROVED_WIKI_ROOT or cwd)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be verified, make no LLM call")
    args = parser.parse_args(argv)

    project_root = Path(args.project or os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_dir = project_root / "wiki"
    runtime = detect_runtime_dir(project_root)
    findings_path = runtime / "lint-semantic.json"

    if not findings_path.exists():
        print("[lint-verify] no lint-semantic.json found — run wiki-lint.sh first.")
        return 0

    findings = json.loads(findings_path.read_text(encoding="utf-8"))
    pending = findings_to_verify(findings)
    if not pending:
        print("[lint-verify] nothing to verify (no unverified warning-severity findings).")
        return 0

    print(f"[lint-verify] {len(pending)} unverified warning-severity finding(s).")
    if args.dry_run:
        for f in pending:
            print(f"  - {f['id']}: {f.get('detail', '')[:80]}")
        return 0

    llm_call = make_conversation_llm_call(runtime, stage_prefix="lint-verify")
    by_id = {f["id"]: f for f in findings}
    batches = [pending[i:i + VERIFY_BATCH_FINDINGS]
               for i in range(0, len(pending), VERIFY_BATCH_FINDINGS)]

    verdicts: dict[str, dict] = {}
    for i, batch in enumerate(batches, 1):
        all_affected = {rel for f in batch for rel in f.get("affectedPages", [])}
        pages_by_rel = read_affected_pages(wiki_dir, sorted(all_affected))
        system, user = build_verify_prompt(batch, pages_by_rel)
        try:
            raw = llm_call(system, user)
        except ConversationPending:
            print(f"[lint-verify] batch {i}/{len(batches)} pending "
                  f"({len(batch)} finding(s)) — awaiting conversation answer",
                  file=sys.stderr)
            return 101
        batch_verdicts = parse_verify_blocks(raw)
        verdicts.update(batch_verdicts)
        print(f"[lint-verify] batch {i}/{len(batches)}: "
              f"{len(batch_verdicts)}/{len(batch)} verdict(s) parsed")

    lint_dir = runtime / "lint"
    for finding_id, v in verdicts.items():
        f = by_id.get(finding_id)
        if f is None:
            continue
        f["verified"] = v["verdict"]
        f["verify_reason"] = v["reason"]
        _annotate_lint_page(lint_dir, f, v["verdict"], v["reason"])

    atomic_write(findings_path, json.dumps(findings, ensure_ascii=False, indent=2))

    from collections import Counter
    c = Counter(v["verdict"] for v in verdicts.values())
    print(f"[lint-verify] done: confirmed={c.get('confirmed', 0)} "
          f"refuted={c.get('refuted', 0)} uncertain={c.get('uncertain', 0)}")
    unresolved = len(pending) - len(verdicts)
    if unresolved:
        print(f"[lint-verify] {unresolved} finding(s) had no parseable verdict "
              f"this run — re-invoke to retry them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
