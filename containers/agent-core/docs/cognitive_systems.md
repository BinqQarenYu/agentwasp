# WASP Cognitive Systems — Ownership Map

Each cognitive subsystem is documented below with its **producer** (who
writes), **consumer** (who reads), **behavior impact** (what changes user-
facing behavior), and **heartbeat log** (the structured log line you can
grep for to verify it is alive). If a system has no consumer or no impact,
it is decorative and a candidate for removal — **do not let any row stay in
that state without a follow-up issue.**

> Last verified: 2026-04-29. Re-verify quarterly or on any cognitive-system
> change.

---

## Active systems

### Knowledge Graph (KG)

| Field           | Value |
| --------------- | ----- |
| Producer        | `memory.knowledge_graph.extract_from_conversation` — fired post-message in `events.handlers` |
| Consumer        | `agent.context.build_context` injects `[KNOWLEDGE GRAPH]` block into system prompt |
| Behavior impact | Agent recalls user-stated facts ("uso AWS", "trabajo en Cloudflare Workers") in future turns |
| Heartbeat log   | `kg.fire` (logged in `knowledge_graph.py`) |
| Storage         | PostgreSQL `knowledge_nodes` + `knowledge_relations`; Redis cache `kg:node:{id}` |

### Temporal World Model (timeline)

| Field           | Value |
| --------------- | ----- |
| Producer        | `memory.temporal.extract_from_text` post-message |
| Consumer        | `build_context` injects `[OBSERVACIONES TEMPORALES]` block |
| Behavior impact | Detects metric/state changes ("CPU 87%", "deploy succeeded"), surfaces deltas vs prior turn |
| Heartbeat log   | `timeline.fire` |
| Storage         | DB `world_timeline` |

### Visual Memory

| Field           | Value |
| --------------- | ----- |
| Producer        | `memory.visual.store_screenshot` enqueued from `skills.builtin.browser` after capture |
| Consumer        | `dashboard.routes.vector_memory` (search UI); future: image-context recall |
| Behavior impact | Operator can search past screenshots from the dashboard |
| Heartbeat log   | `visual_memory.indexed` |
| Storage         | DB `visual_memory` |

### Goal Memory

| Field           | Value |
| --------------- | ----- |
| Producer        | `goal_orchestrator.executor` on task done/failed (importance 0.55/0.85) — wired in March 2026 (was zero callers before) |
| Consumer        | `agent.context.build_context` (semantic-memory recall path) |
| Behavior impact | Agent learns from past goal outcomes; informs replan |
| Heartbeat log   | `goal_memory.observation_recorded` |
| Storage         | DB `memory_records` (memory_type=GOAL) |

### Behavioral Rules

| Field           | Value |
| --------------- | ----- |
| Producer        | `memory.behavioral.queue_correction` from `events.handlers._detect_correction`; `BehavioralLearnerJob` (every 120s) writes to DB |
| Consumer        | `_behavioral_rules` in `build_context` injects `[REGLAS APRENDIDAS]` block |
| Behavior impact | Agent stops repeating user-corrected mistakes; few-shots + SKILL_POISON dynamic patterns |
| Heartbeat log   | `behavioral.rule_learned`, `behavioral.queued` |
| Storage         | DB `behavioral_rules`; Redis `behavioral:pending` queue |

### Episodic Memory

| Field           | Value |
| --------------- | ----- |
| Producer        | `memory.manager.MemoryManager.store` on every Telegram/dashboard turn |
| Consumer        | `build_context` recent-history; `memory.manager.search` for explicit recalls |
| Behavior impact | Multi-turn coherence; "as I told you yesterday" |
| Heartbeat log   | `memory.episodic_stored` |
| Storage         | DB `memory_records` (memory_type=EPISODIC) |

### Procedural Memory

| Field           | Value |
| --------------- | ----- |
| Producer        | `memory.procedural.abstract_procedure` after multi-skill solutions (>2 rounds, >2 unique skills) |
| Consumer        | `build_context._procedural` injects relevant procedures as few-shot hints |
| Behavior impact | Agent reuses successful procedures for similar tasks |
| Heartbeat log   | `procedural.abstracted` |
| Storage         | DB `procedural_memory` |

### Self-Model

| Field           | Value |
| --------------- | ----- |
| Producer        | `agent.self_model.record_message_processed` post-message |
| Consumer        | `build_context._self_model` injects `[SELF-MODEL]` block |
| Behavior impact | Agent reasons about its own strengths/known failures; informs sub-agent decisions |
| Heartbeat log   | `self_model.updated` |
| Storage         | Redis `agent:self_model` + file backup `/data/memory/self_model.json` |

### Epistemic State

| Field           | Value |
| --------------- | ----- |
| Producer        | `agent.epistemic.record_outcome` post-skill-execution |
| Consumer        | `build_context._epistemic` injects `[ESTADO EPISTÉMICO]` block |
| Behavior impact | Per-domain confidence; agent expresses uncertainty in low-confidence domains |
| Heartbeat log   | `epistemic.adjusted` |
| Storage         | Redis `agent:epistemic` |

### Dream / Autonomous / Digest jobs

| Field           | Value |
| --------------- | ----- |
| Producer        | `scheduler.dream.DreamJob` (every 3600s), `scheduler.autonomous.AutonomousGoalGeneratorJob` (every 1800s) |
| Consumer        | Telegram user (proactive notifications); KG/self-model writes feed downstream consumers |
| Behavior impact | Proactive consolidation + autonomous goal generation when thresholds breached |
| Heartbeat log   | `dream.cycle_complete`, `autonomous.evaluated` |
| Storage         | DB `dream_log`; Redis `agent:dream_state`, `agent:autonomous_state` |
| Gating          | CPI flag `agent:cpi_high` skips both jobs to protect under load |

### Cognitive Pressure Index (CPI)

| Field           | Value |
| --------------- | ----- |
| Producer        | `scheduler.cpi_monitor.CognitiveLoadMonitorJob` (every 300s) |
| Consumer        | autonomous, dream, perception jobs read `agent:cpi_high` Redis flag |
| Behavior impact | Throttles cognitive jobs when CPI > 80 |
| Heartbeat log   | `cpi.computed` |
| Storage         | Redis `agent:cpi_high` (TTL 600s) |

### Self-Integrity Monitor

| Field           | Value |
| --------------- | ----- |
| Producer        | `scheduler.integrity.SelfIntegrityMonitorJob` (every 21600s = 6h) |
| Consumer        | Dashboard `/cognitive#integrity` tab |
| Behavior impact | Surfaces drift between self-model claims and observed skill rates |
| Heartbeat log   | `integrity.report_written` |
| Storage         | Redis `agent:integrity_report` |

---

## Read/Write Audit

A subsystem is **healthy** when it has both a producer that fires
regularly AND a consumer that materially affects user-facing behavior.

| System              | Producer Active | Consumer Active | Health |
| ------------------- | --------------- | --------------- | ------ |
| Knowledge Graph     | ✅              | ✅              | OK     |
| Temporal Timeline   | ✅              | ✅              | OK     |
| Visual Memory       | ✅              | ⚠ dashboard only | partial |
| Goal Memory         | ✅ (wired Mar 2026) | ✅           | OK     |
| Behavioral Rules    | ✅              | ✅              | OK     |
| Episodic Memory     | ✅              | ✅              | OK     |
| Procedural Memory   | ✅              | ✅              | OK     |
| Self-Model          | ✅              | ✅              | OK     |
| Epistemic State     | ✅              | ✅              | OK     |
| Dream / Autonomous  | ✅              | ✅ (Telegram)   | OK     |
| CPI Monitor         | ✅              | ✅              | OK     |
| Self-Integrity      | ✅              | ⚠ dashboard only | partial |

Two `partial` rows are expected — they are operator-facing tooling
without LLM-loop consumers. They are not decorative.

---

## How to verify a system is alive

```bash
# Tail the relevant heartbeat log
docker compose logs agent-core --tail=200 -f | grep '<heartbeat name>'

# Or read the storage row directly
docker compose exec agent-postgres psql -U postgres wasp -c \
  "SELECT count(*), max(updated_at) FROM <table>"
```

If the heartbeat log is silent for >24h on a system marked OK above, it
is a regression — either the producer broke or the trigger condition
changed. Investigate before assuming the system is fine.
