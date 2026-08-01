from __future__ import annotations

import json
import re
import time
from pathlib import Path

from _core import canonical_source_path


def _normalize_source_frontmatter(
    response: str, authors_yaml: str, year_yaml: str, url_yaml: str, venue_yaml: str,
) -> str:
    """Normalize the source-page FILE block's frontmatter when the agent's
    Stage 2.6 response ignored the pre-filled template:

    Inject any missing NashSU-parity bibliographic fields
       (authors/year/url/venue) using the values already computed from the
       digest — root cause of the Strauss/Witte pages lacking them.

    The pipeline writes the FILE block verbatim, so a dropped field or empty
    bibliographic field would otherwise persist to disk. A well-formed,
    already-complete block is left untouched (no-op on parse failure or nothing
    to fill). ``related: []`` is valid; NashSU does not impose a related-count
    quota.
    """
    lines = response.split("\n")
    # Locate the FILE block's frontmatter: the `---FILE:...---` line, then the
    # opening `---`, then the next standalone `---` closes the frontmatter.
    file_idx = next((i for i, ln in enumerate(lines) if ln.startswith("---FILE:")), None)
    if file_idx is None or file_idx + 1 >= len(lines) or lines[file_idx + 1].strip() != "---":
        return response
    fm_open = file_idx + 1
    fm_close = next((i for i in range(fm_open + 1, len(lines)) if lines[i].strip() == "---"), None)
    if fm_close is None:
        return response

    fm = lines[fm_open + 1:fm_close]
    desired = {
        "authors": authors_yaml,
        "year": year_yaml,
        "url": url_yaml,
        "venue": venue_yaml,
    }
    empty_yaml = {"", "[]", '""', "''", "null", "~"}

    # A generated block may keep a field but blank out a value that the digest
    # already supplied. Treat that the same as a missing field; otherwise the
    # pre-filled bibliographic contract can still be silently lost.
    for index, line in enumerate(fm):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if (
            key in desired
            and value.strip().lower() in empty_yaml
            and desired[key].strip().lower() not in empty_yaml
        ):
            fm[index] = f"{key}: {desired[key]}"
    lines[fm_open + 1:fm_close] = fm
    present = {ln.split(":", 1)[0].strip() for ln in fm if ":" in ln}

    # Inject missing bibliographic fields before the frontmatter close.
    additions = [
        f"{key}: {val}"
        for key, val in desired.items()
        if key not in present
    ]
    if additions:
        lines[fm_close:fm_close] = additions

    return "\n".join(lines)


def _validate_source_file_block(
    response: str,
    source_rel: str,
) -> None:
    """Require one well-formed, non-empty source FILE block at the exact path.

    This is the NashSU-aligned structural gate: it protects parser/write
    integrity without prescribing body headings or the number of concepts,
    entities, or claims in the summary.
    """
    header_pattern = r"^---\s*FILE:\s*(.*?)\s*---\s*$"
    header_matches = list(re.finditer(
        header_pattern,
        response,
        re.MULTILINE | re.IGNORECASE,
    ))
    headers = [match.group(1) for match in header_matches]
    expected = f"wiki/sources/{source_rel}.md"
    normalized = [path.strip() for path in headers]
    if normalized != [expected]:
        raise RuntimeError(
            "Stage 2.6 must emit exactly one source FILE block at "
            f"{expected}; got {normalized or 'none'}."
        )
    if len(re.findall(
            r"^---\s*END\s+FILE\s*---\s*$",
            response,
            re.MULTILINE | re.IGNORECASE,
    )) != 1:
        raise RuntimeError(
            "Stage 2.6 source FILE block must have exactly one END FILE marker."
        )

    start = header_matches[0].end()
    if start < len(response) and response[start] == "\n":
        start += 1
    content = response[start:]
    end_match = re.search(
        r"^---\s*END\s+FILE\s*---\s*$",
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    if not end_match:
        raise RuntimeError("Stage 2.6 source FILE block is not closed.")
    file_content = content[:end_match.start()]
    lines = file_content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(
            "Stage 2.6 source FILE block must start with YAML frontmatter."
        )
    fm_close = next(
        (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if fm_close is None or not "\n".join(lines[fm_close + 1:]).strip():
        raise RuntimeError(
            "Stage 2.6 source FILE block must contain a non-empty body."
        )


def source_analysis_text(
    global_digest: dict,
    chunk_analyses: list[dict] | None = None,
    chunk_claims: list | None = None,
) -> str:
    """Serialize the complete Stage 2 analysis for deterministic recovery.

    NashSU's fallback source page preserves its full analysis rather than
    cutting it to a summary-sized prefix.  improved-wiki's equivalent analysis
    is the rolled-up digest plus every per-chunk analysis.  ``chunk_claims`` is
    retained as a compatibility fallback for older callers that do not carry
    the full chunk list.
    """
    payload: dict = {"global_digest": global_digest}
    if chunk_analyses is not None:
        payload["chunk_analyses"] = chunk_analyses
    elif chunk_claims is not None:
        payload["chunk_claims"] = chunk_claims
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def build_fallback_source_summary_content(
    source_identity: str,
    analysis_text: str,
    date: str,
) -> str:
    """Build NashSU's deterministic minimum source-summary page."""
    source_yaml = json.dumps(source_identity, ensure_ascii=False)
    title_yaml = json.dumps(
        f"Source: {source_identity}",
        ensure_ascii=False,
    )
    return "\n".join([
        "---",
        "type: source",
        f"title: {title_yaml}",
        f"created: {date}",
        f"updated: {date}",
        f"sources: [{source_yaml}]",
        "tags: []",
        "related: []",
        "---",
        "",
        f"# Source: {source_identity}",
        "",
        analysis_text or "(Analysis not available)",
        "",
    ])


def build_fallback_source_summary(
    source_rel: str,
    source_identity: str,
    analysis_text: str,
    date: str,
) -> str:
    """Wrap the deterministic source summary in one exact FILE block."""
    content = build_fallback_source_summary_content(
        source_identity,
        analysis_text,
        date,
    )
    return (
        f"---FILE:wiki/sources/{source_rel}.md---\n"
        f"{content.rstrip()}\n"
        "---END FILE---\n"
    )
