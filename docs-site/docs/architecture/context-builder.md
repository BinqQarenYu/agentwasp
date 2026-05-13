---
id: context-builder
title: Context Builder
description: How memory is assembled into the LLM system prompt.
---

# Context Builder

`memory/context_builder.py` assembles the system prompt for every LLM call. The output is a single string that goes into the model's `system` slot.

## Composition

```
[Top of system prompt]

1. prime.md                                ─ operator override block
2. WASP identity                           ─ name, role, current model
3. Active Flow Lock                        ─ if domain-locked from prior turn
4. Knowledge Graph (per-chat compact)
5. Self-Model                              ─ strengths, known failures, prefs
6. Epistemic State                         ─ high/medium/low confidence domains
7. Temporal Observations                   ─ recent entity changes (prices, etc.)
8. Procedural Memory                       ─ matching procedures as few-shots
9. Behavioral Rules                        ─ all active learned rules
10. Episodic History                       ─ last N exchanges
11. Vector Memory neighbors                ─ semantic similarity to current msg
12. World Model                            ─ EntityState snapshots
13. Skill catalog                          ─ available skills + signatures

[End of system prompt]
```

Each block is optional and gated by feature flags. If a flag is off (e.g., `vector_memory_enabled=false`), the corresponding block is skipped.

## Injection budget

To keep prompts manageable, each layer is capped:

| Layer | Cap |
|-------|-----|
| Episodic | adaptive (last N exchanges, sized to remaining budget) |
| Semantic / vector | 5 entries |
| Procedural | 3 procedures |
| Goal-scoped | 5 observations |
| Knowledge graph | (compact format, varies) |
| Behavioral rules | all active rules |
| Self-model | strengths + known failures + active preferences |
| Epistemic state | high/medium/low summary |
| Temporal | recent observations relevant to current text |

Total memory block typically 2–8 KB.

## Adaptive truncation

When the assembled prompt approaches the model's context window, `ModelManager.generate()` retries progressively:

```
full history → keep 4 exchanges → keep 2 → keep 1
```

System prompt is always preserved. Logged as `model_manager.overflow_recovered` on success.

## Per-chat locality

The Context Builder filters memory by `chat_id` so different operators (or different threads) don't see each other's context. The Knowledge Graph is global by design (operator-shared facts), but the episodic, vector, and goal-scoped layers are chat-scoped.

## Goal-scoped context

When a Goal is executing, `build_context(goal_id=...)` adds:

- `GoalMemory.get_observations(goal_id)` — observations recorded during this goal
- `reflection_engine.get_recent(goal_id)` — recent reflection insights

This gives the executor a working memory that doesn't leak into unrelated chats.

## Active Flow Lock

`flow:{chat_id}` Redis key (TTL 15 min). When a chat turn establishes a domain (e.g., crypto price lookup), the next message inherits that domain unless the user explicitly switches.

`is_explicit_domain_switch()` and `is_crypto_recovery_followup()` decide whether to clear the lock. The lock survives failed LLM rounds.

The lock is injected into the system prompt as `[ACTIVE FLOW — CONTEXT LOCK]` so the model knows what conversation it is continuing.

## What the Context Builder does NOT do

- It does not reorder or rerank inside layers — that is `memory/ranking.py`.
- It does not call the LLM for extraction — KG, behavioral, and procedural extraction are separate pipelines.
- It does not run the policy guards — those run on the candidate response.

## Observability

Each invocation logs `event=context_builder.assembled` with: `tokens_estimated`, `episodic_count`, `vector_count`, `procedural_count`, `kg_size`, `behavioral_rule_count`. Visible at `/live` and in structured logs.

## See also

- [Memory](/core-concepts/memory) — layer-by-layer detail
- [Knowledge Graph](/core-concepts/knowledge-graph)
- [Cognitive Systems → Behavioral Learning](/cognitive-systems/behavioral-learning)
- [Cognitive Systems → Temporal Reasoning](/cognitive-systems/temporal-reasoning)
- [Runtime](/architecture/runtime) — boot and perception
