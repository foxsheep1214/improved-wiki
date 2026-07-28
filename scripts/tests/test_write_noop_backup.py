"""Stage 3.1 no-op writes must not create redundant page-history snapshots."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_3_write as write_stage  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _page(body: str, sources: list[str]) -> str:
    source_yaml = ", ".join(f'"{source}"' for source in sources)
    return (
        "---\n"
        "type: concept\n"
        'title: "Existing Title"\n'
        "tags: [radar]\n"
        "related: []\n"
        f"sources: [{source_yaml}]\n"
        "created: 2025-01-02\n"
        "updated: 2025-01-02\n"
        "---\n\n"
        f"{body}\n"
    )


def _history_pages(cfg: _core.Config) -> list[Path]:
    history = cfg.runtime_dir / "page-history"
    return list(history.glob("*.md")) if history.exists() else []


class TestNoopWriteBackup(unittest.TestCase):
    def test_byte_identical_overwrite_skips_backup_and_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            path = cfg.wiki_dir / "concepts" / "range-resolution.md"
            path.parent.mkdir(parents=True)
            content = _page(
                "## Definition\nStable wording.",
                ["raw/Book/Radar.pdf"],
            )
            path.write_text(content, encoding="utf-8")
            before_mtime = path.stat().st_mtime_ns

            write_stage.stage_3_1_write_wiki_file(path, content, cfg)

            self.assertEqual(content, path.read_text(encoding="utf-8"))
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)
            self.assertEqual([], _history_pages(cfg))

    def test_idempotent_cross_source_remerge_skips_backup(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            path = cfg.wiki_dir / "concepts" / "range-resolution.md"
            path.parent.mkdir(parents=True)
            existing = _page(
                "## Definition\nAlready merged wording from both books.",
                ["raw/Book/First.pdf", "raw/Book/Second.pdf"],
            )
            incoming = _page(
                "## Definition\nOriginal contribution from the second book.",
                ["raw/Book/Second.pdf"],
            )
            path.write_text(existing, encoding="utf-8")
            before_mtime = path.stat().st_mtime_ns

            write_stage.stage_3_1_write_wiki_file(
                path,
                incoming,
                cfg,
                merge=True,
                source_file="raw/Book/Second.pdf",
            )

            self.assertEqual(existing, path.read_text(encoding="utf-8"))
            self.assertEqual(before_mtime, path.stat().st_mtime_ns)
            self.assertEqual([], _history_pages(cfg))

    def test_changed_overwrite_still_creates_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            path = cfg.wiki_dir / "concepts" / "range-resolution.md"
            path.parent.mkdir(parents=True)
            existing = _page(
                "## Definition\nOld wording.",
                ["raw/Book/Radar.pdf"],
            )
            changed = _page(
                "## Definition\nNew wording.",
                ["raw/Book/Radar.pdf"],
            )
            path.write_text(existing, encoding="utf-8")

            write_stage.stage_3_1_write_wiki_file(path, changed, cfg)

            self.assertEqual(changed, path.read_text(encoding="utf-8"))
            backups = _history_pages(cfg)
            self.assertEqual(1, len(backups))
            self.assertEqual(existing, backups[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
