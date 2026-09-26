# Lint


- Plain `wiki-lint.sh` runs structural + semantic checks and, by default,
  `emit-review`, `fix`, `fix-links`, `sweep`, and one `dedup` round.
  `--no-<action>` overrides an individual default.
- Semantic/sweep/dedup may return exit 101 for conversation handoff. Answer the
  prompt and resume the exact invocation. Exit-101 re-entry continues one
  durable logical lint run: semantic performs one complete scan for that run
  and completed stages are not restarted. Sweep likewise preserves NashSU's
  single hard budget of at most five 40-item judge batches across re-entry and
  stops at the first batch that resolves zero items. Use `--reset-lint-run`
  only to discard an abandoned checkpoint and intentionally start over. After
  one requested dedup round, continue remaining stages with `--no-dedup`
  unless the user explicitly asks for full convergence.
- After all preceding default stages finish, plain lint exits **102** with
  `DELETE_ORPHANS_CONFIRMATION_REQUIRED`. This is a required pause: ask the
  user whether to run delete-orphans. Do not infer consent.
  - If approved: run `wiki-lint.sh --delete-orphans-only`; it performs a fresh
    structural scan, then emits orphan preview/Review items.
  - If declined: stop; the preceding lint/fix/sweep/dedup work is already done.
- `--diagnostic-only` keeps structural + semantic lint but disables every wiki
  mutation and the exit-102 checkpoint. `--structural-only` is the deterministic
  structural-only diagnostic route.
- Delete-orphans remains preview + Review generation; it does **not** delete
  pages. Real deletion is the separately confirmed
  `wiki-lint-fix.py --delete-orphans --apply` command.
- Keep improved-wiki's documented semantic batching/safety extensions; v0.6.6
  parity covers normalized indexed structural suggestions and exact normalized
  filtering of false `missing-page` findings.
- Structural lint reports `invalid-frontmatter` (error) for a block that is
  unclosed, empty or not valid YAML: every reader treats such a page as having
  no frontmatter. It is not auto-fixed; `--strict` counts it as critical.
- Graph is a peer command, not a lint phase. `wiki-lint.sh` never invokes
  `graph.py`; run Graph explicitly when graph artifacts are requested.
- Graph writes its derived gap report to
  `<project>/.llm-wiki/knowledge-gaps.md`, never under `wiki/REVIEW/`; it is
  graph state, not a Review item.


## Entry points

Run from the target project with `IMPROVED_WIKI_ROOT` set:

```bash
bash "$SKILL_DIR/scripts/wiki-lint.sh" --structural-only
bash "$SKILL_DIR/scripts/wiki-lint.sh" --diagnostic-only
bash "$SKILL_DIR/scripts/wiki-lint.sh"
```

Every lint run holds `<runtime>/lint.lock`, so two lint runs never overlap.
A run whose effective flags leave every wiki mutation off (`--structural-only`,
`--diagnostic-only`, or all `--no-<action>` flags) holds only that lock: it runs
beside an ingest writer or a reserved spine and warns that findings may reflect
a partial write. Any mutating run also joins the ingest project lock, including
across shell children, and a reserved ingest spine blocks it; the standalone
`wiki-lint-fix.py --apply` / `--emit-review` does the same. `--help` needs no
lock. Do not remove lock files to bypass a refusal. Runtime selection is
read-only; see `runtime-layout.md` for explicit migration.
