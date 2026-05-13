---
id: opportunity-engine
title: Opportunity Engine
description: Detects automation patterns from episodic history; suggests subscriptions or recurring tasks.
---

# Opportunity Engine

`src/scheduler/opportunity.py`. Feature-flagged via `opportunity_engine_enabled` (default `true`).

## What it does

Watches the operator's recent activity in episodic memory and surfaces opportunities to automate recurring patterns. It does not execute anything autonomously — every suggestion appears at `/opportunities` for operator review.

## Detection patterns

The engine looks for five classes of automation opportunity:

| Pattern | Trigger | Suggested action |
|---------|---------|------------------|
| Crypto monitoring | Operator repeatedly asks for the same coin's price | Create a price alert (`subscribe`) |
| News / RSS | Operator regularly fetches the same site for news | Subscribe to its RSS feed |
| Website monitoring | Operator periodically captures the same URL | Create a `monitors` entry |
| Periodic reports | Operator generates the same report manually | Create a recurring task (`task_manager`) |
| API watching | Operator polls the same endpoint | Set up a scheduled job |

## Storage

```
Opportunity(
    opp_type,               -- crypto | news | website | report | api
    description,            -- human-readable summary
    confidence,             -- 0..1
    source,                 -- "episodic_pattern" | "knowledge_graph" | etc.
    related_entities,       -- JSON array of entity references
    action_policy,          -- "suggest" | "auto-execute" (always "suggest" by default)
    status,                 -- "pending" | "accepted" | "rejected" | "expired"
    fingerprint,            -- dedup key
    created_at
)
```

## Deduplication

Each opportunity has a `fingerprint` (e.g., entity + opp_type). Opportunities with the same fingerprint within 48 h are deduplicated. Maximum 2 new opportunities per day to avoid notification storms.

## Operator review

`/opportunities` shows pending suggestions with:

- Description of the pattern
- Confidence
- Suggested action (with parameters)
- Accept / Reject buttons

Accept → executes the suggested action (creates the subscription, task, or monitor). Reject → marks status `rejected`; future opportunities with the same fingerprint will not appear unless cleared.

## CPI gating

Skipped when `agent:cpi_high` is set.

## See also

- [Reflection Engine](/cognitive-systems/reflection-engine) — goal-level insights
- [Scheduler](/core-concepts/scheduler) — recurring tasks
- [subscribe skill](/core-concepts/skills) — RSS feeds and price alerts
