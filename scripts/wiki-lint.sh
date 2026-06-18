#!/bin/bash
# wiki-lint.sh — Mechanical scan of wiki/ for structural problems.
#
# Detects 6 categories of issues:
#   1. broken-link        — [[wikilink]] points to a non-existent page
#   2. orphan              — page no other page links to
#   3. no-outlinks         — page has no outbound [[wikilink]]s
#   4. missing-frontmatter — page lacks the required YAML block
#   5. missing-domain      — concept/entity page lacks 'domain' in frontmatter
#   6. invalid-domain      — domain value not in allowed list (see references/domains.md)
#
# Plus a 5th category when --semantic is passed:
#   5. semantic       — LLM-driven contradiction/stale/missing-page/suggestion
#
# Output:
#   - wiki/lint/*.md              — human-browsable lint pages (each finding one .md)
#                                    Frontmatter: resolved: false → true to mark fixed
#   - Cleanup: resolved-lint pages auto-deleted on next run
#   - wiki/.lint-cache.json       — JSON array (for tooling)
#   - stdout: summary line
#
# Usage:
#   $ ./wiki-lint.sh                            # scan + write wiki/lint/ pages
#   $ ./wiki-lint.sh --verbose                  # show every finding
#   $ ./wiki-lint.sh --summary                  # one-line summary only
#   $ ./wiki-lint.sh --strict                   # exit 1 for critical issues
#   $ ./wiki-lint.sh --semantic                 # also run LLM semantic lint
#   $ ./wiki-lint.sh --fix                      # auto-fix: missing-frontmatter, missing-domain
#   $ ./wiki-lint.sh --json-only                # old behavior: JSON only, no wiki/lint/ pages
#
# Configuration via env:
#   IMPROVED_WIKI_ROOT — path to project root (default: cwd)
#   LLM_API_KEY        — required for --semantic
#   LLM_BASE_URL       — default https://api.minimaxi.com
#   LLM_MODEL          — default MiniMax-M3
#
# Exit code:
#   0 — clean (or with findings but no --strict)
#   1 — broken-link or missing-frontmatter found (only with --strict)
#   2 — script error

set -uo pipefail

WIKI_ROOT="${IMPROVED_WIKI_ROOT:-$(pwd)}"
WIKI_DIR="$WIKI_ROOT/wiki"
export WIKI_DIR
LINT_PAGES_DIR="$WIKI_DIR/lint"
# Detect runtime dir (NashSU-aligned: .llm-wiki/)
# Auto-migrate from .iwiki-runtime/ if it still exists
if [ -d "$WIKI_ROOT/.iwiki-runtime" ]; then
    echo "[lint] Migrating .iwiki-runtime/ → .llm-wiki/" >&2
    mkdir -p "$WIKI_ROOT/.llm-wiki"
    mv "$WIKI_ROOT/.iwiki-runtime"/* "$WIKI_ROOT/.llm-wiki/" 2>/dev/null || true
    rmdir "$WIKI_ROOT/.iwiki-runtime" 2>/dev/null || true
fi
if [ -f "$WIKI_DIR/.ingest-cache.json" ] || [ -f "$WIKI_DIR/ingest-cache.json" ] || [ -d "$WIKI_DIR/.extract-tmp" ] || [ -d "$WIKI_DIR/extract-tmp" ] || [ -d "$WIKI_DIR/.ingest-progress" ] || [ -d "$WIKI_DIR/ingest-progress" ]; then
    RUNTIME_DIR="$WIKI_DIR"  # legacy layout
else
    RUNTIME_DIR="$WIKI_ROOT/.llm-wiki"
fi
mkdir -p "$RUNTIME_DIR"
LINT_CACHE="$RUNTIME_DIR/lint-cache.json"
LINT_LOCK="$RUNTIME_DIR/lint-lock"
# Semantic cache: prefer runtime dir, fall back to .llm-wiki (backward compat)
if [ -d "$WIKI_ROOT/.llm-wiki" ] && [ ! -d "$RUNTIME_DIR" ]; then
  SEMANTIC_CACHE="$WIKI_ROOT/.llm-wiki/lint-semantic.json"
else
  SEMANTIC_CACHE="$RUNTIME_DIR/lint-semantic.json"
fi

VERBOSE=false
SUMMARY=false
STRICT=false
SEMANTIC=false
AUTO_FIX=false
JSON_ONLY=false
SEMANTIC_LIMIT=""
SEMANTIC_TOKENS=""
SEM_ARGS=()
for arg in "$@"; do
  case $arg in
    --verbose|-v) VERBOSE=true ;;
    --summary)    SUMMARY=true ;;
    --strict)     STRICT=true ;;
    --semantic)   SEMANTIC=true ;;
    --fix)        AUTO_FIX=true ;;
    --json-only)  JSON_ONLY=true ;;
    --semantic-limit=*) SEMANTIC_LIMIT="${arg#*=}" ;;
    --semantic-tokens=*) SEMANTIC_TOKENS="${arg#*=}" ;;
    --help|-h)
      grep -E "^#( |!)" "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -d "$WIKI_DIR" ]; then
  echo "ERROR: wiki/ does not exist under $WIKI_ROOT" >&2
  exit 2
fi

# Acquire lock to avoid concurrent runs
if [ -e "$LINT_LOCK" ]; then
  if pgrep -f "wiki-lint.sh" >/dev/null; then
    echo "[lint] Another instance running, exiting." >&2
    exit 0
  else
    rm -f "$LINT_LOCK"
  fi
fi
trap 'rm -f "$LINT_LOCK"' EXIT
touch "$LINT_LOCK"

# ---------- Cleanup: remove resolved lint pages ----------
if [ "$JSON_ONLY" != true ] && [ -d "$LINT_PAGES_DIR" ]; then
  REMOVED=0
  for f in "$LINT_PAGES_DIR"/*.md; do
    [ -f "$f" ] || continue
    if grep -q '^resolved:\s*true\s*$' "$f"; then
      rm -f "$f"
      REMOVED=$((REMOVED + 1))
    fi
  done
  if [ "$REMOVED" -gt 0 ]; then
    echo "[lint] Cleaned $REMOVED resolved lint page(s)"
  fi
fi

# ---------- Run the python linter ----------
# All the heavy lifting is in Python for clean CJK handling.
# Use a temp .py file rather than heredoc for maximum compatibility with
# bash 3.x and to avoid heredoc/redirection ordering bugs.
LINT_SCRIPT=$(mktemp -t wiki-lint-XXXXXX.py)
trap "rm -f '$LINT_SCRIPT' '$LINT_CACHE.tmp' '$LINT_CACHE.tmp.err'" EXIT

cat > "$LINT_SCRIPT" <<'PYEOF'
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

wiki_dir = Path(os.environ["WIKI_DIR"])
findings: list[dict] = []
now_ms = int(time.time() * 1000)

# 1. Collect all wiki pages and build a dual-index slug map.
#    Matches NashSU app's runStructuralLint() (src/lib/lint.ts L46-65):
#      - Case-insensitive resolution: [[Transformer]] matches transformer.md
#      - Dual indexing: both the relative path AND the basename are indexed,
#        so short wikilinks like [[foo]] resolve when the file is
#        entities/foo.md (and full-path [[entities/foo]] also works).
STATE_SKIP = {"lint-cache.json", "ingest-cache.json", "ingest-queue.json", "ingest-lock"}
ANCHOR_FILES = {"index.md", "log.md"}  # NashSU parity: only these 2 excluded from structural lint

slug_map: dict[str, str] = {}        # lowercased key -> original stem
pages: dict[str, Path] = {}          # original stem -> Path
for path in sorted(wiki_dir.rglob("*.md")):
    rel = path.relative_to(wiki_dir)
    if rel.name in STATE_SKIP or rel.name in ANCHOR_FILES:
        continue
    # Skip lint/ directory — scanning lint pages creates infinite feedback
    # (lint pages reference other lint pages via [[wikilinks]])
    if rel.parts[0] == "lint":
        continue
    rel_stem = str(rel.with_suffix(""))       # e.g. "entities/foo-bar"
    basename_stem = path.stem                  # e.g. "foo-bar"
    pages[rel_stem] = path
    # NashSU parity: last-write-wins (Map.set), not first-write-wins
    slug_map[rel_stem.lower()] = rel_stem
    slug_map[basename_stem.lower()] = rel_stem

def resolve_slug(target: str) -> Optional[str]:
    """Case-insensitive wikilink resolution (NashSU parity). Returns the
    original (non-lowercased) stem if the target exists, else None."""
    if target in pages:
        return target
    return slug_map.get(target.lower())

# 2. Scan for [[wikilinks]] in every page
WIKILINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|[^\]]+)?\]\]")
out_links: dict[str, set[str]] = {stem: set() for stem in pages}
broken_links: list[dict] = []

for stem, path in pages.items():
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        findings.append({
            "type": "read-error",
            "severity": "warning",
            "page": str(path.relative_to(wiki_dir)),
            "detail": f"Could not read: {e}",
            "id": f"lint-read-{stem}",
            "createdAt": now_ms,
        })
        continue

    for m in WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        out_links[stem].add(target)
        if resolve_slug(target) is None:
            broken_links.append({
                "type": "broken-link",
                "severity": "warning",
                "page": str(path.relative_to(wiki_dir)),
                "detail": f"Broken link: [[{target}]] — target page not found.",
                "id": f"lint-bl-{stem}-{len(broken_links)}",
                "createdAt": now_ms,
            })

# 3. Compute in-links (inverse of out-links, case-insensitive resolution)
in_links: dict[str, set[str]] = {stem: set() for stem in pages}
for src, targets in out_links.items():
    for tgt in targets:
        resolved = resolve_slug(tgt)
        if resolved is not None and resolved in in_links:
            in_links[resolved].add(src)

# 4. Find orphans (no in-links) — NashSU parity: no frontmatter/char filters
for stem, path in pages.items():
    if in_links[stem]:
        continue
    try:
        path.read_text(encoding="utf-8")
    except Exception:
        continue
    findings.append({
        "type": "orphan",
        "severity": "info",
        "page": str(path.relative_to(wiki_dir)),
        "detail": "No other pages link to this page.",
        "id": f"lint-orphan-{stem}",
        "createdAt": now_ms,
    })

# 5. Find no-outlinks — NashSU parity: no frontmatter/char filters
for stem, path in pages.items():
    if out_links[stem]:
        continue
    try:
        path.read_text(encoding="utf-8")
    except Exception:
        continue
    findings.append({
        "type": "no-outlinks",
        "severity": "info",
        "page": str(path.relative_to(wiki_dir)),
        "detail": "This page has no [[wikilink]] references to other pages.",
        "id": f"lint-nol-{stem}",
        "createdAt": now_ms,
    })

# 6. Find missing frontmatter
for stem, path in pages.items():
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    if not re.match(r"^---\s*\n", text):
        findings.append({
            "type": "missing-frontmatter",
            "severity": "error",
            "page": str(path.relative_to(wiki_dir)),
            "detail": "Page has no YAML frontmatter block (must start with ---).",
            "id": f"lint-mf-{stem}",
            "createdAt": now_ms,
        })

# 7. Find pages with missing or invalid 'domain' field
#    Domain is required for concept and entity pages (Plan B: disambiguation).
#    Source/comparison/synthesis/etc pages are exempt.
VALID_DOMAINS = {
    "circuit-fundamentals", "power-electronics", "thermal-management",
    "emc", "signal-integrity", "digital-circuits", "pcb-design",
    "rf-microwave", "radar-systems", "analog-circuits",
    "semiconductor-devices", "reliability-engineering", "general",
    "computer-architecture", "manufacturing", "packaging",
    "electronics", "hardware", "organization",
    # Expanded from HardwareWiki lint analysis (2026-06-17)
    "physics", "analog", "mathematics", "electrical-engineering",
    "analog-circuit-design", "digital-design", "hardware-design",
    "power-integrity", "signals-and-systems", "control-systems",
    "high-speed-serial-io", "standards", "system", "systems",
    "engineering", "organizations", "test-and-measurement",
    "embedded-systems", "motor-control", "magnetic-design",
}

def _normalize_domain(d: str) -> str:
    """Normalize domain strings: lowercase, spaces→dashes, common aliases."""
    d = d.strip().strip('"').strip("'").lower()
    d = d.replace(" ", "-").replace("_", "-")
    # Common LLM-generated aliases → canonical
    aliases = {
        "rf-and-microwave-engineering": "rf-microwave",
        "analog-electronics": "analog-circuits",
        "digital-electronics": "digital-circuits",
        "people": "general",
        "person": "general",
        "power": "power-electronics",
        "thermal": "thermal-management",
        "signal": "signal-integrity",
        "pcb": "pcb-design",
    }
    return aliases.get(d, d)
DOMAIN_APPLICABLE_DIRS = {"concepts", "entities"}
for stem, path in pages.items():
    # Determine page directory from relative path
    rel = str(path.relative_to(wiki_dir))
    page_dir = rel.split("/")[0] if "/" in rel else ""
    if page_dir not in DOMAIN_APPLICABLE_DIRS:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not fm_match:
        continue  # already caught by missing-frontmatter
    fm_text = fm_match.group(1)
    # Also extract page type from frontmatter to double-check
    page_type = ""
    for line in fm_text.split("\n"):
        m = re.match(r'type:\s*(\S+)', line)
        if m:
            page_type = m.group(1).strip()
    # Only concept and entity pages need domain
    if page_type not in ("concept", "entity"):
        continue
    domain_found = None
    for line in fm_text.split("\n"):
        m = re.match(r'domain:\s*(\S+)', line)
        if m:
            domain_found = m.group(1).strip()
            break
    if domain_found is None:
        findings.append({
            "type": "missing-domain",
            "severity": "warning",
            "page": str(path.relative_to(wiki_dir)),
            "detail": "Concept/entity page missing 'domain' field in frontmatter. Add 'domain: <domain-slug>' (see references/domains.md).",
            "id": f"lint-md-{stem}",
            "createdAt": now_ms,
        })
    elif _normalize_domain(domain_found) not in VALID_DOMAINS:
        findings.append({
            "type": "invalid-domain",
            "severity": "warning",
            "page": str(path.relative_to(wiki_dir)),
            "detail": f"Domain '{domain_found}' is not in the allowed list. Valid domains: {', '.join(sorted(VALID_DOMAINS))}.",
            "id": f"lint-id-{stem}",
            "createdAt": now_ms,
        })

# 7. Add broken-links last
findings.extend(broken_links)

print(json.dumps(findings, ensure_ascii=False, indent=2))
PYEOF

python3 "$LINT_SCRIPT" > "$LINT_CACHE.tmp"

# Atomic move
mv "$LINT_CACHE.tmp" "$LINT_CACHE"

# ---------- Summarize ----------
SUMMARY_LINE=$(python3 -c "
import json
from collections import Counter
findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
c = Counter(f['type'] for f in findings)
total = sum(c.values())
parts = [f'{total} findings', f'broken-link: {c.get(\"broken-link\", 0)}', f'orphan: {c.get(\"orphan\", 0)}', f'no-outlinks: {c.get(\"no-outlinks\", 0)}', f'missing-frontmatter: {c.get(\"missing-frontmatter\", 0)}', f'missing-domain: {c.get(\"missing-domain\", 0)}', f'invalid-domain: {c.get(\"invalid-domain\", 0)}']
print(' | '.join(parts))
")

echo "[lint] $SUMMARY_LINE"

# ---------- Write lint pages to wiki/lint/ (human-browsable) ----------
if [ "$JSON_ONLY" != true ]; then
  mkdir -p "$LINT_PAGES_DIR"

  # Delete old lint pages to regenerate with latest scan results
  # (lint pages regenerated below)
  # Remove all existing unresolved lint pages (they get regenerated)
  for old_f in "$LINT_PAGES_DIR"/*.md; do
    [ -f "$old_f" ] || continue
    # Keep resolved pages (they will have been cleaned above)
    rm -f "$old_f"
  done

  # Write one .md per finding
  python3 -c "
import json, os, time, re
from pathlib import Path

findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
lint_dir = Path(os.environ.get('LINT_PAGES_DIR', '$WIKI_DIR/lint'))
lint_dir.mkdir(parents=True, exist_ok=True)
date_str = time.strftime('%Y-%m-%d')

severity_icon = {'error': '❌', 'warning': '⚠️', 'info': 'ℹ️'}
written = 0
# Track filename counts to dedup (broken-link can have multiple per page)
fname_counts = {}
for f in findings:
    ftype = f.get('type', 'unknown')
    severity = f.get('severity', 'info')
    page_ref = f.get('page', 'unknown')
    detail = f.get('detail', '')
    icon = severity_icon.get(severity, 'ℹ️')

    # Safe filename: ftype-page[-NN]
    safe_type = re.sub(r'[^\w-]', '', ftype)[:30]
    safe_page = re.sub(r'[^\w\.\-一-鿿]', '-', page_ref)[:40]
    safe_page = safe_page.replace('.md', '')
    base_name = f'{safe_type}-{safe_page}'
    base_name = re.sub(r'-{2,}', '-', base_name)
    n = fname_counts.get(base_name, 0) + 1
    fname_counts[base_name] = n
    if n > 1:
        filename = f'{base_name}-{n:02d}.md'
    else:
        filename = f'{base_name}.md'

    page_path = lint_dir / filename

    # Frontmatter
    fm = f'''---
type: lint
lint_type: {ftype}
severity: {severity}
page: \"{page_ref}\"
resolved: false
created: {date_str}
---

# {icon} [{ftype}] {page_ref}

{detail}

## Resolution
_修复后，将 frontmatter 中 \`resolved: false\` 改为 \`resolved: true\`，下次 lint 时自动清理。_
'''
    tmp = page_path.with_suffix(page_path.suffix + '.tmp')
    tmp.write_text(fm, encoding='utf-8')
    tmp.rename(page_path)
    written += 1

print(f'[lint] {written} lint pages → {lint_dir}')
"
  LINT_PAGE_COUNT=$(ls "$LINT_PAGES_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
  echo "[lint] Pages: $LINT_PAGE_COUNT findings in $LINT_PAGES_DIR/"
fi

# ---------- Optional: semantic lint (LLM-driven) ----------
if [ "$SEMANTIC" = true ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SEM_ARGS=()
  [ -n "$SEMANTIC_LIMIT" ]  && SEM_ARGS+=(--limit "$SEMANTIC_LIMIT")
  [ -n "$SEMANTIC_TOKENS" ] && SEM_ARGS+=(--max-tokens "$SEMANTIC_TOKENS")
  if [ -z "${LLM_API_KEY:-}" ] && [ -z "${MINIMAX_CN_API_KEY:-}" ]; then
    echo "[lint] --semantic: LLM_API_KEY not set, skipping (export it then re-run)" >&2
  else
    export LLM_API_KEY="${LLM_API_KEY:-$MINIMAX_CN_API_KEY}"
    echo "[lint] --semantic: running LLM semantic pass ..."
    IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/wiki-lint-semantic.py" "${SEM_ARGS[@]}" || \
      echo "[lint] --semantic: sub-script exited non-zero, continuing" >&2
  fi
fi

# ---------- Combined summary line (with --semantic) ----------
if [ "$SEMANTIC" = true ] && [ -e "$SEMANTIC_CACHE" ]; then
  SUMMARY_LINE=$(python3 -c "
import json, os
from collections import Counter
findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
if os.path.exists('$SEMANTIC_CACHE'):
    findings += json.load(open('$SEMANTIC_CACHE', 'r', encoding='utf-8'))
c = Counter(f['type'] for f in findings)
total = sum(c.values())
parts = [f'{total} findings',
         f'broken-link: {c.get(\"broken-link\", 0)}',
         f'orphan: {c.get(\"orphan\", 0)}',
         f'no-outlinks: {c.get(\"no-outlinks\", 0)}',
         f'missing-frontmatter: {c.get(\"missing-frontmatter\", 0)}',
         f'missing-domain: {c.get(\"missing-domain\", 0)}',
         f'invalid-domain: {c.get(\"invalid-domain\", 0)}',
         f'semantic: {c.get(\"semantic\", 0)}']
print(' | '.join(parts))
")
  echo "[lint+semantic] $SUMMARY_LINE"
fi

if [ "$VERBOSE" = true ]; then
  python3 -c "
import json, os
findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
if os.path.exists('$SEMANTIC_CACHE'):
    findings += json.load(open('$SEMANTIC_CACHE', 'r', encoding='utf-8'))
for f in findings:
    print(f\"  [{f['severity']:7}] {f['type']:20} {f['page']:50} {f['detail'][:120]}\")
"
fi

# ---------- Exit code (with --strict) ----------
if [ "$STRICT" = true ]; then
  HAS_ERRORS=$(python3 -c "
import json
findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
errors = sum(1 for f in findings if f['type'] in ('broken-link', 'missing-frontmatter', 'missing-domain'))
print(errors)
")
  if [ "$HAS_ERRORS" != "0" ]; then
    echo "[lint] --strict: $HAS_ERRORS critical issues found" >&2
    exit 1
  fi
fi

# ── Auto-fix (NashSU lint-fixes.ts parity) ──
if [ "$AUTO_FIX" = true ]; then
  FIXED=0
  echo "[lint] Auto-fix: repairing missing-domain and missing-frontmatter..."
  TIMESTAMP=$(date +%Y-%m-%d)

  python3 << PYEOF
import json, re, pathlib
cache = json.loads(open('${LINT_CACHE}', 'r'))
fixed = 0
for f in cache:
    path = pathlib.Path(f.get('path', ''))
    if not path.exists():
        continue
    t = f.get('type', '')
    if t == 'missing-domain':
        text = path.read_text()
        if 'domain:' not in text[:500]:
            text = re.sub(r'(^title:.+$)', r'\1\ndomain: general', text, count=1, flags=re.MULTILINE)
            path.write_text(text)
            fixed += 1
    elif t == 'missing-frontmatter':
        text = path.read_text()
        if not text.startswith('---'):
            fm = f'---\ntype: concept\ntitle: "{path.stem}"\ndomain: general\ncreated: {timestamp}\nupdated: {timestamp}\ntags: []\nrelated: []\n---\n\n'
            path.write_text(fm + text)
            fixed += 1
print(fixed)
PYEOF
  FIXED=$?
  echo "[lint] Auto-fix: repaired $FIXED issues"
fi

exit 0
