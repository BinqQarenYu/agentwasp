---
id: reflection
title: Reflection
description: Goal post-mortems and dream-cycle reflection — operator-facing view.
---

# Reflection

WASP runs reflection at two levels: per-goal post-mortems and the dream-cycle narrative. Both produce structured artifacts the operator can review.

## Per-goal post-mortem

`scheduler/goal_meta_reflection.py`. Runs once when a goal completes or fails.

### Output (one row in `execution_reflection`)

| Field | Meaning |
|-------|---------|
| `intent` | Inferred from the goal objective |
| `skills_used` | Ordered list of skills the executor ran |
| `duration_ms` | Total wall-clock time |
| `success` | True/False |
| `efficiency_score` | 0–1, weighted by skills used vs minimum needed, retries, replans |
| `issues` | JSON array of issues encountered (timeouts, replans, gate hits) |
| `insight` | Short LLM-generated note |
| `suggestion` | Improvement proposal |
| `pattern_key` | Normalized for SkillPattern detection |
| `recurring_pattern` | True if this matches a known SkillPattern |

### How it's used

- **Operator** reads it at `/goals` (per-goal detail page) and `/cognitive` (recent reflections).
- **Context Builder** injects recent reflection insights into similar future goals.
- **Skill Evolution** uses `pattern_key` and `recurring_pattern` to detect candidates for composite-skill synthesis.

Maximum 3 reflection rows per goal. Older reflections are pruned by `execution_reflection_pruner` (daily).

## Dream-cycle reflection

`scheduler/dream.py`. Runs during the dream cycle (idle-time, gated by inactivity + CPI).

### Output (one row in `dream_log`)

| Field | Meaning |
|-------|---------|
| `reflection` | Short LLM narrative on the day's activity |
| `improvements_proposed` | Count of proposed self-improvements |
| `improvements_json` | The actual proposals (each is a diff) |
| `memories_consolidated` | Episodic entries promoted to semantic |
| `kg_nodes_added` | New entities discovered |
| `prefetch_done` | Whether crypto prices were prefetched |

### How it's used

- **Operator** reads the narrative at `/cognitive`.
- **Self-Improve dashboard** shows proposals from `improvements_json` for review at `/self-improve`.
- **Failure-pattern analysis** updates `self_model["known_failures"]`.

The dream cycle does NOT modify code on its own — every improvement is operator-gated.

## Reading reflections

| Where | What |
|-------|------|
| `/goals` (per-goal page) | Goal Meta-Reflection for that goal |
| `/cognitive` (Reflections tab) | Recent goal reflections + dream narratives |
| `/self-improve` | Pending dream-cycle improvements |
| `dream_log` table | Raw history of dream cycles |
| `execution_reflection` table | Raw history of goal post-mortems |

## When reflection runs

| Trigger | What runs |
|---------|-----------|
| Goal completes | Goal Meta-Reflection (one-shot) |
| Goal fails | Goal Meta-Reflection (one-shot) |
| Dream cycle activates | Dream reflection + memory consolidation + improvement proposals |

## Disabling

To turn off reflection entirely:

```bash
GOAL_META_REFLECTION_ENABLED=false
DREAM_ENABLED=false
```

This stops the LLM-cost of reflection. Operator loses post-mortem visibility; self-model accumulates failures less precisely.

## See also

- [Reflection Engine](/cognitive-systems/reflection-engine) — internals
- [Autonomous Goals](/advanced/autonomous-goals) — dream cycle activation
- [Self-Improve](/integrations/dashboard) — proposal review
