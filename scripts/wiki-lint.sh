#!/bin/bash
# wiki-lint.sh — NashSU parity lint: scan wiki/ for structural + semantic issues.
#
# NashSU lint.ts has exactly two functions:
#   1. runStructuralLint (always)
#   2. runSemanticLint  (always, if LLM configured)
#
# This script mirrors that: structural scan always runs, semantic runs by
# default (--no-semantic to skip). Fix/sweep/dedup/delete-orphans are separate
# commands in NashSU and are NOT built into this lint script — use the
# standalone tools: wiki-lint-fix.py, sweep_reviews.py, cross_source_dedup.py.
#
# Detects:
#   1. broken-link        — [[wikilink]] points to a non-existent page
#   2. orphan              — page no other page links to
#   3. no-outlinks         — page has no outbound [[wikilink]]s
#   4. missing-frontmatter — page lacks the required YAML block
#   5. semantic            — contradiction / stale / missing-page / suggestion /
#                             term-ambiguity (LLM-driven, --no-semantic to skip)
#
# Output:
#   - .llm-wiki/lint/*.md         — human-browsable lint pages
#   - .llm-wiki/lint-cache.json   — JSON array (for tooling)
#   - stdout: summary line
#
# Usage:
#   $ ./wiki-lint.sh                 # structural + semantic + fix + fix-links + sweep + dedup + delete-orphans
#   $ ./wiki-lint.sh --no-semantic    # skip LLM semantic
#   $ ./wiki-lint.sh --no-fix-links   # skip auto link-fix pass
#   $ ./wiki-lint.sh --fix           # + auto-fix missing-frontmatter
#   $ ./wiki-lint.sh --fix-links     # + auto-fix broken-link/orphan/no-outlinks
#                                     (--no-stub mode: broken→review, no bulk stubs)
#   $ ./wiki-lint.sh --verbose       # show every finding
#   $ ./wiki-lint.sh --strict        # exit 1 for critical issues
#   $ ./wiki-lint.sh --json-only     # JSON only, no .md lint pages
#
# Standalone commands (not built into lint — NashSU parity):
#   sweep_reviews.py                 # auto-resolve satisfied review items
#   cross_source_dedup.py            # cross-source concept dedup
#   wiki-lint-fix.py --delete-orphans # cascade-delete orphan pages
#   lint_verify_semantic.py          # improved-wiki-only extra (no NashSU
#                                     # equivalent): re-verify severity=="warning"
#                                     # semantic-lint findings against FULL page
#                                     # content (the semantic pass itself only
#                                     # sees a 500-char preview per page, batched
#                                     # blind with no cross-batch memory). Run
#                                     # after a lint pass, not part of the default
#                                     # chain — see the module docstring.
#
# Exit code:
#   0 — clean (or with findings but no --strict)
#   1 — broken-link or missing-frontmatter found (only with --strict)
#   2 — script error

set -uo pipefail

WIKI_ROOT="${IMPROVED_WIKI_ROOT:-$(pwd)}"
WIKI_DIR="$WIKI_ROOT/wiki"
export WIKI_DIR

# Detect runtime dir (aligned with _paths.py detect_runtime_dir())
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
    RUNTIME_DIR="$WIKI_DIR"
else
    RUNTIME_DIR="$WIKI_ROOT/.llm-wiki"
fi
mkdir -p "$RUNTIME_DIR"

LINT_PAGES_DIR="$RUNTIME_DIR/lint"
if [ -d "$WIKI_DIR/lint" ] && [ "$WIKI_DIR/lint" != "$LINT_PAGES_DIR" ]; then
    mkdir -p "$LINT_PAGES_DIR"
    mv "$WIKI_DIR/lint"/*.md "$LINT_PAGES_DIR/" 2>/dev/null || true
    rmdir "$WIKI_DIR/lint" 2>/dev/null || true
    echo "[lint] Migrated wiki/lint/ → $LINT_PAGES_DIR" >&2
fi
LINT_CACHE="$RUNTIME_DIR/lint-cache.json"
SEMANTIC_CACHE="$RUNTIME_DIR/lint-semantic.json"

# ── Flags ──
VERBOSE=false
STRICT=false
SEMANTIC=true           # NashSU parity: semantic always runs
AUTO_FIX=true           # --no-fix to skip
FIX_LINKS=true          # --no-fix-links to skip
SWEEP=true              # --no-sweep to skip
DEDUP=true              # --no-dedup to skip
DELETE_ORPHANS=true     # --no-delete-orphans to skip
JSON_ONLY=false
SEMANTIC_LIMIT=""
SEMANTIC_TOKENS=""
for arg in "$@"; do
  case $arg in
    --verbose|-v) VERBOSE=true ;;
    --summary)    SUMMARY=true ;;
    --strict)     STRICT=true ;;
    --semantic)   SEMANTIC=true ;;
    --no-semantic) SEMANTIC=false ;;
    --fix)        AUTO_FIX=true ;;
    --no-fix)     AUTO_FIX=false ;;
    --fix-links)  FIX_LINKS=true ;;
    --no-fix-links) FIX_LINKS=false ;;
    --json-only)  JSON_ONLY=true ;;
    --sweep)       SWEEP=true ;;
    --no-sweep)    SWEEP=false ;;
    --dedup)       DEDUP=true ;;
    --no-dedup)    DEDUP=false ;;
    --delete-orphans) DELETE_ORPHANS=true ;;
    --no-delete-orphans) DELETE_ORPHANS=false ;;
    --semantic-limit=*) SEMANTIC_LIMIT="${arg#*=}" ;;
    --semantic-tokens=*) SEMANTIC_TOKENS="${arg#*=}" ;;
    --help|-h)
      grep -E "^#( |\!)" "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -d "$WIKI_DIR" ]; then
  echo "ERROR: wiki/ does not exist under $WIKI_ROOT" >&2
  exit 2
fi

# ── Lock (mkdir-atomic, race-free; PID staleness reclaim) ──
# mkdir is atomic — only one instance wins. The prior PID-file approach
# (check→kill -0→rm→write) was non-atomic and racy: two lints could both pass
# the existence check and both write. A hard-crashed process leaves a stale
# lockdir; we reclaim it only after confirming the recorded PID is dead, so a
# live process is never displaced.
LINT_LOCKDIR="$RUNTIME_DIR/lint-lock.d"
if ! mkdir "$LINT_LOCKDIR" 2>/dev/null; then
  oldpid=$(cat "$LINT_LOCKDIR/pid" 2>/dev/null)
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "[lint] Another instance (pid $oldpid) running, exiting." >&2
    exit 0
  fi
  rm -rf "$LINT_LOCKDIR"
  mkdir "$LINT_LOCKDIR" 2>/dev/null || { echo "[lint] Lock reclaim failed, exiting." >&2; exit 0; }
fi
echo $$ > "$LINT_LOCKDIR/pid"

# ── Phase 1: Structural lint ──
LINT_SCRIPT=$(mktemp -t wiki-lint-XXXXXX.py)
trap "rm -rf '$LINT_LOCKDIR'; rm -f '$LINT_SCRIPT' '$LINT_CACHE.tmp' '$LINT_CACHE.tmp.err'" EXIT

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

wiki_dir = Path(os.environ["WIKI_DIR"])
findings: list[dict] = []
now_ms = int(time.time() * 1000)

STATE_SKIP = {"lint-cache.json", "ingest-cache.json", "ingest-queue.json", "ingest-lock"}
ANCHOR_FILES = {"index.md", "log.md"}
AGGREGATE_FILES = {"index.md", "log.md", "overview.md", "schema.md"}
SKIP_DIRS = {"lint", "REVIEW", "clusters", "media"}

pages: dict[str, Path] = {}
for path in sorted(wiki_dir.rglob("*.md")):
    rel = path.relative_to(wiki_dir)
    if rel.name in STATE_SKIP or rel.name in ANCHOR_FILES:
        continue
    if rel.parts and rel.parts[0] in SKIP_DIRS:
        continue
    pages[str(rel.with_suffix(""))] = path

contents: dict[str, str] = {}
for stem, path in pages.items():
    try:
        contents[stem] = path.read_text(encoding="utf-8")
    except Exception as e:
        findings.append({
            "type": "read-error", "severity": "warning",
            "page": str(path.relative_to(wiki_dir)),
            "detail": f"Could not read: {e}",
            "id": f"lint-read-{stem}", "createdAt": now_ms,
        })

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

for stem, path in pages.items():
    if path.name in AGGREGATE_FILES:
        continue
    text = contents.get(stem)
    if text is None:
        continue
    if not re.match(r"^---\s*\n", text):
        findings.append({
            "type": "missing-frontmatter", "severity": "error",
            "page": str(path.relative_to(wiki_dir)),
            "detail": "Page has no YAML frontmatter block (must start with ---).",
            "id": f"lint-mf-{stem}", "createdAt": now_ms,
        })

print(json.dumps(findings, ensure_ascii=False, indent=2))
PYEOF

# Run structural lint. Guard the cache write: a failed python run leaves a
# partial/empty .tmp, and a blind `mv` would clobber the last good cache —
# breaking every downstream --from-cache consumer (fix-links, sweep, dedup).
if ! python3 "$LINT_SCRIPT" > "$LINT_CACHE.tmp" 2> "$LINT_CACHE.tmp.err"; then
  echo "[lint] Structural lint failed — keeping previous cache." >&2
  cat "$LINT_CACHE.tmp.err" >&2
  rm -f "$LINT_CACHE.tmp" "$LINT_CACHE.tmp.err"
  exit 1
fi
rm -f "$LINT_CACHE.tmp.err"
mv "$LINT_CACHE.tmp" "$LINT_CACHE"

# ── Summary ──
SUMMARY_LINE=$(python3 -c "
import json
from collections import Counter
findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
c = Counter(f['type'] for f in findings)
total = sum(c.values())
parts = [f'{total} findings', f'broken-link: {c.get(\"broken-link\", 0)}', f'orphan: {c.get(\"orphan\", 0)}', f'no-outlinks: {c.get(\"no-outlinks\", 0)}', f'missing-frontmatter: {c.get(\"missing-frontmatter\", 0)}', f'read-error: {c.get(\"read-error\", 0)}']
print(' | '.join(parts))
")
echo "[lint] $SUMMARY_LINE"

# ── Write lint pages ──
if [ "$JSON_ONLY" != true ]; then
  mkdir -p "$LINT_PAGES_DIR"
  find "$LINT_PAGES_DIR" -maxdepth 1 -name '*.md' -print0 | xargs -0 rm -f
  python3 -c "
import json, os, time, re
from pathlib import Path

findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
lint_dir = Path(os.environ.get('LINT_PAGES_DIR', '$RUNTIME_DIR/lint'))
lint_dir.mkdir(parents=True, exist_ok=True)
date_str = time.strftime('%Y-%m-%d')

severity_icon = {'error': '❌', 'warning': '⚠️', 'info': 'ℹ️'}
written = 0
fname_counts = {}
for f in findings:
    ftype = f.get('type', 'unknown')
    severity = f.get('severity', 'info')
    page_ref = f.get('page', 'unknown')
    detail = f.get('detail', '')
    icon = severity_icon.get(severity, 'ℹ️')

    safe_type = re.sub(r'[^\w-]', '', ftype)[:30]
    safe_page = re.sub(r'[^\w\.\-一-鿿]', '-', page_ref)[:40]
    safe_page = safe_page.replace('.md', '')
    base_name = f'{safe_type}-{safe_page}'
    base_name = re.sub(r'-{2,}', '-', base_name)
    n = fname_counts.get(base_name, 0) + 1
    fname_counts[base_name] = n
    filename = f'{base_name}-{n:02d}.md' if n > 1 else f'{base_name}.md'
    page_path = lint_dir / filename

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

    fm = f'''---
type: lint
lint_type: {ftype}
severity: {severity}
page: \"{page_ref}\"
created: {date_str}
---

# {icon} [{ftype}] {page_ref}

{detail}
{suggestion}'''
    tmp = page_path.with_suffix(page_path.suffix + '.tmp')
    tmp.write_text(fm, encoding='utf-8')
    tmp.rename(page_path)
    written += 1

print(f'[lint] {written} lint pages → {lint_dir}')
"
  LINT_PAGE_COUNT=$(ls "$LINT_PAGES_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
  echo "[lint] Pages: $LINT_PAGE_COUNT findings in $LINT_PAGES_DIR/"
fi

# ── Phase 2: Semantic lint (NashSU parity: always runs) ──
if [ "$SEMANTIC" = true ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SEM_ARGS=()
  [ -n "$SEMANTIC_LIMIT" ]  && SEM_ARGS+=(--limit "$SEMANTIC_LIMIT")
  [ -n "$SEMANTIC_TOKENS" ] && \
    echo "[lint] --semantic: --semantic-tokens is ignored in conversation mode" >&2
  echo "[lint] --semantic: running conversation-mode semantic pass ..."
  IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/wiki-lint-semantic.py" ${SEM_ARGS:+"${SEM_ARGS[@]}"}
  sem_rc=$?
  if [ "$sem_rc" -eq 101 ]; then
    echo "[lint] --semantic: conversation handoff pending (exit 101) — the calling agent must answer the written prompt and re-invoke wiki-lint-semantic.py to finish the semantic pass." >&2
    exit 101
  elif [ "$sem_rc" -ne 0 ]; then
    echo "[lint] --semantic: sub-script exited $sem_rc, continuing" >&2
  fi
fi

# ── Combined summary (with semantic) ──
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
         f'read-error: {c.get(\"read-error\", 0)}',
         f'semantic: {c.get(\"semantic\", 0)}']
print(' | '.join(parts))
")
  echo "[lint+semantic] $SUMMARY_LINE"
fi

# ── Verbose ──
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

# ── Strict ──
if [ "$STRICT" = true ]; then
  HAS_ERRORS=$(python3 -c "
import json
findings = json.load(open('$LINT_CACHE', 'r', encoding='utf-8'))
errors = sum(1 for f in findings if f['type'] in ('broken-link', 'missing-frontmatter'))
print(errors)
")
  if [ "$HAS_ERRORS" != "0" ]; then
    echo "[lint] --strict: $HAS_ERRORS critical issues found" >&2
    exit 1
  fi
fi

# ── Opt-in: Auto-fix missing-frontmatter (--fix) ──
if [ "$AUTO_FIX" = true ]; then
  echo "[lint] Auto-fix: repairing missing-frontmatter..."
  TIMESTAMP=$(date +%Y-%m-%d)
  FIXED=$(python3 << PYEOF
import json, re, pathlib, os
with open('${LINT_CACHE}', 'r') as fh:
    cache = json.load(fh)
wiki_dir = pathlib.Path('${WIKI_DIR}')
fixed = 0
items = cache if isinstance(cache, list) else cache.get('findings', cache.get('items', []))
for f in items:
    page_rel = f.get('page', f.get('path', ''))
    if not page_rel:
        continue
    path = wiki_dir / page_rel
    if not path.exists():
        continue
    t = f.get('type', '')
    if t == 'missing-frontmatter':
        text = path.read_text(encoding='utf-8')
        if not text.startswith('---'):
            DIR_TYPE = {'entities':'entity','concepts':'concept','sources':'source','queries':'query','comparisons':'comparison','synthesis':'synthesis','findings':'finding','thesis':'thesis','methodology':'methodology'}
            ptype = DIR_TYPE.get(page_rel.split('/')[0], 'concept')
            fm = f'---\ntype: {ptype}\ntitle: "{path.stem}"\ncreated: ${TIMESTAMP}\nupdated: ${TIMESTAMP}\ntags: []\nrelated: []\n---\n\n'
            path.write_text(fm + text, encoding='utf-8')
            fixed += 1
            print(f"  fixed missing-frontmatter: {page_rel}")
print(fixed)
PYEOF
)
  echo "[lint] Auto-fix: repaired $FIXED issues"
fi

# ── Opt-in: Auto-fix links (--fix-links, NashSU handleFix parity) ──
if [ "$FIX_LINKS" = true ]; then
  echo "[lint] Auto-fix-links: applying rewrites + append + broken→review (no stubs)..."
  python3 "$SCRIPT_DIR/wiki-lint-fix.py" --apply --no-stub \
    --from-cache "$LINT_CACHE" \
    --project-root "$WIKI_ROOT"
fi

# ── Opt-in: Review sweep (via --all; NashSU sweep-reviews.ts parity) ──
if [ "$SWEEP" = true ]; then
  echo "[lint] Review sweep: resolving satisfied review items..."
  SWEEP_OUT=$(IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/sweep_reviews.py" \
      --project "$WIKI_ROOT" --apply 2>&1 | tail -3)
  echo "[lint] --sweep: $SWEEP_OUT"
fi

# ── Opt-in: Cross-source dedup (via --all; NashSU dedup parity) ──
if [ "$DEDUP" = true ]; then
  echo "[lint] Cross-source dedup: merging near-duplicate concepts..."
  python3 "$SCRIPT_DIR/cross_source_dedup.py" --project "$WIKI_ROOT" 2>&1 | tail -5
fi

# ── Opt-in: Orphan cascade delete (via --all; NashSU handleDeleteOrphan parity) ──
if [ "$DELETE_ORPHANS" = true ]; then
  echo "[lint] Delete-orphans: cascade-deleting orphan pages..."
  python3 "$SCRIPT_DIR/wiki-lint-fix.py" --delete-orphans --apply \
    --from-cache "$LINT_CACHE" \
    --project-root "$WIKI_ROOT"
fi

exit 0
