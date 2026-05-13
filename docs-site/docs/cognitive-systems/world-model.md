---
id: world-model
title: World Model
description: EntityState snapshots and StatePrediction.
---

# World Model

The World Model holds the agent's structured view of real-world entities — currently per-entity snapshots and forecasts. It complements the temporal world model (timeline of observations) by storing the *current* state in a typed form.

## Storage

| Table | Purpose |
|-------|---------|
| `EntityState` | Latest snapshot per entity: `current_value`, `previous_value`, `change_pct`, `trend`, `state_metadata`, `last_updated` |
| `StatePrediction` | LLM forecasts: `entity`, `prediction_text`, `horizon`, `confidence`, `model_used`, `created_at`, `expires_at` |

`EntityState` is updated by the `world_model_job` (hourly) which aggregates the latest rows in `WorldTimeline` per entity.

`StatePrediction` rows are created by the dream cycle (when generating forecasts) and the perception job (when predicting near-term moves).

## Entity types

Tracked entities include:

- Crypto assets (BTC, ETH, etc. — via `WorldTimeline` price extraction)
- System metrics (CPU, RAM, latency, error rate, active users)
- User-mentioned entities from the Knowledge Graph

## Trend computation

```
change_pct = (current_value - previous_value) / previous_value * 100
trend = "up" if change_pct > +5%
        "down" if change_pct < -5%
        "flat" otherwise
```

Tunable thresholds in `memory/temporal.py`.

## Browse

Dashboard `/world-model` shows:

- Per-entity table with current value, change %, trend
- Per-entity timeline chart (from `WorldTimeline`)
- Active predictions

## See also

- [Temporal Reasoning](/cognitive-systems/temporal-reasoning) — observation timeline + history queries
- [Knowledge Graph](/core-concepts/knowledge-graph) — entity registry
- [Memory](/core-concepts/memory) — overall memory layers
