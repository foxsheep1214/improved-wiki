"""Stage 1.1 text extraction for XLSX / ODT / EPUB / RTF.

Companion to the PPTX/DOCX extractor in ``_stage_1_extract.py`` and subject to
the same rules: **stdlib only, no external deps**, and never hand off partial
text — a part that fails to parse is counted, and crossing the skip ratio
raises instead of quietly shortening the source.

NashSU reaches these formats through the Rust ``anydoc`` crate plus
``calamine``/``epub``/``zip`` (0.6.8). None of that is reachable from a Python
skill, so these are independent implementations rather than a port; the goal is
the same readable-text contract Stage 2 already expects, not byte parity with
AnyDoc's output.

Text only. Embedded images are deliberately out of scope: Stage 1.2's image
branches are an explicit per-suffix allowlist, so these formats fall through it
the same way ``.txt`` already does, and figures in EPUB/ODT are lost for now.

  XLSX — OOXML: sharedStrings + sheetData, one section per worksheet.
  ODT  — ODF: content.xml text:h / text:p.
  EPUB — container.xml -> OPF -> spine order, XHTML stripped to text.
  RTF  — hand-rolled control-word scanner (the one non-zip format here).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from posixpath import join as posix_join, normpath as posix_normpath

# A part = one worksheet, one spine document. Mirrors
# _stage_1_extract.OFFICE_XML_SKIP_RAISE_RATIO; kept as its own constant so the
# two extractors can diverge without a silent cross-module coupling.
DOCUMENT_PART_SKIP_RAISE_RATIO = 0.3

_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_ODF_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_OPF_NS = "{http://www.idpf.org/2007/opf}"
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"


# ══════════════════════════════════════════════════════════════════════════════
# XLSX
# ══════════════════════════════════════════════════════════════════════════════

def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """Read xl/sharedStrings.xml into an index-addressable list.

    Every <si> is one string, possibly split across <r> runs; the runs are
    concatenated with no separator because that is how Excel stores a single
    styled cell value.
    """
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    return [
        "".join(node.text or "" for node in item.iter(f"{_SPREADSHEET_NS}t"))
        for item in root.iter(f"{_SPREADSHEET_NS}si")
    ]


def _xlsx_worksheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return [(sheet name, archive path)] in workbook order.

    Resolved through workbook.xml.rels because worksheet file numbering does not
    survive sheet deletion — sheet3.xml can be the second tab. Falls back to a
    numeric sort of xl/worksheets/sheet*.xml when the rels part is missing or
    unreadable, which keeps a damaged-but-listable workbook ingestible.
    """
    names = [n for n in archive.namelist()
             if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    fallback = sorted(
        names,
        key=lambda n: int("".join(c for c in Path(n).stem if c.isdigit()) or "0"),
    )
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return [(Path(n).stem, n) for n in fallback]

    targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in rels.iter(f"{_PACKAGE_REL_NS}Relationship")
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_SPREADSHEET_NS}sheet"):
        target = targets.get(sheet.get(f"{_OFFICE_REL_NS}id"), "")
        if not target:
            continue
        path = posix_normpath(posix_join("xl", target.lstrip("/")))
        if path in archive.namelist():
            sheets.append((sheet.get("name") or Path(path).stem, path))
    return sheets or [(Path(n).stem, n) for n in fallback]


# Built-in numFmtId values that render as a date and/or time (ECMA-376 18.8.30),
# including the CJK date/time block. Custom formats are detected by their code.
_XLSX_DATE_FORMAT_IDS = frozenset(
    list(range(14, 23)) + list(range(27, 37)) + list(range(45, 48))
    + list(range(50, 59))
)
_XLSX_DATE_TOKEN_RE = re.compile(r'(?<!\\)[ymdhs]', re.IGNORECASE)
_XLSX_FORMAT_LITERAL_RE = re.compile(r'"[^"]*"|\[[^\]]*\]')


def _xlsx_is_date_format(code: str) -> bool:
    """True when a custom format code renders a date or time.

    Quoted literals and bracketed sections are stripped first: a currency
    format like ``"$"#,##0`` or a colour tag like ``[Red]`` must not be read as
    carrying month/day tokens.
    """
    return bool(_XLSX_DATE_TOKEN_RE.search(_XLSX_FORMAT_LITERAL_RE.sub("", code)))


def _xlsx_date_styles(archive: zipfile.ZipFile) -> set[int]:
    """Return cellXfs indexes whose number format renders a date.

    A cell's ``s`` attribute indexes cellXfs, which points at a numFmtId; only
    that chain distinguishes a date from a plain number, since both are stored
    as the same serial.
    """
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return set()

    custom = {
        int(fmt.get("numFmtId", "-1")): fmt.get("formatCode", "")
        for fmt in root.iter(f"{_SPREADSHEET_NS}numFmt")
        if (fmt.get("numFmtId") or "").isdigit()
    }
    date_styles: set[int] = set()
    for cell_xfs in root.iter(f"{_SPREADSHEET_NS}cellXfs"):
        for index, xf in enumerate(cell_xfs.iter(f"{_SPREADSHEET_NS}xf")):
            raw_id = xf.get("numFmtId", "0")
            fmt_id = int(raw_id) if raw_id.isdigit() else 0
            if fmt_id in _XLSX_DATE_FORMAT_IDS or _xlsx_is_date_format(
                    custom.get(fmt_id, "")):
                date_styles.add(index)
    return date_styles


def _xlsx_serial_to_text(serial: float, epoch_1904: bool) -> str:
    """Render an Excel date serial as ISO text.

    The 1900 system deliberately reproduces Lotus 1-2-3's non-existent
    1900-02-29, so serials past that fake day sit one behind a true day count —
    hence the two epochs.

    A serial with no whole-day part but a fractional one is time-of-day. That
    test has to be the day part rather than ``serial < 1``, because serial 0 is
    a real date under the 1904 epoch (1904-01-01) and only a degenerate one
    under 1900.
    """
    if epoch_1904:
        epoch = datetime(1904, 1, 1)
        days = serial
    else:
        epoch = datetime(1899, 12, 31)
        days = serial - 1 if serial > 60 else serial
    try:
        moment = epoch + timedelta(days=days)
    except (OverflowError, OSError, ValueError):
        return str(serial)
    has_fraction = abs(serial - int(serial)) > 1e-9
    if int(serial) == 0 and has_fraction:
        return moment.strftime("%H:%M:%S")
    if not has_fraction:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _xlsx_uses_1904_epoch(archive: zipfile.ZipFile) -> bool:
    """True for workbooks on the legacy Mac 1904 date system."""
    try:
        root = ET.fromstring(archive.read("xl/workbook.xml"))
    except (KeyError, ET.ParseError):
        return False
    for props in root.iter(f"{_SPREADSHEET_NS}workbookPr"):
        if (props.get("date1904") or "").lower() in {"1", "true"}:
            return True
    return False


def _xlsx_cell_text(
    cell: ET.Element,
    shared: list[str],
    date_styles: frozenset[int] | set[int] = frozenset(),
    epoch_1904: bool = False,
) -> str:
    """Resolve one <c> element to display text.

    Cell type drives where the value lives: shared-string index, inline string
    element, or the literal <v> payload for formula results, booleans, errors
    and numbers. Numeric cells carrying a date number format are rendered as
    dates — the raw serial is meaningless as evidence.
    """
    cell_type = cell.get("t", "n")
    if cell_type == "s":
        value = cell.findtext(f"{_SPREADSHEET_NS}v")
        if value is None or not value.strip().lstrip("-").isdigit():
            return ""
        index = int(value)
        return shared[index] if 0 <= index < len(shared) else ""
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter(f"{_SPREADSHEET_NS}t")
        )
    if cell_type == "b":
        return {"0": "FALSE", "1": "TRUE"}.get(
            (cell.findtext(f"{_SPREADSHEET_NS}v") or "").strip(), "")

    value = cell.findtext(f"{_SPREADSHEET_NS}v") or ""
    style = cell.get("s")
    if cell_type == "n" and style and style.isdigit() and int(style) in date_styles:
        try:
            return _xlsx_serial_to_text(float(value), epoch_1904)
        except ValueError:
            return value
    return value


def extract_text_xlsx(file_path: Path) -> str:
    """Extract worksheet text from an XLSX workbook.

    One ``## Sheet: <name>`` section per worksheet; each row is its non-empty
    cells joined by ``|``. Column positions are not reconstructed — a gap in a
    row closes up rather than emitting an empty column — because Stage 2 reads
    this as prose evidence, not as an addressable grid.
    """
    sections: list[str] = []
    with zipfile.ZipFile(file_path, "r") as archive:
        shared = _xlsx_shared_strings(archive)
        sheets = _xlsx_worksheets(archive)
        date_styles = _xlsx_date_styles(archive)
        epoch_1904 = _xlsx_uses_1904_epoch(archive)
        skipped = 0
        for sheet_name, sheet_path in sheets:
            try:
                root = ET.fromstring(archive.read(sheet_path))
            except (KeyError, ET.ParseError) as sheet_err:
                skipped += 1
                print(f"[extract] XLSX sheet skipped ({sheet_path}): {sheet_err}")
                continue
            rows: list[str] = []
            for row in root.iter(f"{_SPREADSHEET_NS}row"):
                cells = [
                    _xlsx_cell_text(cell, shared, date_styles, epoch_1904).strip()
                    for cell in row.iter(f"{_SPREADSHEET_NS}c")
                ]
                line = " | ".join(cell for cell in cells if cell)
                if line:
                    rows.append(line)
            if rows:
                sections.append(f"\n## Sheet: {sheet_name}\n" + "\n".join(rows))
        if sheets and skipped / len(sheets) > DOCUMENT_PART_SKIP_RAISE_RATIO:
            raise RuntimeError(
                f"XLSX extraction: {skipped}/{len(sheets)} worksheets failed to "
                f"parse in {file_path.name} — refusing to hand off partial text")
    return "\n".join(sections)


# ══════════════════════════════════════════════════════════════════════════════
# ODT
# ══════════════════════════════════════════════════════════════════════════════

def extract_text_odt(file_path: Path) -> str:
    """Extract body text from an ODF text document.

    Walks content.xml in document order so headings stay attached to the text
    they introduce, and re-emits ``text:h`` as Markdown ATX headings using the
    stored outline level. ``itertext()`` collects styled runs (``text:span``)
    without needing to know the inline formatting vocabulary.
    """
    with zipfile.ZipFile(file_path, "r") as archive:
        root = ET.fromstring(archive.read("content.xml"))

    lines: list[str] = []
    for node in root.iter():
        if node.tag == f"{_ODF_TEXT_NS}h":
            text = "".join(node.itertext()).strip()
            if text:
                raw_level = node.get(f"{_ODF_TEXT_NS}outline-level") or "1"
                level = min(max(int(raw_level) if raw_level.isdigit() else 1, 1), 6)
                lines.append(f"\n{'#' * level} {text}")
        elif node.tag == f"{_ODF_TEXT_NS}p":
            text = "".join(node.itertext()).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# EPUB
# ══════════════════════════════════════════════════════════════════════════════

class _XhtmlTextParser(HTMLParser):
    """Collect visible text, inserting breaks at block boundaries."""

    _SKIP = frozenset({"script", "style", "head"})
    _BLOCK = frozenset({
        "p", "div", "br", "li", "tr", "section", "article", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "pre", "figcaption",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _epub_opf_path(archive: zipfile.ZipFile) -> str:
    """Locate the OPF package document via META-INF/container.xml."""
    root = ET.fromstring(archive.read("META-INF/container.xml"))
    for rootfile in root.iter(f"{_CONTAINER_NS}rootfile"):
        full_path = rootfile.get("full-path")
        if full_path:
            return full_path
    raise RuntimeError("EPUB container.xml declares no rootfile")


def _epub_spine_documents(archive: zipfile.ZipFile) -> list[str]:
    """Return content-document archive paths in reading order.

    Spine order is authoritative — manifest order and filename order both
    routinely disagree with how the book actually reads.
    """
    opf_path = _epub_opf_path(archive)
    opf_dir = str(Path(opf_path).parent)
    root = ET.fromstring(archive.read(opf_path))

    manifest = {
        item.get("id"): item.get("href", "")
        for item in root.iter(f"{_OPF_NS}item")
    }
    names = set(archive.namelist())
    documents: list[str] = []
    for itemref in root.iter(f"{_OPF_NS}itemref"):
        href = manifest.get(itemref.get("idref"), "")
        if not href:
            continue
        base = "" if opf_dir in (".", "") else opf_dir
        path = posix_normpath(posix_join(base, href.lstrip("/")))
        if path in names:
            documents.append(path)
    return documents


def extract_text_epub(file_path: Path) -> str:
    """Extract reading-order text from an EPUB.

    Each spine document becomes one section, titled from its ``<title>`` when
    present so chapter boundaries survive into Stage 2.
    """
    sections: list[str] = []
    with zipfile.ZipFile(file_path, "r") as archive:
        documents = _epub_spine_documents(archive)
        skipped = 0
        for index, path in enumerate(documents, 1):
            try:
                markup = archive.read(path).decode("utf-8", errors="replace")
                parser = _XhtmlTextParser()
                parser.feed(markup)
                body = parser.text()
            except Exception as doc_err:  # malformed XHTML must not kill the book
                skipped += 1
                print(f"[extract] EPUB document skipped ({path}): {doc_err}")
                continue
            if not body:
                continue
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>", markup, re.IGNORECASE | re.DOTALL)
            title = (title_match.group(1).strip() if title_match else "") or f"Section {index}"
            sections.append(f"\n## {title}\n{body}")
        if documents and skipped / len(documents) > DOCUMENT_PART_SKIP_RAISE_RATIO:
            raise RuntimeError(
                f"EPUB extraction: {skipped}/{len(documents)} spine documents "
                f"failed to parse in {file_path.name} — refusing to hand off "
                f"partial text")
    return "\n".join(sections)


# ══════════════════════════════════════════════════════════════════════════════
# RTF
# ══════════════════════════════════════════════════════════════════════════════

# Destinations whose contents are markup, not prose. \* already marks most
# ignorable destinations; these are the common ones that omit it.
_RTF_SKIP_DESTINATIONS = frozenset({
    "fonttbl", "colortbl", "stylesheet", "listtable", "listoverridetable",
    "revtbl", "rsidtbl", "filetbl", "info", "pict", "object", "themedata",
    "colorschememapping", "datastore", "latentstyles", "generator", "xmlnstbl",
    "upr", "fldinst",
})
_RTF_BREAKS = {
    "par": "\n", "line": "\n", "sect": "\n", "page": "\n", "column": "\n",
    "row": "\n", "softline": "\n", "cell": "\t", "tab": "\t", "nestcell": "\t",
}
_RTF_LITERALS = {"\\": "\\", "{": "{", "}": "}", "~": "\u00a0", "_": "-", "-": ""}
_RTF_CONTROL_WORD_RE = re.compile(r"([a-zA-Z]+)(-?\d+)?")


def extract_text_rtf(file_path: Path) -> str:
    """Extract text from an RTF document.

    RTF is the one format here that is not a zip container, and the stdlib has
    no reader for it, so this is a direct scanner over the control-word syntax:
    brace-delimited groups, ``\\word`` control words with an optional numeric
    parameter and an optional single trailing space, and ``\\`` + symbol control
    symbols.

    Three details carry most of the correctness:

    * ``\\'hh`` bytes are buffered and decoded together using the document's
      declared ``\\ansicpg`` codepage, so multi-byte encodings (cp936 and
      friends) survive; decoding each byte alone would mojibake every CJK
      character.
    * ``\\uN`` emits the code point directly and then skips the ``\\ucN``
      replacement characters that follow it, otherwise every Unicode character
      appears twice — once correct, once as its ASCII fallback.
    * Groups opened on a skipped destination stay skipped to their matching
      close brace, which is what keeps font and style tables out of the text.
    """
    raw = file_path.read_bytes()
    # latin-1 keeps a 1:1 byte<->char mapping so \'hh stays addressable; real
    # decoding happens per codepage when the byte buffer flushes.
    source = raw.decode("latin-1")

    codepage_match = re.search(r"\\ansicpg(\d+)", source)
    codepage = f"cp{codepage_match.group(1)}" if codepage_match else "cp1252"
    try:
        "".encode(codepage)
    except LookupError:
        codepage = "cp1252"

    out: list[str] = []
    pending = bytearray()
    # Per-group state: (is_skipped_destination, unicode_fallback_count)
    stack: list[tuple[bool, int]] = [(False, 1)]
    skip_chars = 0
    index = 0
    length = len(source)

    def flush() -> None:
        if pending:
            out.append(pending.decode(codepage, errors="replace"))
            pending.clear()

    while index < length:
        char = source[index]

        if char == "{":
            stack.append(stack[-1])
            index += 1
            continue
        if char == "}":
            flush()
            if len(stack) > 1:
                stack.pop()
            index += 1
            continue

        if char == "\\":
            index += 1
            if index >= length:
                break
            nxt = source[index]

            if nxt == "'":  # hex-escaped byte
                hex_digits = source[index + 1:index + 3]
                index += 3
                if skip_chars > 0:
                    skip_chars -= 1
                    continue
                try:
                    if not stack[-1][0]:
                        pending.append(int(hex_digits, 16))
                except ValueError:
                    pass
                continue

            flush()

            if not nxt.isalpha():  # control symbol
                index += 1
                if nxt == "*":
                    stack[-1] = (True, stack[-1][1])
                elif not stack[-1][0] and nxt in _RTF_LITERALS:
                    out.append(_RTF_LITERALS[nxt])
                elif nxt == "\n" or nxt == "\r":
                    if not stack[-1][0]:
                        out.append("\n")
                continue

            match = _RTF_CONTROL_WORD_RE.match(source, index)
            if not match:
                index += 1
                continue
            word, param = match.group(1), match.group(2)
            index = match.end()
            if index < length and source[index] == " ":
                index += 1  # the delimiter space belongs to the control word

            if word == "uc":
                stack[-1] = (stack[-1][0], int(param) if param else 1)
                continue
            if word == "u":
                code = int(param) if param else 0
                if code < 0:
                    code += 65536
                if not stack[-1][0] and 0 <= code <= 0x10FFFF:
                    out.append(chr(code))
                skip_chars = stack[-1][1]
                continue
            if word in _RTF_SKIP_DESTINATIONS:
                stack[-1] = (True, stack[-1][1])
                continue
            if word in _RTF_BREAKS and not stack[-1][0]:
                out.append(_RTF_BREAKS[word])
            continue

        # Literal text
        index += 1
        if char in "\r\n":
            continue
        if skip_chars > 0:
            skip_chars -= 1
            continue
        if not stack[-1][0]:
            pending.append(ord(char))

    flush()
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


# ══════════════════════════════════════════════════════════════════════════════
# Dispatch
# ══════════════════════════════════════════════════════════════════════════════

_EXTRACTORS = {
    ".xlsx": extract_text_xlsx,
    ".odt": extract_text_odt,
    ".epub": extract_text_epub,
    ".rtf": extract_text_rtf,
}

SUPPORTED_DOCUMENT_SUFFIXES = frozenset(_EXTRACTORS)


def extract_document_text(file_path: Path) -> str:
    """Extract text for one XLSX/ODT/EPUB/RTF source.

    Raises rather than returning empty text: an unreadable or text-free source
    must stop the ingest, not become a wiki page with nothing behind it.
    """
    suffix = file_path.suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise ValueError(f"Unsupported file type: {file_path.suffix}")

    try:
        text = extractor(file_path)
    except RuntimeError:
        raise
    except Exception as err:
        raise RuntimeError(f"Failed to extract text from {file_path.name}: {err}")

    if not text.strip():
        raise RuntimeError(f"No extractable text found in {file_path.name}")
    print(f"[extract] {suffix.upper().lstrip('.')}: {len(text):,} chars")
    return text
