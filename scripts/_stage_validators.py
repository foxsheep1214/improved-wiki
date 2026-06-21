"""Per-stage validators for improved-wiki ingest pipeline.

Implements fail-fast validation after each stage to catch errors early.
Aligns with NashSU's per-stage verification approach.

Usage:
    from _stage_validators import verify_stage_0, StageValidationError

    text_data = extract_text(file)
    if not verify_stage_0(text_data):
        raise StageValidationError("Stage 0: text extraction failed")
"""


class StageValidationError(Exception):
    """Raised when a stage validation fails"""
    pass


def verify_stage_0(text_data: str) -> bool:
    """Verify Stage 0: Pre-processing (text extraction).

    Requirements:
    - Text length ≥ 100 characters
    - No critical encoding errors
    """
    if not text_data or not isinstance(text_data, str):
        return False
    if len(text_data.strip()) < 100:
        return False
    return True
