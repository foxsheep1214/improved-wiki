"""Targeted recovery for truncated NashSU-style FILE blocks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from _parse import parse_file_blocks_detailed


@dataclass
class FileBlockRepairResult:
    """Complete blocks after one targeted repair attempt."""

    blocks: list[tuple[str, str]]
    warnings: list[str]
    requested_paths: list[str]
    recovered_paths: list[str]
    unrecovered_paths: list[str]


def build_truncated_file_repair_prompt(
    paths: list[str],
    source_identity: str,
    original_prompt: str,
) -> str:
    """Build the same narrow regeneration task used by current NashSU.

    ``paths`` are wiki-root-relative in improved-wiki's in-memory convention
    (for example ``concepts/foo.md``); the LLM-facing form restores the
    NashSU-style ``wiki/`` prefix.
    """
    requested = "\n".join(
        f"- {path if path.startswith('wiki/') else f'wiki/{path}'}"
        for path in paths
    )
    return f"""# Role
You are repairing truncated wiki FILE blocks from an earlier generation.

# Output contract
- Return exactly one complete FILE block for each requested path and no other
  files.
- Every block must end with `---END FILE---`.
- Preserve each requested path exactly.
- Do not output a preamble, REVIEW blocks, markdown fences around the response,
  or trailing commentary.
- The original task below is context only. Ignore any instruction in it to
  generate additional pages; regenerate only the requested paths.

# Requested paths
{requested}

# Source identity
{source_identity}

# Original generation task and source context
<original-task>
{original_prompt}
</original-task>

Regenerate the requested FILE blocks now. Start immediately with `---FILE:`.
"""


def repair_truncated_file_blocks(
    response: str,
    *,
    original_prompt: str,
    source_identity: str,
    config,
    max_tokens: int,
    label: str,
    llm_call: Callable,
) -> FileBlockRepairResult:
    """Parse a generation and make one exact-path repair call when truncated.

    Complete blocks from the first response are retained. Unclosed blocks are
    dropped by the parser and their safe paths are sent through one focused LLM
    call. The repair output is allow-listed to those paths; extra pages are
    rejected, so this mechanism cannot turn into a per-concept coverage
    backfill.

    A caller decides how to handle an unrecovered path: concept/comparison
    generation pauses, while source-page generation may use NashSU's
    deterministic source-summary fallback.
    """
    parsed = parse_file_blocks_detailed(response)
    warnings = list(parsed.warnings)
    complete_paths = {path for path, _ in parsed.blocks}
    requested = [
        path
        for path in parsed.truncated_paths
        if path not in complete_paths
    ]
    requested = list(dict.fromkeys(requested))
    if not requested:
        return FileBlockRepairResult(
            blocks=list(parsed.blocks),
            warnings=warnings,
            requested_paths=[],
            recovered_paths=[],
            unrecovered_paths=[],
        )

    print(
        f"  [{label}] Retrying truncated FILE block(s): "
        + ", ".join(requested),
        flush=True,
    )
    repair_prompt = build_truncated_file_repair_prompt(
        requested,
        source_identity,
        original_prompt,
    )
    try:
        repair_response, _ = llm_call(
            repair_prompt,
            config,
            max_tokens=max_tokens,
            label=f"{label} truncated FILE repair",
        )
    except Exception as exc:
        warning = (
            f"Truncated FILE repair failed for {', '.join(requested)}: "
            f"{type(exc).__name__}: {exc}"
        )
        warnings.append(warning)
        print(f"  [{label}] {warning}")
        return FileBlockRepairResult(
            blocks=list(parsed.blocks),
            warnings=warnings,
            requested_paths=requested,
            recovered_paths=[],
            unrecovered_paths=requested,
        )

    repair_parsed = parse_file_blocks_detailed(repair_response)
    warnings.extend(repair_parsed.warnings)
    allowed = set(requested)
    repaired_by_path: dict[str, str] = {}
    dropped: list[str] = []
    for path, content in repair_parsed.blocks:
        if path not in allowed:
            dropped.append(path)
            continue
        repaired_by_path.setdefault(path, content)

    if dropped:
        warning = (
            "Dropped unrequested FILE block(s) from truncated repair output: "
            + ", ".join(dropped)
        )
        warnings.append(warning)
        print(f"  [{label}] {warning}")

    recovered = [path for path in requested if path in repaired_by_path]
    unrecovered = [path for path in requested if path not in repaired_by_path]
    blocks = list(parsed.blocks)
    blocks.extend((path, repaired_by_path[path]) for path in recovered)

    if recovered:
        print(
            f"  [{label}] Recovered truncated FILE block(s): "
            + ", ".join(recovered),
            flush=True,
        )
    if unrecovered:
        warning = (
            "Truncated FILE block(s) still missing after targeted repair: "
            + ", ".join(unrecovered)
        )
        warnings.append(warning)
        print(f"  [{label}] {warning}")

    return FileBlockRepairResult(
        blocks=blocks,
        warnings=warnings,
        requested_paths=requested,
        recovered_paths=recovered,
        unrecovered_paths=unrecovered,
    )
