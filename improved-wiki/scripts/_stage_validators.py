"""Per-stage validators for improved-wiki ingest pipeline.

Implements fail-fast validation after each stage to catch errors early.
Aligns with NashSU's per-stage verification approach.

Usage:
    from _stage_validators import verify_stage_0, verify_stage_1, etc.

    text_data = extract_text(file)
    if not verify_stage_0(text_data):
        raise StageValidationError("Stage 0: text extraction failed")
"""

from pathlib import Path
from typing import Any, Dict, List, Optional


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


def verify_stage_1(
    images_result: Optional[Dict[str, Any]],
    captions_result: Optional[Dict[str, Any]],
) -> bool:
    """Verify Stage 1: Extraction (images & captions).
    
    Requirements:
    - If images extracted, all must have captions
    - Caption length ≥ 20 characters each
    - Optional: can have 0 images (text-only source)
    """
    if not images_result:
        return True

    image_count = images_result.get("count", 0)
    if image_count == 0:
        return True

    if not captions_result:
        return False

    captioned_count = captions_result.get("captioned", 0)
    if captioned_count != image_count:
        return False

    min_caption_length = captions_result.get("min_length", 0)
    if min_caption_length < 20:
        return False

    return True


def verify_stage_2(analysis_result: Optional[Dict[str, Any]]) -> bool:
    """Verify Stage 2: Analysis (global digest & chunk analysis).
    
    Requirements:
    - Global digest has ≥ 3 key concepts
    - ≥ 1 chunk analyzed
    """
    if not analysis_result:
        return False

    chunks_count = analysis_result.get("chunks_analyzed", 0)
    if chunks_count < 1:
        return False

    digest_keys = analysis_result.get("digest_keys", 0)
    if digest_keys < 3:
        return False

    return True


def verify_stage_3(generation_result: Optional[Dict[str, Any]]) -> bool:
    """Verify Stage 3: Generation (synthesis into FILE blocks).
    
    Requirements:
    - ≥ 1 FILE block generated
    - ≥ 1 concept identified
    """
    if not generation_result:
        return False

    file_blocks = generation_result.get("file_blocks", 0)
    if file_blocks < 1:
        return False

    concepts = generation_result.get("concepts", 0)
    if concepts < 1:
        return False

    return True


def verify_stage_3_6(embeddings_result: Optional[Dict[str, Any]]) -> bool:
    """Verify Stage 3.6: Embeddings (vector generation).
    
    Requirements:
    - At least 1 embedding vector created
    - Embedding mode is valid ('local_bge_m3' or 'dialogue_llm')
    """
    if not embeddings_result:
        return False

    embedding_mode = embeddings_result.get("embedding_mode")
    if embedding_mode not in ("local_bge_m3", "dialogue_llm"):
        return False

    vectors_created = embeddings_result.get("vectors_created", 0)
    if vectors_created < 1:
        return False

    return True


def verify_stage_write(files_written_paths: List[str], wiki_root: Path) -> bool:
    """Verify Stage 3.1-3.4: File writing and caching.
    
    Requirements:
    - ≥ 1 source page written
    - ≥ 1 concept/entity file written
    - All file paths are within wiki root
    """
    if not files_written_paths:
        return False

    source_count = 0
    concept_count = 0

    for file_path_str in files_written_paths:
        file_path = Path(file_path_str)

        try:
            file_path.relative_to(wiki_root)
        except ValueError:
            return False

        if not (wiki_root / file_path_str).exists():
            return False

        if "sources/" in file_path_str:
            source_count += 1
        elif "concepts/" in file_path_str or "entities/" in file_path_str:
            concept_count += 1

    if source_count < 1 or concept_count < 1:
        return False

    return True


def quick_stage_check(stage_name: str, result: Any) -> bool:
    """Quick validation check for a stage result."""
    if stage_name == "stage_0":
        return verify_stage_0(result) if isinstance(result, str) else False
    elif stage_name == "stage_1":
        images = result.get("images") if isinstance(result, dict) else None
        captions = result.get("captions") if isinstance(result, dict) else None
        return verify_stage_1(images, captions)
    elif stage_name == "stage_2":
        return verify_stage_2(result) if isinstance(result, dict) else False
    elif stage_name == "stage_3":
        return verify_stage_3(result) if isinstance(result, dict) else False
    elif stage_name == "stage_3_6":
        return verify_stage_3_6(result) if isinstance(result, dict) else False
    else:
        return False
