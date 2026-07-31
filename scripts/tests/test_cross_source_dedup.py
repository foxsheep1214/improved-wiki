"""Tests for cross_source_dedup — orchestration over a tmp wiki with a mock LLM.

No real network: the mock llm_call branches on the system prompt to serve
either the detector response or the merger merged-page.

Run:  python3 scripts/tests/test_cross_source_dedup.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            report = ds.run_phase2(root, _mock_llm(), apply=False, today=FIXED_TODAY,
                                    embedding_prefilter=False)
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
            report = ds.run_phase2(root, _mock_llm(), apply=True, today=FIXED_TODAY,
                                    embedding_prefilter=False)

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


class TestApplySnapshotFreshness(unittest.TestCase):
    """2026-07-10: _apply_merges must not act on a stale in-memory snapshot.

    The pages list is collected once at run_phase2 start; the old code built
    pages_by_slug once from it and never refreshed, so when two groups share a
    page (group 1 merges A+B into A, group 2 merges A+C), group 2's merger was
    fed A's PRE-merge content — its output would silently discard group 1's
    merged-in material. In practice one process invocation usually handled one
    group (each ConversationPending exits and the next invocation re-reads from
    disk), which masked the bug; the parallel-eager dedup flow now applies many
    groups in one invocation, so the snapshot must be updated after each merge.
    """

    def test_second_group_sees_first_groups_merged_content(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "entities").mkdir(parents=True)
            (wiki / "entities" / "alpha.md").write_text(_page(
                "type: entity\ntitle: Alpha\ntags: []\nrelated: []\nsources: [\"a.pdf\"]",
                "Alpha original body.",
            ), encoding="utf-8")
            (wiki / "entities" / "alpha-variant.md").write_text(_page(
                "type: entity\ntitle: Alpha Variant\ntags: []\nrelated: []\nsources: [\"b.pdf\"]",
                "MERGED-IN-BY-GROUP-ONE.",
            ), encoding="utf-8")
            (wiki / "entities" / "alpha-alias.md").write_text(_page(
                "type: entity\ntitle: Alpha Alias\ntags: []\nrelated: []\nsources: [\"c.pdf\"]",
                "Alias body.",
            ), encoding="utf-8")
            (wiki / "index.md").write_text("# Index\n", encoding="utf-8")

            merge_prompts: list[str] = []
            merged_v1 = _page(
                "type: entity\ntitle: Alpha\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
                "tags: []\nrelated: []\nsources: []",
                "Alpha original body.\n\nMERGED-IN-BY-GROUP-ONE.",
            )
            merged_v2 = _page(
                "type: entity\ntitle: Alpha\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
                "tags: []\nrelated: []\nsources: []",
                "Alpha original body.\n\nMERGED-IN-BY-GROUP-ONE.\n\nAlias body.",
            )

            def llm_call(system_prompt: str, user_message: str) -> str:
                if "likely refer to the same" in system_prompt:
                    return json.dumps({"groups": [
                        {"slugs": ["alpha", "alpha-variant"],
                         "reason": "same", "confidence": "high"},
                        {"slugs": ["alpha", "alpha-alias"],
                         "reason": "same", "confidence": "high"},
                    ]})
                merge_prompts.append(user_message)
                return merged_v1 if len(merge_prompts) == 1 else merged_v2

            report = ds.run_phase2(root, llm_call, apply=True, today=FIXED_TODAY,
                                   embedding_prefilter=False)
            self.assertEqual(len(report["applied"]), 2)
            # Group 2's merge prompt must contain group 1's merged output for
            # the shared canonical page — not alpha's original pre-merge body.
            self.assertEqual(len(merge_prompts), 2)
            self.assertIn("MERGED-IN-BY-GROUP-ONE.", merge_prompts[1])
            # And the deleted alpha-variant must no longer appear as a live
            # "other page" in group 2's prompt context.
            final = (wiki / "entities" / "alpha.md").read_text(encoding="utf-8")
            self.assertIn("Alias body.", final)
            self.assertIn("MERGED-IN-BY-GROUP-ONE.", final)


class TestIncrementalEmbedCache(unittest.TestCase):
    """2026-07-11 (#1): the embedding cache is per-page content-hashed —
    unchanged pages reuse their cached vector; only new/changed pages are
    embedded. The old v1 cache (global sorted-ids+count key) invalidated
    wholesale on ANY page-set change, forcing a full re-embed of the whole
    wiki (demonstrated live: a 2-page id dedupe cascaded into re-embedding
    ~7.5K pages and 52/60 detector-batch cache misses)."""

    def _run_detect(self, root, monkey_embed_calls):
        wiki = root / "wiki"
        pages = ds.collect_wiki_pages(wiki)
        summaries = [s for s in (
            _dedup.extract_entity_summary(p, c) for p, c in pages) if s]
        import hashlib

        def fake_bounded(emb_pages):
            monkey_embed_calls.append([pg["id"] for pg in emb_pages])
            return {pg["id"]: [1.0, 0.0] for pg in emb_pages}

        original = ds._embed_pages_bounded
        ds._embed_pages_bounded = fake_bounded
        try:
            runtime = root / ".llm-wiki"
            ds._detect_groups(summaries, pages, lambda s, u: '{"groups": []}',
                              [], embedding_prefilter=True, runtime=runtime)
        finally:
            ds._embed_pages_bounded = original

    def test_second_run_embeds_nothing_third_embeds_only_changed(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "entities").mkdir(parents=True)
            (wiki / "entities" / "aa.md").write_text(_page(
                "type: entity\ntitle: AA\ntags: []\nrelated: []\nsources: []",
                "AA body."), encoding="utf-8")
            (wiki / "entities" / "bb.md").write_text(_page(
                "type: entity\ntitle: BB\ntags: []\nrelated: []\nsources: []",
                "BB body."), encoding="utf-8")

            calls: list = []
            self._run_detect(root, calls)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), ["aa", "bb"])

            # Run 2: nothing changed → zero embed calls.
            self._run_detect(root, calls)
            self.assertEqual(len(calls), 1, "unchanged wiki must not re-embed")

            # Run 3: change ONE page's description → only that page re-embeds
            # (the old v1 cache would have re-embedded both).
            (wiki / "entities" / "bb.md").write_text(_page(
                "type: entity\ntitle: BB\ntags: []\nrelated: []\nsources: []",
                "BB body CHANGED."), encoding="utf-8")
            self._run_detect(root, calls)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1], ["bb"])


class TestApplySlugCollisionGuard(unittest.TestCase):
    """2026-07-11: a merge group containing a slug that maps to MULTIPLE files
    (same basename in different dirs) must be skipped mechanically — the
    slug-keyed pages_by_slug would silently shadow one file and the merge
    could read/delete the wrong one. See known-issues.md."""

    def test_colliding_group_skipped_files_untouched(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "entities").mkdir(parents=True)
            (wiki / "queries").mkdir(parents=True)
            (wiki / "entities" / "xdup.md").write_text(_page(
                "type: entity\ntitle: XDup\ntags: []\nrelated: []\nsources: []",
                "Entity version.",
            ), encoding="utf-8")
            (wiki / "queries" / "xdup.md").write_text(_page(
                "type: query\ntitle: XDup Q\ntags: []\nrelated: []\nsources: []",
                "Query stub version.",
            ), encoding="utf-8")
            (wiki / "entities" / "ypage.md").write_text(_page(
                "type: entity\ntitle: Y\ntags: []\nrelated: []\nsources: []",
                "Y body.",
            ), encoding="utf-8")
            (wiki / "index.md").write_text("# Index\n", encoding="utf-8")

            merge_calls = []

            def llm_call(system_prompt: str, user_message: str) -> str:
                if "likely refer to the same" in system_prompt:
                    return json.dumps({"groups": [
                        {"slugs": ["xdup", "ypage"], "reason": "r",
                         "confidence": "high"},
                    ]})
                merge_calls.append(user_message)
                return "---\ntype: entity\ntitle: X\n---\n\nmerged"

            report = ds.run_phase2(root, llm_call, apply=True, today=FIXED_TODAY,
                                   embedding_prefilter=False)
            self.assertEqual(report["applied"], [])
            self.assertEqual(merge_calls, [])  # merge never attempted
            self.assertTrue((wiki / "entities" / "xdup.md").exists())
            self.assertTrue((wiki / "queries" / "xdup.md").exists())
            self.assertTrue((wiki / "entities" / "ypage.md").exists())


class TestApplyMergesEagerDrain(unittest.TestCase):
    """2026-07-10: _apply_merges must emit ALL uncached merge prompts in one
    invocation before raising ConversationPending once — not stop at the
    first pending group.

    Why this matters more than convenience: every APPLIED merge changes pages
    on disk, and the embedding cache + detector-batch conversation cache are
    both content/id-keyed, so a re-invocation after even one applied merge
    invalidates the whole detection layer (full re-embed + 60 detector
    batches re-answered). Emitting all merge prompts while the disk is still
    unchanged keeps every upstream cache hit; the answered merges then apply
    together in one later invocation. The snapshot-freshness sync (see
    TestApplySnapshotFreshness) makes this safe for groups sharing a page:
    a stale pre-merge prompt simply misses the content-hash cache after the
    earlier group applies, and re-pends with fresh content.
    """

    def test_all_merge_prompts_attempted_before_single_raise(self):
        from _core import ConversationPending
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "entities").mkdir(parents=True)
            for name, body in (("a1", "A one."), ("a2", "A two."),
                               ("b1", "B one."), ("b2", "B two.")):
                (wiki / "entities" / f"{name}.md").write_text(_page(
                    f"type: entity\ntitle: {name}\ntags: []\nrelated: []\nsources: []",
                    body,
                ), encoding="utf-8")
            (wiki / "index.md").write_text("# Index\n", encoding="utf-8")

            merge_attempts = []

            def llm_call(system_prompt: str, user_message: str) -> str:
                if "likely refer to the same" in system_prompt:
                    return json.dumps({"groups": [
                        {"slugs": ["a1", "a2"], "reason": "r", "confidence": "high"},
                        {"slugs": ["b1", "b2"], "reason": "r", "confidence": "high"},
                    ]})
                merge_attempts.append(user_message)
                raise ConversationPending()

            with self.assertRaises(ConversationPending):
                ds.run_phase2(root, llm_call, apply=True, today=FIXED_TODAY,
                              embedding_prefilter=False)
            # BOTH groups' merge prompts were attempted (emitted), not just
            # the first, before the single ConversationPending re-raise.
            self.assertEqual(len(merge_attempts), 2)
            # No merge was applied: all four pages untouched on disk.
            for name in ("a1", "a2", "b1", "b2"):
                self.assertTrue((wiki / "entities" / f"{name}.md").exists())


class TestWhitelist(unittest.TestCase):
    def test_whitelist_suppresses_group(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            report = ds.run_phase2(
                root, _mock_llm(), apply=False, today=FIXED_TODAY,
                whitelist_pairs=[["paos", "聚磷菌"]],
                embedding_prefilter=False,
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
            rc = ds.main(["--project", str(root), "--dry-run", "--no-embedding-prefilter"])
            self.assertEqual(rc, 101)
            conv_dir = root / ".llm-wiki" / "conversation" / "dedup"
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 1)

            # Simulate the calling agent answering the detect prompt with no groups.
            md = md_files[0]
            md.with_suffix(".txt").write_text(json.dumps({"groups": []}),
                                              encoding="utf-8")

            # Second invocation: detect cached → no groups → report written → 0.
            rc = ds.main(["--project", str(root), "--dry-run", "--no-embedding-prefilter"])
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

        def fake_candidate_pairs(emb_pages, *, threshold, embeddings=None):
            captured["threshold"] = threshold
            captured["bodies"] = {p["id"]: p["body"] for p in emb_pages}
            return []  # no pairs → small wiki falls back to full scan

        orig = ds.candidate_pairs
        orig_embed = ds._embed_pages_bounded
        ds.candidate_pairs = fake_candidate_pairs
        ds._embed_pages_bounded = lambda emb_pages: {p["id"]: [1.0] for p in emb_pages}
        try:
            ds._detect_groups(summaries, pages, lambda s, u: '{"groups": []}',
                              [], embedding_prefilter=True)
        finally:
            ds.candidate_pairs = orig
            ds._embed_pages_bounded = orig_embed

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

    def test_single_oversized_cluster_is_split_into_capped_batches(self):
        # 2026-07-10: deliberate non-parity fix. NashSU's batchCandidateClusters
        # keeps one oversized cluster whole (never sub-splits) — confirmed
        # against the local NashSU v0.6.0 checkout, byte-for-byte the same gap.
        # RadarWiki hit this for real: a loose 0.68 similarity threshold's
        # transitive union-find chaining across ~7000 topically-cohesive pages
        # produced a single 4606-member cluster, blowing a single LLM call 57x
        # past the 80-item cap. Every batch must now stay <=80, even one
        # cluster's own worth of candidates.
        summary_by_slug = {f"s{i}": _summ(f"s{i}") for i in range(200)}
        clusters = [[f"s{i}" for i in range(200)]]
        batches = ds._batch_candidate_clusters(clusters, summary_by_slug)
        self.assertGreater(len(batches), 1)
        for b in batches:
            self.assertLessEqual(len(b), ds.DEDUP_DETECTOR_BATCH_SUMMARIES)
            self.assertGreaterEqual(len(b), 2)
        self.assertEqual(sum(len(b) for b in batches), 200)

    def test_oversized_cluster_flushes_pending_batch_first(self):
        # A small cluster accumulating in `current`, followed by an oversized
        # one, must not have the oversized cluster merged into `current` —
        # the pending small batch is flushed first, then the big cluster is
        # split on its own.
        summary_by_slug = {f"s{i}": _summ(f"s{i}") for i in range(210)}
        clusters = [["s0", "s1", "s2"]] + [[f"s{i}" for i in range(10, 210)]]
        batches = ds._batch_candidate_clusters(clusters, summary_by_slug)
        self.assertEqual(batches[0], [summary_by_slug["s0"], summary_by_slug["s1"],
                                       summary_by_slug["s2"]])
        for b in batches[1:]:
            self.assertLessEqual(len(b), ds.DEDUP_DETECTOR_BATCH_SUMMARIES)
        self.assertEqual(sum(len(b) for b in batches), 203)

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


class TestDetectGroupsEagerDrain(unittest.TestCase):
    """2026-07-10: the batch loop inside _detect_groups must emit ALL
    uncached batches' prompts (calling llm_call once per batch) before
    raising ConversationPending a single time — not stop at the first
    pending batch. Mirrors wiki-lint-semantic.py's eager-drain fix; the
    candidate-cluster batches here are independent (disjoint clusters, only
    deduped once at the end via _unique_duplicate_groups), so there's no
    ordering dependency to preserve. Real-world trigger: a RadarWiki dedup
    run that (after the oversized-cluster split fix above) produced dozens
    of detector batches — answering them one round-trip at a time would
    take hours; parallel dispatch needs all prompts emitted up front."""

    def test_all_batches_attempted_before_raising_once(self):
        from _core import ConversationPending
        summaries = [_summ(f"s{i}") for i in range(100)]
        pairs = [(f"s{2 * i}", f"s{2 * i + 1}") for i in range(50)]  # 50 disjoint pairs

        calls = []

        def llm_call(system, user):
            calls.append(user)
            raise ConversationPending()

        with mock.patch.object(ds, "_token_candidate_pairs", return_value=pairs):
            with self.assertRaises(ConversationPending):
                ds._detect_groups(summaries, [], llm_call, [],
                                  embedding_prefilter=False, token_only=True)

        # 100 summaries in 2-item clusters, 80-item cap → >=2 batches. ALL of
        # them must have been attempted (llm_call invoked once per batch)
        # before the single re-raised ConversationPending — not just the first.
        self.assertGreaterEqual(len(calls), 2)


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
        orig_embed = ds._embed_pages_bounded
        ds.candidate_pairs = lambda emb, *, threshold, embeddings=None: candidate_return
        ds._dedup.detect_duplicate_groups = fake_detect
        ds._embed_pages_bounded = lambda emb_pages: {p["id"]: [1.0] for p in emb_pages}
        try:
            ds._detect_groups(summaries, pages, lambda s, u: "", [],
                              embedding_prefilter=True)
        finally:
            ds.candidate_pairs = orig_cp
            ds._dedup.detect_duplicate_groups = orig_dd
            ds._embed_pages_bounded = orig_embed
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

        def boom(emb, *, threshold, embeddings=None):
            raise ds.DuplicatePrefilterError("embedded only 3/251 pages")

        def fake_detect(subset, llm, *, not_duplicates):
            called["full_scan"] += 1
            return []

        orig_cp, orig_dd = ds.candidate_pairs, ds._dedup.detect_duplicate_groups
        orig_embed = ds._embed_pages_bounded
        ds.candidate_pairs = boom
        ds._dedup.detect_duplicate_groups = fake_detect
        ds._embed_pages_bounded = lambda emb_pages: {p["id"]: [1.0] for p in emb_pages}
        try:
            out = ds._detect_groups(summaries, pages, lambda s, u: "", [],
                                    embedding_prefilter=True)
        finally:
            ds.candidate_pairs, ds._dedup.detect_duplicate_groups = orig_cp, orig_dd
            ds._embed_pages_bounded = orig_embed
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

        def fake_cp(emb, *, threshold, embeddings=None):
            return [("paos", "聚磷菌")]  # only the whitelisted pair

        def fake_detect(subset, llm, *, not_duplicates):
            detector_batches.append([s.slug for s in subset])
            return []

        orig_cp, orig_dd = ds.candidate_pairs, ds._dedup.detect_duplicate_groups
        orig_embed = ds._embed_pages_bounded
        ds.candidate_pairs = fake_cp
        ds._dedup.detect_duplicate_groups = fake_detect
        ds._embed_pages_bounded = lambda emb_pages: {p["id"]: [1.0] for p in emb_pages}
        try:
            ds._detect_groups(summaries, pages, lambda s, u: "",
                              [["paos", "聚磷菌"]], embedding_prefilter=True)
        finally:
            ds.candidate_pairs, ds._dedup.detect_duplicate_groups = orig_cp, orig_dd
            ds._embed_pages_bounded = orig_embed
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
            # Embedder stubbed to all-None → coverage error → small-wiki full
            # scan with the mock LLM (no real network in tests).
            with mock.patch.object(ds, "_embed_pages_bounded",
                                   lambda emb_pages: {p["id"]: None for p in emb_pages}):
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


class TestEmbeddingPrefilterDefault(unittest.TestCase):
    """The embedding prefilter is ON by default at the CLI (NashSU dedup-runner
    parity: it always prefilters). The lint command exposes no flag, so the
    default IS the only behavior most users get — it must bound the detector."""

    def _capture_prefilter(self, argv):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            with mock.patch.object(ds, "check_embedding_endpoint", return_value=None), \
                 mock.patch.object(ds, "run_phase2", return_value={"groups": [], "applied": []}) as m:
                ds.main(["--project", str(root), "--dry-run", *argv])
            return m.call_args.kwargs["embedding_prefilter"]

    def test_default_on(self):
        self.assertTrue(self._capture_prefilter([]))

    def test_opt_out_flag_disables(self):
        self.assertFalse(self._capture_prefilter(["--no-embedding-prefilter"]))


class _FakeResp:
    """Minimal urlopen response stub: JSON body with n embedding vectors."""

    def __init__(self, n):
        self._n = n

    def read(self):
        return json.dumps(
            {"data": [{"embedding": [1.0, 0.0]}] * self._n}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestBoundedEmbedding(unittest.TestCase):
    """Fix 4: hard per-request timeout, per-batch skip-with-warning, and the
    consecutive-failure circuit breaker — all with mocked urlopen (no real
    network in tests)."""

    def _pages(self, n):
        return [{"id": f"p{i}", "title": f"t{i}", "tags": [], "body": "b"}
                for i in range(n)]

    def test_timeout_forwarded_and_vectors_returned(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            body = json.loads(req.data.decode("utf-8"))
            return _FakeResp(len(body["input"]))

        with mock.patch.object(ds.urllib.request, "urlopen", fake_urlopen):
            out = ds._embed_pages_bounded(self._pages(3))
        self.assertEqual(seen["timeout"], ds.EMBED_TIMEOUT_S)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(v == [1.0, 0.0] for v in out.values()))

    def test_failed_batch_skipped_run_continues(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("read timed out")
            body = json.loads(req.data.decode("utf-8"))
            return _FakeResp(len(body["input"]))

        with mock.patch.object(ds.urllib.request, "urlopen", fake_urlopen):
            out = ds._embed_pages_bounded(self._pages(32))  # 2 batches of 16
        # Timed-out batch → None members, run continues to the next batch.
        self.assertTrue(all(out[f"p{i}"] is None for i in range(16)))
        self.assertTrue(all(out[f"p{i}"] == [1.0, 0.0] for i in range(16, 32)))

    def test_circuit_breaker_aborts_after_consecutive_failures(self):
        calls = {"n": 0}

        def always_fail(req, timeout=None):
            calls["n"] += 1
            raise ConnectionRefusedError("refused")

        with mock.patch.object(ds.urllib.request, "urlopen", always_fail):
            out = ds._embed_pages_bounded(self._pages(160))  # 10 batches
        # Stops issuing requests after N consecutive failures; every page is
        # still accounted for (None) so coverage handling can kick in.
        self.assertEqual(calls["n"], ds.EMBED_MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(len(out), 160)
        self.assertTrue(all(v is None for v in out.values()))


class TestEmbedderProbe(unittest.TestCase):
    """Fix 4: fail fast (<15s) at startup when Ollama is unreachable, with a
    message naming the fallback flag."""

    def test_unreachable_returns_error_string(self):
        def refuse(url, timeout=None):
            raise ds.urllib.error.URLError("connection refused")

        with mock.patch.object(ds.urllib.request, "urlopen", refuse):
            err = ds.check_embedding_endpoint(timeout=0.1)
        self.assertIsNotNone(err)
        self.assertIn("refused", err)

    def test_http_error_counts_as_reachable(self):
        def http404(url, timeout=None):
            raise ds.urllib.error.HTTPError(url, 404, "not found", None, None)

        with mock.patch.object(ds.urllib.request, "urlopen", http404):
            self.assertIsNone(ds.check_embedding_endpoint(timeout=0.1))

    def test_main_fails_fast_with_fallback_hint(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            buf = io.StringIO()
            with mock.patch.object(ds, "check_embedding_endpoint",
                                   return_value="connection refused"):
                with contextlib.redirect_stderr(buf):
                    rc = ds.main(["--project", str(root), "--dry-run"])
            self.assertEqual(rc, 2)
            self.assertIn("--token-only", buf.getvalue())

    def test_token_only_skips_probe(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)

            def probe_must_not_run(*a, **k):
                raise AssertionError("probe must not run with --token-only")

            with mock.patch.object(ds, "check_embedding_endpoint",
                                   probe_must_not_run):
                rc = ds.main(["--project", str(root), "--dry-run",
                              "--token-only", "--no-llm"])
            self.assertEqual(rc, 0)


class TestTokenOnlyCandidates(unittest.TestCase):
    """Fix 4: deterministic --token-only prefilter (Stage 2.3 matchers)."""

    def test_cjk_bigram_overlap_pairs(self):
        summaries = [_summ("匹配滤波器", title="匹配滤波器"),
                     _summ("匹配滤波器理论", title="匹配滤波器理论"),
                     _summ("多普勒效应", title="多普勒效应")]
        pairs = ds._token_candidate_pairs(summaries)
        self.assertEqual(pairs, [("匹配滤波器", "匹配滤波器理论")])

    def test_ascii_word_overlap_pairs(self):
        summaries = [_summ("matched-filter", title="Matched Filter"),
                     _summ("matched-filter-theory", title="Matched Filter Theory"),
                     _summ("kalman", title="Kalman Filter Basics")]
        pairs = ds._token_candidate_pairs(summaries)
        self.assertEqual(pairs, [("matched-filter", "matched-filter-theory")])

    def test_no_overlap_no_pairs(self):
        summaries = [_summ("radar", title="Radar"), _summ("sonar", title="Sonar")]
        self.assertEqual(ds._token_candidate_pairs(summaries), [])


class TestNoLLM(unittest.TestCase):
    """Fix 4: --no-llm reports prefilter clusters without any LLM call and
    never merges."""

    def test_returns_candidate_clusters_without_detector(self):
        summaries = [_summ("paos", "d"), _summ("聚磷菌", "d"), _summ("vfa", "d")]
        pages = [(f"wiki/entities/{s.slug}.md",
                  _page(f"type: entity\ntitle: {s.slug}", "b")) for s in summaries]

        def detector_must_not_run(*a, **k):
            raise AssertionError("LLM detector must not run with no_llm")

        with mock.patch.object(ds, "candidate_pairs",
                               lambda emb, *, threshold, embeddings=None: [("paos", "聚磷菌")]), \
             mock.patch.object(ds, "_embed_pages_bounded",
                               lambda emb_pages: {p["id"]: [1.0] for p in emb_pages}), \
             mock.patch.object(ds._dedup, "detect_duplicate_groups",
                               detector_must_not_run):
            groups = ds._detect_groups(summaries, pages, lambda s, u: "", [],
                                       embedding_prefilter=True, no_llm=True)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["slugs"]), ["paos", "聚磷菌"])
        self.assertEqual(groups[0]["confidence"], "candidate")

    def test_no_llm_forces_preview_no_merges(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            with mock.patch.object(ds, "candidate_pairs",
                                   lambda emb, *, threshold, embeddings=None: [("paos", "聚磷菌")]), \
                 mock.patch.object(ds, "_embed_pages_bounded",
                                   lambda emb_pages: {p["id"]: [1.0] for p in emb_pages}):
                report = ds.run_phase2(root, _mock_llm(), apply=True,
                                       today=FIXED_TODAY, no_llm=True)
            self.assertEqual(len(report["groups"]), 1)
            self.assertEqual(report["applied"], [])
            # No mutations despite apply=True.
            self.assertTrue((root / "wiki/entities/聚磷菌.md").exists())

    def test_main_rejects_no_llm_without_candidate_source(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            _make_wiki(root)
            rc = ds.main(["--project", str(root), "--dry-run", "--no-llm",
                          "--no-embedding-prefilter"])
            self.assertEqual(rc, 2)



class TestWhitelistCorruptRaises(unittest.TestCase):
    """2026-07-12: a corrupt --whitelist file raises RuntimeError instead of
    silently returning [] (auto-apply would merge protected pairs)."""

    def test_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as t:
            wl = Path(t) / "wl.json"
            wl.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ds.load_whitelist(wl)

    def test_non_list_top_level_raises(self):
        with tempfile.TemporaryDirectory() as t:
            wl = Path(t) / "wl.json"
            wl.write_text(json.dumps({"not_duplicates": "oops"}), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ds.load_whitelist(wl)

    def test_missing_file_still_skipped(self):
        self.assertEqual(ds.load_whitelist(Path("/nonexistent/wl.json")), [])


class TestStorageCorruptWhitelistWarnsNotRaises(unittest.TestCase):
    """The runtime whitelist READ path stays best-effort (warn + []) so a
    corrupt file can't block the detector — but it must be loud."""

    def test_corrupt_runtime_whitelist_returns_empty_with_warning(self):
        with tempfile.TemporaryDirectory() as t:
            rt = Path(t)
            (rt / dstore.WHITELIST_FILE).write_text("{corrupt", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                out = dstore.load_not_duplicates(rt)
            self.assertEqual(out, [])
            self.assertIn("WARNING", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
