# Architecture Decisions

Stable decisions and their rationale live here; operational runbooks link to
this file instead of repeating incident histories.

## ADR-001 — One fresh worker per conversation handoff

**Decision:** Each prompt is answered by one
fresh worker/subagent that handles exactly one handoff. The main conversation
only orchestrates.

**Reason:** Large chunk prompts accumulated in a reused context and diluted
attention. Skolnik (2026-07-07) degraded after a 14-chunk chain; the EW/Radar
Handbook (2026-07-08) showed that even main-conversation one-by-one answering
still accumulated enough context to degrade. Hansen (2026-07-09) exposed a
second risk: an answering worker reported completion after internally
fan-outing work but never published one complete result.

**Consequences:** More handoff overhead, but stable per-call attention.
Responses publish through `.txt.tmp` plus validation and atomic rename.

## ADR-002 — Serial Stage 2.2, single-pass Stage 2.4

**Decision:** Stage 2.2 remains serial because each chunk consumes the prior
validated rolling digest. After all analyses finish, Stage 2.4 runs one
whole-source generation call with the final digest, every chunk analysis, and
bounded raw evidence from every chunk.

**Reason:** NashSU 0.6.6 consolidates all long-source analyses before a single
generation call. Per-chunk generation hid earlier/later evidence from each
prompt and weakened cross-chunk synthesis, thesis, comparison, and terminology
coherence.

**Consequences:** `--parallel` controls Phase-1 cross-book prefetch only.
Stage 2.2 must not be parallelized and Stage 2.4 must not be split into chunk
waves. Stage 2.3 association and post-generation in-source semantic dedup
remain explicit improved-wiki extensions around the single final call.

## ADR-003 — Two-stage cross-book pipeline with one write spine

**Decision:** Phase 1 may overlap across books using two coordinated resource
roles (minerU and captioning), while Stage 2.3+ is a single ordered spine.

**Reason:** OCR/caption artifacts are source-local. Wiki association, merges,
aggregates, and finalization mutate shared state and require deterministic
ordering.

**Consequences:** Kernel worker leases supervise detached processes; a
coordinator lock prevents competing batch/watch schedulers; a short project
flock protects active mutation; a durable spine reservation survives
exit-101 handoffs.

## ADR-004 — No silent quality fallback

**Decision:** Missing caption, embedding, LLM, merge, schema, or required-media
dependencies pause the source after retries.

**Reason:** Quietly substituting OCR figure text, keyword-only retrieval, or
partial merge output produces an apparently complete but lower-quality wiki.

**Consequences:** Repair the dependency and resume from checkpoints. Corrupt
cache/checkpoint files are the exception: they warn and re-derive because that
is correct recovery rather than a quality downgrade.

## ADR-005 — Focused modules with compatibility facades

**Decision:** Configuration, progress/locking, schema/path safety, parsing,
retry, single-source running, batch supervision, status, CLI, and queue logic
have separate owners. `_core.py` and `ingest.py` retain established imports as
compatibility facades.

**Reason:** The former monoliths mixed unrelated mutation boundaries and made
parallelism, recovery, and tests hard to reason about.

**Consequences:** New code imports focused modules. Compatibility names remain,
but private test patches must target the owning module; no global synchronization wrapper remains.

## ADR-006 — NashSU v0.6.11 sync: what was adopted and what was not

**Decision:** The NashSU baseline moves from v0.6.8 to **v0.6.11**. Of the
three intervening releases, exactly four things landed here; the rest were
verified as already-covered, already-stronger, or application-only.

**Adopted**

- **Structured data verbatim (0.6.9)** — NashSU added one rule to three ingest
  prompts (`ingest.ts:2196/2321/2833`). Because Stage 2.2 here emits YAML
  rather than free-form markdown, the rule needed a slot: `structured_data`
  is that slot, modelled on `formulas` (dedicated verbatim list, re-injected
  into Stage 2.4 by its own collector with a REUSE-EXACTLY directive, dropped
  at the same context-degradation tier). A bare prose rule would have had
  nowhere to write, or would have landed in `source_quotes` — the first field
  the budget ladder discards.
- **Caption output language (0.6.10)** — captions now follow the wiki's output
  language instead of the source's. The old behaviour was labelled "NashSU
  parity — language-NEUTRAL"; that parity expired at 0.6.10. NashSU folds the
  language into its caption cache key; the cache here is a `.caption.txt`
  sidecar, so the language dimension lives in a per-media-dir
  `.caption-language` marker. An **unmarked** directory is never invalidated —
  the installed corpora hold thousands of pre-marker captions.
- **Batch Deep Research over reviews (0.6.10)** — `batch_research_reviews.py`,
  selection only. See `process-reviews.md` Step 3c.
- **MinerU 3.0–3.2 backend aliases (0.6.10)** — recorded in
  `mineru-version-tracking.md`, deliberately not auto-mapped (explicit service configuration; runtime checks are documented there).

**Rejected as already covered or stronger here**

- Language-detection fixes (0.6.10): `_language.py` already carries every
  NashSU fix plus a non-Latin share floor, a Greek word-run test, a kana-share
  ratio, and ≥2-function-word requirements. NashSU still decides a language on
  two characters.
- Wikilink consistency (0.6.10): `_enrich_wikilinks.py` already validates
  targets against on-disk slugs, and adds heading/fence skips and alias
  preservation.
- Single-page vector index (0.6.9): `build_embeddings.py upsert --page`.
- Large duplicate scans (0.6.10): `_dedup_embedding.py` is the same
  prefilter + top-k + union-find shape.
- Windows CRLF integrity (0.6.10): `_ingest_sanitize.py` rebuilds from capture
  groups and never had the hard-coded-offset bug.
- CJK filenames (0.6.9): filenames are generated deterministically in code and
  never pass through the model.
- Natural numeric ordering (0.6.11): every numeric name generated here is
  `:04d` zero-padded, so lexicographic order already equals numeric order at
  every enumeration site. The batch CLI's argv order is the caller's explicit
  choice and is load-bearing for `_assert_batch_resume_order` — not sorted.

**Not applicable**

- Configurable ingest reasoning effort (0.6.11): NashSU hard-coded
  `reasoning: off` on ingest calls and some providers answered 400. Structured
  generation here runs through fresh conversation-mode workers and sends no reasoning
  field at all.
- Everything application-layer: MCP tools and HTTP APIs, the answer-context
  panel, streaming chat, file-history retention, scheduled-import filters,
  source-filter UI, i18n, provider routing, desktop shell fixes.
