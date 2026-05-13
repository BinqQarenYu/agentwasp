---
id: resource-governor
title: Resource Governor
description: Redis-backed rate limiting for goals, agents, tasks, LLM, and API calls.
---

# Resource Governor

`src/governance/governor.py` is a Redis-backed rate limiter that prevents any single subsystem from monopolizing model spend or external API calls.

## Default caps

| Resource | Cap | Scope |
|----------|-----|-------|
| Goals per day | 10 | All goals |
| Concurrent agents | 5 | Active sub-agents |
| Tasks per hour | 50 | Custom recurring tasks |
| LLM calls per minute | 30 | All LLM providers combined |
| API calls per minute | 60 | External API calls (integrations) |
| Global tokens per minute | 100 000 | Across all agents |

All caps are configurable via `.env` or `/config`.

## How it works

Redis sliding window:

```
governor:goals:day:{date}              → counter (TTL 86 400 + 60)
governor:agents:active                 → counter
governor:tasks:hour:{hour}             → counter (TTL 3 600 + 60)
governor:llm:minute:{minute}           → counter (TTL 60 + 60)
governor:api:minute:{minute}           → counter (TTL 60 + 60)
agent:global_tokens:{minute}           → counter (TTL 120)
```

Before each LLM call, `LLMGuard.check()` increments the relevant counter and compares against the cap. If the cap is exceeded, the call raises `RateLimitedError` and the loop backs off.

Before each goal creation, `GoalGuard.check()` increments the daily goal counter. Exceeding the cap pauses goal creation until the next day.

## When you hit a cap

- **Goals/day** — pending goals are queued; the operator gets a notification.
- **Concurrent agents** — `agent_manager.create` returns an error with a count of active agents.
- **Tasks/hour** — `task_manager.create` returns an error.
- **LLM calls/minute** — the LLM loop sleeps and retries (exponential backoff).
- **API calls/minute** — integration calls return an error to the agent for replanning.
- **Global tokens/minute** — `agent_tick` is paused until the next minute window.

## Configuring caps

Via `.env`:

```bash
GOALS_PER_DAY_CAP=10
CONCURRENT_AGENTS_CAP=5
TASKS_PER_HOUR_CAP=50
LLM_CALLS_PER_MINUTE_CAP=30
API_CALLS_PER_MINUTE_CAP=60
AGENTS_GLOBAL_TOKEN_BUDGET_PER_MINUTE=100000
```

Or via `/config`. Changes are persisted to `config:overrides` Redis key and applied at startup.

## Goal-specific budgets

`Goal.cognitive_budget`:

```python
CognitiveBudget(
    max_tokens_planning=4000,
    max_tokens_execution=20000,
    max_replans=5,
    max_steps=8,
)
```

Independent of the governor's per-minute caps. Each goal is bounded by its own budget; the governor caps the aggregate.

## Per-request budget

`_REQUEST_BUDGET` enforces a per-execution cap on LLM rounds based on request tier:

| Tier | Cap (rounds) |
|------|--------------|
| simple | 10 |
| normal | 20 |
| complex | 36 |

Tier is detected from `_COMPLEX_MARKERS_RE` in the user text.

## Inspecting governor state

```bash
docker exec agent-redis redis-cli
> GET governor:goals:day:$(date +%F)
> GET governor:llm:minute:$(date -u +%Y-%m-%dT%H:%M)
> GET governor:agents:active
```

The dashboard `/health` page surfaces governor pressure under "Queues" and "Goals".

## CPI vs Resource Governor

The Cognitive Pressure Index (CPI) measures **system load**; the Resource Governor enforces **rate limits**. Different concerns:

| Concern | CPI | Resource Governor |
|---------|-----|-------------------|
| Triggers when | Load is high | Caps are exceeded |
| Effect | Pauses background jobs | Rejects/delays new operations |
| Recovery | When load drops | When the time window rolls over |
| Visible in | `/health`, `/cognitive` | `/health`, error responses |

## See also

- [Goal Engine → Cognitive Budget](/core-concepts/goal-engine)
- [Scheduler → CPI gating](/core-concepts/scheduler#cpi-gating)
- [Monitoring → CPI](/operations/monitoring#cognitive-pressure-index-cpi)
- [Configuration](/operations/configuration#runtime-parameters)
