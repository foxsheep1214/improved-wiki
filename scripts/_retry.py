"""Shared retry policy for conversation-mode LLM operations."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from _llm_api import _is_retryable_exception, _retry_jitter


def call_with_retry(
    fn: Callable[[], Any],
    max_retries: int = 3,
    base_wait: float = 1.0,
    label: str = "",
):
    """Call ``fn`` with the pipeline's retryable-error and jitter policy.

    ``label`` remains accepted for call-site compatibility.
    """
    del label
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as error:
            last_error = error
            if _is_retryable_exception(error) and attempt < max_retries - 1:
                time.sleep(_retry_jitter(base_wait, attempt))
                continue
            raise
    raise last_error  # pragma: no cover - the loop always returns or raises
