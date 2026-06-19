#!/bin/bash
# run-queue.sh — Process the ingest queue (delegates to ingest.py).
#
# Now delegates to `ingest.py --watch --drain` for the main queue processing,
# which uses the same parallel Stage 0-2 + serial Stage 3+ pipeline as batch mode.
# Entries added by wiki-monitor.sh mid-run are picked up between waves.
#
# Usage:
#   $ ./run-queue.sh                        # drain the queue
#   $ ./run-queue.sh --file <path>          # process one file directly (skip queue)
#   $ ./run-queue.sh --priority <file>      # prepend a file to the queue, then drain
#   $ ./run-queue.sh --parallel <N>         # override concurrency (default: 4)
#   $ ./run-queue.sh --watch                # continuous mode (alias for ingest.py --watch)
#   $ ./run-queue.sh --verbose              # show LLM responses
#
# Configuration via env vars:
#   IMPROVED_WIKI_ROOT       — path to project root (default: cwd)
#   LLM_API_KEY              — required for LLM calls
#   LLM_BASE_URL             — default: https://api.minimaxi.com
#   LLM_MODEL                — default: MiniMax-M3

set -euo pipefail

# ---------- Config ----------
WIKI_ROOT="${IMPROVED_WIKI_ROOT:-$(pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INGEST_SCRIPT="$SCRIPT_DIR/ingest.py"
RAW_ROOT="$WIKI_ROOT/raw"

# NOTE: Runtime directory detection duplicates _paths.py logic. If _paths.py changes, update this section too.
# Runtime directory detection (aligned with _paths.py detect_runtime_dir()).
# Priority: 1) .iwiki-runtime/ → migrate  2) .llm-wiki/  3) wiki/ (legacy)  4) .llm-wiki/ (default)
if [ -d "$WIKI_ROOT/.iwiki-runtime" ]; then
    mkdir -p "$WIKI_ROOT/.llm-wiki"
    mv "$WIKI_ROOT/.iwiki-runtime"/* "$WIKI_ROOT/.llm-wiki/" 2>/dev/null || true
    rmdir "$WIKI_ROOT/.iwiki-runtime" 2>/dev/null || true
    RUNTIME="$WIKI_ROOT/.llm-wiki"
elif [ -f "$WIKI_ROOT/.llm-wiki/ingest-cache.json" ] || \
     [ -d "$WIKI_ROOT/.llm-wiki/ingest-progress" ] || \
     [ -f "$WIKI_ROOT/.llm-wiki/embed-cache.json" ]; then
    RUNTIME="$WIKI_ROOT/.llm-wiki"
elif [ -f "$WIKI_ROOT/wiki/.ingest-cache.json" ] || [ -d "$WIKI_ROOT/wiki/.ingest-progress" ]; then
    RUNTIME="$WIKI_ROOT/wiki"
else
    RUNTIME="$WIKI_ROOT/.llm-wiki"
fi

QUEUE_PATH="$RUNTIME/ingest-queue.json"

# ---------- Flags ----------
SPECIFIC_FILE=""
PRIORITY_FILE=""
PARALLEL=""
WATCH_MODE=false
VERBOSE=false

for arg in "$@"; do
  case $arg in
    --file=*)       SPECIFIC_FILE="${arg#*=}" ;;
    --file)         shift; SPECIFIC_FILE="$1" ;;
    --priority=*)   PRIORITY_FILE="${arg#*=}" ;;
    --priority)     shift; PRIORITY_FILE="$1" ;;
    --parallel=*)   PARALLEL="${arg#*=}" ;;
    --parallel)     shift; PARALLEL="$1" ;;
    --watch)        WATCH_MODE=true ;;
    --verbose|-v)   VERBOSE=true ;;
    --help|-h)
      grep -E "^#( |!)" "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# ---------- Sanity checks ----------
if [ ! -d "$RAW_ROOT" ]; then
  echo "ERROR: raw/ does not exist under $WIKI_ROOT" >&2
  exit 1
fi
if [ ! -f "$INGEST_SCRIPT" ]; then
  echo "ERROR: ingest.py not found at $INGEST_SCRIPT" >&2
  exit 1
fi

export IMPROVED_WIKI_ROOT="$WIKI_ROOT"

INGEST_ARGS=("${VERBOSE:+--verbose}")

# ---------- Mode 1: Specific file (bypass queue) ----------
if [ -n "$SPECIFIC_FILE" ]; then
  if [ ! -f "$SPECIFIC_FILE" ]; then
    echo "ERROR: file not found: $SPECIFIC_FILE" >&2
    exit 1
  fi
  echo "Processing single file: $SPECIFIC_FILE"
  exec python3 "$INGEST_SCRIPT" "$SPECIFIC_FILE" "${INGEST_ARGS[@]}"
fi

# ---------- Mode 2: Priority file (prepend, then drain) ----------
if [ -n "$PRIORITY_FILE" ]; then
  if [ ! -f "$PRIORITY_FILE" ]; then
    echo "ERROR: file not found: $PRIORITY_FILE" >&2
    exit 1
  fi
  rel="${PRIORITY_FILE#$RAW_ROOT/}"
  echo "Prepending priority file: raw/$rel"
  python3 -c "
import json, os, time
qp = '$QUEUE_PATH'
queue = json.load(open(qp)) if os.path.exists(qp) else []
if not isinstance(queue, list): queue = []
# Remove duplicate
queue = [q for q in queue if q.get('sourcePath') != 'raw/$rel']
# Prepend
queue.insert(0, {
    'id': f'ingest-priority-{int(time.time() * 1000)}',
    'sourcePath': 'raw/$rel',
    'status': 'pending',
    'addedAt': int(time.time() * 1000),
    'retryCount': 0, 'error': None, 'priority': True,
})
os.makedirs(os.path.dirname(qp), exist_ok=True)
json.dump(queue, open(qp, 'w'), ensure_ascii=False, indent=2)
print(f'Prepended. Queue size: {len(queue)}.')
"
  # Fall through to drain mode below
fi

# ---------- Mode 3: Continuous watch ----------
if [ "$WATCH_MODE" = true ]; then
  echo "Starting continuous queue watcher..."
  exec python3 "$INGEST_SCRIPT" --watch --poll-interval 30 "${INGEST_ARGS[@]}"
fi

# ---------- Default: Drain the queue ----------
if [ ! -f "$QUEUE_PATH" ]; then
  echo "Queue not found: $QUEUE_PATH"
  echo "Run wiki-monitor.sh first to populate it, or use --file <path> for one-off."
  exit 0
fi

PENDING=$(python3 -c "
import json
q = json.load(open('$QUEUE_PATH'))
pending = [e for e in q if e.get('status') in ('pending',)]
print(len(pending))
")

if [ "$PENDING" = "0" ]; then
  echo "Queue is empty — nothing to do."
  exit 0
fi

echo "Draining queue: $PENDING pending entries"
PARALLEL_ARG=()
if [ -n "$PARALLEL" ]; then
  PARALLEL_ARG=("--parallel" "$PARALLEL")
fi

exec python3 "$INGEST_SCRIPT" --watch --drain --poll-interval 10 \
  "${PARALLEL_ARG[@]}" "${INGEST_ARGS[@]}"
