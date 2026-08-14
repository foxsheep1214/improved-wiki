#!/usr/bin/env python3
"""Write one NashSU v0.6.8-compatible Deep Research query page.

The calling agent remains responsible for source collection and LLM synthesis.
This helper owns the deterministic part of NashSU's ``deep-research.ts``:

* URL/fallback-key deduplication and the 20-source cap;
* Unicode/NFKC query filename generation with a UTC timestamp;
* local-calendar ``created`` metadata;
* ``<think>``/``<thinking>`` removal;
* the v0.6.8 synthesis-completeness gate (exit 4, retryable, nothing written);
* exact research frontmatter and code-generated References.

It deliberately does not auto-ingest the saved page, update aggregate files, or
create reviews. Current NashSU keeps the research result as a directly
searchable query page and only performs an optional page-scoped embedding.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
import json
from pathlib import Path
import re
import sys
import unicodedata

from _paths import atomic_write
# Shared with the review Create Page / save: paths, as NashSU routes all three
# query-page writers through wiki-filename.ts.
from _wiki_filename import make_query_file_name, make_query_slug


MAX_RESEARCH_SOURCES = 20
MIN_RESEARCH_CONTENT_CHARS = 120
MIN_RESEARCH_BLOCK_CHARS = 40
_CITATION_MARKER_RE = re.compile(r"\[([\d,\-\s]+)\]")
_CITATION_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
_HEADING_MARKER_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>\s*[\s\S]*?</think(?:ing)?>\s*",
    flags=re.IGNORECASE,
)
_UNCLOSED_THINK_RE = re.compile(
    r"<think(?:ing)?>\s*[\s\S]*$",
    flags=re.IGNORECASE,
)


def make_deep_research_filename(
    topic: str,
    now: datetime | None = None,
    *,
    local_timezone: tzinfo | None = None,
) -> tuple[str, str]:
    """Return ``(filename, local_created_date)`` for one research topic."""
    return make_query_file_name(
        f"research-{topic}", now, local_timezone=local_timezone)


def research_source_key(source: dict[str, str]) -> str:
    """Port NashSU's URL-first, case-insensitive research-source key."""
    url = source.get("url", "")
    if url:
        return url.lower()
    return (
        f"{source.get('source', '')}:"
        f"{source.get('title', '')}:"
        f"{source.get('snippet', '')}"
    ).lower()


def normalize_research_sources(value: object) -> list[dict[str, str]]:
    """Validate, deduplicate, and cap source records in input order."""
    if isinstance(value, dict):
        value = value.get("results")
    if not isinstance(value, list):
        raise ValueError("Research sources JSON must be an array or an object with a results array")

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Research source {index} must be an object")
        source = {
            key: str(raw.get(key, ""))
            for key in ("title", "url", "snippet", "source")
        }
        if not source["title"].strip():
            raise ValueError(f"Research source {index} has no title")
        key = research_source_key(source)
        if key in seen:
            continue
        seen.add(key)
        results.append(source)
        if len(results) >= MAX_RESEARCH_SOURCES:
            break
    return results


def strip_thinking_blocks(text: str) -> str:
    """Match NashSU's two-regex cleanup and preserve the remaining text."""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.lstrip()


@dataclass
class ResearchSynthesisValidation:
    """Whether a synthesis may be persisted, plus the text that was checked."""

    valid: bool
    cleaned: str
    cited_source_indexes: list[int]
    error: str | None


def meaningful_character_count(content: str) -> int:
    """Count Unicode letters and numbers.

    Language-neutral on purpose: it must not depend on localized section names
    or on language-specific tokenization.
    """
    return sum(1 for char in content if char.isalnum())


def cited_research_source_indexes(content: str, source_count: int) -> list[int]:
    """Return one-based source indexes cited with Markdown-style [N] markers."""
    cited: set[int] = set()
    for match in _CITATION_MARKER_RE.finditer(content):
        for part in match.group(1).split(","):
            token = part.strip()
            span = _CITATION_RANGE_RE.fullmatch(token)
            if span:
                start, end = int(span.group(1)), int(span.group(2))
                if start <= end and end - start <= source_count:
                    cited.update(
                        index
                        for index in range(start, end + 1)
                        if 1 <= index <= source_count
                    )
                continue
            if token.isdigit() and 1 <= int(token) <= source_count:
                cited.add(int(token))
    return sorted(cited)


def validate_research_synthesis(
    content: str,
    source_count: int,
) -> ResearchSynthesisValidation:
    """Gate a synthesis before it can become a wiki page.

    A completed research task must contain substantive prose and cite at least
    one collected source. A stream can finish successfully and still carry no
    assistant prose — for example only a reasoning block — and such output must
    stay retryable instead of being saved as a references-only artifact.

    Validation runs on the same cleaned text that gets persisted, so content
    cannot pass the gate and then become empty at save time.
    """
    cleaned = strip_thinking_blocks(content).strip()
    cited = cited_research_source_indexes(cleaned, source_count)
    blocks = [
        block
        for block in re.split(r"\n\s*\n", cleaned)
        if meaningful_character_count(_HEADING_MARKER_RE.sub("", block))
        >= MIN_RESEARCH_BLOCK_CHARS
    ]
    if meaningful_character_count(cleaned) < MIN_RESEARCH_CONTENT_CHARS or not blocks:
        return ResearchSynthesisValidation(
            valid=False,
            cleaned=cleaned,
            cited_source_indexes=cited,
            error="The research synthesis was empty or incomplete. Please retry.",
        )
    if source_count > 0 and not cited:
        return ResearchSynthesisValidation(
            valid=False,
            cleaned=cleaned,
            cited_source_indexes=cited,
            error="The research synthesis did not cite any collected sources. Please retry.",
        )
    return ResearchSynthesisValidation(
        valid=True,
        cleaned=cleaned,
        cited_source_indexes=cited,
        error=None,
    )


def build_research_page(
    topic: str,
    synthesis: str,
    sources: list[dict[str, str]],
    created_date: str,
) -> str:
    """Assemble the exact deterministic research-page envelope."""
    escaped_topic = topic.replace('"', '\\"')
    references = "\n".join(
        f"{index}. [{source['title']}]({source['url']}) — {source['source']}"
        for index, source in enumerate(sources, 1)
    )
    return "\n".join(
        [
            "---",
            "type: query",
            f'title: "Research: {escaped_topic}"',
            f"created: {created_date}",
            "origin: deep-research",
            "tags: [research]",
            "---",
            "",
            f"# Research: {topic}",
            "",
            strip_thinking_blocks(synthesis),
            "",
            "## References",
            "",
            references,
            "",
        ]
    )


def _parse_now(raw: str | None) -> datetime | None:
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    value = datetime.fromisoformat(candidate)
    if value.tzinfo is None:
        raise ValueError("--now must include a UTC offset or Z")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write a NashSU v0.6.8-compatible Deep Research query page",
    )
    parser.add_argument("--project", required=True, help="Wiki project root")
    parser.add_argument("--topic", required=True, help="Confirmed research topic")
    parser.add_argument(
        "--synthesis-file",
        required=True,
        help="UTF-8 file containing the LLM synthesis verbatim",
    )
    parser.add_argument(
        "--sources-file",
        required=True,
        help="UTF-8 JSON source array (title/url/snippet/source)",
    )
    parser.add_argument("--now", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    wiki_dir = project / "wiki"
    if not wiki_dir.is_dir():
        print(f"ERROR: wiki directory not found: {wiki_dir}", file=sys.stderr)
        return 2

    try:
        source_value = json.loads(
            Path(args.sources_file).expanduser().read_text(encoding="utf-8")
        )
        sources = normalize_research_sources(source_value)
        if not sources:
            print("No research sources found; no page written.", file=sys.stderr)
            return 3
        synthesis = Path(args.synthesis_file).expanduser().read_text(encoding="utf-8")
        file_name, created_date = make_deep_research_filename(
            args.topic,
            _parse_now(args.now),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    validation = validate_research_synthesis(synthesis, len(sources))
    if not validation.valid:
        print(f"ERROR: {validation.error}", file=sys.stderr)
        return 4

    output = wiki_dir / "queries" / file_name
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        output,
        build_research_page(
            args.topic, validation.cleaned, sources, created_date
        ),
    )
    print(output.relative_to(project).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
