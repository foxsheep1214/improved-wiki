"""Tests for the Stage 3.1 write-time link normalizer (audit 2026-07-02, A5/M6).

One normalization pass applied to every non-listing FILE block right before
stage_3_1_write_wiki_file:
  1. related: entries → prefixed bare slugs (strip [[..]]/quotes, resolve the
     prefix against batch ∪ on-disk universe, drop unresolvable with a warn).
  2. Bare body wikilinks [[foo]] → prefixed when uniquely resolvable;
     ambiguous/missing left as-is + warned (never de-linked automatically).
     Newly generated FILE blocks can opt into strict mode, which de-links
     already-prefixed targets absent from the frozen batch ∪ disk inventory.
     Exception (fix 2026-07-02): the exact concepts/+entities/ twin pair
     resolves to concepts/ — in body links AND related:.
  3. H1 heading lines: embedded wikilinks stripped to plain text.
  4. Self-links (own slug in body or related) de-linked/removed.
  5. D4 backstop (fix 2026-07-02): bare figure/table refs (图X.X / 表X.X /
     Fig X-X / Table X-X) wrapped as [[<source-page>|据<ref>]] — idempotent,
     skips headings/code/math/existing links and the source page itself.

Also locks in: already-clean pages pass through byte-identical, and every fix
prints a loud per-page [normalize] line (never silent).

Stdlib unittest only — no network/LLM.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _stage_3_write import (  # noqa: E402
    stage_3_1_build_slug_dirs,
    stage_3_1_normalize_page_links,
    stage_3_1_write_wiki_file,
    _stage_3_1_scan_wiki_slug_dirs,
)
import _stage_3_write as stage_write  # noqa: E402

# Shared slug→dirs universe (batch ∪ disk shape the builders produce).
SLUG_DIRS: dict[str, set[str]] = {
    "matched-filter": {"concepts"},
    "bell-labs": {"entities"},
    "both-ways": {"concepts", "entities"},          # twin pair → concepts/ wins
    "tri-ways": {"concepts", "entities", "sources/Book"},  # truly ambiguous
    "dup-doc": {"sources/A", "sources/B"},          # truly ambiguous (2 sources)
    "radar-handbook": {"sources/Book"},
    "pulse-compression": {"concepts"},              # "own page" in most tests
    "marcum": {"entities"},
}

OWN = "concepts/pulse-compression.md"
SOURCE_SLUG = "sources/Book/雷达系统分析与建模 - 2007 - Barton"


def _page(related_line: str = 'related: []', body: str = "\n# Title\n\nBody.\n") -> str:
    return (
        "---\n"
        "type: concept\n"
        'title: "Pulse Compression"\n'
        "created: 2026-07-02\n"
        "updated: 2026-07-02\n"
        "tags: [radar]\n"
        f"{related_line}\n"
        'sources: ["raw/Book/x.pdf"]\n'
        "---\n"
        f"{body}"
    )


def _run(rel_path: str, content: str, slug_dirs=None, **kwargs):
    """Run the normalizer, capturing stdout. Returns (new_content, printed)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = stage_3_1_normalize_page_links(
            rel_path, content, slug_dirs or SLUG_DIRS, **kwargs)
    return out, buf.getvalue()


class TestRelatedNormalization(unittest.TestCase):
    def test_strips_wikilink_wrapping_and_prefixes_bare_names(self):
        content = _page('related: ["[[concepts/matched-filter]]", "bell-labs"]')
        out, printed = _run(OWN, content)
        self.assertIn('related: ["concepts/matched-filter", "entities/bell-labs"]', out)
        self.assertIn("[normalize]", printed)

    def test_single_unquoted_wikilink_entry(self):
        # `related: [[concepts/matched-filter]]` parses to `[concepts/...]`
        # with stray single brackets — must still resolve, not be dropped.
        content = _page("related: [[concepts/matched-filter]]")
        out, _ = _run(OWN, content)
        self.assertIn('related: ["concepts/matched-filter"]', out)

    def test_unresolvable_entry_dropped_with_warn(self):
        content = _page('related: ["ghost-page", "concepts/matched-filter"]')
        out, printed = _run(OWN, content)
        self.assertIn('related: ["concepts/matched-filter"]', out)
        self.assertNotIn("ghost-page", out)
        self.assertIn("dropped 1 unresolvable", printed)
        self.assertIn("ghost-page", printed)

    def test_wrong_prefix_corrected_to_actual_dir(self):
        content = _page('related: ["entities/matched-filter"]')
        out, _ = _run(OWN, content)
        self.assertIn('related: ["concepts/matched-filter"]', out)

    def test_ambiguous_stem_kept_bare_with_warn(self):
        # 3-way ambiguity (NOT the concepts+entities pair) — don't guess.
        content = _page('related: ["tri-ways"]')
        out, printed = _run(OWN, content)
        self.assertIn('related: ["tri-ways"]', out)
        self.assertIn("ambiguous", printed)
        self.assertIn("tri-ways", printed)

    def test_duplicates_collapse_after_normalization(self):
        content = _page('related: ["matched-filter", "concepts/matched-filter", "[[matched-filter]]"]')
        out, _ = _run(OWN, content)
        self.assertIn('related: ["concepts/matched-filter"]', out)
        self.assertEqual(out.count("concepts/matched-filter"), 1)

    def test_self_link_removed_from_related(self):
        content = _page('related: ["pulse-compression", "concepts/pulse-compression", "entities/bell-labs"]')
        out, printed = _run(OWN, content)
        self.assertIn('related: ["entities/bell-labs"]', out)
        self.assertIn("self-link", printed)

    def test_same_stem_in_other_dir_is_not_a_self_link(self):
        # Page concepts/both-ways relating to entities/both-ways is legitimate.
        content = _page('related: ["entities/both-ways"]')
        out, _ = _run("concepts/both-ways.md", content)
        self.assertIn('related: ["entities/both-ways"]', out)

    def test_block_form_related_normalized(self):
        content = (
            "---\n"
            "type: concept\n"
            'title: "X"\n'
            "related:\n"
            '  - "[[concepts/matched-filter]]"\n'
            "  - bell-labs\n"
            "tags: []\n"
            "---\n"
            "\n# Title\n"
        )
        out, _ = _run(OWN, content)
        self.assertIn('related: ["concepts/matched-filter", "entities/bell-labs"]', out)
        self.assertIn("tags: []", out)  # following field intact

    def test_absent_related_field_not_inserted(self):
        content = (
            "---\n"
            "type: concept\n"
            'title: "X"\n'
            "---\n"
            "\n# Title\n"
        )
        out, _ = _run(OWN, content)
        self.assertNotIn("related:", out)

    def test_sources_subdir_prefix_resolved_in_full(self):
        content = _page('related: ["radar-handbook"]')
        out, _ = _run(OWN, content)
        self.assertIn('related: ["sources/Book/radar-handbook"]', out)


class TestBodyWikilinks(unittest.TestCase):
    def test_bare_unique_link_gets_prefix(self):
        content = _page(body="\n# Title\n\nSee [[matched-filter]] for detail.\n")
        out, printed = _run(OWN, content)
        self.assertIn("[[concepts/matched-filter]]", out)
        self.assertIn("prefixed 1 bare wikilink", printed)

    def test_alias_preserved_when_prefixing(self):
        content = _page(body="\n# Title\n\n见 [[matched-filter|匹配滤波器]]。\n")
        out, _ = _run(OWN, content)
        self.assertIn("[[concepts/matched-filter|匹配滤波器]]", out)

    def test_anchor_preserved_when_prefixing(self):
        content = _page(body="\n# Title\n\nSee [[matched-filter#定义]].\n")
        out, _ = _run(OWN, content)
        self.assertIn("[[concepts/matched-filter#定义]]", out)

    def test_ambiguous_left_as_is_with_warn(self):
        # 3-way ambiguity (NOT the concepts+entities pair) — don't guess.
        content = _page(body="\n# Title\n\nSee [[tri-ways]].\n")
        out, printed = _run(OWN, content)
        self.assertIn("[[tri-ways]]", out)  # NOT de-linked, NOT prefixed
        self.assertIn("left as-is", printed)
        self.assertIn("[[tri-ways]]", printed)

    def test_missing_left_as_is_with_warn(self):
        content = _page(body="\n# Title\n\nSee [[no-such-page]].\n")
        out, printed = _run(OWN, content)
        self.assertIn("[[no-such-page]]", out)  # never de-linked automatically
        self.assertIn("left as-is", printed)

    def test_missing_prefixed_link_preserved_by_default(self):
        content = _page(
            body="\n# Title\n\nSee [[concepts/no-such-page|forward ref]].\n")
        out, printed = _run(OWN, content)
        self.assertIn("[[concepts/no-such-page|forward ref]]", out)
        self.assertEqual(printed, "")

    def test_already_prefixed_link_untouched(self):
        content = _page(body="\n# Title\n\nSee [[concepts/matched-filter]].\n")
        out, printed = _run(OWN, content)
        self.assertIn("[[concepts/matched-filter]]", out)
        self.assertEqual(printed, "")  # clean page → silent

    def test_missing_prefixed_link_delinked_in_generated_strict_mode(self):
        content = _page(
            body=(
                "\n# Title\n\n"
                "See [[concepts/no-such-page|the omitted candidate]] and "
                "[[entities/another-missing]].\n"
            )
        )
        out, printed = _run(
            OWN, content, strict_missing_targets=True)
        self.assertIn("See the omitted candidate and another-missing.", out)
        self.assertNotIn("[[concepts/no-such-page", out)
        self.assertNotIn("[[entities/another-missing", out)
        self.assertIn("de-linked 2 missing generated target(s)", printed)

    def test_valid_prefixed_link_kept_in_generated_strict_mode(self):
        content = _page(
            body="\n# Title\n\nSee [[concepts/matched-filter|filter]].\n")
        out, printed = _run(
            OWN, content, strict_missing_targets=True)
        self.assertIn("[[concepts/matched-filter|filter]]", out)
        self.assertEqual(printed, "")

    def test_wrong_prefixed_body_link_corrected_to_unique_actual_dir(self):
        content = _page(
            body="\n# Title\n\nSee [[sources/book/radar-handbook|source]].\n"
        )
        out, printed = _run(OWN, content)
        self.assertIn("[[sources/Book/radar-handbook|source]]", out)
        self.assertIn("corrected 1 wrongly-prefixed wikilink", printed)

    def test_table_alias_separator_is_escaped(self):
        content = _page(body=(
            "\n# Title\n\n"
            "| Dimension | Value |\n"
            "|---|---|\n"
            "| Filter | [[concepts/matched-filter|Matched filter]] |\n"
        ))
        out, printed = _run(OWN, content)
        self.assertIn(
            "[[concepts/matched-filter\\|Matched filter]]",
            out,
        )
        self.assertIn("escaped 1 Markdown-table wikilink alias pipe", printed)

    def test_already_escaped_table_alias_is_valid_and_idempotent(self):
        content = _page(body=(
            "\n# Title\n\n"
            "| Dimension | Value |\n"
            "|---|---|\n"
            "| Filter | [[concepts/matched-filter\\|Matched filter]] |\n"
        ))
        out, printed = _run(
            OWN,
            content,
            strict_missing_targets=True,
        )
        self.assertEqual(out, content)
        self.assertEqual(printed, "")


class TestH1Stripping(unittest.TestCase):
    def test_h1_wikilinks_stripped_to_alias_text(self):
        content = _page(body="\n# 脉冲压缩 与 [[concepts/matched-filter|匹配滤波器]]\n\nBody.\n")
        out, printed = _run(OWN, content)
        self.assertIn("# 脉冲压缩 与 匹配滤波器\n", out)
        self.assertNotIn("# 脉冲压缩 与 [[", out)
        self.assertIn("H1: de-linked 1", printed)

    def test_h1_bare_wikilink_stripped_to_stem_text(self):
        content = _page(body="\n# [[matched-filter]]\n\nBody.\n")
        out, _ = _run(OWN, content)
        self.assertIn("# matched-filter\n", out)

    def test_h2_wikilinks_not_stripped(self):
        content = _page(body="\n# Title\n\n## See [[concepts/matched-filter]]\n")
        out, _ = _run(OWN, content)
        self.assertIn("## See [[concepts/matched-filter]]", out)


class TestSelfLinks(unittest.TestCase):
    def test_bare_self_link_delinked(self):
        content = _page(body="\n# Title\n\n另见 [[pulse-compression]]。\n")
        out, printed = _run(OWN, content)
        self.assertIn("另见 pulse-compression。", out)
        self.assertNotIn("[[pulse-compression]]", out)
        self.assertIn("self-link", printed)

    def test_prefixed_self_link_delinked(self):
        content = _page(body="\n# Title\n\n另见 [[concepts/pulse-compression]]。\n")
        out, _ = _run(OWN, content)
        self.assertIn("另见 pulse-compression。", out)

    def test_aliased_self_link_delinked_to_alias(self):
        content = _page(body="\n# Title\n\n另见 [[concepts/pulse-compression|脉冲压缩]]。\n")
        out, _ = _run(OWN, content)
        self.assertIn("另见 脉冲压缩。", out)

    def test_same_stem_other_dir_body_link_kept(self):
        # entities/marcum linking [[concepts/marcum]] is a cross-dir link,
        # not a self-link (prefixed non-self links are left as-is).
        slug_dirs = dict(SLUG_DIRS)
        slug_dirs["marcum"] = {"concepts", "entities"}
        content = _page(body="\n# Title\n\nSee [[concepts/marcum]].\n")
        out, _ = _run("entities/marcum.md", content, slug_dirs)
        self.assertIn("[[concepts/marcum]]", out)


class TestSameStemTwinPolicy(unittest.TestCase):
    """concepts/+entities/ twin pair resolves to concepts/ (fix 2026-07-02)."""

    def test_related_twin_pair_resolves_to_concepts(self):
        content = _page('related: ["both-ways"]')
        out, printed = _run(OWN, content)
        self.assertIn('related: ["concepts/both-ways"]', out)
        self.assertIn("resolved 1 same-stem ambiguity → concepts/", printed)
        self.assertNotIn("ambiguous, kept bare", printed)

    def test_body_twin_pair_resolves_to_concepts(self):
        content = _page(body="\n# Title\n\nSee [[both-ways]].\n")
        out, printed = _run(OWN, content)
        self.assertIn("[[concepts/both-ways]]", out)
        self.assertIn("resolved 1 same-stem ambiguity → concepts/", printed)
        self.assertNotIn("left as-is", printed)

    def test_body_twin_alias_and_anchor_preserved(self):
        content = _page(body="\n# Title\n\n见 [[both-ways#用法|时基电路]]。\n")
        out, _ = _run(OWN, content)
        self.assertIn("[[concepts/both-ways#用法|时基电路]]", out)

    def test_twin_resolution_then_self_link_removed(self):
        # concepts/both-ways listing bare "both-ways" resolves to itself.
        content = _page('related: ["both-ways", "entities/bell-labs"]')
        out, printed = _run("concepts/both-ways.md", content)
        self.assertIn('related: ["entities/bell-labs"]', out)
        self.assertIn("self-link", printed)

    def test_two_sources_pair_is_true_ambiguity(self):
        # ≥2 dirs that are NOT the concepts+entities pair keep the old policy.
        content = _page('related: ["dup-doc"]', body="\n# Title\n\nSee [[dup-doc]].\n")
        out, printed = _run(OWN, content)
        self.assertIn('related: ["dup-doc"]', out)
        self.assertIn("[[dup-doc]]", out)
        self.assertIn("ambiguous, kept bare", printed)
        self.assertIn("left as-is", printed)


class TestFigureRefWrapping(unittest.TestCase):
    """D4 backstop: bare 图/表/Fig/Table refs → [[<source-page>|据<ref>]]."""

    def _run_fig(self, content, rel_path=OWN, slug=SOURCE_SLUG):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = stage_3_1_normalize_page_links(
                rel_path, content, SLUG_DIRS, source_page_slug=slug)
        return out, buf.getvalue()

    def test_chinese_figure_ref_wrapped(self):
        content = _page(body="\n# Title\n\n如图9.1所示，脉压比可达 30 dB。\n")
        out, printed = self._run_fig(content)
        self.assertIn(f"如[[{SOURCE_SLUG}|据图9.1]]所示", out)
        self.assertIn("wrapped 1 figure/table ref(s)", printed)

    def test_table_fig_and_table_variants_wrapped(self):
        content = _page(body="\n# Title\n\n见表2.6；参 Fig. 3-1 与 Table 4-2。\n")
        out, printed = self._run_fig(content)
        self.assertIn(f"[[{SOURCE_SLUG}|据表2.6]]", out)
        self.assertIn(f"[[{SOURCE_SLUG}|据Fig. 3-1]]", out)
        self.assertIn(f"[[{SOURCE_SLUG}|据Table 4-2]]", out)
        self.assertIn("wrapped 3 figure/table ref(s)", printed)

    def test_figure_ref_inserted_in_markdown_table_escapes_alias_pipe(self):
        content = _page(body=(
            "\n# Title\n\n"
            "| Evidence | Result |\n"
            "|---|---|\n"
            "| See Fig. 3-1 | 30 dB |\n"
        ))
        out, printed = self._run_fig(content)
        self.assertIn(
            f"[[{SOURCE_SLUG}\\|据Fig. 3-1]]",
            out,
        )
        self.assertIn("escaped 1 Markdown-table wikilink alias pipe", printed)

    def test_fullwidth_dot_and_hyphen_separators(self):
        content = _page(body="\n# Title\n\n对比图2．6 与图3-1。\n")
        out, _ = self._run_fig(content)
        self.assertIn(f"[[{SOURCE_SLUG}|据图2．6]]", out)
        self.assertIn(f"[[{SOURCE_SLUG}|据图3-1]]", out)

    def test_idempotent_second_pass_no_change(self):
        content = _page(body="\n# Title\n\n如图9.1所示，另见表2.6。\n")
        once, _ = self._run_fig(content)
        twice, printed = self._run_fig(once)
        self.assertEqual(twice, once)
        self.assertEqual(printed, "")

    def test_ref_inside_existing_wikilink_untouched(self):
        content = _page(body="\n# Title\n\n[[concepts/matched-filter|见图2.6详解]]。\n")
        out, printed = self._run_fig(content)
        self.assertIn("[[concepts/matched-filter|见图2.6详解]]", out)
        self.assertEqual(printed, "")

    def test_headings_excluded(self):
        content = _page(body="\n# Title\n\n## 图3.1 的讨论\n\n### 表2.6 数据\n")
        out, printed = self._run_fig(content)
        self.assertIn("## 图3.1 的讨论", out)
        self.assertIn("### 表2.6 数据", out)
        self.assertEqual(printed, "")

    def test_code_and_math_spans_excluded(self):
        content = _page(body=(
            "\n# Title\n\n代码 `plot(图3.1)` 与公式 $图3.2$ 不动。\n"
            "```\n图4.1 在代码块里\n```\n"))
        out, printed = self._run_fig(content)
        self.assertIn("`plot(图3.1)`", out)
        self.assertIn("$图3.2$", out)
        self.assertIn("图4.1 在代码块里", out)
        self.assertEqual(printed, "")

    def test_markdown_image_path_untouched(self):
        content = _page(body="\n# Title\n\n![标注](media/图3-1.png)\n")
        out, printed = self._run_fig(content)
        self.assertIn("![标注](media/图3-1.png)", out)
        self.assertEqual(printed, "")

    def test_source_page_itself_excluded(self):
        content = _page(body="\n# Title\n\n如图9.1所示。\n")
        out, printed = self._run_fig(content, rel_path=f"{SOURCE_SLUG}.md")
        self.assertIn("如图9.1所示。", out)
        self.assertNotIn("据图", out)
        self.assertEqual(printed, "")

    def test_no_slug_param_no_wrap(self):
        content = _page(body="\n# Title\n\n如图9.1所示。\n")
        out, printed = _run(OWN, content)  # source_page_slug omitted
        self.assertIn("如图9.1所示。", out)
        self.assertEqual(printed, "")

    def test_frontmatter_untouched(self):
        content = _page('related: []', body="\n# Title\n\n见图1.2。\n").replace(
            'title: "Pulse Compression"', 'title: "图1.1 解析"')
        out, _ = self._run_fig(content)
        self.assertIn('title: "图1.1 解析"', out)  # frontmatter ref not wrapped
        self.assertIn(f"[[{SOURCE_SLUG}|据图1.2]]", out)


class TestPassThrough(unittest.TestCase):
    CLEAN = (
        "---\n"
        "type: concept\n"
        'title: "脉冲压缩"\n'
        "created: 2026-07-01\n"
        "updated: 2026-07-02\n"
        "tags: [radar]\n"
        'related: ["concepts/matched-filter", "entities/bell-labs"]\n'
        'sources: ["raw/Book/x.pdf"]\n'
        "---\n"
        "\n"
        "# 脉冲压缩\n"
        "\n"
        "正文 [[concepts/matched-filter]] 与 [[entities/bell-labs|贝尔实验室]]。\n"
        "\n"
        "## See Also\n"
        "- [[concepts/matched-filter]]\n"
    )

    def test_clean_page_byte_identical_and_silent(self):
        out, printed = _run(OWN, self.CLEAN)
        self.assertEqual(out, self.CLEAN)
        self.assertEqual(printed, "")

    def test_clean_page_without_related_byte_identical(self):
        content = "---\ntype: concept\ntitle: \"X\"\n---\n\n# X\n\nPlain body, no links.\n"
        out, printed = _run(OWN, content)
        self.assertEqual(out, content)
        self.assertEqual(printed, "")

    def test_targeted_fix_leaves_rest_byte_identical(self):
        dirty = self.CLEAN.replace("[[entities/bell-labs|贝尔实验室]]", "[[bell-labs|贝尔实验室]]")
        out, _ = _run(OWN, dirty)
        self.assertEqual(out, self.CLEAN)  # only the targeted span changed


class TestPostMergeNormalization(unittest.TestCase):
    def test_llm_merge_output_is_normalized_before_atomic_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wiki = root / "wiki"
            path = wiki / OWN
            path.parent.mkdir(parents=True)
            path.write_text(_page(), encoding="utf-8")
            config = SimpleNamespace(
                wiki_dir=wiki,
                runtime_dir=root / "runtime",
            )
            merged = _page(
                'related: ["entities/matched-filter", "[", "]"]',
                body=(
                    "\n# Title\n\n"
                    "See [[sources/book/radar-handbook|source]].\n"
                ),
            )
            with patch.object(
                stage_write,
                "_stage_3_1_merge_page_content",
                return_value=merged,
            ):
                stage_3_1_write_wiki_file(
                    path,
                    _page(),
                    config,
                    merge=True,
                    normalize_rel_path=OWN,
                    slug_dirs=SLUG_DIRS,
                )

            written = path.read_text(encoding="utf-8")
            self.assertIn(
                'related: ["concepts/matched-filter"]',
                written,
            )
            self.assertIn(
                "[[sources/Book/radar-handbook|source]]",
                written,
            )
            self.assertNotIn('related: ["entities/matched-filter"', written)


class TestUniverseBuilders(unittest.TestCase):
    def _make_wiki(self, root: Path) -> Path:
        wiki = root / "wiki"
        for rel in ("concepts/alpha.md", "entities/beta.md", "sources/Book/gamma.md",
                    "REVIEW/2026-07-02-item.md", "concepts/_audit_x.md", "index.md"):
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("---\ntype: concept\n---\nbody\n", encoding="utf-8")
        return wiki

    def test_scan_wiki_slug_dirs_excludes_artifacts_anchors_and_system(self):
        with tempfile.TemporaryDirectory() as td:
            wiki = self._make_wiki(Path(td))
            config = SimpleNamespace(wiki_dir=wiki)
            got = _stage_3_1_scan_wiki_slug_dirs(config)
        self.assertEqual(got, {
            "alpha": {"concepts"},
            "beta": {"entities"},
            "gamma": {"sources/Book"},
        })

    def test_build_slug_dirs_unions_batch_with_disk(self):
        valid = {"sources", "concepts", "entities", "queries", "comparisons"}
        blocks = [
            ("concepts/new-concept.md", "---\ntype: concept\ntitle: N\n---\nbody"),
            # bare filename → auto-correct routes by frontmatter type
            ("Some Entity.md", "---\ntype: entity\ntitle: Some Entity\n---\nbody"),
            # wiki/ prefix → auto-correct strips it
            ("wiki/concepts/other.md", "---\ntype: concept\ntitle: O\n---\nbody"),
            ("index.md", "# Index"),           # listing page — excluded
        ]
        with tempfile.TemporaryDirectory() as td:
            wiki = self._make_wiki(Path(td))
            config = SimpleNamespace(wiki_dir=wiki)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = stage_3_1_build_slug_dirs(blocks, config, valid, {})
        self.assertEqual(got.get("new-concept"), {"concepts"})
        self.assertEqual(got.get("Some Entity"), {"entities"})
        self.assertEqual(got.get("other"), {"concepts"})
        self.assertNotIn("index", got)
        self.assertEqual(got.get("alpha"), {"concepts"})  # disk pages present
        self.assertEqual(buf.getvalue(), "")  # quiet pre-pass: no duplicate prints


if __name__ == "__main__":
    unittest.main()
