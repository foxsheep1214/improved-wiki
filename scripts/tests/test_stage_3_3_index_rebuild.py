"""Stage 3.3 keeps ``wiki/index.md`` equal to the on-disk page inventory.

Regression for the 2026-09-14 HardwareWiki audit (12,083 pages). Above the old
250-page LLM-rewrite ceiling, Stage 3.3 only inserted the source line directly
under ``## source`` — unsorted, with a stray blank line — and never indexed the
concept/entity/methodology/finding/comparison/query pages the same ingest had
just written, so 316 typed pages were missing from the index.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_3_write as stage3  # noqa: E402

# Comfortably above the old 250-page LLM-rewrite ceiling.
FILLER_PAGE_COUNT = 260

SOURCE_NAME = "ADI AD9361 RF Agile Transceiver - 2014 - Analog Devices"
SOURCE_REL = f"sources/Datasheet/{SOURCE_NAME}.md"

# (wiki-relative path, frontmatter type, title) of every page one ingest wrote.
INGEST_PAGES = [
    (SOURCE_REL, "source", "ADI AD9361 RF Agile Transceiver"),
    ("concepts/zero-if-receiver.md", "concept", "Zero-IF Receiver"),
    # Lower-case title: sorting must ignore case.
    ("concepts/automatic-gain-control.md", "concept", "automatic gain control"),
    ("entities/ad9361.md", "entity", "AD9361"),
    ("methodology/iq-imbalance-calibration.md", "methodology",
     "IQ Imbalance Calibration"),
    ("findings/lo-leakage-dominates-dc-offset.md", "finding",
     "LO Leakage Dominates DC Offset"),
    ("comparisons/zero-if-vs-superheterodyne.md", "comparison",
     "Zero-IF vs Superheterodyne"),
    ("queries/why-does-zero-if-need-dc-calibration.md", "query",
     "Why does Zero-IF need DC calibration?"),
]


def _config(root: Path) -> _core.Config:
    return _core.Config(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=root / ".llm-wiki",
        cache_path=root / ".llm-wiki" / "ingest-cache.json",
        progress_dir=root / ".llm-wiki" / "ingest-progress",
        extract_tmp_dir=root / ".llm-wiki" / "extract-tmp",
        llm_model="test",
        caption_api_key="",
        caption_base_url="http://127.0.0.1",
        caption_model="test",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
    )


def _write_page(wiki_dir: Path, rel: str, page_type: str, title: str) -> None:
    path = wiki_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\ntype: {page_type}\ntitle: "{title}"\n---\n\n'
        f"# {title}\n\nBody.\n",
        encoding="utf-8",
    )


def _write_ingest_pages(wiki_dir: Path) -> None:
    for rel, page_type, title in INGEST_PAGES:
        _write_page(wiki_dir, rel, page_type, title)


def _build_large_wiki(wiki_dir: Path) -> None:
    """A wiki past the old ceiling whose index predates this ingest.

    The prior index carries the damage the single-line append left in
    HardwareWiki: source lines stacked unsorted under ``## source`` with blank
    lines between them, plus an entry for a page that no longer exists.
    """
    for n in range(FILLER_PAGE_COUNT):
        _write_page(
            wiki_dir, f"concepts/filler-{n:04d}.md", "concept", f"Filler {n:04d}")
    _write_page(
        wiki_dir, "sources/Book/Alpha Handbook.md", "source", "Alpha Handbook")
    _write_page(
        wiki_dir, "sources/Book/Zeta Handbook.md", "source", "Zeta Handbook")
    filler_lines = "".join(
        f"- [[concepts/filler-{n:04d}|Filler {n:04d}]]\n"
        for n in range(FILLER_PAGE_COUNT)
    )
    (wiki_dir / "index.md").write_text(
        "# Wiki Index\n\n## concept\n\n"
        + filler_lines
        + "- [[concepts/deleted-page|Deleted Page]]\n\n"
        "## source\n\n"
        "- [[sources/Book/Zeta Handbook|Zeta Handbook]]\n\n"
        "- [[sources/Book/Alpha Handbook|Alpha Handbook]]\n",
        encoding="utf-8",
    )
    _write_ingest_pages(wiki_dir)


def _index_sections(index_text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(type, [(target, title), ...]), ...]`` in file order."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for line in index_text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.startswith("- [[") and line.endswith("]]") and sections:
            target, _, title = line[4:-2].partition("|")
            sections[-1][1].append((target, title))
    return sections


def _on_disk_targets(wiki_dir: Path) -> set[str]:
    return {
        path.relative_to(wiki_dir).with_suffix("").as_posix()
        for path in wiki_dir.rglob("*.md")
        if path.stem not in {"index", "log", "overview"}
    }


class _FakeModel:
    """Answers Stage 3.3's overview prompt and records every prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt, config, max_tokens=None):
        self.prompts.append(prompt)
        return ("# Overview\n\nTopic synthesis.\n", "end_turn")


class TestStage33IndexRebuild(unittest.TestCase):
    def setUp(self):
        self._original_call = stage3.call_anthropic_protocol
        self.model = _FakeModel()
        stage3.call_anthropic_protocol = self.model

    def tearDown(self):
        stage3.call_anthropic_protocol = self._original_call

    def _run_stage_3_3(self, root: Path) -> _core.Config:
        cfg = _config(root)
        raw = root / "raw" / "Datasheet" / f"{SOURCE_NAME}.pdf"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"datasheet")
        stage3.stage_3_3_aggregate_repair(
            cfg.wiki_dir / SOURCE_REL,
            raw,
            _core.file_sha256(raw),
            "mineru-api",
            cfg,
        )
        return cfg

    def test_large_wiki_index_lists_every_page_the_ingest_wrote(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _build_large_wiki(root / "wiki")

            cfg = self._run_stage_3_3(root)

            index_path = cfg.wiki_dir / "index.md"
            listed = {
                target
                for _kind, pages in _index_sections(
                    index_path.read_text(encoding="utf-8"))
                for target, _title in pages
            }
            for rel, _page_type, _title in INGEST_PAGES:
                self.assertIn(rel[:-len(".md")], listed)
            # Nothing on disk is missing and nothing listed is stale.
            self.assertEqual(listed, _on_disk_targets(cfg.wiki_dir))
            # The postcondition Stage 3.3 enforces before aggregate_done.
            stage3._assert_aggregate_outputs(
                cfg.wiki_dir / "log.md",
                index_path,
                f"raw/Datasheet/{SOURCE_NAME}.pdf",
                "0" * 64,
                SOURCE_NAME,
            )

    def test_large_wiki_index_is_grouped_by_type_and_title_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _build_large_wiki(root / "wiki")

            cfg = self._run_stage_3_3(root)

            index_text = (cfg.wiki_dir / "index.md").read_text(encoding="utf-8")
            sections = _index_sections(index_text)
            self.assertEqual(
                [kind for kind, _pages in sections],
                ["comparison", "concept", "entity", "finding",
                 "methodology", "query", "source"],
            )
            for kind, pages in sections:
                titles = [title for _target, title in pages]
                self.assertEqual(titles, sorted(titles, key=str.lower), kind)
            by_kind = dict(sections)
            for rel, page_type, title in INGEST_PAGES:
                self.assertIn((rel[:-len(".md")], title), by_kind[page_type])
            # No blank line may separate two entries of one section.
            self.assertNotRegex(index_text, r"\]\]\n\n- \[\[")

    def test_small_wiki_index_is_rebuilt_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            wiki_dir = root / "wiki"
            _write_page(
                wiki_dir, "sources/Book/Alpha Handbook.md", "source",
                "Alpha Handbook")
            # The shape the retired model rewrite produced.
            (wiki_dir / "index.md").write_text(
                "# Index\n\n## Sources（来源）\n\n"
                "- [[Alpha Handbook]] — Alpha Handbook\n",
                encoding="utf-8",
            )
            _write_ingest_pages(wiki_dir)

            cfg = self._run_stage_3_3(root)

            self.assertEqual(
                (cfg.wiki_dir / "index.md").read_text(encoding="utf-8"),
                stage3.rebuild_index_deterministic(cfg.wiki_dir),
            )
            # In conversation mode every model call is an exit-101 handoff.
            self.assertEqual(
                [p for p in self.model.prompts if "index.md" in p], [])

    def test_replayed_stage_3_3_does_not_snapshot_an_unchanged_index(self):
        # A conversation handoff re-runs Stage 3.3 from the top. The rebuilt
        # index is byte-identical then and must not add a page-history copy.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _build_large_wiki(root / "wiki")

            cfg = self._run_stage_3_3(root)
            self._run_stage_3_3(root)

            snapshots = list(
                (cfg.runtime_dir / "page-history").glob("*_index.md"))
            self.assertEqual(len(snapshots), 1)


if __name__ == "__main__":
    unittest.main()
