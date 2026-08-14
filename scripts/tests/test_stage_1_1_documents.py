"""Stage 1.1 text extraction for XLSX / ODT / EPUB / RTF."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_1_1_documents as documents  # noqa: E402


def _zip(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path


def _extract(fn, path: Path) -> str:
    """Run an extractor with its progress prints suppressed."""
    with redirect_stdout(io.StringIO()):
        return fn(path)


# ── XLSX ──────────────────────────────────────────────────────────────────────

_SHARED_STRINGS = (
    '<?xml version="1.0"?>'
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<si><t>Part Number</t></si>"
    "<si><t>Vendor</t></si>"
    "<si><r><t>YCC</t></r><r><t>87</t></r></si>"
    "</sst>"
)


def _sheet(rows: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )


def _workbook_xlsx(tmp: Path) -> Path:
    """A workbook whose tab order deliberately disagrees with file numbering."""
    return _zip(tmp / "book.xlsx", {
        "xl/sharedStrings.xml": _SHARED_STRINGS,
        "xl/workbook.xml": (
            '<?xml version="1.0"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets>'
            '<sheet name="Data" sheetId="1" r:id="rId7"/>'
            '<sheet name="Notes" sheetId="2" r:id="rId3"/>'
            '</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId7" Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ),
        "xl/worksheets/sheet1.xml": _sheet(
            '<row><c t="inlineStr"><is><t>Checked</t></is></c>'
            '<c t="b"><v>1</v></c></row>'
        ),
        "xl/worksheets/sheet2.xml": _sheet(
            '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
            '<row><c t="s"><v>2</v></c><c><v>3.3</v></c></row>'
        ),
    })


class TestXlsx(unittest.TestCase):
    def test_shared_inline_numeric_and_boolean_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            text = _extract(
                documents.extract_text_xlsx, _workbook_xlsx(Path(directory)))
        self.assertIn("Part Number | Vendor", text)
        # Multi-run shared strings are one cell value, not two.
        self.assertIn("YCC87 | 3.3", text)
        self.assertIn("Checked | TRUE", text)

    def test_sheet_order_follows_workbook_rels_not_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            text = _extract(
                documents.extract_text_xlsx, _workbook_xlsx(Path(directory)))
        self.assertIn("## Sheet: Data", text)
        self.assertIn("## Sheet: Notes", text)
        self.assertLess(
            text.index("## Sheet: Data"),
            text.index("## Sheet: Notes"),
            "workbook tab order must win over sheetN.xml numbering",
        )

    def test_date_formatted_cells_render_as_dates_not_serials(self):
        styles = (
            '<?xml version="1.0"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts>'
            '<numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
            # Quoted literal contains y/a — it must not be read as a date token.
            '<numFmt numFmtId="165" formatCode="&quot;May&quot;#,##0"/>'
            '</numFmts>'
            '<cellXfs>'
            '<xf numFmtId="0"/>'
            '<xf numFmtId="14"/>'
            '<xf numFmtId="164"/>'
            '<xf numFmtId="165"/>'
            '</cellXfs></styleSheet>'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = _zip(Path(directory) / "dated.xlsx", {
                "xl/styles.xml": styles,
                "xl/worksheets/sheet1.xml": _sheet(
                    '<row>'
                    '<c s="1"><v>45311</v></c>'
                    '<c s="2"><v>45311</v></c>'
                    '<c s="3"><v>45311</v></c>'
                    '<c s="0"><v>42</v></c>'
                    '</row>'
                ),
            })
            text = _extract(documents.extract_text_xlsx, path)
        self.assertIn("2024-01-20 | 2024-01-20 | 45311 | 42", text)

    def test_1900_leap_year_bug_boundary(self):
        # Excel keeps Lotus's non-existent 1900-02-29 at serial 60.
        self.assertEqual(documents._xlsx_serial_to_text(1, False), "1900-01-01")
        self.assertEqual(documents._xlsx_serial_to_text(59, False), "1900-02-28")
        self.assertEqual(documents._xlsx_serial_to_text(61, False), "1900-03-01")

    def test_1904_epoch_workbooks_shift_by_their_own_epoch(self):
        self.assertEqual(documents._xlsx_serial_to_text(0, True), "1904-01-01")
        self.assertNotEqual(
            documents._xlsx_serial_to_text(45311, True),
            documents._xlsx_serial_to_text(45311, False),
        )

    def test_fractional_serials_keep_the_time_component(self):
        self.assertEqual(
            documents._xlsx_serial_to_text(45311.5, False), "2024-01-20 12:00:00")
        self.assertEqual(documents._xlsx_serial_to_text(0.25, False), "06:00:00")

    def test_unparseable_sheets_past_the_ratio_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _zip(Path(directory) / "broken.xlsx", {
                "xl/worksheets/sheet1.xml": _sheet("<row><c><v>1</v></c></row>"),
                "xl/worksheets/sheet2.xml": "<not-xml",
            })
            with self.assertRaises(RuntimeError) as caught:
                _extract(documents.extract_text_xlsx, path)
        self.assertIn("refusing to hand off partial text", str(caught.exception))


# ── ODT ───────────────────────────────────────────────────────────────────────

class TestOdt(unittest.TestCase):
    def test_headings_keep_outline_level_and_document_order(self):
        content = (
            '<?xml version="1.0"?>'
            '<office:document-content'
            ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
            ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            '<office:body><office:text>'
            '<text:h text:outline-level="1">Chapter One</text:h>'
            '<text:p>Body with a <text:span>styled</text:span> run.</text:p>'
            '<text:h text:outline-level="3">Deep Section</text:h>'
            '<text:p>Tail.</text:p>'
            '</office:text></office:body></office:document-content>'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = _zip(Path(directory) / "doc.odt", {"content.xml": content})
            text = _extract(documents.extract_text_odt, path)

        self.assertIn("# Chapter One", text)
        self.assertIn("### Deep Section", text)
        # Styled runs must not split the sentence.
        self.assertIn("Body with a styled run.", text)
        self.assertLess(text.index("Chapter One"), text.index("Deep Section"))


# ── EPUB ──────────────────────────────────────────────────────────────────────

def _epub(tmp: Path) -> Path:
    """A book whose spine order is the reverse of its filename order."""
    return _zip(tmp / "book.epub", {
        "META-INF/container.xml": (
            '<?xml version="1.0"?>'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>'
            '</container>'
        ),
        "OEBPS/content.opf": (
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf">'
            '<manifest>'
            '<item id="a" href="a.xhtml"/>'
            '<item id="b" href="b.xhtml"/>'
            '</manifest>'
            '<spine><itemref idref="b"/><itemref idref="a"/></spine>'
            '</package>'
        ),
        "OEBPS/a.xhtml": (
            "<html><head><title>Second Read</title>"
            "<style>p { color: red }</style></head>"
            "<body><p>Alpha body.</p></body></html>"
        ),
        "OEBPS/b.xhtml": (
            "<html><head><title>First Read</title></head>"
            "<body><h1>Beta</h1><p>Beta body &amp; more.</p>"
            "<script>ignored()</script></body></html>"
        ),
    })


class TestEpub(unittest.TestCase):
    def test_spine_order_titles_and_markup_stripping(self):
        with tempfile.TemporaryDirectory() as directory:
            text = _extract(documents.extract_text_epub, _epub(Path(directory)))

        self.assertLess(
            text.index("First Read"),
            text.index("Second Read"),
            "spine order must win over manifest and filename order",
        )
        self.assertIn("## First Read", text)
        self.assertIn("Beta body & more.", text)
        self.assertNotIn("ignored()", text)
        self.assertNotIn("color: red", text)

    def test_missing_rootfile_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _zip(Path(directory) / "bad.epub", {
                "META-INF/container.xml": (
                    '<?xml version="1.0"?>'
                    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles/></container>'
                ),
            })
            with self.assertRaises(RuntimeError):
                _extract(documents.extract_text_epub, path)


# ── RTF ───────────────────────────────────────────────────────────────────────

class TestRtf(unittest.TestCase):
    def _rtf(self, body: str, directory: str, encoding: str = "latin-1") -> str:
        path = Path(directory) / "doc.rtf"
        path.write_bytes(body.encode(encoding))
        return _extract(documents.extract_text_rtf, path)

    def test_control_tables_are_dropped_and_prose_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi{\fonttbl{\f0\froman Times;}}"
                r"{\colortbl;\red0\green0\blue0;}"
                r"Visible prose.\par Second line.}",
                directory,
            )
        self.assertNotIn("Times", text)
        self.assertNotIn("froman", text)
        self.assertEqual(text, "Visible prose.\nSecond line.")

    def test_ignorable_destination_marker_skips_unknown_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi{\*\someunknowndest hidden payload}Kept.}",
                directory,
            )
        self.assertEqual(text, "Kept.")

    def test_group_scope_restores_after_a_skipped_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi A{\fonttbl junk}B}", directory)
        self.assertEqual(text, "AB")

    def test_escaped_braces_and_backslash_are_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi a\{b\}c\\d}", directory)
        self.assertEqual(text, "a{b}c\\d")

    def test_hex_escapes_decode_through_the_declared_codepage(self):
        with tempfile.TemporaryDirectory() as directory:
            latin = self._rtf(
                r"{\rtf1\ansi\ansicpg1252 caf\'e9}", directory)
            self.assertEqual(latin, "café")

        with tempfile.TemporaryDirectory() as directory:
            # cp936 needs both bytes decoded together; per-byte decoding mojibakes.
            cjk = self._rtf(
                r"{\rtf1\ansi\ansicpg936 \'c4\'e3\'ba\'c3}", directory)
            self.assertEqual(cjk, "你好")

    def test_unicode_escape_emits_once_and_skips_its_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi\uc1\u9731 ?done}", directory)
        self.assertEqual(text, "☃done")

    def test_uc_zero_keeps_the_following_character(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi\uc0\u9731 X}", directory)
        self.assertEqual(text, "☃X")

    def test_negative_unicode_parameter_wraps_into_range(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(r"{\rtf1\ansi\uc1\u-3600 ?}", directory)
        self.assertEqual(text, chr(-3600 + 65536))

    def test_tabs_and_paragraph_breaks(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self._rtf(
                r"{\rtf1\ansi one\tab two\par three}", directory)
        self.assertEqual(text, "one\ttwo\nthree")


# ── Dispatch and routing ──────────────────────────────────────────────────────

class TestDispatch(unittest.TestCase):
    def test_unsupported_suffix_is_rejected(self):
        with self.assertRaises(ValueError):
            documents.extract_document_text(Path("/nonexistent/file.key"))

    def test_text_free_source_raises_instead_of_returning_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _zip(Path(directory) / "empty.odt", {
                "content.xml": (
                    '<?xml version="1.0"?>'
                    '<office:document-content'
                    ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
                    ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                    '<office:body><office:text/></office:body>'
                    '</office:document-content>'
                ),
            })
            with self.assertRaises(RuntimeError) as caught:
                _extract(documents.extract_document_text, path)
        self.assertIn("No extractable text", str(caught.exception))

    def test_unreadable_container_becomes_a_runtime_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.epub"
            path.write_bytes(b"not a zip archive")
            with self.assertRaises(RuntimeError):
                _extract(documents.extract_document_text, path)

    def test_stage_1_1_routes_new_suffixes_off_the_mineru_path(self):
        import _stage_1_extract as extract

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "doc.rtf"
            path.write_bytes(rb"{\rtf1\ansi Routed through Stage 1.1.}")
            with redirect_stdout(io.StringIO()):
                text, method = extract.stage_1_1_extract_text(path, config=None)

        self.assertEqual(text, "Routed through Stage 1.1.")
        # Stage 1.2 keys image extraction off a "mineru" prefix; these must not
        # collide with it.
        self.assertEqual(method, "document-rtf")
        self.assertFalse(method.startswith("mineru"))


if __name__ == "__main__":
    unittest.main()
