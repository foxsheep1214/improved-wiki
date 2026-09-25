# Runtime layout and explicit migration

`_paths.detect_runtime_dir()` is the single, read-only selector used by Python
and shell entry points. New projects use `.llm-wiki`. Legacy `.iwiki-runtime`
remains readable in place; legacy `wiki/` runtime files are supported when no
new runtime state exists. Current `.llm-wiki` state wins over legacy `wiki/`
files, including when only the completion ledger exists. Hidden legacy state names
(such as `.ingest-cache.json`) require explicit migration before use. Mixed active state in
`.iwiki-runtime` and `.llm-wiki` is rejected, never automatically reconciled.

Preview a migration before applying it:

```bash
python3 "$SKILL_DIR/scripts/migrate_runtime.py" --project "$IMPROVED_WIKI_ROOT" --from .iwiki-runtime
python3 "$SKILL_DIR/scripts/migrate_runtime.py" --project "$IMPROVED_WIKI_ROOT" --from .iwiki-runtime --apply
# For legacy runtime files under wiki/, use --from wiki instead.
```

Apply requires idle writers, queue/watch processes and prefetch workers, and no
reserved spine. It locks both layouts, rechecks the preview and source hashes,
rejects symlinks and differing destination content, then moves files without
overwriting. Byte-identical duplicates can be consolidated. A conflict leaves
data unchanged; reconcile its contents explicitly before retrying. Interrupted
migration can be previewed and resumed. Lock inodes stay in place. Stop unrelated
external writers before migration; they cannot be governed by project locks.

Only known runtime entries move from `wiki/`; source pages, raw files and other
wiki content do not. Source reclassification is a separate coordinated operation
that must update references, cache and history projections.
