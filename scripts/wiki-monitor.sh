#!/bin/bash
# Compatibility entry point. Atomic scan/merge logic lives in queue_cli.py.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/queue_cli.py" scan "$@"
