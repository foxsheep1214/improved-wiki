#!/bin/bash
# wiki-lint.sh — NashSU parity lint: scan wiki/ for structural + semantic issues.
#
# NashSU lint has exactly two scan functions:
#   1. runStructuralLint (always)
#   2. runSemanticLint  (optional in the v0.6.6 UI)
#
# By user policy, improved-wiki's plain lint command runs structural + semantic
# scans, routes semantic warnings to REVIEW, fixes frontmatter/links, sweeps
# resolved reviews, and runs one cross-source dedup round. After those stages
# finish it stops at a delete-orphans confirmation checkpoint (exit 102); the
# calling agent must ask the user before running the confirmed orphan preview.
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
#   $ ./wiki-lint.sh                 # full default maintenance, then ask about delete-orphans
#   $ ./wiki-lint.sh --diagnostic-only # structural + semantic, no wiki mutation
#   $ ./wiki-lint.sh --structural-only # deterministic structural diagnostic only
#   $ ./wiki-lint.sh --no-semantic   # skip semantic only; other defaults still run
#   $ ./wiki-lint.sh --emit-review   # route semantic warnings to wiki/REVIEW/ (default)
#   $ ./wiki-lint.sh --fix           # auto-fix missing-frontmatter (default)
#   $ ./wiki-lint.sh --fix-links     # auto-fix broken-link/orphan/no-outlinks (default)
#                                     (--no-stub mode: broken→review, no bulk stubs;
#                                      2026-07-10: rewrites need score>=0.9, lower
#                                      scores → REVIEW/suggestion items instead)
#   $ ./wiki-lint.sh --sweep         # resolve satisfied review items (default)
#   $ ./wiki-lint.sh --dedup         # cross-source semantic dedup/merge (default)
#   $ ./wiki-lint.sh --no-delete-orphans # skip the final confirmation checkpoint
#   $ ./wiki-lint.sh --delete-orphans-only # confirmed continuation: fresh scan + orphan preview/REVIEW
#   $ ./wiki-lint.sh --reset-lint-run # discard an abandoned exit-101 checkpoint
#   $ ./wiki-lint.sh --verbose       # show every finding
#   $ ./wiki-lint.sh --strict        # exit 1 for critical issues
#   $ ./wiki-lint.sh --json-only     # JSON only, no .md lint pages
#
# The delete-orphans stage is PREVIEW + review items only (2026-07-10) — it
# never deletes. Real delete remains a separately confirmed command:
# wiki-lint-fix.py --delete-orphans --apply.
#
# The mutation flags above drive these standalone commands:
#   sweep_reviews.py                 # auto-resolve satisfied review items
#   cross_source_dedup.py            # cross-source concept dedup
#   wiki-lint-fix.py --delete-orphans # cascade-delete orphan pages
#   lint_verify_semantic.py          # improved-wiki-only extra (no NashSU
#                                     # equivalent): re-verify severity=="warning"
#                                     # semantic-lint findings against FULL page
#                                     # content (the semantic pass itself only
#                                     # sees a 500-char preview per page, batched
#                                     # blind with no cross-batch memory). Run
#                                     # after a lint pass; see the module docstring.
#
# Exit code:
#   0 — clean (or with findings but no --strict)
#   1 — broken-link or missing-frontmatter found (only with --strict)
#   2 — script error
#   101 — conversation handoff pending: --semantic, --sweep, or --dedup wrote
#         a prompt and is waiting for the calling agent's answer. Answer it
#         (per that sub-script's own conversation dir) and re-invoke
#         wiki-lint.sh with the same flags to continue requested stages. (2026-07-10:
#         --sweep/--dedup used to swallow this and silently fall through to
#         later stages without actually applying the sweep/dedup — fixed to
#         propagate exit 101 the same way --semantic already did.)
#   102 — all preceding default stages finished; ask the user whether to run
#         delete-orphans. If approved, invoke --delete-orphans-only. Do not
#         treat 102 as failure or run the continuation without confirmation.
#
# --dedup convergence note (2026-07-12, user-directed): cross_source_dedup.py
# batches are content-hash keyed, so each merge round shifts the wiki page
# set and re-invalidates nearly all batches — re-running wiki-lint.sh after
# answering one round of dedup handoffs tends to re-emit a near-full new
# batch set rather than shrinking monotonically. Calling-agent policy:
# answer ONE round of dedup conversation handoffs, then re-invoke with
# the remaining requested flags but without --dedup. Only keep looping dedup
# rounds if the user explicitly asks to run it to full convergence. See
# references/dedup-design.md for the full rationale.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR
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
    echo "[lint] Legacy wiki/lint/ detected; leaving it untouched during this diagnostic run. New lint pages go to $LINT_PAGES_DIR." >&2
fi
LINT_CACHE="$RUNTIME_DIR/lint-cache.json"
SEMANTIC_CACHE="$RUNTIME_DIR/lint-semantic.json"

# ── Flags ──
VERBOSE=false
STRICT=false
SEMANTIC=true           # semantic remains part of default lint
EMIT_REVIEW=true        # default: warning semantic findings → REVIEW
AUTO_FIX=true           # default: repair missing frontmatter
FIX_LINKS=true          # default: apply safe link fixes / route unsafe ones
SWEEP=true              # default: resolve satisfied review items
DEDUP=true              # default: run one cross-source dedup round
DELETE_ORPHANS=ask      # default: stop after lint and ask the user
JSON_ONLY=false
SEMANTIC_LIMIT=""
SEMANTIC_TOKENS=""
RESET_LINT_RUN=false
STRUCTURAL_ONLY_MODE=false
DELETE_ORPHANS_ONLY_MODE=false
for arg in "$@"; do
  case $arg in
    --verbose|-v) VERBOSE=true ;;
    --strict)     STRICT=true ;;
    --semantic)   SEMANTIC=true ;;
    --no-semantic) SEMANTIC=false ;;
    --emit-review) EMIT_REVIEW=true ;;
    --no-emit-review) EMIT_REVIEW=false ;;
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
    --diagnostic-only)
      EMIT_REVIEW=false
      AUTO_FIX=false
      FIX_LINKS=false
      SWEEP=false
      DEDUP=false
      DELETE_ORPHANS=false
      ;;
    --structural-only)
      STRUCTURAL_ONLY_MODE=true
      SEMANTIC=false
      EMIT_REVIEW=false
      AUTO_FIX=false
      FIX_LINKS=false
      SWEEP=false
      DEDUP=false
      DELETE_ORPHANS=false
      ;;
    --delete-orphans-only)
      DELETE_ORPHANS_ONLY_MODE=true
      SEMANTIC=false
      EMIT_REVIEW=false
      AUTO_FIX=false
      FIX_LINKS=false
      SWEEP=false
      DEDUP=false
      DELETE_ORPHANS=true
      ;;
    --reset-lint-run) RESET_LINT_RUN=true ;;
    --semantic-limit=*) SEMANTIC_LIMIT="${arg#*=}" ;;
    --semantic-tokens=*) SEMANTIC_TOKENS="${arg#*=}" ;;
    --help|-h)
      grep -E "^#( |\!)" "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Structural-only and the separately confirmed delete-orphans continuation are
# standalone commands. They must not consume or overwrite an exit-101
# checkpoint belonging to a full/diagnostic logical lint run.
STATEFUL_LINT_RUN=true
if [ "$STRUCTURAL_ONLY_MODE" = true ] || [ "$DELETE_ORPHANS_ONLY_MODE" = true ]; then
  STATEFUL_LINT_RUN=false
fi

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
# Install the cleanup trap IMMEDIATELY after winning the mkdir lock
# (2026-07-12): any exit between winning the lock and a later trap install
# used to orphan the lockdir. The trap is extended below as further
# resources (flock holder, temp script) come into existence.
trap "rm -rf '$LINT_LOCKDIR'" EXIT
echo $$ > "$LINT_LOCKDIR/pid"

# ── Ingest/lint mutual exclusion (2026-07-11) ──
# Hold the SAME flock ingest.py uses (runtime/ingest.lock, see _core.ProjectLock)
# for the duration of this lint run. This is real mutual exclusion, not a
# check: if an ingest is mid-write, lint refuses to start (its fix/dedup/
# delete stages write to wiki/); if lint is running, a new ingest's
# lock.acquire() fails with its normal "another ingest may be running"
# message. NashSU never needs this — it is a single-window desktop app whose
# UI serializes everything. The holder process keeps the flock's fd open and
# dies with this script: normally killed by the EXIT trap, and as a backstop
# it watches for parent death itself (2026-07-12) — if this script is killed
# hard (SIGKILL, no trap), the holder gets reparented, notices getppid()
# changed, and exits, releasing the flock instead of sleeping on it forever.
INGEST_LOCK_FILE="$RUNTIME_DIR/ingest.lock"
LOCK_FIFO=$(mktemp -u -t wiki-lint-lockfifo-XXXXXX)
mkfifo "$LOCK_FIFO"
python3 - "$INGEST_LOCK_FILE" "$$" > "$LOCK_FIFO" <<'PYLOCK' &
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("BUSY", flush=True)
    sys.exit(3)
print("LOCKED", flush=True)
# Parent-death watchdog: exit (auto-releasing the flock) as soon as the
# launching lint script (pid passed as argv[2] — NOT getppid(), which races
# with reparenting when the parent dies early) is gone. kill(pid, 0) only
# probes existence.
_parent = int(sys.argv[2])
while True:
    try:
        os.kill(_parent, 0)
    except OSError:
        sys.exit(0)
    time.sleep(2)
PYLOCK
LOCK_HOLDER_PID=$!
# Drop the holder from the job table so the EXIT-trap kill doesn't emit a
# noisy "Terminated" job notice (with the whole heredoc) on every clean exit.
disown "$LOCK_HOLDER_PID" 2>/dev/null || true
read -r LOCK_STATUS < "$LOCK_FIFO"
rm -f "$LOCK_FIFO"
# Extend the trap to also kill the holder (lockdir cleanup already trapped).
trap "kill '$LOCK_HOLDER_PID' 2>/dev/null; rm -rf '$LINT_LOCKDIR'" EXIT
if [ "$LOCK_STATUS" != "LOCKED" ]; then
  echo "[lint] A Stage 2.3+ writer holds the project lock ($INGEST_LOCK_FILE) — refusing to run lint concurrently. Wait for that write spine to finish; ordinary background OCR does not hold this lock (see maintenance-cleanup.md)." >&2
  exit 1
fi

# ── Logical lint-run checkpoint (survives exit 101) ──
LINT_RUN_STATE="$RUNTIME_DIR/lint-run-state.json"
LINT_RUN_ACTIVE=false
LINT_RUN_ID=""

if [ "$RESET_LINT_RUN" = true ]; then
  python3 "$SCRIPT_DIR/_lint_run_state.py" reset "$LINT_RUN_STATE" \
    --related-state "$RUNTIME_DIR/review-sweep-run.json" || exit 2
  echo "[lint] Discarded the previous logical lint-run checkpoint." >&2
fi

if [ "$STATEFUL_LINT_RUN" = true ]; then
  if ! LINT_RUN_ID=$(python3 "$SCRIPT_DIR/_lint_run_state.py" begin "$LINT_RUN_STATE"); then
    echo "[lint] Could not start/resume logical lint-run state." >&2
    exit 2
  fi
  LINT_RUN_ACTIVE=true
  echo "[lint] Logical run: $LINT_RUN_ID"
fi

lint_stage_done() {
  [ "$LINT_RUN_ACTIVE" = true ] || return 1
  python3 "$SCRIPT_DIR/_lint_run_state.py" is-done "$LINT_RUN_STATE" "$1"
}

lint_mark_done() {
  [ "$LINT_RUN_ACTIVE" = true ] || return 0
  python3 "$SCRIPT_DIR/_lint_run_state.py" mark-done "$LINT_RUN_STATE" "$1"
}

lint_finish_run() {
  [ "$LINT_RUN_ACTIVE" = true ] || return 0
  python3 "$SCRIPT_DIR/_lint_run_state.py" finish "$LINT_RUN_STATE"
  LINT_RUN_ACTIVE=false
}

# ── Phase 1: Structural lint ──
LINT_SCRIPT=$(mktemp -t wiki-lint-XXXXXX.py)
trap "kill '$LOCK_HOLDER_PID' 2>/dev/null; rm -rf '$LINT_LOCKDIR'; rm -f '$LINT_SCRIPT' '$LINT_CACHE.tmp' '$LINT_CACHE.tmp.err'" EXIT

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

# Shared constants (2026-07-12) — this embedded scan used to carry literal
# copies that drifted from the python-side sets.
from _lint_suggest import ANCHOR_FILES, AGGREGATE_FILES, STATE_FILES as STATE_SKIP
from _paths import WIKI_ARTIFACT_DIRS as SKIP_DIRS

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

run_structural_scan() {
  # Guard the cache write: a failed python run leaves a partial/empty .tmp,
  # and a blind mv would clobber the last good cache used by fix/orphan stages.
  if ! python3 "$LINT_SCRIPT" > "$LINT_CACHE.tmp" 2> "$LINT_CACHE.tmp.err"; then
    echo "[lint] Structural lint failed — keeping previous cache." >&2
    cat "$LINT_CACHE.tmp.err" >&2
    rm -f "$LINT_CACHE.tmp" "$LINT_CACHE.tmp.err"
    return 1
  fi
  rm -f "$LINT_CACHE.tmp.err"
  mv "$LINT_CACHE.tmp" "$LINT_CACHE"
  return 0
}

if ! run_structural_scan; then
  lint_finish_run
  exit 1
fi
CACHE_DIRTY_AFTER_SCAN=false

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

# ── Phase 2: Semantic lint (user-selected default; --no-semantic skips) ──
if [ "$SEMANTIC" = true ]; then
  if lint_stage_done "semantic"; then
    echo "[lint] --semantic: already complete for logical run $LINT_RUN_ID; skipping full rescan."
  else
    SEM_ARGS=()
    [ -n "$SEMANTIC_LIMIT" ]  && SEM_ARGS+=(--limit "$SEMANTIC_LIMIT")
    [ "$EMIT_REVIEW" = true ] && SEM_ARGS+=(--emit-review)
    [ -n "$SEMANTIC_TOKENS" ] && \
      echo "[lint] --semantic: --semantic-tokens is ignored in conversation mode" >&2
    echo "[lint] --semantic: running conversation-mode semantic pass ..."
    IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/wiki-lint-semantic.py" ${SEM_ARGS:+"${SEM_ARGS[@]}"}
    sem_rc=$?
    if [ "$sem_rc" -eq 101 ]; then
      echo "[lint] --semantic: conversation handoff pending (exit 101) — answer the written prompt and re-run wiki-lint.sh with the same flags; the logical-run checkpoint prevents a second full pass after this one completes." >&2
      exit 101
    elif [ "$sem_rc" -ne 0 ]; then
      echo "[lint] --semantic: sub-script exited $sem_rc, continuing" >&2
    elif ! lint_mark_done "semantic"; then
      echo "[lint] --semantic: failed to persist completion checkpoint." >&2
      lint_finish_run
      exit 2
    fi
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
    lint_finish_run
    exit 1
  fi
fi

# ── Default maintenance: Auto-fix missing-frontmatter ──
if [ "$AUTO_FIX" = true ]; then
  if lint_stage_done "auto_fix"; then
    echo "[lint] Auto-fix: already complete for logical run $LINT_RUN_ID; skipping."
  else
    CACHE_DIRTY_AFTER_SCAN=true
    echo "[lint] Auto-fix: repairing missing-frontmatter..."
    TIMESTAMP=$(date +%Y-%m-%d)
    # Progress lines go to stderr; stdout carries ONLY the final count, so
    # $FIXED can't be a multi-line blob (2026-07-12). Writes are atomic.
    FIXED=$(python3 << PYEOF
import json, re, pathlib, os, sys
sys.path.insert(0, os.environ.get("SCRIPT_DIR", ""))
from _paths import atomic_write
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
            atomic_write(path, fm + text)
            fixed += 1
            print(f"  fixed missing-frontmatter: {page_rel}", file=sys.stderr)
print(fixed)
PYEOF
)
    fix_rc=$?
    if [ "$fix_rc" -ne 0 ]; then
      echo "[lint] Auto-fix: python exited $fix_rc, continuing" >&2
    else
      if ! lint_mark_done "auto_fix"; then
        echo "[lint] Auto-fix: failed to persist completion checkpoint." >&2
        lint_finish_run
        exit 2
      fi
    fi
    case "$FIXED" in
      ''|*[!0-9]*)
        echo "[lint] Auto-fix: unexpected count output '${FIXED}' — treating as 0" >&2
        FIXED=0 ;;
    esac
    echo "[lint] Auto-fix: repaired $FIXED issues"
  fi
fi

# ── Default maintenance: Auto-fix links (NashSU handleFix parity) ──
if [ "$FIX_LINKS" = true ]; then
  if lint_stage_done "fix_links"; then
    echo "[lint] Auto-fix-links: already complete for logical run $LINT_RUN_ID; skipping."
  else
    CACHE_DIRTY_AFTER_SCAN=true
    echo "[lint] Auto-fix-links: applying rewrites + append + broken→review (no stubs)..."
    python3 "$SCRIPT_DIR/wiki-lint-fix.py" --apply --no-stub \
      --from-cache "$LINT_CACHE" \
      --project-root "$WIKI_ROOT"
    fixlinks_rc=$?
    if [ "$fixlinks_rc" -ne 0 ]; then
      echo "[lint] --fix-links: sub-script exited $fixlinks_rc, continuing" >&2
    elif ! lint_mark_done "fix_links"; then
      echo "[lint] --fix-links: failed to persist completion checkpoint." >&2
      lint_finish_run
      exit 2
    fi
  fi
fi

# ── Default maintenance: Review sweep (NashSU sweep-reviews.ts parity) ──
if [ "$SWEEP" = true ]; then
  if lint_stage_done "sweep"; then
    echo "[lint] Review sweep: already complete for logical run $LINT_RUN_ID; skipping."
  else
    CACHE_DIRTY_AFTER_SCAN=true
    echo "[lint] Review sweep: resolving satisfied review items..."
    SWEEP_ARGS=(--project "$WIKI_ROOT" --apply)
    if [ "$LINT_RUN_ACTIVE" = true ]; then
      SWEEP_ARGS+=(--run-id "$LINT_RUN_ID")
    fi
    SWEEP_OUT=$(IMPROVED_WIKI_ROOT="$WIKI_ROOT" python3 "$SCRIPT_DIR/sweep_reviews.py" \
        "${SWEEP_ARGS[@]}" 2>&1 | tail -3)
    sweep_rc=$?
    echo "[lint] --sweep: $SWEEP_OUT"
    if [ "$sweep_rc" -eq 101 ]; then
      echo "[lint] --sweep: conversation handoff pending (exit 101) — answer the written prompt and re-run wiki-lint.sh. The same logical run keeps one hard total of at most 5 judge batches and stops after the first zero-resolved batch." >&2
      exit 101
    elif [ "$sweep_rc" -ne 0 ]; then
      echo "[lint] --sweep: sub-script exited $sweep_rc, continuing" >&2
    elif ! lint_mark_done "sweep"; then
      echo "[lint] --sweep: failed to persist completion checkpoint." >&2
      lint_finish_run
      exit 2
    fi
  fi
fi

# ── Default maintenance: Cross-source dedup (NashSU dedup parity) ──
if [ "$DEDUP" = true ]; then
  if lint_stage_done "dedup"; then
    echo "[lint] Cross-source dedup: already complete for logical run $LINT_RUN_ID; skipping."
  else
    CACHE_DIRTY_AFTER_SCAN=true
    echo "[lint] Cross-source dedup: merging near-duplicate concepts..."
    python3 "$SCRIPT_DIR/cross_source_dedup.py" --project "$WIKI_ROOT" 2>&1 | tail -5
    dedup_rc=${PIPESTATUS[0]}
    if [ "$dedup_rc" -eq 101 ]; then
      echo "[lint] --dedup: conversation handoff pending (exit 101) — answer the written prompt and re-run wiki-lint.sh to finish the same logical lint run." >&2
      exit 101
    elif [ "$dedup_rc" -ne 0 ]; then
      echo "[lint] --dedup: sub-script exited $dedup_rc, continuing" >&2
    elif ! lint_mark_done "dedup"; then
      echo "[lint] --dedup: failed to persist completion checkpoint." >&2
      lint_finish_run
      exit 2
    fi
  fi
fi

# ── Human-gated: Orphan preview/review (NashSU handleDeleteOrphan port) ──
# 2026-07-10 (user-approved lint hardening): PREVIEW + review items by default,
# never an automatic delete. NashSU's delete is a human-clicked per-item button;
# the old `--apply` here batch-deleted every orphan unattended — including
# freshly-ingested pages that wikilink enrichment hadn't linked yet, and pages
# that a --fix-links append in this very run had just rescued (the cache
# predates the fix). Real delete is an explicit, separate step:
#   wiki-lint-fix.py --delete-orphans --apply --from-cache <cache> --project-root <root>
if [ "$DELETE_ORPHANS" = ask ]; then
  echo "[lint] DELETE_ORPHANS_CONFIRMATION_REQUIRED" >&2
  echo "[lint] All preceding default lint/fix/sweep/dedup stages are complete." >&2
  echo "[lint] Ask the user: 是否执行 delete-orphans（仅预览并生成 Review，不会删除页面）？" >&2
  echo "[lint] If approved, run: $0 --delete-orphans-only" >&2
  lint_finish_run
  exit 102
fi

if [ "$DELETE_ORPHANS" = true ]; then
  if [ "$CACHE_DIRTY_AFTER_SCAN" = true ]; then
    echo "[lint] Refreshing structural cache after wiki mutations before orphan review..."
    if ! run_structural_scan; then
      lint_finish_run
      exit 1
    fi
  fi
  echo "[lint] Delete-orphans: preview + review items (real delete: wiki-lint-fix.py --delete-orphans --apply)..."
  python3 "$SCRIPT_DIR/wiki-lint-fix.py" --delete-orphans --emit-review \
    --from-cache "$LINT_CACHE" \
    --project-root "$WIKI_ROOT"
  delorph_rc=$?
  if [ "$delorph_rc" -ne 0 ]; then
    echo "[lint] --delete-orphans: sub-script exited $delorph_rc, continuing" >&2
  fi
fi

lint_finish_run
exit 0
