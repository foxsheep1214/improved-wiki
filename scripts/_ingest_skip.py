"""_ingest_skip.py — Stage 0.2 dedup/skip + stage go/no-go (extracted from ingest.py)."""
from __future__ import annotations

from pathlib import Path

from _config import Config
from _core import is_query_bridge_source
from _progress import (
    file_sha256,
    is_stage_done,
    mark_stage_done,
    unmark_stage_done,
)
from _stage_3_write import _stage_3_2_wiki_path_for_source

def _should_stop_after(config: Config, stage: str) -> bool:
    """Check if we should stop after completing `stage`. Progress already saved before call."""
    if config.stop_after_stage == stage:
        print(f"\n[stop-after-stage] Stage {stage} complete — clean exit (--stop-after-stage={stage})")
        return True
    return False


def _stop_after_stage(config, stage: str) -> bool:
    """Pure check: True iff ``config.stop_after_stage == stage`` (exact match).

    Used inside ``_do_prepare`` to gate the supported boundaries (0=Phase 1,
    1.5=Stage 2.2 analysis, 2/2.0=generation) before the post-prepare check.
    ``stop_after_stage`` is set dynamically on Config by CLI parsing and may be
    absent on Config instances built elsewhere, so read it via ``getattr``.
    Does not print; the raise site owns the user-facing message.
    """
    return getattr(config, "stop_after_stage", None) == stage

def _stage_0_2_should_skip(raw_file: Path, config: Config) -> bool:
    """Return True only if the ingest has fully completed; resume otherwise.

    Stage 0.2 dedup/skip — two gates, in order:

    1. **Complete** — ``ingested`` marker set: the ingest finished (through
       embeddings + validation). Skip. (If the source page was deleted
       externally the marker is stale — clear it and re-ingest.)
    2. **Mid-flight** — source page exists but ``ingested`` not set: pages were
       written but post-write stages may still be pending. Do NOT skip — resume.
       The finer-grained ``write_phase`` / ``write_loop_done`` markers inside
       _do_write skip the non-idempotent 3.1 write loop, so resuming is cheap
       and never re-merges already-written pages.
    3. **Fresh** — no source page: ingest from scratch.

    The completion marker is the single skip signal. ingested (set only
    after a verified, embedded, validated ingest) is the authoritative
    completeness signal.
    """
    h = file_sha256(raw_file)
    # Explicitly ingested query pages (wiki/queries/*.md, or a legacy
    # raw/queries/ bridge copy) deliberately have no Stage 2.4 source page.
    # The `ingested` marker alone is authoritative for this compatibility path;
    # current Deep Research does not invoke it automatically.
    if is_query_bridge_source(raw_file, config):
        if is_stage_done(config, h, "ingested"):
            print("  [skip] Ingest complete (ingested marker present)")
            return True
        return False

    if is_stage_done(config, h, "ingested"):
        if not _stage_3_2_wiki_path_for_source(raw_file, config).exists():
            # Stale marker (source page deleted externally) — clear and re-ingest.
            from _progress import stages_path as _sp
            _sp(config, h).unlink(missing_ok=True)
            return False
        from _media_integrity import (
            assert_cached_media_complete,
            audit_cached_media,
            repair_completed_media,
        )
        valid, reason, _details = audit_cached_media(raw_file, config)
        if not valid:
            print(
                "  [skip] ⚠️ ingested marker has incomplete media "
                f"({reason}) — repairing media before this source may be "
                "considered complete")
            # Clear the authoritative marker before any I/O/API work. If the
            # repair pauses, the next run cannot silently skip the source.
            unmark_stage_done(config, h, "ingested")
            repair_completed_media(raw_file, config)
            assert_cached_media_complete(raw_file, config)
            mark_stage_done(config, h, "ingested")
            print("  [skip] Media repaired and re-verified")
        print("  [skip] Ingest complete (ingested marker present)")
        return True

    source_page = _stage_3_2_wiki_path_for_source(raw_file, config)
    if not source_page.exists():
        return False

    # Source page exists but ingested not done → mid-flight resume.  Do NOT
    # skip: post-write stages may still be pending.  The write_phase marker
    # inside _do_write handles skipping the non-idempotent 3.1 loop.
    print("  [skip:resume] Source page exists, ingested not done — resuming")
    return False
