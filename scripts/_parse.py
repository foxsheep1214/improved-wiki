"""YAML and FILE-block parsers used by the ingest pipeline."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


def parse_yaml_block(response: str) -> dict:
    """Extract the first YAML block from an LLM response."""
    match = re.search(r"```yaml\s*\n(.*?)\n```", response, re.DOTALL)
    yaml_text = match.group(1) if match else response
    from _stage_1_1_scanned import _decode_html_entities

    yaml_text = _decode_html_entities(yaml_text)
    try:
        import yaml

        return yaml.safe_load(yaml_text) or {}
    except ImportError:
        return parse_simple_yaml(yaml_text)
    except Exception as error:
        print(
            f"[parse] yaml.safe_load failed "
            f"({type(error).__name__}: {error}) "
            "— falling back to simple parser"
        )
        return parse_simple_yaml(yaml_text)


def _yaml_is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return (not stripped) or stripped.startswith("#")


def _yaml_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _yaml_scalar(value: str) -> Any:
    """Parse a scalar or inline flow collection in the supported YAML subset."""
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(part) for part in _yaml_split_flow(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        result: dict[str, Any] = {}
        for part in _yaml_split_flow(inner):
            if ":" in part:
                key, item = part.split(":", 1)
                result[key.strip()] = _yaml_scalar(item)
        return result
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in ('"', "'")
    ):
        return value[1:-1]
    return value


def _yaml_split_flow(inner: str) -> list[str]:
    """Split a flow collection on commas outside quotes or nested collections."""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote = ""
    for char in inner:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
        elif char in ('"', "'"):
            quote = char
            buffer.append(char)
        elif char in "[{":
            depth += 1
            buffer.append(char)
        elif char in "]}":
            depth -= 1
            buffer.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    if buffer:
        parts.append("".join(buffer).strip())
    return [part for part in parts if part]


def _yaml_dedent_block(block_lines: list[str]) -> str:
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()
    indents = [_yaml_indent(line) for line in block_lines if line.strip()]
    base = min(indents) if indents else 0
    return "\n".join(
        line[base:] if len(line) >= base else line
        for line in block_lines
    )


def _yaml_parse_block(
    lines: list[str],
    index: int,
    min_indent: int,
) -> tuple[Any, int]:
    count = len(lines)
    while index < count and _yaml_is_blank_or_comment(lines[index]):
        index += 1
    if index >= count or _yaml_indent(lines[index]) < min_indent:
        return {}, index
    if lines[index].strip().startswith("- "):
        return _yaml_parse_list(lines, index, _yaml_indent(lines[index]))
    return _yaml_parse_map(lines, index, _yaml_indent(lines[index]))


_YAML_KEY_RE = re.compile(r"^([\w][\w_\-./]*):\s?(.*)$")
_YAML_BLOCK_SCALAR = {"|", ">", "|-", ">-", "|+", ">+"}


def _yaml_parse_map(
    lines: list[str],
    index: int,
    indent: int,
) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    count = len(lines)
    while index < count:
        if _yaml_is_blank_or_comment(lines[index]):
            index += 1
            continue
        current_indent = _yaml_indent(lines[index])
        if current_indent < indent:
            break
        if current_indent > indent:
            index += 1
            continue
        stripped = lines[index].strip()
        if stripped.startswith("- "):
            break
        match = _YAML_KEY_RE.match(stripped)
        if not match:
            index += 1
            continue
        key, raw_value = match.group(1), match.group(2).strip()
        index += 1
        if raw_value in _YAML_BLOCK_SCALAR:
            block: list[str] = []
            while index < count and (
                _yaml_is_blank_or_comment(lines[index])
                or _yaml_indent(lines[index]) > indent
            ):
                block.append(lines[index])
                index += 1
            result[key] = _yaml_dedent_block(block)
        elif raw_value == "":
            next_index = index
            while (
                next_index < count
                and _yaml_is_blank_or_comment(lines[next_index])
            ):
                next_index += 1
            if (
                next_index < count
                and _yaml_indent(lines[next_index]) > indent
            ):
                child, index = _yaml_parse_block(lines, index, indent + 1)
                result[key] = child
            else:
                result[key] = []
        else:
            result[key] = _yaml_scalar(raw_value)
    return result, index


def _yaml_parse_list(
    lines: list[str],
    index: int,
    indent: int,
) -> tuple[list, int]:
    items: list[Any] = []
    count = len(lines)
    while index < count:
        if _yaml_is_blank_or_comment(lines[index]):
            index += 1
            continue
        current_indent = _yaml_indent(lines[index])
        if (
            current_indent != indent
            or not lines[index].strip().startswith("- ")
        ):
            break
        rest = lines[index].strip()[2:]
        if _YAML_KEY_RE.match(rest):
            item_indent = indent + 2
            nested = [(" " * item_indent) + rest]
            index += 1
            while index < count and (
                _yaml_is_blank_or_comment(lines[index])
                or _yaml_indent(lines[index]) > indent
            ):
                nested.append(lines[index])
                index += 1
            value, _ = _yaml_parse_map(nested, 0, item_indent)
            items.append(value)
        else:
            items.append(_yaml_scalar(rest))
            index += 1
    return items, index


def parse_simple_yaml(text: str):
    """Parse the indentation-aware YAML subset emitted by ingest prompts."""
    value, _ = _yaml_parse_block(text.split("\n"), 0, 0)
    return value


@dataclass
class FileBlockParseResult:
    """Structured result for NashSU-style FILE-block parsing.

    ``truncated_paths`` contains safe wiki-relative paths whose FILE opener was
    seen but whose END FILE marker never arrived.  Those partial bodies are
    deliberately excluded from ``blocks`` so the caller can regenerate exactly
    the missing paths instead of publishing incomplete Markdown.
    """

    blocks: list[tuple[str, str]]
    warnings: list[str]
    truncated_paths: list[str]


def parse_file_blocks_detailed(response: str) -> FileBlockParseResult:
    """Extract complete NashSU or legacy FILE blocks and surface truncation.

    This mirrors NashSU's current parser contract: an unclosed FILE block is
    reported and dropped rather than treated as a valid page.  Unlike the
    upstream parser, a later FILE opener is also treated as a recovery boundary,
    allowing a malformed middle block to be repaired without losing subsequent
    complete blocks.
    """
    from _schema import is_safe_ingest_path

    response = response.replace("\r\n", "\n")
    blocks: list[tuple[str, str]] = []
    warnings: list[str] = []
    truncated_paths: list[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        print(f"  [parse] {message}")

    def note_truncated(path: str, boundary: str) -> None:
        warn(
            f'FILE block "{path}" was not closed before {boundary} — likely '
            "truncation (model hit max_tokens, timeout, or connection "
            "dropped). Block dropped."
        )
        if path not in truncated_paths:
            truncated_paths.append(path)

    file_header_re = re.compile(
        r"^---\s*FILE:\s*(wiki/)?(.+?)\s*---\s*$",
        re.IGNORECASE,
    )
    end_file_re = re.compile(
        r"^---\s*END\s+FILE\s*---\s*$",
        re.IGNORECASE,
    )
    fence_re = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
    known_subdirs = (
        "sources",
        "concepts",
        "entities",
        "queries",
        "comparisons",
        "synthesis",
        "findings",
        "thesis",
        "methodology",
    )

    lines = response.split("\n")
    # Fence state is tracked only *inside* a FILE block, mirroring NashSU's
    # scoping (parseFileBlocks declares fenceMarker in the inner per-block
    # loop; its outer opener scan is fence-blind).  Hoisting the state to the
    # whole response let any fence outside a block swallow every opener that
    # followed: an agent wrapping its entire answer in ```markdown produced
    # zero blocks *and* zero warnings, and a stray fence between two complete
    # blocks silently dropped the second one.
    fence_marker: str | None = None
    fence_length = 0
    current_path: str | None = None
    current_lines: list[str] = []

    for line in lines:
        is_fence_line = False
        fence_match = fence_re.match(line) if current_path is not None else None
        if fence_match:
            run = fence_match.group(1)
            char = run[0]
            length = len(run)
            if fence_marker is None:
                fence_marker = char
                fence_length = length
            elif char == fence_marker and length >= fence_length:
                fence_marker = None
                fence_length = 0
            is_fence_line = True

        if fence_marker is None and not is_fence_line:
            if end_file_re.match(line) and current_path is not None:
                content = "\n".join(current_lines).rstrip() + "\n"
                blocks.append((current_path, content))
                current_path = None
                current_lines = []
                continue

            file_match = file_header_re.match(line)
            if file_match:
                if current_path is not None:
                    note_truncated(current_path, "the next FILE block")
                path = file_match.group(2).strip()
                if not path:
                    warn(
                        "FILE block with empty path skipped "
                        "(LLM omitted the path after ---FILE:)."
                    )
                    current_path = None
                    current_lines = []
                    continue
                if not path.endswith(".md"):
                    # Append the suffix rather than dropping the block. The
                    # write path already defends against this exact slip
                    # (_stage_3_write / _ingest_write both append `.md` with a
                    # note that "agent sometimes outputs paths without .md"),
                    # but killing the block here made that defence unreachable
                    # and lost the page with no warning. NashSU's opener has no
                    # extension check at all (ingest.ts:389-425).
                    warn(f"FILE path missing .md suffix — writing as {path}.md")
                    path = path + ".md"
                parts = path.split("/")
                if len(parts) > 2 and parts[0] != "sources":
                    corrected = f"{parts[0]}/{'-'.join(parts[1:])}"
                    print(f"  [parse] merged / in slug: {path} → {corrected}")
                    path = corrected
                for subdir in known_subdirs:
                    prefix = f"{subdir}-"
                    if path.startswith(prefix):
                        corrected = f"{subdir}/{path[len(prefix):]}"
                        print(
                            f"  [parse] corrected path: {path} → {corrected}"
                        )
                        path = corrected
                        break
                if not is_safe_ingest_path(path):
                    warn(f"unsafe path rejected: {path}")
                    current_path = None
                    current_lines = []
                    continue
                current_path = path
                current_lines = []
                continue

        if current_path is not None:
            current_lines.append(line)

    if current_path is not None:
        note_truncated(current_path, "end of stream")

    if blocks:
        return FileBlockParseResult(blocks, warnings, truncated_paths)

    legacy_header_re = re.compile(
        r"^###\s+File\s+(\d+):\s*([^\n]+\.md)\s*$",
        re.MULTILINE,
    )
    matches = list(legacy_header_re.finditer(response))
    for index, match in enumerate(matches):
        path = match.group(2).strip()
        start = match.end()
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(response)
        )
        content = response[start:end].rstrip() + "\n"
        if path.startswith("wiki/"):
            path = path[len("wiki/"):]
        if not path.endswith(".md"):
            continue
        if not is_safe_ingest_path(path):
            warn(f"unsafe path rejected: {path}")
            continue
        blocks.append((path, content))
    return FileBlockParseResult(blocks, warnings, truncated_paths)


def parse_file_blocks(response: str) -> list[tuple[str, str]]:
    """Compatibility facade returning only complete FILE blocks."""
    return parse_file_blocks_detailed(response).blocks
