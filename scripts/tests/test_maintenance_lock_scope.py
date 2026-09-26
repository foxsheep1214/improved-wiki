"""Which commands join the ingest project lock, and which only the lint lock.

Destructive maintenance (orphan deletion, raw renames, index rewrites) must not
run beside an ingest writer or a reserved write spine. Mutation-free lint must
keep working during a long batch ingest, but two lint runs never overlap.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def put(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("IMPROVED_WIKI_PROJECT_LOCK_FD", "IMPROVED_WIKI_LINT_LOCK_FD"):
        monkeypatch.delenv(name, raising=False)
    put(tmp_path / ".llm-wiki/ingest-events.jsonl")
    put(tmp_path / "wiki/concepts/a.md",
        "---\ntype: concept\ntitle: A\n---\n# A\n\nSee [[concepts/b]].\n")
    put(tmp_path / "wiki/concepts/b.md",
        "---\ntype: concept\ntitle: B\n---\n# B\n\nSee [[concepts/a]].\n")
    put(tmp_path / "wiki/concepts/orphan.md",
        "---\ntype: concept\ntitle: Orphan\n---\n# Orphan\n\nSee [[concepts/a]].\n")
    return tmp_path


@contextmanager
def held(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def reserve_spine(project: Path) -> None:
    put(project / ".llm-wiki/spine-reservation.json",
        json.dumps({"source_path": "raw/Book/x.pdf"}))


def run(project: Path, *argv: str, timeout: int = 60):
    env = dict(os.environ, IMPROVED_WIKI_ROOT=str(project),
               IMPROVED_WIKI_PYTHON=sys.executable,
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    return subprocess.run(list(argv), cwd=project, env=env, capture_output=True,
                          text=True, timeout=timeout)


def lint(project: Path, *flags: str):
    return run(project, "bash", str(SCRIPTS / "wiki-lint.sh"), *flags)


# ── P3: mutation-free lint ───────────────────────────────────────────────────

def test_structural_lint_runs_beside_a_writer_and_a_reserved_spine(project):
    reserve_spine(project)
    with held(project / ".llm-wiki/ingest.lock"):
        result = lint(project, "--structural-only")
    assert result.returncode == 0, result.stderr
    assert "findings may reflect a partial write" in result.stderr
    assert (project / ".llm-wiki/lint-cache.json").exists()


def test_help_needs_no_lock(project):
    reserve_spine(project)
    with held(project / ".llm-wiki/ingest.lock"):
        result = lint(project, "--help")
    assert result.returncode == 0
    assert "wiki-lint.sh" in result.stdout


def test_two_lint_runs_never_overlap(project):
    with held(project / ".llm-wiki/lint.lock"):
        result = lint(project, "--structural-only")
    assert result.returncode != 0
    assert "Another lint run is active" in result.stderr


@pytest.mark.parametrize("flags", [
    ("--no-semantic",),
    ("--diagnostic-only", "--fix"),  # a later flag re-enables a mutation
])
def test_mutating_lint_still_needs_the_project_lock(project, flags):
    with held(project / ".llm-wiki/ingest.lock"):
        result = lint(project, *flags)
    assert result.returncode != 0
    assert "Project writer is active" in result.stderr


# ── P2: standalone destructive writers ───────────────────────────────────────

def test_orphan_delete_apply_refuses_beside_a_writer(project):
    fix = [sys.executable, str(SCRIPTS / "wiki-lint-fix.py"),
           "--delete-orphans", "--project-root", str(project)]
    with held(project / ".llm-wiki/ingest.lock"):
        preview = run(project, *fix)
        applied = run(project, *fix, "--apply")
    assert preview.returncode == 0, preview.stderr
    assert applied.returncode == 1
    assert "Project writer is active" in applied.stderr
    assert (project / "wiki/concepts/orphan.md").exists()


def test_orphan_delete_apply_refuses_with_reserved_spine(project):
    reserve_spine(project)
    result = run(project, sys.executable, str(SCRIPTS / "wiki-lint-fix.py"),
                 "--delete-orphans", "--apply", "--project-root", str(project))
    assert result.returncode == 1
    assert "spine is reserved" in result.stderr
    assert (project / "wiki/concepts/orphan.md").exists()


def test_raw_rename_refuses_with_reserved_spine(project):
    put(project / "schema.md", "# Schema\n\n```yaml\nforbidden_chars:\n  - '#'\n```\n")
    reserve_spine(project)
    result = run(project, sys.executable, str(SCRIPTS / "normalize_raw_names.py"),
                 "--project", str(project), "--fix")
    assert result.returncode == 1
    assert "spine is reserved" in result.stdout


@pytest.mark.parametrize("command", [("sync",), ("delete", "--page", "concepts/a.md"),
                                     ("compact",)])
def test_index_maintenance_refuses_beside_a_writer(project, command):
    with held(project / ".llm-wiki/ingest.lock"):
        result = run(project, sys.executable, str(SCRIPTS / "build_embeddings.py"),
                     "--project", str(project), *command)
    assert result.returncode == 1
    assert "Project writer is active" in result.stderr


def test_research_page_is_kept_but_not_indexed_beside_a_writer(project):
    (project / ".llm-wiki/lancedb").mkdir(parents=True)
    synthesis = put(project / "synthesis.txt",
                    "Buck converters regulate voltage by switching [1]. " * 6)
    sources = put(project / "sources.json", json.dumps([{
        "title": "Buck basics", "url": "https://example.com/buck",
        "snippet": "switching regulator", "source": "web"}]))
    with held(project / ".llm-wiki/ingest.lock"):
        result = run(project, sys.executable, str(SCRIPTS / "write_research_page.py"),
                     "--project", str(project), "--topic", "Buck",
                     "--synthesis-file", str(synthesis), "--sources-file", str(sources))
    assert result.returncode == 0, result.stderr
    assert (project / result.stdout.strip()).is_file()
    assert "embedding: skipped (Project writer is active" in result.stderr
    assert "build_embeddings.py --project" in result.stderr
