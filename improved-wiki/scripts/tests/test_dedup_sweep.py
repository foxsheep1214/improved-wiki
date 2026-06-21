"""Tests for dedup_sweep — orchestration over a tmp wiki with a mock LLM.

No real network: the mock llm_call branches on the system prompt to serve
either the detector response or the merger merged-page.

Run:  python3 scripts/tests/test_dedup_sweep.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dedup_sweep as ds  # noqa: E402
import _dedup  # noqa: E402
from _frontmatter_array import parse_frontmatter_array  # noqa: E402

FIXED_TODAY = lambda: "2026-06-19"  # noqa: E731


def _page(fm: str, body: str) -> str:
    return f"---\n{fm}\n---\n\n{body}"


def _make_wiki(root: Path) -> Path:
    wiki = root / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "lint").mkdir(parents=True)

    (wiki / "entities" / "paos.md").write_text(_page(
        "type: entity\ntitle: PAOs\ntags: [microbiology]\nrelated: [vfa]\nsources: [\"a.pdf\"]",
        "PAOs are polyphosphate accumulating organisms.",
    ), encoding="utf-8")
    (wiki / "entities" / "聚磷菌.md").write_text(_page(
        "type: entity\ntitle: 聚磷菌\ntags: [paos]\nrelated: [pha]\nsources: [\"b.pdf\"]",
        "聚磷菌是聚磷微生物。",
    ), encoding="utf-8")
    (wiki / "concepts" / "vfa.md").write_text(_page(
        "type: concept\ntitle: VFA\nrelated: [paos, 聚磷菌]",
        "Volatile fatty acids. See [[paos]] and [[聚磷菌]].",
    ), encoding="utf-8")
    (wiki / "index.md").write_text(
        "# Index\n\n- [PAOs](entities/paos.md)\n- [聚磷菌](entities/聚磷菌.md)\n",
        encoding="utf-8",
    )
    (wiki / "log.md").write_text("# Log\nanchor, must be skipped\n", encoding="utf-8")
    (wiki / "lint" / "noise.md").write_text("# lint page\nskip me\n", encoding="utf-8")
    return wiki


def _mock_llm():
    """Returns a mock llm_call branching on the system prompt."""
    merged = _page(
        "type: entity\ntitle: PAOs\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        "tags: []\nrelated: []\nsources: []",
        "PAOs are polyphosphate accumulating organisms.\n\n聚磷菌是聚磷微生物。",
    )

    def llm_call(system_prompt: str, user_message: str) -> str:
        if "likely refer to the same" in system_prompt:
            return json.dumps({"groups": [{
                "slugs": ["paos", "聚磷菌"],
                "reason": "EN vs Chinese for the same organism.",
                "confidence": "high",
            }]})
        return merged  # merger prompt

    return llm_call


class TestCollectAndSummaries(unittest.TestCase):
    def test_excludes_anchors_state_and_skipdirs(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            pages = ds.collect_wiki_pages(root / "wiki")
            names = [p.split("/")[-1] for p, _ in pages]
            self.assertIn("paos.md", names)
            self.assertIn("聚磷菌.md", names)
            self.assertIn("vfa.md", names)
            self.assertNotIn("index.md", names)
            self.assertNotIn("log.md", names)
            self.assertNotIn("noise.md", names)  # lint/ skipped

    def test_build_summaries_skips_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            wiki.mkdir(parents=True)
            (wiki / "a.md").write_text("---\ntype: entity\ntitle: A\n---\nbody", encoding="utf-8")
            (wiki / "b.md").write_text("# no frontmatter here", encoding="utf-8")
            pages = ds.collect_wiki_pages(wiki)
            slugs = [s.slug for s in (_dedup.extract_entity_summary(p, c) for p, c in pages) if s is not None]
            self.assertEqual(slugs, ["a"])


class TestDryRun(unittest.TestCase):
    def test_detects_group_writes_report_no_mutations(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            report = ds.run_phase2(root, _mock_llm(), apply=False, today=FIXED_TODAY)
            self.assertEqual(len(report["groups"]), 1)
            self.assertEqual(report["groups"][0]["slugs"], ["paos", "聚磷菌"])
            self.assertEqual(report["applied"], [])
            # No mutations: 聚磷菌.md still present, no backup dir.
            self.assertTrue((root / "wiki/entities/聚磷菌.md").exists())
            self.assertEqual(list((root / ".llm-wiki").glob("dedup-backup-*")), [])
            # Report written.
            self.assertTrue((root / ".llm-wiki/dedup-report.json").exists())


class TestApply(unittest.TestCase):
    def test_merges_backups_deletes_rewrites_and_prunes_index(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            report = ds.run_phase2(root, _mock_llm(), apply=True, today=FIXED_TODAY)

            self.assertEqual(len(report["applied"]), 1)
            applied = report["applied"][0]
            self.assertEqual(applied["canonical"], "paos")
            self.assertEqual(applied["merged_away"], ["聚磷菌"])

            # Canonical page exists with merged body + union frontmatter + updated stamp.
            canon = (root / "wiki/entities/paos.md").read_text(encoding="utf-8")
            self.assertIn("聚磷菌是聚磷微生物。", canon)
            self.assertIn("updated: 2026-06-19", canon)
            self.assertEqual(
                sorted(parse_frontmatter_array(canon, "sources")),
                ["a.pdf", "b.pdf"],
            )

            # Merged-away page deleted.
            self.assertFalse((root / "wiki/entities/聚磷菌.md").exists())

            # Cross-ref rewrite on vfa.md: [[聚磷菌]] → [[paos]], related deduped.
            vfa = (root / "wiki/concepts/vfa.md").read_text(encoding="utf-8")
            self.assertIn("[[paos]]", vfa)
            self.assertNotIn("聚磷菌", vfa)
            self.assertEqual(parse_frontmatter_array(vfa, "related"), ["paos"])

            # Backup dir contains pre-merge paos, 聚磷菌, AND vfa (rewritten).
            backup_dirs = list((root / ".llm-wiki").glob("dedup-backup-*"))
            self.assertEqual(len(backup_dirs), 1)
            bdir = backup_dirs[0]
            self.assertTrue((bdir / "wiki/entities/paos.md").exists())
            self.assertTrue((bdir / "wiki/entities/聚磷菌.md").exists())
            self.assertTrue((bdir / "wiki/concepts/vfa.md").exists())
            # vfa backup is the PRE-rewrite content (still references 聚磷菌).
            vfa_backup = (bdir / "wiki/concepts/vfa.md").read_text(encoding="utf-8")
            self.assertIn("聚磷菌", vfa_backup)

            # index.md pruned of the 聚磷菌 line, keeps PAOs line.
            idx = (root / "wiki/index.md").read_text(encoding="utf-8")
            self.assertNotIn("聚磷菌", idx)
            self.assertIn("entities/paos.md", idx)


class TestWhitelist(unittest.TestCase):
    def test_whitelist_suppresses_group(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            report = ds.run_phase2(
                root, _mock_llm(), apply=False, today=FIXED_TODAY,
                whitelist_pairs=[["paos", "聚磷菌"]],
            )
            self.assertEqual(report["groups"], [])

    def test_whitelist_file_loaded(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            wl = root / "wl.json"
            wl.write_text(json.dumps({"not_duplicates": [["paos", "聚磷菌"]]}), encoding="utf-8")
            pairs = ds.load_whitelist(wl)
            self.assertEqual(pairs, [["paos", "聚磷菌"]])


class TestMainConversationHandoff(unittest.TestCase):
    """End-to-end: main() exits 101 on first detect, resumes to 0 after the
    calling agent answers the conversation prompt."""

    def test_main_pending_then_resume(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)

            # First invocation: detect call is uncached → ConversationPending → 101.
            # --semantic opts into phase 2 (the LLM path); without it main() only
            # runs deterministic phase 1 and returns 0.
            rc = ds.main(["--project", str(root), "--semantic", "--dry-run"])
            self.assertEqual(rc, 101)
            conv_dir = root / ".llm-wiki" / "conversation" / "dedup"
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 1)

            # Simulate the calling agent answering the detect prompt with no groups.
            md = md_files[0]
            md.with_suffix(".txt").write_text(json.dumps({"groups": []}),
                                              encoding="utf-8")

            # Second invocation: detect cached → no groups → report written → 0.
            rc = ds.main(["--project", str(root), "--semantic", "--dry-run"])
            self.assertEqual(rc, 0)
            report = json.loads(
                (root / ".llm-wiki" / "dedup-report.json").read_text("utf-8")
            )
            # Written report nests phase-2 results under "phase2".
            self.assertEqual(report["phase2"]["groups"], [])
            self.assertFalse(report["apply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
