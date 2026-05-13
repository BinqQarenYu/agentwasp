---
id: goal-engine
title: Goal Engine
description: TaskGraph plans, plan critic, executor, replan storms, autonomy modes.
---

# Goal Engine

When the Decision Layer routes a request to `GOAL`, `GoalOrchestrator.create_goal(objective, chat_id, ...)` instantiates a Goal record. The orchestrator then plans, executes, and replans through completion or failure.

## Concepts

| Concept | Definition |
|---------|------------|
| **Goal** | A persistent objective with a state machine (`PENDING` → `ACTIVE` → `COMPLETED` / `FAILED` / `PAUSED`). Has a chat_id, a TaskGraph, and a budget. |
| **TaskGraph** | A DAG of tasks (≤ 8 nodes). Each task has a skill call, dependencies, and an output. |
| **Plan** | The TaskGraph generated for a goal by `PlanGenerator`. |
| **Replan** | Regenerating the TaskGraph mid-execution after a failure. |
| **Stability backoff** | Cooldown after consecutive failures. |

## Lifecycle

```
GoalOrchestrator.create_goal()
   │
   ▼
PlanGenerator → TaskGraph
   │     (max 8 steps, max 4000 tokens, max 3 retries)
   ▼
PlanCritic (optional, feature-flagged)
   │
   ▼
Goal status = ACTIVE
   │
   ▼  (every goal_tick — 15s default — up to 3 steps per tick)
GoalStepExecutor.step(goal)
   ├─ Budget check
   ├─ Stability check (backoff window?)
   ├─ Stability lock (replan storm?)
   ├─ Step limit check
   ├─ Runtime limit check
   ├─ Autonomy gate (RESTRICTED+ in SEMI/MANUAL → confirm)
   ├─ Skill dispatch via SkillExecutor
   ├─ Episodic memory write
   └─ Event emission (TASK_COMPLETED / TASK_FAILED)
   │
   ▼
state == COMPLETED, FAILED, or replan loop
```

## Constants

| Constant | Value |
|----------|-------|
| `MAX_PLAN_STEPS` | 8 |
| `MAX_PLAN_TOKENS` | 4 000 |
| `MAX_PLAN_RETRIES` | 3 |
| `PLAN_SKILL_BUDGET` | 2 000 chars |
| `MAX_REPLAN_COUNT` | 3–5 (configurable) |
| `REPLAN_STORM_COUNT` | 3 |
| `REPLAN_STORM_WINDOW` | 5 min |
| `goal_tick_interval` | 15 s |

## Plan generation

`PlanGenerator` calls the LLM with a budget-capped skill catalog and produces a TaskGraph. The system prompt includes:

- Autonomous system setup pattern (`agent_manager` + `task_manager` for recurring goals)
- Crypto API URLs (Binance, CoinGecko)
- Skill selection rules and examples
- Critical planning rules (no loops, no circular deps, etc.)

The catalog is budget-capped to keep context lean. Skills not in the catalog cannot be planned in.

## Plan Critic

`plan_critic_enabled = True` (default) runs a second LLM pass over every generated TaskGraph. The critic validates:

- No skill calls outside the registered catalog
- No circular dependencies
- Each step has at least one valid input mapping
- Side-effect skills have explicit user intent in the goal objective

If the critic rejects, the TaskGraph is regenerated (up to `MAX_PLAN_RETRIES`).

## Replan storms

If a goal replans `>= REPLAN_STORM_COUNT (3)` times within `REPLAN_STORM_WINDOW (5 min)`, it is flipped to FAILED with the partial output rendered to the user. This prevents infinite-loop token burn.

## Autonomy modes

| Mode | Skills run automatically | Skills require confirmation |
|------|-------------------------|---------------------------|
| FULL | All | None |
| SEMI (default) | SAFE, MONITORED, CONTROLLED | RESTRICTED, PRIVILEGED |
| MANUAL | None | All |

The operator can change a goal's autonomy mode at `/goals`.

## Goal priority

Goals run in priority-desc order:

| Source | Priority |
|--------|----------|
| User-created | 8 |
| Agent-created | 6 |
| Autonomous | 3 |
| Default | 5 |

## Stability layer

After consecutive failures, `GoalStepExecutor` enters a backoff window. The next tick checks `stability.backoff_until`; if not yet elapsed, the step is skipped.

After 10 minutes paused, a goal is auto-failed unless explicitly resumed by the operator.

## Replan triggers

A replan is triggered by:

- Skill failure during execution
- Plan Critic rejection
- Drift detected by the Response Validator

Each replan increments `replan_count`. After `MAX_REPLAN_COUNT` replans, the goal is flagged FAILED.

## Plan Lock

Goals have a `plan_locked` field. After the first successful step, `plan_locked = true` so subsequent failures cannot regenerate the entire plan from scratch — only individual failed nodes are retried. This prevents spurious replans for transient failures.

## Cognitive Budget

```python
CognitiveBudget(
    max_tokens_planning=4000,
    max_tokens_execution=20000,
    max_replans=5,
    max_steps=8,
)
```

`check_planning_tokens()` and `check_replan()` raise `BudgetExceeded` when limits are hit.

## Goals vs Tasks vs Reminders

Three different scheduling concepts; not interchangeable:

| Concept | Owner | Granularity | Persistence | Use case |
|---------|-------|-------------|-------------|----------|
| **Goals** | `GoalOrchestrator` | TaskGraph (≤ 8 steps) | `goals` table + Redis | Multi-step plans with replanning |
| **Tasks** (recurring custom) | `task_manager` skill | interval-only (seconds) | Redis hash `custom_tasks` | "Every N hours run this instruction" |
| **Reminders** | `reminders` skill | seconds-minutes | `reminders` table | One-shot or daily/weekly alerts |

See [Scheduler](/core-concepts/scheduler) for tasks and reminders.

## Dashboard

`/goals` shows:

- Active goals with TaskGraph visualization (DAG rendered with step states)
- Historical goals with outcome
- Per-goal: pause, resume, archive, replan, change autonomy mode

## See also

- [Skills](/core-concepts/skills) — what each skill does
- [Scheduler](/core-concepts/scheduler) — recurring tasks and reminders
- [Resource Governor](/core-concepts/resource-governor) — limits
- [Cognitive Systems → Plan Critic](/cognitive-systems/plan-critic) — LLM validation detail
- [Architecture → Orchestration](/architecture/orchestration) — wiring detail
