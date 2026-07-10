"""End-to-end conversation-handoff test for wiki-lint-semantic (round ii).

The module filename has a hyphen so it is loaded via importlib. Verifies:
  * main() exits 101 on first run (prompt written, ConversationPending).
  * After the calling agent writes the result file, main() resumes to 0 and
    writes lint-semantic.json with the parsed findings.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wiki_lint_semantic", _SCRIPTS_DIR / "wiki-lint-semantic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _page(fm: str, body: str) -> str:
    return f"---\n{fm}\n---\n\n{body}"


class TestSemanticLintConversation(unittest.TestCase):
    def test_main_pending_then_resume(self):
        wls = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "concepts" / "buck.md").write_text(_page(
                "type: concept\ntitle: Buck\n",
                "# Buck\nA buck converter steps down voltage.",
            ), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["wiki-lint-semantic.py"]
            try:
                # First run: uncached → ConversationPending → 101.
                self.assertEqual(wls.main(), 101)
                conv_dir = root / ".llm-wiki" / "conversation" / "semantic-lint"
                md_files = list(conv_dir.glob("*.md"))
                self.assertEqual(len(md_files), 1)

                # Simulate the agent answering with one LINT block.
                md = md_files[0]
                md.with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Add a datasheet---\n"
                    "PAGES: concepts/buck.md\n"
                    "Consider linking a switching-regulator datasheet.\n"
                    "---END LINT---\n",
                    encoding="utf-8",
                )

                # Second run: cached result read → findings written → 0.
                self.assertEqual(wls.main(), 0)
                findings = json.loads(
                    (root / ".llm-wiki" / "lint-semantic.json").read_text("utf-8")
                )
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["severity"], "info")
                self.assertEqual(findings[0]["page"], "Add a datasheet")
                self.assertEqual(findings[0]["affectedPages"], ["concepts/buck.md"])
            finally:
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root


class TestBatching(unittest.TestCase):
    def test_chunk_batches_small_returns_single(self):
        wls = _load_module()
        # "p%d.md" (5 chars) + "text" (4 chars) = 9 chars/item x 5 = 45 chars,
        # comfortably under a 200-char budget.
        summaries = [("p%d.md" % i, "text") for i in range(5)]
        batches = wls.chunk_batches(summaries, target_chars=200)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 5)

    def test_chunk_batches_splits_at_boundary(self):
        wls = _load_module()
        # Each item is 9 chars; a 27-char budget fits exactly 3 items/batch.
        summaries = [("p%d.md" % i, "text") for i in range(7)]
        batches = wls.chunk_batches(summaries, target_chars=27)
        self.assertEqual(len(batches), 3)
        self.assertEqual([len(b) for b in batches], [3, 3, 1])

    def test_chunk_batches_oversized_single_item_gets_own_batch(self):
        wls = _load_module()
        summaries = [("a.md", "x" * 500), ("b.md", "short")]
        batches = wls.chunk_batches(summaries, target_chars=100)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 1)

    def test_chunk_batches_empty_input_returns_empty(self):
        wls = _load_module()
        self.assertEqual(wls.chunk_batches([], target_chars=1000), [])

    def test_dedup_findings_collapses_cross_batch_dupes(self):
        wls = _load_module()
        findings = [
            {"page": "Add a datasheet", "detail": "[suggestion] link a datasheet"},
            {"page": "add a datasheet", "detail": "[suggestion] link a datasheet"},
            {"page": "Add a datasheet", "detail": "[stale] outdated ref"},
            {"page": "Other", "detail": "[suggestion] something else"},
        ]
        out = wls.dedup_findings(findings)
        self.assertEqual(len(out), 3)


class TestLanguageDirectiveInPrompt(unittest.TestCase):
    """The enriched language directive (proper-noun / identifier preservation
    clauses + override) must reach the semantic-lint system prompt."""

    def test_preservation_clauses_present_in_system_prompt(self):
        wls = _load_module()
        summaries = [("concepts/buck.md", "A buck converter steps down voltage.")]
        system_prompt, _ = wls.build_prompt(summaries)
        self.assertIn("MANDATORY OUTPUT LANGUAGE", system_prompt)
        self.assertIn("Do not translate, transliterate", system_prompt)
        self.assertIn("paper titles", system_prompt)
        self.assertIn("URLs", system_prompt)

    def test_output_language_override_reaches_prompt(self):
        wls = _load_module()
        old = os.environ.get("IMPROVED_WIKI_OUTPUT_LANGUAGE")
        os.environ["IMPROVED_WIKI_OUTPUT_LANGUAGE"] = "French"
        try:
            summaries = [("concepts/buck.md", "A buck converter steps down voltage.")]
            system_prompt, _ = wls.build_prompt(summaries)
            self.assertIn("French", system_prompt)
        finally:
            if old is None:
                os.environ.pop("IMPROVED_WIKI_OUTPUT_LANGUAGE", None)
            else:
                os.environ["IMPROVED_WIKI_OUTPUT_LANGUAGE"] = old


class TestSemanticLintBatchedE2E(unittest.TestCase):
    def test_all_pending_batches_emitted_together(self):
        """3 pages (boost/buck/flyback, sorted), a target_chars budget that
        exactly fits the first two previews (boost+buck) → 2 batches, BOTH
        initially uncached. Eager-drain (2026-07-10, mirrors ingest.py Stage
        2.4's _generate_all_chunks_parallel): round 1 must emit prompt files
        for ALL uncached batches in a single invocation (not just the first),
        so the calling agent can dispatch subagents for them in parallel,
        then return 101 once. Round 2 (after both answered): both cached →
        0, findings merged + deduped + renumbered. Monkeypatches
        resolve_batch_target_chars (2026-07-10: replaces the old
        SEMANTIC_BATCH_PAGES page-count knob) with a fixed small budget so
        the split is deterministic regardless of the real probed/default
        context size."""
        wls = _load_module()
        original_resolver = wls.resolve_batch_target_chars
        wls.resolve_batch_target_chars = lambda state_dir: 156
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki" / "concepts"
            wiki.mkdir(parents=True)
            for name in ("buck", "boost", "flyback"):
                (wiki / f"{name}.md").write_text(_page(
                    f"type: concept\ntitle: {name}\n",
                    f"# {name}\nA {name} converter.",
                ), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["wiki-lint-semantic.py"]
            conv_dir = root / ".llm-wiki" / "conversation" / "semantic-lint"
            try:
                # Round 1: BOTH batches uncached → both prompts emitted in
                # this single invocation (the eager-drain guarantee).
                self.assertEqual(wls.main(), 101)
                md_pending = [p for p in conv_dir.glob("*.md")
                              if not p.with_suffix(".txt").exists()]
                self.assertEqual(len(md_pending), 2)
                md_pending.sort(key=lambda p: p.name)
                md_pending[0].with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Buck note---\n"
                    "PAGES: concepts/buck.md\n Buck detail.\n---END LINT---\n",
                    encoding="utf-8")
                md_pending[1].with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Flyback note---\n"
                    "PAGES: concepts/flyback.md\n Flyback detail.\n---END LINT---\n",
                    encoding="utf-8")

                self.assertEqual(wls.main(), 0)
                findings = json.loads(
                    (root / ".llm-wiki" / "lint-semantic.json").read_text("utf-8"))
                self.assertEqual(len(findings), 2)
                self.assertEqual({f["page"] for f in findings},
                                 {"Buck note", "Flyback note"})
                ids = [f["id"] for f in findings]
                self.assertEqual(len(set(ids)), len(ids))
            finally:
                wls.resolve_batch_target_chars = original_resolver
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root

    def test_answering_one_of_two_still_reports_the_other_pending(self):
        """Same 2-batch setup, but only ONE of the two round-1 prompts gets
        answered before the next invocation. The drain loop must read the
        answered batch from cache instantly and still report the other as
        pending (not silently skip it, not re-emit the already-answered one
        as pending too)."""
        wls = _load_module()
        original_resolver = wls.resolve_batch_target_chars
        wls.resolve_batch_target_chars = lambda state_dir: 156
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki" / "concepts"
            wiki.mkdir(parents=True)
            for name in ("buck", "boost", "flyback"):
                (wiki / f"{name}.md").write_text(_page(
                    f"type: concept\ntitle: {name}\n",
                    f"# {name}\nA {name} converter.",
                ), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["wiki-lint-semantic.py"]
            conv_dir = root / ".llm-wiki" / "conversation" / "semantic-lint"
            try:
                self.assertEqual(wls.main(), 101)
                md_pending = sorted(
                    (p for p in conv_dir.glob("*.md")
                     if not p.with_suffix(".txt").exists()),
                    key=lambda p: p.name)
                self.assertEqual(len(md_pending), 2)
                md_pending[0].with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Only one answered---\n"
                    "PAGES: concepts/buck.md\n detail.\n---END LINT---\n",
                    encoding="utf-8")

                # Second invocation: the answered one is a cache hit; the
                # other must still be reported pending, not lost.
                self.assertEqual(wls.main(), 101)
                still_pending = [p for p in conv_dir.glob("*.md")
                                 if not p.with_suffix(".txt").exists()]
                self.assertEqual(len(still_pending), 1)
                self.assertEqual(still_pending[0].name, md_pending[1].name)
                self.assertFalse(
                    (root / ".llm-wiki" / "lint-semantic.json").exists())
            finally:
                wls.resolve_batch_target_chars = original_resolver
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root


class TestParseLintBlocksHyphenatedTitle(unittest.TestCase):
    """Regression (2026-07-10): a title containing a hyphen (e.g. a model
    number like "MIL-STD-1553") must still parse. NashSU's own regex uses
    [^\\n-]+? for the title group, which stops at the first hyphen and then
    fails to find the closing ---, silently dropping the whole block. Our
    copy deviates on purpose: [^\\n]+? for the title group only."""

    def test_hyphenated_title_parses(self):
        wls = _load_module()
        raw = (
            "---LINT: missing-page | warning | MIL-STD-1553 总线体系结构缺少统领概念页---\n"
            "PAGES: concepts/1553-command-word-structure.md\n"
            "七个子页面都引用了这个概念，但它自己没有页面。\n"
            "---END LINT---\n"
        )
        results = wls.parse_lint_blocks(raw, now_ms=0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["page"], "MIL-STD-1553 总线体系结构缺少统领概念页")

    def test_mixed_hyphenated_and_plain_titles_all_parse(self):
        wls = _load_module()
        raw = (
            "---LINT: suggestion | warning | Plain title---\n"
            "PAGES: a.md\nbody one\n---END LINT---\n"
            "---LINT: missing-page | warning | SA-2 has no canonical page---\n"
            "PAGES: b.md\nbody two\n---END LINT---\n"
            "---LINT: stale | info | F-16 radar model outdated---\n"
            "PAGES: c.md\nbody three\n---END LINT---\n"
        )
        results = wls.parse_lint_blocks(raw, now_ms=0)
        self.assertEqual(len(results), 3)
        self.assertEqual(
            [r["page"] for r in results],
            ["Plain title", "SA-2 has no canonical page", "F-16 radar model outdated"],
        )


class TestResolveBatchTargetChars(unittest.TestCase):
    """2026-07-10: batch sizing is now context-derived instead of a fixed
    SEMANTIC_BATCH_PAGES=200. Covers cache-hit, cache-miss/default, and the
    lint-specific env override."""

    def _write_cache(self, runtime_dir: Path, model_env: str, context: int, age_s: int = 0):
        import time
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "probed-context.json").write_text(json.dumps({
            "model_env": model_env, "context": context,
            "probed_at": int(time.time()) - age_s,
        }), encoding="utf-8")

    def test_uses_cached_probe_when_fresh_and_model_matches(self):
        wls = _load_module()
        with tempfile.TemporaryDirectory() as t:
            runtime = Path(t) / ".llm-wiki"
            self._write_cache(runtime, model_env="", context=1_000_000)
            old = os.environ.get("ANTHROPIC_MODEL")
            os.environ.pop("ANTHROPIC_MODEL", None)
            try:
                target_chars = wls.resolve_batch_target_chars(runtime)
                # 1M context x 0.33 = 330K tokens, capped at the 256K lint
                # ceiling -> target_chars = min(768_000, 256_000*4) = 768_000.
                self.assertEqual(target_chars, 768_000)
            finally:
                if old is not None:
                    os.environ["ANTHROPIC_MODEL"] = old

    def test_falls_back_to_default_context_when_no_cache(self):
        wls = _load_module()
        with tempfile.TemporaryDirectory() as t:
            runtime = Path(t) / ".llm-wiki"
            runtime.mkdir(parents=True)
            target_chars = wls.resolve_batch_target_chars(runtime)
            from _core import _CONTEXT_SIZE_DEFAULT, _compute_chunk_targets
            _, expected = _compute_chunk_targets(
                0, _CONTEXT_SIZE_DEFAULT, hard_ceil=wls._LINT_TARGET_TOKENS_HARD_CEIL)
            self.assertEqual(target_chars, expected)

    def test_env_override_changes_ceiling(self):
        wls = _load_module()
        with tempfile.TemporaryDirectory() as t:
            runtime = Path(t) / ".llm-wiki"
            self._write_cache(runtime, model_env="", context=1_000_000)
            old_model = os.environ.get("ANTHROPIC_MODEL")
            old_ceil = os.environ.get("IMPROVED_WIKI_LINT_TARGET_TOKENS_CEIL")
            os.environ.pop("ANTHROPIC_MODEL", None)
            os.environ["IMPROVED_WIKI_LINT_TARGET_TOKENS_CEIL"] = "40000"
            try:
                target_chars = wls.resolve_batch_target_chars(runtime)
                self.assertEqual(target_chars, 160_000)  # 40_000 * 4 chars/token
            finally:
                if old_model is not None:
                    os.environ["ANTHROPIC_MODEL"] = old_model
                if old_ceil is None:
                    os.environ.pop("IMPROVED_WIKI_LINT_TARGET_TOKENS_CEIL", None)
                else:
                    os.environ["IMPROVED_WIKI_LINT_TARGET_TOKENS_CEIL"] = old_ceil


class TestCollectSummariesDirExclusion(unittest.TestCase):
    """Regression: derived-artifact dirs (REVIEW/, clusters/, media/, lint/)
    must NOT be fed to the semantic-lint LLM — they are diagnostics this port
    writes under wiki/, not source knowledge. Mirrors the structural lint."""

    def test_skips_derived_artifact_dirs(self):
        wls = _load_module()
        with tempfile.TemporaryDirectory() as td:
            wiki = Path(td) / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "REVIEW").mkdir()
            (wiki / "clusters").mkdir()
            (wiki / "lint").mkdir()
            (wiki / "concepts" / "a.md").write_text(
                _page("type: concept\ntitle: A", "# A\nbody"), encoding="utf-8")
            (wiki / "REVIEW" / "r1.md").write_text(
                _page("type: review", "# review item"), encoding="utf-8")
            (wiki / "clusters" / "c1.md").write_text(
                _page("type: cluster", "# cluster hub"), encoding="utf-8")
            (wiki / "lint" / "l1.md").write_text(
                _page("type: lint", "# lint finding"), encoding="utf-8")

            summaries = wls.collect_summaries(wiki)
            paths = {p for p, _ in summaries}
            self.assertEqual(paths, {"concepts/a.md"})


if __name__ == "__main__":
    unittest.main()
