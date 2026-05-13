---
id: capability-evolution-engine
title: Capability Evolution Engine
description: Discovers and registers new capabilities from successful execution traces.
---

# Capability Evolution Engine

`src/scheduler/capability_evolution.py`. Discovers new agent capabilities from successful execution traces and registers them as named, reusable patterns.

## Concept

A "capability" is a named, parameterized procedure that the agent has learned to perform reliably. Examples:

- "summarize a long article" → web_search → fetch_url → render_report
- "back up a directory" → shell tar → file_ops move
- "update a spreadsheet" → http_request → integration_skill

Capabilities are higher-level than skills (which are atomic) and persist as `Capability` rows in Postgres.

## Detection

The job runs hourly. It scans recent successful execution traces in `execution_reflection` and `audit_log`, looking for trace patterns where:

- The same skill sequence repeats across goals (`>= MIN_OCCURRENCES`).
- The sequence has a clear input-output signature.
- The success rate is `>= MIN_SUCCESS_RATE` (default 0.8).

## Storage

```
Capability(
    name,                      -- generated slug
    trigger_keywords,          -- JSON array
    steps,                     -- ordered (skill, parameter mapping)
    success_count,             -- runs that completed
    failure_count,             -- runs that failed
    source_trace_ids,          -- audit log references
    created_at
)
```

## Use during planning

`PlanGenerator` includes the registered capability list in its skill catalog under "Higher-level capabilities". When the operator's request matches a capability's trigger keywords, the planner can call the capability as a single step instead of expanding the full skill sequence — saving planning tokens.

## Operator review

Generated capabilities surface at `/skill-evolution` (shared page with composite skills). The operator can:

- Inspect the steps
- Test by triggering manually
- Approve → promotes to first-class capability used by the planner
- Reject → marks declined; will not regenerate from same pattern within 7 days

## Difference from Skill Evolution

Both engines produce reusable abstractions, but at different levels:

| | Skill Evolution | Capability Evolution |
|--|----------------|---------------------|
| Output | Composite Python skill in `/data/skills/` | `Capability` row in Postgres |
| Granularity | Replaces a multi-skill chain with one Python call | Hints to the planner; still expands at execution |
| Risk | Generated code runs in-process | No code; metadata only |
| Operator review | At `/skill-evolution` | At `/skill-evolution` |
| Default state | Inactive until approved | Inactive until approved |

## Disabling

The engine is opt-in (not auto-registered in `main.py` by default). To register, patch `main.py`:

```python
from src.scheduler.capability_evolution import CapabilityEvolutionJob
scheduler.register("capability_evolution", 3600, CapabilityEvolutionJob())
```

Or feature-flag via `/config`.

## See also

- [Skill Evolution](/cognitive-systems/skill-evolution)
- [Skills](/core-concepts/skills) — atomic catalog
- [Goal Engine](/core-concepts/goal-engine) — how plans use capabilities
