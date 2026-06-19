#!/bin/bash
# wiki-monitor.sh — Cron entry point for the improved-wiki ingest pipeline.
#
# Scans raw/ for new files and populates the ingest queue.
#   1. For each file under raw/...
#   2. Compute its SHA-256 hash
#   3. Compare against ingest-cache.json
#   4. If hash unchanged: skip
#   5. If hash changed OR not in cache: add to ingest-queue.json
#
# This script is a no-op when the queue is already up to date.
# It's safe to run frequently (just a hash diff, takes <1s for thousands of files).
#
# Configuration via env vars:
#   IMPROVED_WIKI_ROOT  — path to project root (default: cwd)
#
# Cron entry:
#   0 2 * * * /Users/skyfend/.hermes/skills/improved-wiki/scripts/wiki-monitor.sh
#
# Or run manually:
#   $ ./wiki-monitor.sh
#   $ ./wiki-monitor.sh --verbose   # show what was added
#   $ ./wiki-monitor.sh --dry-run   # don't write the queue, just print what would be added

set -euo pipefail

# ---------- Config ----------
WIKI_ROOT="${IMPROVED_WIKI_ROOT:-$(pwd)}"
RAW_ROOT="$WIKI_ROOT/raw"

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
elif [ -f "$WIKI_ROOT/wiki/.ingest-cache.json" ] || [ -f "$WIKI_ROOT/wiki/ingest-cache.json" ] || \
     [ -d "$WIKI_ROOT/wiki/.extract-tmp" ] || [ -d "$WIKI_ROOT/wiki/extract-tmp" ] || \
     [ -d "$WIKI_ROOT/wiki/.ingest-progress" ] || [ -d "$WIKI_ROOT/wiki/ingest-progress" ]; then
    RUNTIME="$WIKI_ROOT/wiki"  # legacy layout
else
    RUNTIME="$WIKI_ROOT/.llm-wiki"
fi

CACHE_PATH="$RUNTIME/ingest-cache.json"
QUEUE_PATH="$RUNTIME/ingest-queue.json"
PROGRESS_DIR="$RUNTIME/ingest-progress"
LOCK_PATH="$RUNTIME/ingest-lock"

# ---------- Flags ----------
VERBOSE=false
DRY_RUN=false
for arg in "$@"; do
  case $arg in
    --verbose|-v) VERBOSE=true ;;
    --dry-run|-n) DRY_RUN=true ;;
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
if [ ! -d "$WIKI_ROOT/wiki" ]; then
  echo "ERROR: wiki/ does not exist under $WIKI_ROOT — run setup first (see references/initial-setup.md)" >&2
  exit 1
fi

# ---------- Acquire lock (atomic — single instance only) ----------
if [ -e "$LOCK_PATH" ]; then
  if pgrep -f "ingest.py\|wiki-monitor.sh" >/dev/null; then
    echo "[monitor] Another instance is running, exiting." >&2
    exit 0
  else
    echo "[monitor] Stale lock found, removing." >&2
    rm -f "$LOCK_PATH"
  fi
fi
trap 'rm -f "$LOCK_PATH"' EXIT
touch "$LOCK_PATH"

# ---------- Helpers ----------
log() {
  if [ "$VERBOSE" = true ] || [ "$DRY_RUN" = true ]; then
    echo "[monitor] $1"
  fi
}

# Compute sha256 of a file (works on macOS and Linux)
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    # macOS shasum
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# Get the cached hash for a file (relative to raw/), or "" if not cached
cached_hash() {
  local rel="$1"
  if [ ! -f "$CACHE_PATH" ]; then
    echo ""
    return
  fi
  # Use python to safely extract from JSON
  python3 -c "
import json, sys
try:
    with open('$CACHE_PATH', 'r', encoding='utf-8') as f:
        cache = json.load(f)
    rel = sys.argv[1]
    entry = cache.get('entries', {}).get(rel)
    if entry:
        print(entry.get('hash', ''))
    else:
        print('')
except Exception:
    print('')
" "$rel" 2>/dev/null
}

# ---------- Read existing queue ----------
if [ -f "$QUEUE_PATH" ]; then
  QUEUE_CONTENT=$(cat "$QUEUE_PATH")
else
  QUEUE_CONTENT="[]"
fi

# ---------- Walk raw/ and diff ----------
ADDED=0
SKIPPED=0
FAILED=0
TO_ADD=""

# Use python for the walk to handle CJK filenames safely
python3 <<EOF > "/tmp/wiki-monitor-$$.json"
import hashlib
import json
import os
import sys
from pathlib import Path

raw_root = Path("$RAW_ROOT")
wiki_root = Path("$WIKI_ROOT")
cache_path = Path("$CACHE_PATH")
to_add = []

# Load cache
cache = {}
if cache_path.exists():
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8")).get("entries", {})
    except Exception:
        cache = {}

# Walk raw/ recursively
for path in sorted(raw_root.rglob("*")):
    if not path.is_file():
        continue
    # Skip hidden files and the queue / cache files
    if any(part.startswith(".") for part in path.relative_to(raw_root).parts):
        continue
    if path.name in {"ingest-cache.json", "ingest-queue.json", "ingest-lock"}:
        continue

    # Compute hash
    try:
        h = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as e:
        print(f"ERROR: failed to hash {path}: {e}", file=sys.stderr)
        continue

    # Check cache
    rel = str(path.relative_to(raw_root))
    cached = cache.get(rel, {}).get("hash")
    if cached == h:
        continue  # already ingested, skip

    to_add.append({
        "rel": rel,
        "hash": h,
        "size": path.stat().st_size,
        "addedAt": int(time.time() * 1000) if False else 0,  # placeholder
    })

# Add timestamps
import time
for item in to_add:
    item["addedAt"] = int(time.time() * 1000)

print(json.dumps(to_add, ensure_ascii=False))
EOF

# Load the result
TO_ADD_JSON=$(cat "/tmp/wiki-monitor-$$.json")
rm -f "/tmp/wiki-monitor-$$.json"
ADDED_COUNT=$(echo "$TO_ADD_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")

if [ "$ADDED_COUNT" = "0" ]; then
  log "No new or changed files. Queue is up to date."
  exit 0
fi

log "Found $ADDED_COUNT new/changed files."

if [ "$DRY_RUN" = true ]; then
  echo "DRY RUN — would add to $QUEUE_PATH:"
  echo "$TO_ADD_JSON" | python3 -c "
import json, sys
items = json.load(sys.stdin)
for item in items:
    print(f'  + {item[\"rel\"]}  ({item[\"size\"]:,} bytes, hash {item[\"hash\"][:16]})')
"
  exit 0
fi

# ---------- Merge into existing queue ----------
python3 <<EOF > "$QUEUE_PATH.tmp"
import json
import sys

queue_path = "$QUEUE_PATH"
to_add = json.loads('''$TO_ADD_JSON''')

# Load existing queue
if __import__('os').path.exists(queue_path):
    with open(queue_path, 'r', encoding='utf-8') as f:
        queue = json.load(f)
    if not isinstance(queue, list):
        queue = []
else:
    queue = []

# Find existing entries by source path
existing = {Path-like: i for i, Path-like in enumerate([])}  # placeholder
existing_paths = {entry.get("sourcePath"): i for i, entry in enumerate(queue) if "sourcePath" in entry}

# Merge — for each new item, if not already in queue, append
added_now = 0
for item in to_add:
    rel = item["rel"]
    if rel in existing_paths:
        # Already queued — skip (or could update hash)
        continue
    queue.append({
        "id": f"ingest-{item['addedAt']}-{rel.replace('/', '-').replace(' ', '_')[:32]}",
        "sourcePath": f"raw/{rel}",
        "status": "pending",
        "addedAt": item["addedAt"],
        "hash": item["hash"],
        "size": item["size"],
        "retryCount": 0,
        "error": None,
    })
    added_now += 1

with open(queue_path, 'w', encoding='utf-8') as f:
    json.dump(queue, f, ensure_ascii=False, indent=2)

print(f"Added {added_now} new entries. Queue size: {len(queue)}.")
EOF

mv "$QUEUE_PATH.tmp" "$QUEUE_PATH"

log "Done. Run ./run-queue.sh to process the queue."
exit 0
