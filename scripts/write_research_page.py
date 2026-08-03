#!/usr/bin/env python3
"""Write one NashSU v0.6.7-compatible Deep Research query page.

The calling agent remains responsible for source collection and LLM synthesis.
This helper owns the deterministic part of NashSU's ``deep-research.ts``:

* URL/fallback-key deduplication and the 20-source cap;
* Unicode/NFKC query filename generation with a UTC timestamp;
* local-calendar ``created`` metadata;
* ``<think>``/``<thinking>`` removal;
* exact research frontmatter and code-generated References.

It deliberately does not auto-ingest the saved page, update aggregate files, or
create reviews. Current NashSU keeps the research result as a directly
searchable query page and only performs an optional page-scoped embedding.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, tzinfo
import json
from pathlib import Path
import re
import sys
import unicodedata

from _paths import atomic_write


MAX_RESEARCH_SOURCES = 20
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>\s*[\s\S]*?</think(?:ing)?>\s*",
    flags=re.IGNORECASE,
)
_UNCLOSED_THINK_RE = re.compile(
    r"<think(?:ing)?>\s*[\s\S]*$",
    flags=re.IGNORECASE,
)


def make_query_slug(title: str) -> str:
    """Port NashSU ``makeQuerySlug`` without using ingest's broader slugger."""
    normalized = unicodedata.normalize("NFKC", title).strip()
    slug = re.sub(r"\s+", "-", normalized)
    slug = "".join(
        char
        for char in slug
        if char == "-" or unicodedata.category(char)[:1] in {"L", "N"}
    )
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    truncated = slug[:50]
    return truncated or "query"


def make_deep_research_filename(
    topic: str,
    now: datetime | None = None,
    *,
    local_timezone: tzinfo | None = None,
) -> tuple[str, str]:
    """Return ``(filename, local_created_date)`` for one research topic."""
    instant = now or datetime.now().astimezone()
    if instant.tzinfo is None:
        instant = instant.astimezone()
    local_instant = instant.astimezone(local_timezone) if local_timezone else instant.astimezone()
    utc_instant = instant.astimezone(timezone.utc)
    slug = make_query_slug(f"research-{topic}")
    timestamp = utc_instant.strftime("%Y-%m-%d-%H%M%S")
    return f"{slug}-{timestamp}.md", local_instant.strftime("%Y-%m-%d")


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
        description="Write a NashSU v0.6.7-compatible Deep Research query page",
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

    output = wiki_dir / "queries" / file_name
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        output,
        build_research_page(args.topic, synthesis, sources, created_date),
    )
    print(output.relative_to(project).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
