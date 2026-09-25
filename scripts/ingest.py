#!/usr/bin/env python3
"""Stable ingest CLI facade.

Implementation is split by responsibility:

- ``_ingest_runner``: one source and its completion gate
- ``_batch_supervisor``: Phase-1 workers and the serial write spine
- ``_batch_status``: read-only diagnostics
- ``_ingest_cli``: argument parsing and exit-code mapping

Private imports remain direct aliases for existing automation. Patch the owning
module in tests; this facade never synchronizes or mutates module globals.
"""
from __future__ import annotations

import sys

# Retained direct aliases for callers that imported these from the old facade.
import os
import time

from _core import (
    BATCH_MAX_CONCURRENT,
    ConversationPending,
    PrepareStopAfter,
    detect_template_type,
    set_current_file as _set_current_file,
)
from _progress import (
    ProjectLock,
    file_sha256,
    is_stage_done,
    mark_stage_done,
)
from _batch_coordination import (
    BatchCoordinatorBusy,
    SpineReservationConflict,
    batch_coordinator_slot,
    clear_prefetch_pause_marker,
    is_prefetch_paused,
    load_spine_reservation,
    prefetch_pause_path,
    refresh_spine_reservation,
    release_spine_reservation,
    reserve_spine,
    write_prefetch_pause_marker,
)
from _conversation_router import (
    _load_task_manifest,
    call_anthropic_protocol,
)

# Direct aliases preserve imports; tests patch the owning implementation.
from _config import Config
from _ingest_prepare import _do_prepare
from _ingest_write import _do_write
from _stage_3_7_embed import stage_3_7_embed_new_pages

import _batch_status
import _batch_supervisor
import _ingest_cli
import _ingest_runner


_RUNNER_FUNCTIONS = (
    "_is_ingestable_source_path",
    "_finalize_book",
    "ingest_one",
)
_SUPERVISOR_FUNCTIONS = (
    "_bg_state_path",
    "_load_bg_state",
    "_save_bg_state",
    "_env_float",
    "_safe_int",
    "_safe_float",
    "_batch_source_hash",
    "_batch_pause_path",
    "_write_batch_pause_marker",
    "_clear_batch_pause_marker",
    "_raise_if_batch_paused",
    "_pid_probe",
    "_read_worker_status",
    "_worker_lease_state",
    "_worker_entry_state",
    "_worker_entry_phase",
    "_terminate_bg_worker",
    "_prune_bg_state",
    "_launch_bg_extract",
    "_fill_prefetch_slots",
    "_wait_extract_done",
    "_pause_batch_workers",
    "_run_background_extract_worker",
    "batch_ingest",
    "_assert_batch_resume_order",
    "_batch_ingest_under_coordinator",
)
_STATUS_FUNCTIONS = (
    "_read_json_object",
    "_batch_status_snapshot",
    "_print_batch_status",
)
_CLI_FUNCTIONS = (
    "apply_context_budget",
    "main",
)


for _name in _RUNNER_FUNCTIONS:
    globals()[_name] = getattr(_ingest_runner, _name)
for _name in _SUPERVISOR_FUNCTIONS:
    globals()[_name] = getattr(_batch_supervisor, _name)
for _name in _STATUS_FUNCTIONS:
    globals()[_name] = getattr(_batch_status, _name)
for _name in _CLI_FUNCTIONS:
    globals()[_name] = getattr(_ingest_cli, _name)

BatchPaused = _batch_supervisor.BatchPaused
BatchPrefetchPaused = _batch_supervisor.BatchPrefetchPaused
_BackgroundWorkerInterrupted = _batch_supervisor._BackgroundWorkerInterrupted
_BG_HEARTBEAT_STALE_SECONDS = (
    _batch_supervisor._BG_HEARTBEAT_STALE_SECONDS
)
_BG_WAIT_POLL_SECONDS = _batch_supervisor._BG_WAIT_POLL_SECONDS
_BG_MAX_WALL_SECONDS = _batch_supervisor._BG_MAX_WALL_SECONDS
_BATCH_PREFETCH_PROCESS_LIMIT = (
    _batch_supervisor._BATCH_PREFETCH_PROCESS_LIMIT
)
_WORKER_TERMINAL_STATES = _batch_supervisor._WORKER_TERMINAL_STATES
_WORKER_HAS_MINERU_TURN = _batch_supervisor._WORKER_HAS_MINERU_TURN


if __name__ == "__main__":
    sys.exit(main())
