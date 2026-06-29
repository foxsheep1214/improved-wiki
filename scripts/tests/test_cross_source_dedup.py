"""Tests for cross_source_dedup — orchestration over a tmp wiki with a mock LLM.

No real network: the mock llm_call branches on the system prompt to serve
either the detector response or the merger merged-page.

Run:  python3 scripts/tests/test_cross_source_dedup.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cross_source_dedup as ds  # noqa: E402
import _dedup  # noqa: E402
import _dedup_storage as dstore  # noqa: E402
from _dedup import EntitySummary  # noqa: E402
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
            # main() defaults to LLM semantic (NashSU parity).
            rc = ds.main(["--project", str(root), "--dry-run"])
            self.assertEqual(rc, 101)
            conv_dir = root / ".llm-wiki" / "conversation" / "dedup"
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 1)

            # Simulate the calling agent answering the detect prompt with no groups.
            md = md_files[0]
            md.with_suffix(".txt").write_text(json.dumps({"groups": []}),
                                              encoding="utf-8")

            # Second invocation: detect cached → no groups → report written → 0.
            rc = ds.main(["--project", str(root), "--dry-run"])
            self.assertEqual(rc, 0)
            report = json.loads(
                (root / ".llm-wiki" / "dedup-report.json").read_text("utf-8")
            )
            # Written report nests phase-2 results under "phase2".
            self.assertEqual(report["phase2"]["groups"], [])
            self.assertFalse(report["apply"])


def _summ(slug, desc="", title=None, tags=None):
    return EntitySummary(slug=slug, path=f"wiki/entities/{slug}.md",
                         type="entity", title=title or slug,
                         tags=tags or [], description=desc)


class TestPrefilterThresholdOverride(unittest.TestCase):
    """Task 1+2: cross-source path must call candidate_pairs with the 0.68
    override (NOT the 0.82 module default) and vector the short description."""

    def test_passes_068_threshold_and_description_body(self):
        summaries = [_summ("a", "alpha desc"), _summ("b", "beta desc")]
        pages = [("wiki/entities/a.md", _page("type: entity\ntitle: a", "FULL BODY A")),
                 ("wiki/entities/b.md", _page("type: entity\ntitle: b", "FULL BODY B"))]
        captured = {}

        def fake_candidate_pairs(emb_pages, *, threshold):
            captured["threshold"] = threshold
            captured["bodies"] = {p["id"]: p["body"] for p in emb_pages}
            return []  # no pairs → small wiki falls back to full scan

        orig = ds.candidate_pairs
        ds.candidate_pairs = fake_candidate_pairs
        try:
            ds._detect_groups(summaries, pages, lambda s, u: '{"groups": []}',
                              [], embedding_prefilter=True)
        finally:
            ds.candidate_pairs = orig

        self.assertEqual(captured["threshold"], ds.DEDUP_PREFILTER_THRESHOLD)
        self.assertEqual(captured["threshold"], 0.68)
        # Vectored the short description, NOT the full body.
        self.assertEqual(captured["bodies"]["a"], "alpha desc")
        self.assertNotIn("FULL BODY", captured["bodies"]["a"])


class TestBatching(unittest.TestCase):
    """Task 3: clusters packed into <=80-summary batches; identical groups
    deduped across batches."""

    def test_many_clusters_bounded_per_batch(self):
        # NashSU batchCandidateClusters packs WHOLE clusters; it flushes once a
        # batch reaches >=80, so no batch combines clusters past the cap. (A
        # single oversized cluster stays whole — NashSU does not sub-split one
        # cluster.) 50 two-member clusters = 100 summaries → must be >1 batch,
        # each <= 80.
        summary_by_slug = {f"s{i}": _summ(f"s{i}") for i in range(100)}
        clusters = [[f"s{2*i}", f"s{2*i+1}"] for i in range(50)]
        batches = ds._batch_candidate_clusters(clusters, summary_by_slug)
        self.assertGreater(len(batches), 1)
        for b in batches:
            self.assertLessEqual(len(b), ds.DEDUP_DETECTOR_BATCH_SUMMARIES)
        self.assertEqual(sum(len(b) for b in batches), 100)

    def test_single_oversized_cluster_kept_whole(self):
        # Faithful to NashSU: one 200-member cluster is one batch, not split.
        summary_by_slug = {f"s{i}": _summ(f"s{i}") for i in range(200)}
        clusters = [[f"s{i}" for i in range(200)]]
        batches = ds._batch_candidate_clusters(clusters, summary_by_slug)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 200)

    def test_small_clusters_packed_together(self):
        summary_by_slug = {f"s{i}": _summ(f"s{i}") for i in range(6)}
        clusters = [["s0", "s1"], ["s2", "s3"], ["s4", "s5"]]
        batches = ds._batch_candidate_clusters(clusters, summary_by_slug)
        self.assertEqual(len(batches), 1)  # 6 < 80 → all in one batch
        self.assertEqual(len(batches[0]), 6)

    def test_unique_groups_dedups_across_batches(self):
        groups = [
            {"slugs": ["paos", "聚磷菌"], "reason": "r1", "confidence": "high"},
            {"slugs": ["聚磷菌", "PAOS"], "reason": "r2", "confidence": "low"},  # same set
            {"slugs": ["x", "y"], "reason": "r3", "confidence": "high"},
        ]
        out = ds._unique_duplicate_groups(groups)
        self.assertEqual(len(out), 2)
        keys = {ds._normalize_slug_group_key(g["slugs"]) for g in out}
        self.assertIn(ds._normalize_slug_group_key(["paos", "聚磷菌"]), keys)
        self.assertIn(ds._normalize_slug_group_key(["x", "y"]), keys)


class TestEmptyPrefilterLimit(unittest.TestCase):
    """Task 4: zero candidate pairs → full scan only when summaries<=250."""

    def _run(self, n_summaries, candidate_return):
        summaries = [_summ(f"s{i}", f"desc{i}") for i in range(n_summaries)]
        pages = [(f"wiki/entities/s{i}.md",
                  _page(f"type: entity\ntitle: s{i}", f"body{i}"))
                 for i in range(n_summaries)]
        called = {"full_scan": 0}

        def fake_detect(subset, llm, *, not_duplicates):
            called["full_scan"] += 1
            return []

        orig_cp = ds.candidate_pairs
        orig_dd = ds._dedup.detect_duplicate_groups
        ds.candidate_pairs = lambda emb, *, threshold: candidate_return
        ds._dedup.detect_duplicate_groups = fake_detect
        try:
            ds._detect_groups(summaries, pages, lambda s, u: "", [],
                              embedding_prefilter=True)
        finally:
            ds.candidate_pairs = orig_cp
            ds._dedup.detect_duplicate_groups = orig_dd
        return called["full_scan"]

    def test_small_wiki_empty_pairs_falls_back_to_full_scan(self):
        # 10 summaries, no pairs → full scan runs once.
        self.assertEqual(self._run(10, []), 1)

    def test_large_wiki_empty_pairs_skips_full_scan(self):
        # 251 summaries (> 250 limit), no pairs → no full scan (avoid #359 hang).
        self.assertEqual(self._run(251, []), 0)

    def test_coverage_error_large_wiki_skips(self):
        summaries = [_summ(f"s{i}", f"d{i}") for i in range(251)]
        pages = [(f"wiki/entities/s{i}.md", _page(f"type: entity\ntitle: s{i}", "b"))
                 for i in range(251)]
        called = {"full_scan": 0}

        def boom(emb, *, threshold):
            raise ds.DuplicatePrefilterError("embedded only 3/251 pages")

        def fake_detect(subset, llm, *, not_duplicates):
            called["full_scan"] += 1
            return []

        orig_cp, orig_dd = ds.candidate_pairs, ds._dedup.detect_duplicate_groups
        ds.candidate_pairs = boom
        ds._dedup.detect_duplicate_groups = fake_detect
        try:
            out = ds._detect_groups(summaries, pages, lambda s, u: "", [],
                                    embedding_prefilter=True)
        finally:
            ds.candidate_pairs, ds._dedup.detect_duplicate_groups = orig_cp, orig_dd
        self.assertEqual(out, [])
        self.assertEqual(called["full_scan"], 0)  # large wiki skipped


class TestWhitelistPairPrefilter(unittest.TestCase):
    """Task 5: whitelisted pairs dropped BEFORE clustering."""

    def test_filters_whitelisted_pair(self):
        pairs = [("paos", "聚磷菌"), ("x", "y")]
        not_dup = [["PAOS", "聚磷菌"]]  # case-insensitive match
        out = ds._filter_whitelisted_pairs(pairs, not_dup)
        self.assertEqual(out, [("x", "y")])

    def test_empty_whitelist_passthrough(self):
        pairs = [("a", "b")]
        self.assertEqual(ds._filter_whitelisted_pairs(pairs, []), pairs)

    def test_whitelisted_pair_removed_before_clustering(self):
        # paos--聚磷菌 whitelisted; bridging pair to a third page also present.
        # Pre-cluster filter must drop the whitelisted edge so it can't merge.
        summaries = [_summ("paos", "d"), _summ("聚磷菌", "d"), _summ("z", "d")]
        pages = [(f"wiki/entities/{s.slug}.md",
                  _page(f"type: entity\ntitle: {s.slug}", "b")) for s in summaries]
        detector_batches = []

        def fake_cp(emb, *, threshold):
            return [("paos", "聚磷菌")]  # only the whitelisted pair

        def fake_detect(subset, llm, *, not_duplicates):
            detector_batches.append([s.slug for s in subset])
            return []

        orig_cp, orig_dd = ds.candidate_pairs, ds._dedup.detect_duplicate_groups
        ds.candidate_pairs = fake_cp
        ds._dedup.detect_duplicate_groups = fake_detect
        try:
            ds._detect_groups(summaries, pages, lambda s, u: "",
                              [["paos", "聚磷菌"]], embedding_prefilter=True)
        finally:
            ds.candidate_pairs, ds._dedup.detect_duplicate_groups = orig_cp, orig_dd
        # All pairs filtered out → no clusters → detector never called.
        self.assertEqual(detector_batches, [])


class TestWhitelistWritePath(unittest.TestCase):
    """Task 6: --mark-not-duplicate records a pair idempotently."""

    def test_add_not_duplicate_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            rt = Path(t)
            self.assertTrue(dstore.add_not_duplicate(rt, ["paos", "聚磷菌"]))
            # Re-add same pair (any order/casing) → no-op.
            self.assertFalse(dstore.add_not_duplicate(rt, ["聚磷菌", "PAOS"]))
            loaded = dstore.load_not_duplicates(rt)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(sorted(loaded[0]), sorted(["paos", "聚磷菌"]))

    def test_add_rejects_single_slug(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(dstore.add_not_duplicate(Path(t), ["only"]))

    def test_cli_mark_not_duplicate_writes_and_is_read_by_detector(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            rc = ds.main(["--project", str(root),
                          "--mark-not-duplicate", "paos", "聚磷菌"])
            self.assertEqual(rc, 0)
            # Written to dedup-whitelist.json under runtime dir.
            from _paths import detect_runtime_dir
            rt = detect_runtime_dir(root)
            loaded = dstore.load_not_duplicates(rt)
            self.assertEqual(len(loaded), 1)
            # run_phase2 picks it up as a whitelist pair → suppresses the group.
            report = ds.run_phase2(root, _mock_llm(), apply=False, today=FIXED_TODAY)
            self.assertEqual(report["groups"], [])


class TestMergeLock(unittest.TestCase):
    """Task 7: merge+persist runs under an exclusive file lock."""

    def test_merge_lock_serializes(self):
        with tempfile.TemporaryDirectory() as t:
            rt = Path(t)
            # Re-entrant from a different fd must block; we assert the lock file
            # is created and a second non-blocking acquire fails while held.
            import fcntl
            with ds._merge_lock(rt):
                lock_path = rt / "dedup-merge.lock"
                self.assertTrue(lock_path.exists())
                fd = os.open(str(lock_path), os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            # After release, a non-blocking acquire succeeds.
            fd = os.open(str(rt / "dedup-merge.lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
