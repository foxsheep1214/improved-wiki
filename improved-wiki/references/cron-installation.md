# Cron installation

The `improved-wiki` pipeline is meant to run unattended. The cron entry is one line, pointing at the `wiki-monitor.sh` script (which exits 0 fast when there's no work to do).

---

## Install on macOS / Linux

```bash
# Open the crontab editor
crontab -e

# Add this line (one shot per day at 02:00 — monitor only; the queue worker has its own line below)
0 2 * * * $SKILL_DIR/scripts/wiki-monitor.sh

# Save and exit. Verify the crontab took:
crontab -l | grep wiki-monitor
```

The script:
- Diffs `raw/` against `.llm-wiki/ingest-cache.json`
- For each new file, adds it to `.llm-wiki/ingest-queue.json`
- Then exits

`wiki-monitor.sh` does NOT itself run the queue. To process the queue, you need a separate cron entry for `run-queue.sh`. The reason for the split: the monitor is fast (just a hash diff) and can run frequently; the queue worker is slow (conversation-mode LLM handoffs) and runs on its own schedule.

Standard 2-cron setup:

```cron
# 02:00 — scan for new files (fast, idempotent)
0 2 * * * $SKILL_DIR/scripts/wiki-monitor.sh

# 02:30 — process the queue (slow, may take hours)
30 2 * * * $SKILL_DIR/scripts/run-queue.sh
```

Both scripts are no-op when there's no work, so running the crons daily is essentially free.

---

## Verify the cron is firing

```bash
# Check the system log for the last cron run
log show --predicate 'process == "cron"' --last 1d 2>&1 | grep wiki-monitor | tail -5
```

If the cron is firing, you'll see a line per day. If not, check:
- `crontab -l` — is the entry there?
- The script has `chmod +x` permissions
- The user account has permission to write to `wiki/`

---

## Adjusting the schedule

The default is once per day at 02:00 (low-activity hours). Adjust based on your volume:

| Pattern | Cron line | Use case |
|---|---|---|
| Daily at 02:00 | `0 2 * * * ...` | Default; handles 1-5 new files/day |
| Every 6 hours | `0 */6 * * * ...` | Active knowledge worker adding multiple files/day |
| Hourly | `0 * * * * ...` | Power user; runs frequently but each run is a no-op when empty |
| Weekly (Sunday 03:00) | `0 3 * * 0 ...` | Light user; check once a week |

The cron line is a single shell command. If you need more complex logic (e.g. only run if disk is not full), wrap it:

```bash
0 2 * * * /bin/bash -c '[ $(df -k ~/Documents/知识库/ | tail -1 | awk "{print \$4}") -gt 1048576 ] && $SKILL_DIR/scripts/wiki-monitor.sh'
```

This is a common idiom: "only run if at least 1GB free".

---

## Lint cron (separate, optional)

The ingest cron handles new files. A second cron can run **Lint** periodically to:
- Mark `status: outdated` on news pages > 6 months old
- Detect newly broken wikilinks
- Re-check `wiki/log.md` for missing entries

```bash
# Lint weekly (Sunday 04:00)
0 4 * * 0 $SKILL_DIR/scripts/wiki-lint.sh
```

`wiki-lint.sh` is a structural lint scanner — see `scripts/wiki-lint.sh` and `references/nashsu-lint-source-analysis.md` for details.

---

## Disabling the cron

```bash
# Comment out the line
crontab -e
# add a # at the start of the line

# Or remove it entirely
crontab -l | grep -v wiki-monitor | crontab -
```

Disabling does NOT delete the cache or the queue — they survive. Re-enabling picks up where it left off.

---

## Why not use agent cron?

Agent cron (LLM-driven periodic tasks) is for **LLM-driven jobs** (the agent runs the prompt each tick). The `improved-wiki` pipeline is a **pure shell pipeline** (no LLM needed for the trigger step — the LLM runs inside `ingest.py`, not at the cron layer).

If you used agent cron for this, you'd be paying tokens to detect "no new files" — wasteful. Use the system cron for shell pipelines; use agent cron for LLM-driven periodic tasks (daily briefings, etc.).

This distinction is also captured in your persistent memory: "成功不推送，失败才发飞书。配置 no_agent cron 时默认 deliver=feishu + no_agent=true".

---

## See also

- `SKILL.md` — The ingest pipeline reference
- `references/known-issues.md` — Failure handling
- `references/initial-setup.md` — Verifying the install
