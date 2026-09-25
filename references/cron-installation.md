# Scheduled scans and conversation-mode ingestion

A scheduler can run deterministic scans and structural diagnostics without an
agent. Full ingestion, semantic lint, sweep and dedup need an agent to consume
conversation prompts, publish validated answers and resume after exit 101.
A shell cron entry alone cannot finish these workflows.

For a scheduled scan, configure the actual project, interpreter and skill path:

```bash
export SKILL_DIR="$HOME/.agents/skills/improved-wiki"
export IMPROVED_WIKI_ROOT="$HOME/Documents/知识库/MyWiki"
bash "$SKILL_DIR/scripts/wiki-monitor.sh"
# Independent deterministic diagnostic:
bash "$SKILL_DIR/scripts/wiki-lint.sh" --structural-only
```

`wiki-monitor.sh` scans/merges the queue; it does not answer LLM prompts.
`run-queue.sh` consumes work until completion, a pending handoff or an error.
Before consuming, establish the authorized source list/project and an agent
handoff driver following `delegate-mode.md`. Preserve the same context budget
on every resume. Do not interpret a queued job or exit 101 as a completed ingest.

Use the host scheduler appropriate to the user's request; no vendor-specific
`no_agent`, notification destination or delivery flag is part of this CLI.
Avoid duplicate schedulers; project/coordinator locks and durable reservations
remain authoritative. Pause/resume controls are documented in
`batch-parallel-prefetch.md`. Disabling a schedule does not delete its queue,
checkpoints or completion history.
