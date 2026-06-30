#!/usr/bin/env bash
# run-ingest.sh — conversation-mode-aware wrapper around ingest.py.
#
# ingest.py uses exit code 101 to signal a ConversationPending HANDOFF: it has
# written an LLM prompt file and is returning control to the driving agent
# (answer the prompt → re-run). A handoff is NOT a failure — but a task UI that
# only inspects the exit code marks any non-zero exit as "failed", which is
# misleading for the dozens of normal handoffs in a single ingest.
#
# This wrapper presents the handoff as SUCCESS-with-control-transfer:
#   - exit 101 (handoff)  -> print a clear marker, exit 0  (UI: completed)
#   - exit 0   (complete) -> pass through, exit 0          (UI: completed)
#   - any other non-zero  -> pass through                  (UI: failed = real error)
#
# The driver decides continue-vs-stop by OUTPUT CONTENT, not the exit code:
#   - output contains "CONVERSATION →"  -> a handoff is pending: answer + re-run.
#   - output contains "Batch complete" / no CONVERSATION block -> done.
#
# Usage: PY=~/.venv/bin/python3 run-ingest.sh <same args as ingest.py>
#        (PY defaults to ~/.venv/bin/python3 — system python3 lacks 3.11 features.)

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-$HOME/.venv/bin/python3}"

"$PY" "$SCRIPT_DIR/ingest.py" "$@"
ec=$?

if [ "$ec" -eq 101 ]; then
  echo "[handoff] ✅ ConversationPending — control returned to the agent (exit 101 → 0)."
  echo "[handoff] Answer the CONVERSATION prompt shown above (write the .txt), then re-run to advance."
  exit 0
fi

exit "$ec"
