"""Stable process exit codes shared by the ingest CLI and shell wrappers."""
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    BATCH_PAUSED = 75
    PREFETCH_PAUSED = 76
    SPINE_CONFLICT = 77
    COORDINATOR_BUSY = 78
    HANDOFF_PENDING = 101


OK = ExitCode.OK
ERROR = ExitCode.ERROR
USAGE = ExitCode.USAGE
BATCH_PAUSED = ExitCode.BATCH_PAUSED
PREFETCH_PAUSED = ExitCode.PREFETCH_PAUSED
SPINE_CONFLICT = ExitCode.SPINE_CONFLICT
COORDINATOR_BUSY = ExitCode.COORDINATOR_BUSY
HANDOFF_PENDING = ExitCode.HANDOFF_PENDING
