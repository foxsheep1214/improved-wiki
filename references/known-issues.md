# Known issues

Only unresolved limitations belong here. Fixed regressions are covered by the
test suite and Git history; operational procedures live in their dedicated
runbooks.

## Cross-directory basename slug collisions

The cross-source dedup protocol identifies pages by filename stem. Pages such
as `concepts/x.md` and `methodology/x.md` therefore share the id `x` even
though both paths may be valid schema outcomes.

The dangerous mutation path is guarded: embedding candidates are unique by
slug, `cross_source_dedup.py` refuses to merge a group containing a collision,
and structural lint reports `slug-collision` findings. A collision is therefore
visible and cannot make dedup read or delete the wrong page.

The remaining limitation is content-level: colliding pages are excluded from
automatic cross-source merging. Review each lint finding and decide whether
the two schema types should remain separate or be consolidated. Do not bypass
the guard by editing a dedup group manually.

Changing the whole dedup protocol from slug ids to path ids would also change
LLM response contracts and persisted cache keys, so it is intentionally left
as a future migration rather than hidden inside a cleanup.
