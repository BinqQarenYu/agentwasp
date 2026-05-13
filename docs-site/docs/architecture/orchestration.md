---
id: orchestration
title: Orchestration
description: Goal orchestrator, agent runtime, plan/execute/replan loop.
---

# Orchestration

The orchestration layer turns objectives into TaskGraphs and runs them through completion or failure. Two interlocking systems:

- **GoalOrchestrator** — manages individual goals
- **AgentRuntime** — manages persistent sub-agents

## GoalOrchestrator

`src/goal_orchestrator/orchestrator.py`

```python
GoalOrchestrator(
    max_concurrent=3,
    default_autonomy_mode=AutonomyMode.SEMI,
    plan_critic=None,            # late-wired
    governor=None,               # late-wired
    reflection_engine=None,      # late-wired
    capability_evolution_engine=None,  # late-wired
    execution_backend=None,
)
```

### Methods

| Method | Purpose |
|--------|---------|
| `create_goal(objective, chat_id, ...)` | Create a Goal, generate a TaskGraph, set state to `ACTIVE` |
| `tick()` | Step every active goal up to 3 times |
| `pause(goal_id)` | Move a goal to `PAUSED` |
| `resume(goal_id)` | Move a paused goal back to `ACTIVE` |
| `archive(goal_id)` | Move a goal to `ARCHIVED` (soft delete) |
| `replan(goal_id)` | Force regeneration of the TaskGraph |
| `invoke(goal_id, message)` | Per-goal invocation (used by AgentRuntime) |

### Constants

```python
MAX_REPLAN_COUNT = 5
MAX_PLAN_STEPS = 8
default_autonomy_mode = SEMI
```

## PlanGenerator

`src/goal_orchestrator/planner.py`

Generates a TaskGraph from a goal objective via the LLM:

```
MAX_PLAN_RETRIES = 3
MAX_PLAN_TOKENS = 4 000
PLAN_SKILL_BUDGET = 2 000 chars
```

The skill catalog is budget-capped to 2 000 chars; only the most relevant skills make it into the planner's context. The system prompt includes:

- Autonomous setup pattern (`agent_manager` + `task_manager` for recurring goals)
- Crypto API URLs (Binance, CoinGecko)
- Skill selection rules
- Critical planning rules (no loops, no circular deps)

## PlanCritic

`src/goal_orchestrator/plan_critic.py` (feature-flagged via `plan_critic_enabled`).

A second LLM pass that validates the generated TaskGraph. Checks:

- Skills referenced exist in the catalog
- No circular dependencies
- Side-effect skills have explicit user intent in the goal objective
- Plan stays within step limit

If the critic rejects, the planner retries (up to `MAX_PLAN_RETRIES`).

## GoalStepExecutor

`src/goal_orchestrator/executor.py`

```python
async def step(self, goal: Goal) -> StepResult:
    # 1. Budget check
    self.budget.check_planning_tokens(goal)
    # 2. Stability backoff
    if self.stability.in_backoff(goal):
        return StepResult(action="stability_backoff")
    # 3. Stability lock
    if self.stability.replan_storm(goal):
        return StepResult(action="stability_lock")
    # 4. Step limit
    if len(goal.completed_tasks) >= MAX_PLAN_STEPS:
        return StepResult(action="step_limit_reached")
    # 5. Runtime limit
    if self._runtime_exceeded(goal):
        return StepResult(action="runtime_limit_reached")
    # 6. Autonomy gate
    next_task = self._pick_next_task(goal)
    if needs_confirmation(next_task, goal.autonomy_mode):
        return StepResult(action="autonomy_confirmation_required")
    # 7. Execute
    result = await self.skill_executor.execute(next_task.skill_call)
    # 8. Episodic write + event emission
    ...
    return StepResult(goal=goal, event=event, action=action)
```

`StepResult` carries `(goal, event, action)`. `action` is one of: `step_executed`, `step_failed`, `goal_completed`, `goal_failed`, `replan_triggered`, `stability_backoff`, `stability_lock`, `step_limit_reached`, `runtime_limit_reached`, `autonomy_confirmation_required`, `budget_exceeded`, `sandbox_denied`, `autonomy_blocked`.

## Stability layer

`src/goal_orchestrator/stability.py`

| Concept | Purpose |
|---------|---------|
| Backoff | Cooldown after consecutive failures (`backoff_until`) |
| Replan storm | `>= REPLAN_STORM_COUNT (3)` replans within `REPLAN_STORM_WINDOW (5 min)` → goal flipped to FAILED |
| Intervention recording | Per-goal record of stability events for reflection |

PAUSED goals auto-resume after backoff expires; auto-fail after 10 min stuck.

## AgentRuntime

`src/agent_manager/runtime.py`

Each persistent sub-agent has its own `AgentRuntime` instance. Responsibilities:

- Maintain the agent's chat-id and goal queue
- Tick the agent's active goal (single goal at a time per agent)
- Switch the active model when the agent has `model_provider` / `model_name` set
- Clean up archived goals

`tick()` calls `goal_orchestrator.invoke(goal_id, ...)` for the agent's current goal. State machine: `IDLE` → `RUNNING` → `IDLE` per cycle.

## Multi-brain agents

`Agent` records have `model_provider` and `model_name` fields. When set, the runtime temporarily switches the active provider for that agent's tick (best-effort; logs warning on failure).

The `AgentManagerSkill._create()` does not yet expose these as natural-language parameters. Set them via the `/agents` create form or direct DB insert.

## Inter-agent messaging

Agents communicate via the `agent_messages` Postgres table:

```
AgentMessage(from_agent_id, to_agent_id, content, message_type, metadata, created_at, read_at)
```

The `agent_manager(action="send_message", agent_id=X, message=Y)` skill writes a row. The receiving agent's tick reads its inbox and processes new messages.

## Meta-Agent Supervisor

`meta_orchestrate` skill (feature-flagged via `meta_agent_enabled`). Decomposes a high-level objective into a team of sub-agents, monitors progress, synthesizes results.

```
meta_orchestrate(
    objective="Research and compare top 5 Python web frameworks",
    team_size=5,
    strategy="parallel_then_synthesize"
)
```

## See also

- [Goal Engine](/core-concepts/goal-engine) — concepts
- [Agent Architecture](/core-concepts/agent-architecture) — pipeline
- [Advanced → Agent Orchestration](/advanced/agent-orchestration) — patterns and use cases
- [Plan Critic](/cognitive-systems/plan-critic) — validation detail
- [Resource Governor](/core-concepts/resource-governor) — caps
