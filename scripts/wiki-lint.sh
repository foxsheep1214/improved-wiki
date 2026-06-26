#!/bin/bash
# wiki-lint.sh — Mechanical scan of wiki/ for structural problems.
#
# Lint phases (mirrors Ingest's Phase convention):
#   Phase 0 · 前置检查     — lock + cleanup resolved lint pages
#   Phase 1 · 结构扫描     — collect pages, run structural detection (6 categories)
#   Phase 2 · 语义扫描     — optional LLM semantic lint (--semantic)
#   Phase 3 · 写入         — write .lint-cache.json + .llm-wiki/lint/*.md + summary
#   Phase 4 · 自动修复     — optional --fix / --fix-links
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
#   - .llm-wiki/lint/*.md         — human-browsable lint pages (each finding one .md)
#                                    Frontmatter: resolved: false → true to mark fixed
#   - Cleanup: resolved-lint pages auto-deleted on next run
#   - .llm-wiki/lint-cache.json   — JSON array (for tooling)
#   - stdout: summary line
#
# Usage:
#   $ ./wiki-lint.sh                            # scan + write .llm-wiki/lint/ pages
#   $ ./wiki-lint.sh --verbose                  # show every finding
#   $ ./wiki-lint.sh --summary                  # one-line summary only
#   $ ./wiki-lint.sh --strict                   # exit 1 for critical issues
#   $ ./wiki-lint.sh --semantic                 # also run LLM semantic lint
#   $ ./wiki-lint.sh --fix                      # auto-fix: missing-frontmatter, missing-domain
#   $ ./wiki-lint.sh --fix-links                # apply suggested_target/source wikilink fixes
#   $ ./wiki-lint.sh --json-only                # old behavior: JSON only, no .llm-wiki/lint/ pages
#   $ ./wiki-lint.sh --sweep                    # also report auto-resolvable review items (read-only)
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
# Detect runtime dir (aligned with _paths.py detect_runtime_dir()):
# Priority: 1) .iwiki-runtime/ → migrate  2) .llm-wiki/  3) wiki/ (legacy)  4) .llm-wiki/ (default)
# Auto-migrate from .iwiki-runtime/ if it still exists
if [ -d "$WIKI_ROOT/.iwiki-runtime" ]; then
    echo "[lint] Migrating .iwiki-runtime/ → .llm-wiki/" >&2
    mkdir -p "$WIKI_ROOT/.llm-wiki"
    mv "$WIKI_ROOT/.iwiki-runtime"/* "$WIKI_ROOT/.llm-wiki/" 2>/dev/null || true
    rmdir "$WIKI_ROOT/.iwiki-runtime" 2>/dev/null || true
    RUNTIME_DIR="$WIKI_ROOT/.llm-wiki"
elif [ -f "$WIKI_ROOT/.llm-wiki/ingest-cache.json" ] || \
     [ -d "$WIKI_ROOT/.llm-wiki/ingest-progress" ] || \
     [ -f "$WIKI_ROOT/.llm-wiki/embed-cache.json" ]; then
    RUNTIME_DIR="$WIKI_ROOT/.llm-wiki"
elif [ -f "$WIKI_DIR/.ingest-cache.json" ] || [ -f "$WIKI_DIR/ingest-cache.json" ] || \
     [ -d "$WIKI_DIR/.extract-tmp" ] || [ -d "$WIKI_DIR/extract-tmp" ] || \
     [ -d "$WIKI_DIR/.ingest-progress" ] || [ -d "$WIKI_DIR/ingest-progress" ]; then
    RUNTIME_DIR="$WIKI_DIR"  # legacy layout
else
    RUNTIME_DIR="$WIKI_ROOT/.llm-wiki"
fi
mkdir -p "$RUNTIME_DIR"
# Lint pages live under the runtime dir (not wiki/) — they are derived
# diagnostic output, not source knowledge; keeping them out of wiki/ avoids
# polluting search/graph scans and matches NashSU's "lint state is runtime,
# not wiki content" boundary. Auto-migrate legacy wiki/lint/ on first run.
LINT_PAGES_DIR="$RUNTIME_DIR/lint"
if [ -d "$WIKI_DIR/lint" ] && [ "$WIKI_DIR/lint" != "$LINT_PAGES_DIR" ]; then
    mkdir -p "$LINT_PAGES_DIR"
    mv "$WIKI_DIR/lint"/*.md "$LINT_PAGES_DIR/" 2>/dev/null || true
    rmdir "$WIKI_DIR/lint" 2>/dev/null || true
    echo "[lint] Migrated wiki/lint/ → $LINT_PAGES_DIR" >&2
fi
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
FIX_LINKS=false
JSON_ONLY=false
SWEEP=false
DEDUP=false
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
    --fix-links)  FIX_LINKS=true ;;
    --json-only)  JSON_ONLY=true ;;
    --sweep)       SWEEP=true ;;
    --dedup)       DEDUP=true ;;
    --dry-run)     ;;  # consumed; forwarded to dedup_sweep by the --dedup branch
    --semantic-limit=*) SEMANTIC_LIMIT="${arg#*=}" ;;
    --semantic-tokens=*) SEMANTIC_TOKENS="${arg#*=}" ;;
    --help|-h)
      grep -E "^#( |!)" "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# --dedup: run LLM semantic dedup (NashSU dedup.ts parity).
# Standalone action — NOT run after ingest, only on explicit request.
# Auto-applies (deletes files); --dry-run previews without writes.
if [ "$DEDUP" = true ]; then
  DEDUP_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cross_source_dedup.py"
  DEDUP_ARGS=("$DEDUP_SCRIPT")
  for a in "$@"; do
    case "$a" in --dry-run) DEDUP_ARGS+=(--dry-run) ;; esac
  done
  exec python3 "${DEDUP_ARGS[@]}" --project "$WIKI_ROOT"
fi

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
  # find -print0 + read -d '' so filenames with spaces (macOS " 2.md"
  # collision suffixes) survive — unquoted `for f in *.md` word-splits spaced
  # names into two tokens and rm silently fails on both halves.
  while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue
    if grep -q '^resolved:\s*true\s*$' "$f"; then
      rm -f "$f"
      REMOVED=$((REMOVED + 1))
    fi
  done < <(find "$LINT_PAGES_DIR" -maxdepth 1 -name '*.md' -print0)
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR
cat > "$LINT_SCRIPT" <<'PYEOF'
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from _lint_suggest import run_structural_lint
from _lint_domains import load_valid_domains

wiki_dir = Path(os.environ["WIKI_DIR"])
skill_root = Path(os.environ["SCRIPT_DIR"]).parent
project_root = wiki_dir.parent
findings: list[dict] = []
now_ms = int(time.time() * 1000)

# 1. Collect all wiki pages (exclude state files, anchor files, and the
#    lint/ directory — scanning lint pages creates infinite feedback).
#    No slug_map here: broken-link/orphan/no-outlinks detection + suggestions
#    are delegated to run_structural_lint (single source of truth), which
#    builds its own case-insensitive dual-index slug map internally.
STATE_SKIP = {"lint-cache.json", "ingest-cache.json", "ingest-queue.json", "ingest-lock",
              "domains.md"}  # domains.md is a project config file, not a wiki page
ANCHOR_FILES = {"index.md", "log.md"}  # NashSU parity: only these 2 excluded from structural lint

pages: dict[str, Path] = {}          # original stem -> Path
for path in sorted(wiki_dir.rglob("*.md")):
    rel = path.relative_to(wiki_dir)
    if rel.name in STATE_SKIP or rel.name in ANCHOR_FILES:
        continue
    if rel.parts[0] == "lint":
        continue
    rel_stem = str(rel.with_suffix(""))       # e.g. "entities/foo-bar"
    pages[rel_stem] = path

# 2. Read every page's content.
contents: dict[str, str] = {}  # stem -> text
for stem, path in pages.items():
    try:
        contents[stem] = path.read_text(encoding="utf-8")
    except Exception as e:
        findings.append({
            "type": "read-error",
            "severity": "warning",
            "page": str(path.relative_to(wiki_dir)),
            "detail": f"Could not read: {e}",
            "id": f"lint-read-{stem}",
            "createdAt": now_ms,
        })

# 3. Structural lint (broken-link / orphan / no-outlinks) WITH suggestions —
#    single source of truth: run_structural_lint. This replaces the previous
#    duplicate out_links/in_links/broken_links scan (which recomputed what
#    run_structural_lint already computes internally). Page ids and createdAt
#    are attached here; the engine already supplies severity / detail /
#    broken_target / suggested_target / suggested_source.
structural_pages = [(str(pages[s].relative_to(wiki_dir)), contents[s]) for s in pages if s in contents]
_bl_counter = 0
for _f in run_structural_lint(structural_pages):
    _stem_key = re.sub(r"\.md$", "", _f["page"])
    if _f["type"] == "broken-link":
        _bl_counter += 1
        _f["id"] = f"lint-bl-{_stem_key}-{_bl_counter}"
    elif _f["type"] == "orphan":
        _f["id"] = f"lint-orphan-{_stem_key}"
    elif _f["type"] == "no-outlinks":
        _f["id"] = f"lint-nol-{_stem_key}"
    _f["createdAt"] = now_ms
    findings.append(_f)

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
#    Domain is required for concept and entity pages (used for graph
#    partitioning and query routing — not for slug collision, which is
#    handled by Stage 3.1 page-merge since 2026-06-26).
#    Source/comparison/synthesis/etc pages are exempt.
#    Valid-domain set is loaded from <project>/wiki/domains.md (project-level
#    override) or <skill>/references/domains.md (skill default) — NOT
#    hardcoded. If neither parses (empty set), invalid-domain is skipped
#    (lenient) so non-hardware wikis aren't flagged for using their own
#    domains. missing-domain is always checked (presence of the field).
VALID_DOMAINS = load_valid_domains(project_root, skill_root)

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
    elif VALID_DOMAINS and _normalize_domain(domain_found) not in VALID_DOMAINS:
        findings.append({
            "type": "invalid-domain",
            "severity": "warning",
            "page": str(path.relative_to(wiki_dir)),
            "detail": f"Domain '{domain_found}' is not in the allowed list. Valid domains: {', '.join(sorted(VALID_DOMAINS))}.",
            "id": f"lint-id-{stem}",
            "createdAt": now_ms,
        })

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

# ---------- Write lint pages to .llm-wiki/lint/ (human-browsable) ----------
if [ "$JSON_ONLY" != true ]; then
  mkdir -p "$LINT_PAGES_DIR"

  # Delete old lint pages to regenerate with latest scan results.
  # find -print0 | xargs -0 rm handles filenames with spaces (macOS " 2.md"
  # collision suffixes) — the old `for old_f in *.md; rm "$old_f"` word-split
  # on spaces and silently failed to delete them, causing stale pages to
  # accumulate across runs.
  find "$LINT_PAGES_DIR" -maxdepth 1 -name '*.md' -print0 \
    | xargs -0 rm -f

  # Write one .md per finding
  python3 -c "
import json, os, time, re
from pathlib import Path

findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
lint_dir = Path(os.environ.get('LINT_PAGES_DIR', '$RUNTIME_DIR/lint'))
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

    # Suggested fix from the NashSU-parity suggestion engine.
    # suggested_target / suggested_source are short_names like
    # "concepts/transformer.md" — strip the .md so the rendered [[wikilink]]
    # resolves (slug_map keys are stems without the extension).
    sug_target = f.get('suggested_target')
    sug_source = f.get('suggested_source')
    if sug_target:
        sug_target = re.sub(r'\.md$', '', sug_target)
    if sug_source:
        sug_source = re.sub(r'\.md$', '', sug_source)
    suggestion = ''
    if sug_target:
        suggestion = f'\n## Suggested Fix\nLink to [[{sug_target}]] — closest existing page by slug/title similarity.\n'
    elif sug_source:
        suggestion = f'\n## Suggested Fix\n[[{sug_source}]] could link to this page (related by shared terms).\n'

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
{suggestion}
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

# ---------- Optional: review sweep report (read-only, --sweep) ----------
# Runs sweep_reviews.py in dry-run (never --apply) so lint stays non-mutating.
# Surfaces how many pending wiki/REVIEW/ items are now auto-resolvable; closing
# them still requires the standalone `sweep_reviews.py --apply` command.
if [ "$SWEEP" = true ]; then
  echo "[lint] --sweep: scanning wiki/REVIEW/ for auto-resolvable items..."
  SWEEP_SUMMARY=$(IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/sweep_reviews.py" \
      --project "$WIKI_ROOT" --json 2>/dev/null \
    | sed -n '/^--- JSON ---$/,$p' | sed '1d' \
    | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print('no review items (or sweep_reviews.py unavailable)')
else:
    try:
        d = json.loads(raw)
        print(f\"{d.get('resolved',0)} of {d.get('total',0)} auto-resolvable, {d.get('pending',0)} still pending\")
    except Exception:
        print('parse error')
")
  echo "[lint] --sweep: $SWEEP_SUMMARY"
  echo "[lint] --sweep: read-only report — run 'sweep_reviews.py --project <root> --apply' to close resolved items"
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
  echo "[lint] Auto-fix: repairing missing-domain and missing-frontmatter..."
  TIMESTAMP=$(date +%Y-%m-%d)

  FIXED=$(python3 << PYEOF
import json, re, pathlib, os
with open('${LINT_CACHE}', 'r') as fh:
    cache = json.load(fh)
wiki_dir = pathlib.Path('${WIKI_DIR}')
fixed = 0
items = cache if isinstance(cache, list) else cache.get('findings', cache.get('items', []))
for f in items:
    # Lint cache uses 'page' field (relative path from wiki root)
    page_rel = f.get('page', f.get('path', ''))
    if not page_rel:
        continue
    path = wiki_dir / page_rel
    if not path.exists():
        continue
    t = f.get('type', '')
    if t == 'missing-domain':
        text = path.read_text()
        if 'domain:' not in text[:500]:
            text = re.sub(r'(^title:.+$)', r'\1\ndomain: general', text, count=1, flags=re.MULTILINE)
            path.write_text(text)
            fixed += 1
            print(f"  fixed missing-domain: {page_rel}")
    elif t == 'missing-frontmatter':
        text = path.read_text()
        if not text.startswith('---'):
            fm = f'---\ntype: concept\ntitle: "{path.stem}"\ndomain: general\ncreated: ${TIMESTAMP}\nupdated: ${TIMESTAMP}\ntags: []\nrelated: []\n---\n\n'
            path.write_text(fm + text)
            fixed += 1
            print(f"  fixed missing-frontmatter: {page_rel}")
print(fixed)
PYEOF
)
  echo "[lint] Auto-fix: repaired $FIXED issues"
fi

# ── Auto-fix links (--fix-links; applies the suggestion engine's output) ──
# broken-link → replace [[broken]] with [[suggested]] in the source page
# no-outlinks → append a "## Related" link to the suggested target
# orphan      → append a "## Related" link to this page in the suggested source
# Only acts when a suggestion exists. Wiki pages are edited in place; re-run
# wiki-lint.sh afterwards to confirm the findings clear.
if [ "$FIX_LINKS" = true ]; then
  echo "[lint] Auto-fix-links: applying suggestion-engine wikilink fixes..."
  FIXED_LINKS=$(python3 << PYEOF
import json, re, pathlib
with open('${LINT_CACHE}', 'r') as fh:
    cache = json.load(fh)
wiki_dir = pathlib.Path('${WIKI_DIR}')
items = cache if isinstance(cache, list) else cache.get('findings', cache.get('items', []))

def _slug(short_name):
    return re.sub(r'\.md\$', '', short_name)

fixed = 0
for f in items:
    t = f.get('type', '')
    page_rel = f.get('page', '')
    if not page_rel:
        continue
    src_path = wiki_dir / page_rel

    if t == 'broken-link':
        sug = f.get('suggested_target')
        broken = f.get('broken_target')
        if not sug or not broken or not src_path.exists():
            continue
        sug_slug = _slug(sug)
        # Replace [[broken]] and [[broken|alias]] → [[suggested|alias]] (or [[suggested]])
        pattern = re.compile(r'\[\[' + re.escape(broken) + r'(\|[^\]]+)?\]\]')
        new_text, n = pattern.subn(
            lambda m: f'[[{sug_slug}{m.group(1) or ""}]]',
            src_path.read_text(encoding='utf-8'),
        )
        if n > 0:
            src_path.write_text(new_text, encoding='utf-8')
            fixed += n
            print(f"  fixed broken-link: {page_rel} [[{broken}]] → [[{sug_slug}]] ({n}x)")

    elif t == 'no-outlinks':
        sug = f.get('suggested_target')
        if not sug or not src_path.exists():
            continue
        sug_slug = _slug(sug)
        text = src_path.read_text(encoding='utf-8')
        addition = f"\n\n## Related\n\n- [[{sug_slug}]]\n"
        if f'[[{sug_slug}]]' not in text:
            src_path.write_text(text.rstrip() + addition, encoding='utf-8')
            fixed += 1
            print(f"  fixed no-outlinks: {page_rel} → appended [[{sug_slug}]]")

    elif t == 'orphan':
        sug = f.get('suggested_source')
        if not sug:
            continue
        sug_path = wiki_dir / sug
        if not sug_path.exists():
            continue
        orphan_slug = _slug(page_rel)
        text = sug_path.read_text(encoding='utf-8')
        addition = f"\n\n## Related\n\n- [[{orphan_slug}]]\n"
        if f'[[{orphan_slug}]]' not in text:
            sug_path.write_text(text.rstrip() + addition, encoding='utf-8')
            fixed += 1
            print(f"  fixed orphan: {sug} → appended [[{orphan_slug}]]")
print(fixed)
PYEOF
)
  echo "[lint] Auto-fix-links: applied $FIXED_LINKS wikilink fix(es)"
fi

exit 0
