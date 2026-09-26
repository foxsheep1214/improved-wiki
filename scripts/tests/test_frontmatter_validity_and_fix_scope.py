"""Unusable frontmatter is repaired, refused or reported; fixes stay in scope.

The shared reader parses frontmatter as YAML and returns ``{}`` for a block it
cannot read, which hides type/sources/related from every reader. Writers must
not produce such a page and lint must surface existing ones. Separately, the
review-fix guard must notice edits to pages the Review never declared.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import review_fix_guard as guard
from _frontmatter import frontmatter_error, parse_frontmatter
from _ingest_sanitize import sanitize_ingested_file_content
from _stage_3_write import stage_3_2_write_wiki_file
from write_research_page import build_research_page

SCRIPTS = Path(__file__).resolve().parents[1]
COLON_TITLE = '---\ntype: concept\ntitle: Buck: 死区控制\nsources: ["raw/a.pdf"]\n---\n# Buck\n'
BROKEN_QUOTE = '---\ntype: concept\ntitle: "A" and B\n---\n# A\n'


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── P4: detection and repair ─────────────────────────────────────────────────

@pytest.mark.parametrize("content, expected", [
    ("---\ntype: concept\ntitle: Fine\n---\nbody", None),
    ("# no frontmatter\n", None),
    ("---\n---\nbody", "empty"),  # the shared reader does not see it either
    ("---\ntype: concept\ntitle: open\nbody without a closing fence\n", "no closing"),
    (COLON_TITLE, "not valid YAML at line 3"),
    ("---\n- a\n- b\n---\nbody", "mapping"),
])
def test_frontmatter_error(content, expected):
    error = frontmatter_error(content)
    assert (error is None) if expected is None else (expected in error)


def test_sanitizer_quotes_only_what_breaks_yaml():
    repaired = sanitize_ingested_file_content(COLON_TITLE)
    assert frontmatter_error(repaired) is None
    assert parse_frontmatter(repaired)[0] == {
        "type": "concept", "title": "Buck: 死区控制", "sources": ["raw/a.pdf"]}
    valid = '---\ntype: concept\ntitle: Fine # note\ntags: [a]\n---\nbody\n'
    assert sanitize_ingested_file_content(valid) == valid
    assert sanitize_ingested_file_content(BROKEN_QUOTE) == BROKEN_QUOTE


def test_stage_3_2_writes_repaired_and_refuses_unrepairable(tmp_path):
    good = tmp_path / "wiki/concepts/buck.md"
    stage_3_2_write_wiki_file(good, COLON_TITLE)
    assert parse_frontmatter(good.read_text(encoding="utf-8"))[0]["title"] == "Buck: 死区控制"
    bad = tmp_path / "wiki/concepts/a.md"
    with pytest.raises(RuntimeError, match="refuses to write a.md"):
        stage_3_2_write_wiki_file(bad, BROKEN_QUOTE)
    assert not bad.exists()


def test_structural_lint_reports_invalid_frontmatter(tmp_path, monkeypatch):
    for name in ("IMPROVED_WIKI_PROJECT_LOCK_FD", "IMPROVED_WIKI_LINT_LOCK_FD"):
        monkeypatch.delenv(name, raising=False)
    put(tmp_path / ".llm-wiki/ingest-events.jsonl", "")
    put(tmp_path / "wiki/concepts/a.md", BROKEN_QUOTE + "\nSee [[concepts/b]].\n")
    put(tmp_path / "wiki/concepts/b.md",
        "---\ntype: concept\ntitle: B\n---\n# B\n\nSee [[concepts/a]].\n")
    env = dict(os.environ, IMPROVED_WIKI_ROOT=str(tmp_path),
               IMPROVED_WIKI_PYTHON=sys.executable,
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", str(SCRIPTS / "wiki-lint.sh"),
                             "--structural-only", "--strict"],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "invalid-frontmatter: 1" in result.stdout
    findings = json.loads((tmp_path / ".llm-wiki/lint-cache.json").read_text())
    assert [f["page"] for f in findings if f["type"] == "invalid-frontmatter"] == [
        "concepts/a.md"]


def test_research_title_with_backslash_stays_parseable():
    page = build_research_page(r'Regex \d in C:\data "q"', "body", [], "2026-09-26")
    assert frontmatter_error(page) is None
    assert parse_frontmatter(page)[0]["title"] == r'Research: Regex \d in C:\data "q"'


# ── P5: whole-wiki scope at finalize ─────────────────────────────────────────

REVIEW = """---
type: review
review_type: confirm
affected_pages: [concepts/a.md]
resolved: false
---

# [confirm] check A
"""


@pytest.fixture
def repair(tmp_path):
    wiki = tmp_path / "wiki"
    put(wiki / "concepts/a.md", "---\ntype: concept\ntitle: A\n---\nOld value 5 V.\n")
    put(wiki / "concepts/b.md", "---\ntype: concept\ntitle: B\n---\nUntouched.\n")
    put(wiki / "log.md", "# Log\n")
    review = put(wiki / "REVIEW/confirm/check-a.md", REVIEW)
    put(wiki / "REVIEW/confirm/other.md", REVIEW)
    state = tmp_path / "state.json"
    guard.write_fix_snapshot(state, guard.create_fix_snapshot(review, ["concepts/a.md"]))
    time.sleep(0.01)  # distinct mtimes on coarse filesystems
    return wiki, review, state


def finalize(review, state):
    return guard.finalize_fix(review, state, "recomputed; structural lint clean")


def test_declared_edit_with_aggregate_and_review_churn_finalizes(repair):
    wiki, review, state = repair
    put(wiki / "concepts/a.md", "---\ntype: concept\ntitle: A\n---\nCorrect value 3.3 V.\n")
    put(wiki / "log.md", "# Log\n- repaired A\n")
    put(wiki / "REVIEW/confirm/other.md", REVIEW + "\nnote\n")
    assert finalize(review, state).startswith("Fixed: wiki/concepts/a.md")


@pytest.mark.parametrize("stray", ["edit", "add", "delete"])
def test_undeclared_change_keeps_review_pending(repair, stray):
    wiki, review, state = repair
    put(wiki / "concepts/a.md", "---\ntype: concept\ntitle: A\n---\nCorrect value 3.3 V.\n")
    if stray == "edit":
        put(wiki / "concepts/b.md", "---\ntype: concept\ntitle: B\nrelated: []\n---\nUntouched.\n")
    elif stray == "add":
        put(wiki / "concepts/c.md", "---\ntype: concept\ntitle: C\n---\nNew.\n")
    else:
        (wiki / "concepts/b.md").unlink()
    with pytest.raises(ValueError, match="undeclared pages"):
        finalize(review, state)
    assert "resolved: false" in review.read_text(encoding="utf-8")


def test_repair_that_breaks_frontmatter_is_refused(repair):
    wiki, review, state = repair
    put(wiki / "concepts/a.md", BROKEN_QUOTE)
    with pytest.raises(ValueError, match="unusable frontmatter"):
        finalize(review, state)
    assert "resolved: false" in review.read_text(encoding="utf-8")


def test_old_snapshot_must_be_retaken(repair):
    _wiki, review, state = repair
    data = json.loads(state.read_text())
    data["version"] = 1
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="re-run --snapshot"):
        finalize(review, state)
