"""_ingest_skip.py — Stage 0.2 dedup/skip + stage go/no-go (extracted from ingest.py)."""
from __future__ import annotations

from pathlib import Path

from _core import Config, file_sha256, is_stage_done
from _stage_3_write import _stage_3_1_wiki_path_for_source

def _should_stop_after(config: Config, stage: str, result: dict) -> bool:
    """Check if we should stop after completing `stage`. Progress already saved before call."""
    if config.stop_after_stage == stage:
        print(f"\n[stop-after-stage] Stage {stage} complete — clean exit (--stop-after-stage={stage})")
        return True
    return False


def _stop_after_stage(config, stage: str) -> bool:
    """Pure check: True iff ``config.stop_after_stage == stage`` (exact match).

    Used inside ``_do_prepare`` to gate Stage-0..2 boundaries so
    ``--stop-after-stage`` actually halts at the requested point (0=extract,
    1=global digest, 2=generation) instead of running all of Stage 0-2 before
    the post-prepare check. ``stop_after_stage`` is set dynamically on Config
    (ingest.py arg parsing) and may be absent on Config instances built
    elsewhere, so read it via getattr. Does NOT print — the raise site prints.
    """
    return getattr(config, "stop_after_stage", None) == stage

def _stage_0_2_should_skip(raw_file: Path, config: Config) -> bool:
    """Return True only if the ingest has fully completed; resume otherwise.

    Stage 0.2 dedup/skip — two gates, in order:

    1. **Complete** — ``stage_4_1`` marker set: the ingest finished (through
       embeddings + validation). Skip. (If the source page was deleted
       externally the marker is stale — clear it and re-ingest.)
    2. **Mid-flight** — source page exists but ``stage_4_1`` not set: pages were
       written but post-write stages may still be pending. Do NOT skip — resume.
       The finer-grained ``write_phase`` / ``write_loop_done`` markers inside
       _do_write skip the non-idempotent 3.1 write loop, so resuming is cheap
       and never re-merges already-written pages.
    3. **Fresh** — no source page: ingest from scratch.

    The completion marker is the single skip signal. stage_4_1 (set only
    after a verified, embedded, validated ingest) is the authoritative
    completeness signal.
    """
    h = file_sha256(raw_file)
    if is_stage_done(config, h, "stage_4_1"):
        if not _stage_3_1_wiki_path_for_source(raw_file, config).exists():
            # Stale marker (source page deleted externally) — clear and re-ingest.
            from _core import stages_path as _sp
            _sp(config, h).unlink(missing_ok=True)
            return False
        print(f"  [skip] Ingest complete (stage_4_1 marker present)")
        return True

    source_page = _stage_3_1_wiki_path_for_source(raw_file, config)
    if not source_page.exists():
        return False

    # Source page exists but stage_4_1 not done → mid-flight resume.  Do NOT
    # skip: post-write stages may still be pending.  The write_phase marker
    # inside _do_write handles skipping the non-idempotent 3.1 loop.
    print(f"  [skip:resume] Source page exists, stage_4_1 not done — resuming")
    return False
