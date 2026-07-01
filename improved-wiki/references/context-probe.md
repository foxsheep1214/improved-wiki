# Context-Window Probe

At ingest start the pipeline probes the **live conversation model's** context window and uses it to size every chunk/budget. This replaces the former `LLM_CONTEXT_SIZE` env convention (removed 2026-06-27).

## Why

improved-wiki runs in conversation mode: the calling agent answers each prompt with whatever model the current session uses (GLM-5.2 today, DeepSeek V4 Pro next session, …). Chunk size is `context × 0.33` (capped at 64K tokens), so it must track the *actual* model context — a static env value is either wrong (over-estimates → overflow; under-estimates → wasted capacity and 2× the round-trips) or stale the moment the agent switches models.

The probe makes the pipeline **model-portable**: no registry to maintain, no env to set, no re-deriving when the model changes.

## How it works

1. `ingest.py` main(), before any file processing, calls `_probe_and_apply_context(config)` (delete-only paths skip it).
2. `resolve_context(config)` checks `.llm-wiki/probed-context.json`:
   - **Cache hit** (same `llm_model`, age < 7 days) → reuse, zero round-trips.
   - **Cache miss** → `probe_context(config)`: one `call_anthropic_protocol` round-trip under a dedicated `ctxprobe` conversation prefix, asking the model for its max context in tokens.
3. The response is parsed (first 4+ digit integer; tolerates prose/commas/markdown) and sanity-gated to `[8_000, 10_000_000]`.
3b. **Known-model validation.** The probe prompt asks the model for "the largest value you are confident of", so a cautious *known* model can lowball its real context (→ chunks too small) and a confused one can overshoot (→ chunks too large to drive). When the model's self-report matches `_KNOWN_MODEL_CONTEXT` (authoritative specs for Claude Opus 4.6/4.7/4.8, Sonnet 4.6, Haiku 4.5, Fable 5, GLM-5.2), the probed value is **pinned to the spec** instead of the model's guess. Unknown models keep their (sanity-gated) self-report. Matched against the *self-report*, not the env name, which the probe distrusts. (Real case: Claude Opus 4.8 — genuinely 1M — was observed self-reporting both `1000000` and a cautious `200000` across two probes; the guard pins both to 1M.) The pin runs on a *fresh* probe only; a cache reuse returns the stored value as-is, so an already-wrong cached entry needs `--reprobe` (or a hand-edit) to correct.
4. On pass, `Config.apply_context(context_size)` recomputes `source_budget`, `target_tokens`, `target_chars` from the probed value.
5. The value is written to `probed-context.json` for future cache hits.

The probe uses the same conversation-mode handoff as every stage: first pass writes the prompt and raises `ConversationPending` (exit 101); the agent answers; on re-invoke the router returns the cached answer and the probe completes. The router also caches by prompt hash, so even without `probed-context.json` a resume re-reads the answer without a new round-trip.

## Budget math (no extra margin)

The probe value is used **as-is** — no 0.85 safety margin. Headroom comes from the existing reserves in `apply_context`:

- 15% response reserve
- 25% stable-context reserve (schema/purpose/index/overview), floored at 12K, capped at 50K
- 8% instruction reserve

≈ 48% total reserved, which is conservative. Adding a margin on top double-counts and was found (2026-06-27 verification) to regress ≤200K-context models into more chunks.

## Chunk sizing by context

`target_tokens = min(64_000, max(12_000, context × 0.33))` — decoupled from `source_budget` (2026-06-27). Each chunk is one analysis round-trip; its safe size is bounded by the context window, not the per-source digest budget.

| Probed context | target_tokens | Skolnik (3M chars ≈ 750K tok) est. chunks |
|---|---|---|
| 128K | 42K | ~18 (cap doesn't bind; 0.33×ctx governs) |
| 200K | 64K | ~12 |
| 1M | 64K | ~12 |

The 64K cap (`_TARGET_TOKENS_HARD_CEIL`, set 2026-07-01; was briefly 192K) is a hard UPPER ceiling. An A/B ingest (Barton, 448pp, 1M ctx) showed 64K (finer 4-chunk splitting) gives +27% concept coverage and cleaner driving than a 192K whole-book single chunk — which was too large to analyze/generate in one call and had to fan out anyway. The cap only binds for context > ~194K; small books stay 1 chunk. Override per-run with `IMPROVED_WIKI_TARGET_TOKENS_CEIL` (e.g. 192000 to restore whole-book chunks).

## No-silent-fallback

If the probe response parses to nothing or falls outside `[8K, 10M]`, `probe_context` raises `RuntimeError` and the ingest pauses. It never silently falls back to a guessed context. This matches the skill-wide no-silent-fallback policy.

## Recovery (if the probe misbehaves)

If a model persistently misreports its context, hand-edit the cache and re-run — no code change or env var needed:

```bash
cat > ~/Documents/知识库/<WikiName>/.llm-wiki/probed-context.json << 'EOF'
{"model": "glm-5.2", "context": 1000000, "probed_at": 0}
EOF
```

`probed_at: 0` disables the TTL (treated as fresh). The `model` field must match `config.llm_model` (from `ANTHROPIC_MODEL`) or the cache is ignored and a real probe runs. Delete the file to force a fresh probe.

## Touch points

- `scripts/_context_probe.py` — probe prompt, parse, sanity gate, cache, `resolve_context`.
- `scripts/_core.py` — `Config.apply_context`, `_compute_chunk_targets` (decoupled, 64K cap, `IMPROVED_WIKI_TARGET_TOKENS_CEIL` override), `from_env` (placeholder only, no env read).
- `scripts/ingest.py` — `_probe_and_apply_context` called in watch + normal ingest paths (delete path skips).
