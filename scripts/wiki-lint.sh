#!/bin/bash
# wiki-lint.sh — Mechanical scan of wiki/ for structural problems.
#
# Lint phases (logical taxonomy, mirrors Ingest's Phase convention). NOTE: the
# numbering is a CLASSIFICATION, not the execution order — see "Execution order"
# below.
#   Phase 0 · 前置检查     — lock, runtime-dir detect/migrate
#   Phase 1 · 结构扫描     — collect pages, run MECHANICAL structural detection
#                            (4 categories, NO LLM). Includes the frontmatter
#                            checks — these are mechanical YAML
#                            checks, NOT the LLM "semantic" phase (Phase 2).
#   Phase 2 · 语义扫描     — optional LLM semantic lint (--semantic); the ONLY LLM phase
#   Phase 3 · 写入         — write lint-cache.json + .llm-wiki/lint/*.md + summary
#   Phase 4 · 自动修复     — optional --fix / --fix-links
#
# Execution order (phases do NOT run in numeric order):
#   0 lock → 1 structural → 3 write cache + lint pages → 2 semantic (--semantic)
#   → --sweep → --strict → 4 auto-fix (--fix / --fix-links).
#   I.e. Phase 3 (write) runs BEFORE Phase 2 (semantic).
#
# Detects 4 categories of issues:
#   1. broken-link        — [[wikilink]] points to a non-existent page
#   2. orphan              — page no other page links to
#   3. no-outlinks         — page has no outbound [[wikilink]]s
#   4. missing-frontmatter — page lacks the required YAML block
#
# Plus a 6th category when --semantic is passed (Phase 2, LLM-driven):
#   6. semantic       — contradiction / stale / missing-page / suggestion /
#                       term-ambiguity (all collapsed to
#                       finding type 'semantic')
#
# Output:
#   - .llm-wiki/lint/*.md         — human-browsable lint pages (each finding one .md)
#                                    Regenerated fresh on every run
#   - .llm-wiki/lint-cache.json   — JSON array (for tooling)
#   - stdout: summary line
#
# Usage:
#   $ ./wiki-lint.sh                            # scan + write .llm-wiki/lint/ pages
#   $ ./wiki-lint.sh --verbose                  # show every finding
#   $ ./wiki-lint.sh --summary                  # one-line summary only
#   $ ./wiki-lint.sh --strict                   # exit 1 for critical issues
#   $ ./wiki-lint.sh --semantic                 # also run LLM semantic lint
#   $ ./wiki-lint.sh --fix                      # auto-fix: missing-frontmatter
#   $ ./wiki-lint.sh --fix-links                # apply suggested_target/source wikilink fixes
#   $ ./wiki-lint.sh --json-only                # old behavior: JSON only, no .llm-wiki/lint/ pages
#   $ ./wiki-lint.sh --sweep                    # also report auto-resolvable review items (read-only, rule-based)
#   $ ./wiki-lint.sh --delete-orphans           # PREVIEW orphan cascade delete (dry-run; apply via wiki-lint-fix.py --apply)
#
# Configuration via env:
#   IMPROVED_WIKI_ROOT — path to project root (default: cwd)
# NOTE: --semantic runs in CONVERSATION MODE (current model, NO API key needed).
# It is driven by the calling agent via a prompt-file handoff (exit 101), so it
# is normally run by invoking wiki-lint-semantic.py directly. When run via
# --semantic here, a pending handoff is propagated as exit 101 for the agent to
# answer and re-invoke. (The old LLM_API_KEY / LLM_BASE_URL / LLM_MODEL /
# max-tokens knobs belonged to the retired direct-MiniMax path and no longer
# apply.)
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
SEMANTIC_CACHE="$RUNTIME_DIR/lint-semantic.json"

VERBOSE=false
SUMMARY=false
STRICT=false
SEMANTIC=false
AUTO_FIX=false
FIX_LINKS=false
JSON_ONLY=false
SWEEP=false
DEDUP=false
DELETE_ORPHANS=false
SEMANTIC_LIMIT=""
SEMANTIC_TOKENS=""
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
    --delete-orphans) DELETE_ORPHANS=true ;;
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

# Acquire lock to avoid concurrent runs. PID-in-lockfile + `kill -0` liveness:
# the old `pgrep -f wiki-lint.sh` matched THIS process (and its launching shell),
# so the stale-lock recovery branch was unreachable — a crashed/OOM-killed run
# left the wiki permanently un-lintable until someone deleted the lock by hand.
# Storing the PID and probing it detects a genuinely live instance and otherwise
# reclaims the stale lock.
if [ -e "$LINT_LOCK" ]; then
  oldpid=$(cat "$LINT_LOCK" 2>/dev/null)
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "[lint] Another instance (pid $oldpid) running, exiting." >&2
    exit 0
  fi
  rm -f "$LINT_LOCK"
fi
# NOTE: this EXIT trap is intentionally re-set below (the LINT_SCRIPT trap) to
# ALSO remove $LINT_LOCK — bash replaces traps, it does not append, so the lock
# must be listed in whichever EXIT trap is installed last or it leaks.
trap 'rm -f "$LINT_LOCK"' EXIT
echo $$ > "$LINT_LOCK"

# ---------- Run the python linter ----------
# All the heavy lifting is in Python for clean CJK handling.
# Use a temp .py file rather than heredoc for maximum compatibility with
# bash 3.x and to avoid heredoc/redirection ordering bugs.
LINT_SCRIPT=$(mktemp -t wiki-lint-XXXXXX.py)
# Re-installs the EXIT trap (replacing the lock-only one above), so it must keep
# removing $LINT_LOCK too — otherwise the lock leaks on every normal exit.
trap "rm -f '$LINT_SCRIPT' '$LINT_CACHE.tmp' '$LINT_CACHE.tmp.err' '$LINT_LOCK'" EXIT

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

# 1. Collect all wiki pages (exclude state files, anchor files, and the
#    lint/ directory — scanning lint pages creates infinite feedback).
#    No slug_map here: broken-link/orphan/no-outlinks detection + suggestions
#    are delegated to run_structural_lint (single source of truth), which
#    builds its own case-insensitive dual-index slug map internally.
STATE_SKIP = {"lint-cache.json", "ingest-cache.json", "ingest-queue.json", "ingest-lock"}
ANCHOR_FILES = {"index.md", "log.md"}  # NashSU parity: only these 2 excluded from page COLLECTION
# Aggregate pages stay IN the scan universe (so their outlinks count toward inbound,
# preventing false orphans on pages only the overview links to) but are EXEMPT from
# FINDINGS — matching _lint_suggest.AGGREGATE_FILES and the suggestion/semantic/fix
# engines. The pipeline writes overview.md / schema.md without content frontmatter by
# design, so the missing-frontmatter check must skip them (else a false positive).
AGGREGATE_FILES = {"index.md", "log.md", "overview.md", "schema.md"}

SKIP_DIRS = {"lint", "REVIEW", "clusters", "media"}  # clusters/ = graph-generated, not source knowledge (match semantic lint + graph.py)
pages: dict[str, Path] = {}          # original stem -> Path
for path in sorted(wiki_dir.rglob("*.md")):
    rel = path.relative_to(wiki_dir)
    if rel.name in STATE_SKIP or rel.name in ANCHOR_FILES:
        continue
    if rel.parts and rel.parts[0] in SKIP_DIRS:
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

# 4. Find missing frontmatter (aggregate pages are exempt — they have no
#    content frontmatter by design; matches AGGREGATE_FILES / the engine).
for stem, path in pages.items():
    if path.name in AGGREGATE_FILES:
        continue
    text = contents.get(stem)
    if text is None:
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
parts = [f'{total} findings', f'broken-link: {c.get(\"broken-link\", 0)}', f'orphan: {c.get(\"orphan\", 0)}', f'no-outlinks: {c.get(\"no-outlinks\", 0)}', f'missing-frontmatter: {c.get(\"missing-frontmatter\", 0)}', f'read-error: {c.get(\"read-error\", 0)}']
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

# ---------- Optional: semantic lint (LLM-driven) ----------
if [ "$SEMANTIC" = true ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SEM_ARGS=()
  [ -n "$SEMANTIC_LIMIT" ]  && SEM_ARGS+=(--limit "$SEMANTIC_LIMIT")
  # Semantic lint is conversation mode (current model) — no LLM_API_KEY /
  # base-url / model / max-tokens. --semantic-tokens is a legacy no-op here (the
  # model's own context governs the call); warn if the caller still passes it.
  [ -n "$SEMANTIC_TOKENS" ] && \
    echo "[lint] --semantic: --semantic-tokens is ignored in conversation mode" >&2
  echo "[lint] --semantic: running conversation-mode semantic pass ..."
  IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/wiki-lint-semantic.py" "${SEM_ARGS[@]}"
  sem_rc=$?
  if [ "$sem_rc" -eq 101 ]; then
    echo "[lint] --semantic: conversation handoff pending (exit 101) — the calling agent must answer the written prompt and re-invoke wiki-lint-semantic.py to finish the semantic pass." >&2
    exit 101
  elif [ "$sem_rc" -ne 0 ]; then
    echo "[lint] --semantic: sub-script exited $sem_rc, continuing" >&2
  fi
fi

# ---------- Optional: review sweep report (read-only, --sweep) ----------
# Runs sweep_reviews.py in dry-run (never --apply) AND --no-llm so lint stays
# non-mutating and never triggers the sweep's conversation-mode LLM judge
# (which would exit 101 and hand off mid-report). This is a rule-based read-only
# count only; the LLM judge + actual resolution live in the standalone
# `sweep_reviews.py --apply` command.
if [ "$SWEEP" = true ]; then
  echo "[lint] --sweep: scanning wiki/REVIEW/ for auto-resolvable items (rule-based)..."
  SWEEP_SUMMARY=$(IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/sweep_reviews.py" \
      --project "$WIKI_ROOT" --no-llm --json 2>/dev/null \
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
         f'read-error: {c.get(\"read-error\", 0)}',
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
errors = sum(1 for f in findings if f['type'] in ('broken-link', 'missing-frontmatter'))
print(errors)
")
  if [ "$HAS_ERRORS" != "0" ]; then
    echo "[lint] --strict: $HAS_ERRORS critical issues found" >&2
    exit 1
  fi
fi

# ── Auto-fix (NashSU lint-fixes.ts parity) ──
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
    # Lint cache uses 'page' field (relative path from wiki root)
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
            # Derive type from the top-level directory (NashSU WIKI_TYPE_DIRS),
            # not a hard-coded 'concept': an entities/sources/queries/... page
            # given type: concept breaks type<->directory schema routing.
            # 'concept' stays the genuine fallback for pages outside a
            # recognized type dir.
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

# ── Auto-fix links (--fix-links; delegates to wiki-lint-fix.py via cache) ──
# broken-link → rewrite [[broken]] → [[suggested]] (preserves alias)
# no-outlinks → append [[suggested]] under ## Related
# orphan      → append [[orphan]] in the suggested source page
# Reads suggestions from $LINT_CACHE — no rescan, no O(n²) overhead.
if [ "$FIX_LINKS" = true ]; then
  echo "[lint] Auto-fix-links: applying suggestion-engine wikilink fixes..."
  python3 "$SCRIPT_DIR/wiki-lint-fix.py" --apply \
    --from-cache "$LINT_CACHE" \
    --project-root "$WIKI_ROOT"
fi

# ── Orphan cascade delete (--delete-orphans; DESTRUCTIVE, preview-only here) ──
# Deleting an orphan cascades (page file + index.md entry + inbound [[links]] +
# related: refs), so the lint command only PREVIEWS it (dry-run, no writes). To
# actually delete, run the standalone applier with --apply:
#   wiki-lint-fix.py --delete-orphans --apply --from-cache <cache> --project-root <root>
if [ "$DELETE_ORPHANS" = true ]; then
  echo "[lint] --delete-orphans: previewing orphan cascade delete (dry-run, no writes)..."
  python3 "$SCRIPT_DIR/wiki-lint-fix.py" --delete-orphans \
    --from-cache "$LINT_CACHE" \
    --project-root "$WIKI_ROOT"
  echo "[lint] --delete-orphans: preview only — re-run wiki-lint-fix.py with --apply to delete"
fi

exit 0
