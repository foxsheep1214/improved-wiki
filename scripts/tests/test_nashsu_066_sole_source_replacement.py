"""NashSU 0.6.6 corrected-source replacement at the Stage 3 write boundary."""
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


def _page(body: str, sources: list[str], *, title: str = "Existing Title") -> str:
    source_yaml = ", ".join(f'"{source}"' for source in sources)
    return (
        "---\n"
        "type: concept\n"
        f'title: "{title}"\n'
        "tags: [radar]\n"
        "related: []\n"
        f"sources: [{source_yaml}]\n"
        "created: 2025-01-02\n"
        "updated: 2025-01-02\n"
        "---\n\n"
        f"{body}\n"
    )


class TestSoleSourceReplacement(unittest.TestCase):
    def test_writer_replaces_stale_body_and_locks_identity_fields(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            path = cfg.wiki_dir / "concepts" / "range-resolution.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                _page(
                    "## Definition\nObsolete wording.",
                    ["raw/Book/Radar.pdf"],
                ),
                encoding="utf-8",
            )
            incoming = _page(
                "## Definition\nCorrected wording.",
                ["raw/Book/Radar.pdf"],
                title="Generated Title Must Not Replace Existing",
            )

            write_stage.stage_3_1_write_wiki_file(
                path,
                incoming,
                cfg,
                merge=True,
                source_file="raw/Book/Radar.pdf",
            )

            result = path.read_text(encoding="utf-8")
            self.assertIn("Corrected wording", result)
            self.assertNotIn("Obsolete wording", result)
            self.assertIn('title: "Existing Title"', result)
            self.assertIn("created: 2025-01-02", result)
            self.assertTrue(
                list((cfg.runtime_dir / "page-history").glob("*.md")),
                "replacement should preserve a recoverable backup",
            )

    def test_multi_source_and_same_basename_other_path_are_not_sole_owner(self):
        multi = _page(
            "body",
            ["raw/Book/A/Radar.pdf", "raw/Book/B/Radar.pdf"],
        )
        other_path = _page("body", ["raw/Book/B/Radar.pdf"])
        self.assertFalse(
            write_stage._stage_3_1_is_owned_only_by_source(
                multi,
                "raw/Book/A/Radar.pdf",
            )
        )
        self.assertFalse(
            write_stage._stage_3_1_is_owned_only_by_source(
                other_path,
                "raw/Book/A/Radar.pdf",
            )
        )
        self.assertTrue(
            write_stage._stage_3_1_is_owned_only_by_source(
                other_path,
                "raw/Book/B/Radar.pdf",
            )
        )


if __name__ == "__main__":
    unittest.main()
