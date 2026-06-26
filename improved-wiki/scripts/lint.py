#!/usr/bin/env python3
"""lint.py — Global wiki consistency checks (Phase 2 of NashSU refactor).

Thin CLI wrapper over ``_lint_suggest.run_structural_lint`` — the faithful
port of NashSU ``lint.ts`` (orphan / broken-link / no-outlinks, each with a
deterministic fix suggestion). The old in-house stub here only scanned
``concepts/`` + ``entities/``, was case-sensitive, and treated ``[stem]``
markdown links as wikilinks; it has been replaced by the real engine.

For auto-fixing the findings this surfaces, see ``wiki-lint-fix.py``.
For LLM semantic lint, see ``wiki-lint-semantic.py``.

Usage:
    python3 lint.py
    python3 lint.py --wiki-root ~/Documents/知识库/HardwareWiki
"""
import argparse
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))

from _lint_suggest import run_structural_lint  # noqa: E402

# NashSU parity: only index.md and log.md excluded (mirror _lint_suggest +
# wiki-lint.sh). overview.md is scanned like any content page.
_ANCHOR_FILES = {"index.md", "log.md"}


def collect_pages(wiki_root: Path) -> list[tuple[str, str]]:
    """Walk wiki/ and return [(short_name, content), ...] for every .md page
    except the anchor aggregates. short_name is relative to wiki_root."""
    pages: list[tuple[str, str]] = []
    if not wiki_root.is_dir():
        return pages
    for path in sorted(wiki_root.rglob("*.md")):
        if path.name in _ANCHOR_FILES:
            continue
        rel = path.relative_to(wiki_root)
        try:
            pages.append((str(rel), path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return pages


def lint_wiki(wiki_root: Path) -> list[dict]:
    """Run structural lint over wiki/ and return the finding list.

    Each finding: {type, severity, page, detail, broken_target?,
    suggested_target?, suggested_source?}.
    """
    pages = collect_pages(wiki_root)
    return run_structural_lint(pages, with_suggestions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wiki-wide structural lint")
    parser.add_argument("--wiki-root", type=Path, help="Wiki root directory")
    args = parser.parse_args()

    wiki_root = args.wiki_root or (Path.cwd() / "wiki")
    if not wiki_root.exists():
        print(f"❌ Wiki root not found: {wiki_root}")
        return 1

    print(f"🔍 Lint: Global Wiki Consistency Check")
    print(f"  Wiki: {wiki_root}")
    print()

    findings = lint_wiki(wiki_root)
    by_type: dict[str, list[dict]] = {}
    for f in findings:
        by_type.setdefault(f["type"], []).append(f)

    if not findings:
        print("✅ All checks passed!")
        return 0

    labels = {
        "broken-link": "broken references",
        "orphan": "orphan pages (no incoming links)",
        "no-outlinks": "pages with no outgoing links",
    }
    for kind in ("broken-link", "orphan", "no-outlinks"):
        items = by_type.get(kind, [])
        if not items:
            continue
        print(f"⚠️  Found {len(items)} {labels[kind]}:")
        for f in items:
            suggestion = f.get("suggested_target") or f.get("suggested_source")
            sugg = f" → suggest: {suggestion}" if suggestion else ""
            if kind == "broken-link":
                print(f"    - {f['page']}: [[{f.get('broken_target', '')}]]{sugg}")
            else:
                print(f"    - {f['page']}{sugg}")
        print()

    print(f"Total: {len(findings)} finding(s). Run `wiki-lint-fix.py` to auto-fix.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
