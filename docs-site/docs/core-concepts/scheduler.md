---
id: scheduler
title: Scheduler
description: 41 registered jobs, scheduling primitives, and limits.
---

# Scheduler

WASP runs continuously even when no one is asking it questions. This page lists every scheduled job and explains what triggers autonomous behavior.

## Three scheduling primitives

NOT interchangeable.

| Concept | Owner | Granularity | Persistence | Use case |
|---------|-------|-------------|-------------|----------|
| **Reminders** | `reminders` skill | seconds-minutes | `reminders` table | One-shot or daily/weekly alerts; can trigger sub-agents |
| **Tasks** (recurring custom) | `task_manager` skill | interval (seconds), no fixed clock time | Redis hash `custom_tasks` | "Every N hours run this instruction" |
| **Goals** | `GoalOrchestrator` | TaskGraph (≤ 8 steps) | `goals` table + Redis | Multi-step plans with replanning |

The scheduler's *jobs* are different from the operator-facing primitives. Jobs are internal periodic functions that keep WASP itself running.

## Registered jobs (41 total)

All 41 jobs below are registered in `src/main.py` at startup. Their default cadence and feature-flag gating is listed; toggle the flag on `/config` to disable.

### Always-on infrastructure

| Job | Default interval | What it does |
|---|---|---|
| `health_check` | 60 s | Liveness probe; updates `agent:health` |
| `db_maintenance` | weekly | `VACUUM ANALYZE` via `AUTOCOMMIT` (no table locking) |
| `audit_retention` | 6 h | Bounded batch deletion of `audit_log` rows older than `AUDIT_RETENTION_DAYS` (default 30) |
| `memory_cleanup` | daily | Drops episodic entries below the importance floor |
| `snapshot` | daily | Serializes memory state into `MemorySnapshot` and writes to `/data/backups/` |

### Memory + learning maintenance

| Job | Default interval | What it does |
|---|---|---|
| `vector_index` | 600 s | Backfills missing embeddings for `MemoryEmbedding` rows |
| `promotion` | 12 h | Promotes recurring/important episodic entries to semantic memory |
| `kg_pruner` | daily | Prunes low-confidence / orphaned KG nodes |
| `kg_insights_updater` | hourly | Refreshes derived KG insights |
| `learning_pruner` | daily | Drops stale `LearningExample` rows below use-count threshold |
| `behavioral_pruner` | daily | Drops behavioral rules with zero applications after N days |
| `procedural_pruner` | daily | Drops procedural memory entries below success threshold |
| `execution_reflection_pruner` | daily | Drops `execution_reflection` rows past retention |

### Operator-facing primitives

| Job | Default interval | What it does |
|---|---|---|
| `reminder_checker` | 30 s | Scans `reminders` for due rows; fires them; agent-linked reminders restart the agent's goal cycle |
| `monitor_checker` | 5 min | Polls website monitors created via `monitors` skill |
| `subscription_checker` | 5 min | RSS feeds + price alerts created via `subscribe` skill |
| `custom_task_runner` | 60 s | Iterates `custom_tasks` Redis hash; dispatches due tasks |
| `checkin` | hourly | Optional proactive check-in to the operator (skipped if no episodic memory exists yet — fresh-install guard) |
| `digest` | configurable | Periodic digest (daily/weekly summary) |

### Goals + agents

| Job | Default interval | What it does |
|---|---|---|
| `goal_tick` | 15 s | Executes up to 3 consecutive steps per ACTIVE goal (priority desc) |
| `agent_tick` | 15 s | Cleanup pass for sub-agents (state transitions, archived goals) |
| `goal_meta_reflection` | configurable | Per-goal post-mortem at completion or failure |

### Cognitive systems

| Job | Default interval | What it does |
|---|---|---|
| `reflection` | hourly | Synthesizes recent episodic memory into higher-level summaries |
| `dream` | 1 h (gated) | Activates when operator inactive ≥ 2 h AND (night 1–7 am OR ≥ 4 h idle); max once per 6 h |
| `autonomous` | 30 min | Autonomous Goal Generator |
| `proactive` | configurable | Suggests actions based on recent state |
| `perception` | 15 min | Background crypto/web perception for assets tracked in the KG |
| `opportunity_engine` | hourly | Detects automation opportunities from episodic patterns |
| `opportunities_processor` | hourly | Processes ranked opportunities into goal candidates |
| `cpi_monitor` | 5 min | Computes Cognitive Pressure Index; sets `agent:cpi_high` when > 80 |
| `self_integrity` | 6 h | Cross-checks self-model strengths vs actual skill rates |
| `behavioral_learner` | 120 s | Drains `behavioral:pending` queue; LLM extracts rules; saves to `behavioral_rules` |
| `world_model` | hourly | Updates `EntityState` snapshots from recent `WorldTimeline` rows |
| `skill_evolution` | 6 h | Identifies recurring `SkillPattern` rows and synthesizes composite skills |
| `capability_evolution` | hourly | Discovers and registers new capabilities from successful execution traces |
| `capability_learner` | hourly | Updates capability confidence scores from recent outcomes |
| `execution_intelligence_monitor` | hourly | Tracks LLM call efficiency; flags expensive patterns |
| `execution_knowledge_sync` | hourly | Syncs execution-derived knowledge into the KG |

### Resource hygiene

| Job | Default interval | What it does |
|---|---|---|
| `browser_session_cleanup` | 300 s | Idle Reaper for Chromium sessions (CPU: ~81% → ~0.25% when idle) |
| `disk_monitor` | hourly | Watches `/data/` free space; warns when low |
| `screenshot_cleanup` | daily | Removes screenshots older than 30 days |

## CPI gating

When `agent:cpi_high` is set (CPI > 80), the heavy background jobs (`autonomous`, `dream`, `perception`, `opportunity_engine`, `proactive`) check the flag and **skip** for the cycle. This prevents background work from amplifying load during pressure.

See [Monitoring → CPI](/operations/monitoring) for details.

## Catch-up on restart

When agent-core restarts, the scheduler reads `last_run_at` from Redis for each job and immediately fires anything that should have run while the container was down (with a ceiling of one make-up fire per job). PEL zombie recovery (`XAUTOCLAIM` at startup) reclaims any pending Redis Streams entries that were in-flight at crash time.

## Reminders

Operator creates: *"remind me to back up the disk in 3 hours"*.

Stored fields:

- `due_at` — absolute UTC timestamp, or relative offset
- `recurring` — `null`, `daily`, `weekly`, `monthly`
- `agent_id` — optional sub-agent to restart when fired
- `agent_objective` — text to seed the agent's goal cycle

`ReminderCheckerJob` (30 s) polls due reminders. Agent-linked reminders call `agent_orchestrator.create_agent_goal()`. A 🤖 Telegram notification is sent at fire time.

`delete_reminder` accepts `keyword="all"` or any substring of the reminder text.

## Custom recurring tasks

Operator creates: *"every 6 hours, fetch BTC price and email me a report"*.

Behavior:

- Stored as `custom_tasks` Redis hash entries.
- `interval_seconds` is the only schedule primitive.
- `next_run_at = created_at + interval` (NOT `now`).
- `custom_task_runner` (60 s) dispatches due tasks as a system-prefixed message.

### Limitation: no fixed clock times

`task_manager` does NOT support clock-time scheduling or daypart phrases. When the user requests one ("every Monday at 9am"), the agent creates an interval-only task and the response includes an automatic disclaimer (see [Skill Safety → Schedule Honesty](/security/skill-safety)).

To approximate "every Monday at 9am":

1. Create the task at 9am on a Monday.
2. Set `interval=604800` (one week).

The task runs every 7 days from creation. Drift can occur if the host loses time.

## Operator commands

| Action | How |
|--------|-----|
| List recurring tasks | Telegram: *"list my tasks"* (auto-detected fast path); or dashboard `/scheduler` |
| Delete a task | Telegram: *"delete the X task"*; or per-row delete in `/scheduler` |
| Pause a job | Toggle the relevant feature flag in `/config` |
| Inspect job state | Redis: `GET agent:autonomous_state`, `GET agent:dream_state`, `GET agent:integrity_report` |

## See also

- [Goal Engine](/core-concepts/goal-engine) — multi-step plans
- [Resource Governor](/core-concepts/resource-governor) — caps
- [Cognitive Systems → Opportunity Engine](/cognitive-systems/opportunity-engine)
- [Advanced → Autonomous Goals](/advanced/autonomous-goals)
- [Monitoring → CPI](/operations/monitoring)
