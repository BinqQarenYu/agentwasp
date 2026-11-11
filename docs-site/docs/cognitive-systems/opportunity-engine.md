---
id: opportunity-engine
title: Opportunity Engine
description: Detects automation patterns from episodic history; suggests subscriptions or recurring tasks.
---

# Opportunity Engine

`src/opportunity_engine.py` runs through `src/scheduler/opportunity.py` every two hours. The job is registered during core startup and skips work when no notification chat is configured or cognitive pressure is high.

## What it does

Watches recent episodic activity, execution reflections, and knowledge-graph insights for recurring workflows. It sends ordinary suggestions to the configured notification chat; it does not directly execute skills in response to a detected pattern.

High-confidence automation opportunities from known sources (`KG` or `pattern`) may be recorded as `draft_goal` and promoted by the scheduler into a goal when confidence is at least `0.80`. The goal uses the orchestrator's configured default autonomy mode, so goal execution remains subject to that mode and the normal goal, skill, and policy controls.

## Detection patterns

The engine combines episodic keyword matching and event clustering with reflection and knowledge-graph signals. The legacy keyword path recognizes:

| Pattern | Trigger | Suggested action |
|---------|---------|------------------|
| Crypto monitoring | Operator repeatedly asks for the same coin's price | Create a price alert (`subscribe`) |
| News / RSS | Operator regularly fetches the same site for news | Subscribe to its RSS feed |
| Website monitoring | Operator periodically captures the same URL | Create a `monitors` entry |
| Periodic reports | Operator generates the same report manually | Create a recurring task (`task_manager`) |
| API watching | Operator polls the same endpoint | Set up a scheduled job |

## Storage and review

```
Opportunity(
    opp_type,               -- detected workflow / opportunity type
    description,            -- human-readable summary
    confidence,             -- 0..1
    source,                 -- pattern | reflection | KG
    related_entities,       -- JSON array of entity references
    action_policy,          -- suggest_only | draft_goal
    status,                 -- new | seen | pending_goal | goal_created | ...
    fingerprint,            -- dedup key
    created_at
)
```

The `/opportunities/api/list` endpoint exposes stored opportunities and `/opportunities/api/{id}/status` records `seen`, `accepted`, or `rejected`. Updating a status does not itself execute an action. Identical fingerprints are suppressed for 24 hours; notification count is capped at three per day, and the same opportunity type is not suggested again for 48 hours.

## CPI gating

Skipped when `agent:cpi_high` is set.

## See also

- [Reflection Engine](/cognitive-systems/reflection-engine) — goal-level insights
- [Scheduler](/core-concepts/scheduler) — recurring tasks
- [subscribe skill](/core-concepts/skills) — RSS feeds and price alerts
