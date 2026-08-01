"""NashSU 0.6.6-compatible Markdown chunking for embeddings.

This is a direct Python port of ``src/lib/text-chunker.ts`` from
llm_wiki-0.6.6.  It intentionally keeps the same defaults and ordering:

* target 1000 chars, hard maximum 1500 chars, minimum 200 chars, overlap 200;
* split by heading-defined section before paragraph/line/sentence/space;
* never split a fenced code block or a multi-line Markdown table;
* strip YAML frontmatter and carry a heading breadcrumb on every chunk;
* merge tiny chunks before injecting overlap.

Python counts Unicode code points while JavaScript counts UTF-16 code units.
That only changes boundaries for non-BMP characters (for example emoji); the
Markdown and CJK behavior used by the wiki is otherwise equivalent.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


DEFAULT_TARGET_CHARS = 1000
DEFAULT_MAX_CHARS = 1500
DEFAULT_MIN_CHARS = 200
DEFAULT_OVERLAP_CHARS = 200


@dataclass(frozen=True)
class MarkdownChunk:
    index: int
    text: str
    heading_path: str
    oversized: bool


@dataclass(frozen=True)
class _Options:
    target_chars: int
    max_chars: int
    min_chars: int
    overlap_chars: int


@dataclass(frozen=True)
class _Section:
    text: str
    body_start: int
    heading_path: str


@dataclass(frozen=True)
class _Atom:
    text: str
    offset: int
    indivisible: bool
    kind: str


@dataclass(frozen=True)
class _Piece:
    text: str
    offset: int


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SENTENCE_RE = re.compile(r"([。！？!?；;]+\s*|(?:\.\s+))")
_WHITESPACE_RE = re.compile(r"(\s+)")


def strip_frontmatter(content: str) -> tuple[str, int]:
    """Return ``(body, body_offset)`` for a leading YAML frontmatter block."""
    opener = re.match(r"\A---\r?\n", content)
    if not opener:
        return content, 0
    close = re.search(
        r"(?m)^---[ \t]*(?:\r?\n|\Z)",
        content[opener.end():],
    )
    if not close:
        return content, 0
    body_offset = opener.end() + close.end()
    return content[body_offset:], body_offset


def chunk_markdown(
    content: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[MarkdownChunk]:
    """Split one Markdown page using NashSU 0.6.6 embedding semantics."""
    target_chars = max(1, int(target_chars))
    max_chars = max(1, int(max_chars))
    min_chars = max(0, int(min_chars))
    overlap_chars = max(0, int(overlap_chars))
    if max_chars < target_chars:
        max_chars = target_chars
    if overlap_chars >= target_chars:
        overlap_chars = target_chars // 2
    opts = _Options(target_chars, max_chars, min_chars, overlap_chars)

    body, body_offset = strip_frontmatter(content)
    if not body.strip():
        return []

    out: list[MarkdownChunk] = []
    for section in _split_into_sections(body, body_offset):
        for piece in _chunk_section(section, opts):
            out.append(
                MarkdownChunk(
                    index=len(out),
                    text=piece.text,
                    heading_path=section.heading_path,
                    oversized=len(piece.text) > opts.max_chars,
                )
            )
    return out


def _split_into_sections(body: str, body_offset: int) -> list[_Section]:
    lines = body.split("\n")
    sections: list[_Section] = []
    headings: dict[int, str] = {}
    current_lines: list[str] = []
    current_start = body_offset
    current_heading_path = ""
    in_fence = False
    fence_marker = ""
    char_cursor = body_offset

    def flush() -> None:
        text = "\n".join(current_lines)
        if text.strip():
            sections.append(_Section(text, current_start, current_heading_path))

    for index, line in enumerate(lines):
        line_len = len(line) + (1 if index < len(lines) - 1 else 0)
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0] * len(marker)
            elif line.startswith(fence_marker) and line.strip() == fence_marker:
                in_fence = False
            current_lines.append(line)
            char_cursor += line_len
            continue

        heading_match = None if in_fence else _HEADING_RE.match(line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            headings[level] = heading_match.group(2).strip()
            for deeper in range(level + 1, 7):
                headings.pop(deeper, None)
            path_parts = [
                f"{'#' * item_level} {headings[item_level]}"
                for item_level in range(1, 7)
                if item_level in headings
            ]
            current_lines = [line]
            current_start = char_cursor
            current_heading_path = " > ".join(path_parts)
            char_cursor += line_len
            continue

        current_lines.append(line)
        char_cursor += line_len

    flush()
    return sections


def _chunk_section(section: _Section, opts: _Options) -> list[_Piece]:
    if len(section.text) <= opts.target_chars:
        return [_Piece(section.text, 0)]
    atoms = _tokenize_atoms(section.text)
    pieces = _split_atoms_to_pieces(atoms, opts)
    sized = _size_pieces(pieces, opts)
    merged = _merge_small(sized, opts)
    return _apply_overlap(merged, opts)


def _tokenize_atoms(text: str) -> list[_Atom]:
    atoms: list[_Atom] = []
    lines = text.split("\n")
    cursor = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            start = cursor
            body_lines = [line]
            scan = index + 1
            cursor += len(line) + 1
            while scan < len(lines):
                body_lines.append(lines[scan])
                cursor += len(lines[scan]) + 1
                if lines[scan].startswith(marker) and lines[scan].strip() == marker:
                    scan += 1
                    break
                scan += 1
            atoms.append(_Atom("\n".join(body_lines), start, True, "code"))
            index = scan
            continue

        if line.startswith("|"):
            scan = index
            while scan < len(lines) and lines[scan].startswith("|"):
                scan += 1
            if scan - index >= 2:
                start = cursor
                content = "\n".join(lines[index:scan])
                cursor += len(content) + (1 if scan < len(lines) else 0)
                atoms.append(_Atom(content, start, True, "table"))
                index = scan
                continue

        if not line.strip():
            cursor += len(line) + 1
            index += 1
            continue

        start = cursor
        body_lines: list[str] = []
        while (
            index < len(lines)
            and lines[index].strip()
            and not _FENCE_RE.match(lines[index])
        ):
            body_lines.append(lines[index])
            cursor += len(lines[index]) + 1
            index += 1
        atoms.append(_Atom("\n".join(body_lines), start, False, "paragraph"))

    return atoms


def _split_atoms_to_pieces(atoms: list[_Atom], opts: _Options) -> list[_Piece]:
    pieces: list[_Piece] = []
    for atom in atoms:
        if atom.indivisible:
            pieces.append(_Piece(atom.text, atom.offset))
        elif len(atom.text) <= opts.target_chars:
            pieces.append(_Piece(atom.text, atom.offset))
        else:
            pieces.extend(_recursive_split(atom.text, atom.offset, opts.target_chars))
    return pieces


def _recursive_split(text: str, base_offset: int, target_chars: int) -> list[_Piece]:
    paragraph_pieces = _split_keeping_separator(text, re.compile(r"(\n{2,})"))
    out: list[_Piece] = []
    cursor = base_offset

    for chunk in paragraph_pieces:
        if not chunk:
            continue
        if len(chunk) <= target_chars:
            out.append(_Piece(chunk, cursor))
            cursor += len(chunk)
            continue

        for splitter in (
            lambda value: _split_keeping_separator(value, re.compile(r"(\n+)")),
            lambda value: _split_keeping_separator(value, _SENTENCE_RE),
            lambda value: _split_keeping_separator(value, _WHITESPACE_RE),
        ):
            subs = splitter(chunk)
            if len(subs) > 1 and all(len(item) <= target_chars for item in subs):
                sub_cursor = cursor
                for item in subs:
                    if item:
                        out.append(_Piece(item, sub_cursor))
                        sub_cursor += len(item)
                cursor += len(chunk)
                break

            any_too_big = False
            sub_out: list[_Piece] = []
            sub_cursor = cursor
            for item in subs:
                if not item:
                    continue
                if len(item) <= target_chars:
                    sub_out.append(_Piece(item, sub_cursor))
                else:
                    any_too_big = True
                sub_cursor += len(item)
            if not any_too_big and len(subs) > 1:
                out.extend(sub_out)
                cursor += len(chunk)
                break

        if (
            not out
            or out[-1].offset + len(out[-1].text) <= cursor
        ):
            slice_cursor = cursor
            for start in range(0, len(chunk), target_chars):
                item = chunk[start:start + target_chars]
                out.append(_Piece(item, slice_cursor))
                slice_cursor += len(item)
            cursor += len(chunk)

    return out


def _split_keeping_separator(text: str, pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    last = 0
    for match in pattern.finditer(text):
        end = match.end()
        out.append(text[last:end])
        last = end
    if last < len(text):
        out.append(text[last:])
    return [item for item in out if item]


def _size_pieces(pieces: list[_Piece], opts: _Options) -> list[_Piece]:
    out: list[_Piece] = []
    buffer = ""
    buffer_offset: int | None = None
    for piece in pieces:
        if not piece.text:
            continue
        if len(piece.text) > opts.target_chars:
            if buffer and buffer_offset is not None:
                out.append(_Piece(buffer, buffer_offset))
            out.append(piece)
            buffer = ""
            buffer_offset = None
            continue
        if (
            buffer
            and buffer_offset is not None
            and len(buffer) + len(piece.text) > opts.target_chars
        ):
            out.append(_Piece(buffer, buffer_offset))
            buffer = piece.text
            buffer_offset = piece.offset
            continue
        if not buffer:
            buffer_offset = piece.offset
        buffer += piece.text
    if buffer and buffer_offset is not None:
        out.append(_Piece(buffer, buffer_offset))
    return out


def _merge_small(pieces: list[_Piece], opts: _Options) -> list[_Piece]:
    if len(pieces) < 2:
        return pieces
    out: list[_Piece] = []
    for piece in pieces:
        previous = out[-1] if out else None
        if (
            previous is not None
            and len(previous.text) < opts.min_chars
            and len(previous.text) + len(piece.text) <= opts.max_chars
        ):
            out[-1] = _Piece(previous.text + piece.text, previous.offset)
        else:
            out.append(piece)
    return out


def _apply_overlap(pieces: list[_Piece], opts: _Options) -> list[_Piece]:
    if opts.overlap_chars <= 0 or len(pieces) < 2:
        return pieces
    out = [pieces[0]]
    for index in range(1, len(pieces)):
        previous = pieces[index - 1]
        current = pieces[index]
        tail = previous.text[-opts.overlap_chars:]
        snapped = _snap_overlap_head(tail)
        out.append(_Piece(snapped + current.text, current.offset - len(snapped)))
    return out


def _snap_overlap_head(tail: str) -> str:
    sentence = re.search(r"[。！？!?.;；]\s*", tail)
    if sentence:
        after = sentence.end()
        if 0 < after < len(tail):
            return tail[after:]
    whitespace = re.search(r"\s", tail)
    if whitespace and whitespace.start() < len(tail) - 1:
        return tail[whitespace.start() + 1:]
    return tail


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MIN_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "DEFAULT_TARGET_CHARS",
    "MarkdownChunk",
    "chunk_markdown",
    "strip_frontmatter",
]
