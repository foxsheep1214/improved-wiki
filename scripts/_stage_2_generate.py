"""Phase 2 backward-compat aliases — all functions now live in dedicated modules.

Split 2026-06-21: _stage_2_generate.py (1414 lines) → 5 focused modules.
This file re-exports everything so existing imports still work.
"""
from _stage_2_4_generation import (           # noqa: F401
    _stage_2_4_generate_chunk,
    _stage_2_4_build_prompt,
    _stage_2_4_per_concept_fallback,
    _stage_2_4_extract_names,
    _build_image_reference_section,
    _build_per_concept_prompt,
    _build_per_entity_prompt,
    PER_CONCEPT_BATCH_MAX,
)
from _stage_2_6_source_page import (           # noqa: F401
    stage_2_6_source_page,
)
from _stage_2_7_query_generation import (      # noqa: F401
    stage_2_7_query_generation,
    _stage_2_7_build_prompt,
)
from _stage_2_9_comparison import (             # noqa: F401
    stage_2_9_comparison_generation,
    _stage_2_9_build_prompt_disambiguation,
    _stage_2_9_build_prompt_in_source,
)
from _stage_2_10_review import (                # noqa: F401
    stage_2_10_review_suggestions,
)

__all__ = [
    "stage_2_6_source_page",
    "stage_2_7_query_generation",
    "stage_2_9_comparison_generation",
    "stage_2_10_review_suggestions",
    "_stage_2_4_generate_chunk",
    "_stage_2_4_build_prompt",
    "_stage_2_4_per_concept_fallback",
    "_stage_2_4_extract_names",
    "_build_image_reference_section",
    "_stage_2_7_build_prompt",
    "_stage_2_9_build_prompt_disambiguation",
    "_stage_2_9_build_prompt_in_source",
]
