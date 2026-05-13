---
id: debugging
title: Debugging
description: Diagnostic tools for inspecting WASP at runtime.
---

# Debugging

When `/health` doesn't tell you enough and `/traces` doesn't either, drop into the runtime.

## Quick diagnostics

```bash
# Container status
docker compose ps

# Recent logs
docker compose logs agent-core --tail=200

# Health endpoint with full detail
curl -s http://localhost:8080/health | jq

# Queue depths
docker exec agent-redis redis-cli XLEN events:incoming
docker exec agent-redis redis-cli XLEN events:outgoing
docker exec agent-redis redis-cli XLEN events:saccadic
docker exec agent-redis redis-cli LLEN behavioral:pending
```

## Inspecting Redis

```bash
docker exec -it agent-redis redis-cli
```

Useful keys:

| Pattern | Description |
|---------|-------------|
| `agent:self_model` | Living self-model (JSON) |
| `agent:epistemic` | Domain confidence map |
| `agent:cpi` | Cognitive Pressure Index snapshot |
| `agent:cpi_high` | Flag — set when CPI > 80 |
| `agent:dream_state` | Last dream cycle |
| `agent:autonomous_state` | Autonomous goal generator state |
| `agent:integrity_report` | Self-Integrity Monitor output |
| `flow:{chat_id}` | Active flow context lock (TTL 15min) |
| `apikeys` | Hash of model provider keys (encrypted) |
| `agents` | Hash of sub-agent records |
| `goals` | Hash of goal records |
| `custom_tasks` | Hash of recurring tasks |
| `behavioral:pending` | Correction queue (cap 50) |
| `behavioral:rules:cache` | Hot rule cache (TTL 5 min) |
| `kg:node:*` | Knowledge graph node cache |
| `kg:index` | Name-to-id lookup |
| `cb:state:{integration_id}` | Circuit breaker state |
| `config:overrides` | Runtime feature-flag overrides |
| `recovery:*` | Recovery memory FIFO |
| `self_improve:proposals` | Pending self-improve proposals |

```redis
> KEYS agent:*
> HGETALL agents
> XRANGE events:incoming - + COUNT 5
> XPENDING events:incoming agent-core-group
```

## Inspecting PostgreSQL

```bash
docker exec -it agent-postgres psql -U agent -d agent
```

```sql
\dt                                        -- list tables
\d audit_log                               -- describe a table

-- Recent audit entries
SELECT timestamp, action, error
FROM audit_log
ORDER BY timestamp DESC LIMIT 20;

-- Active goals
SELECT id, objective, state, replan_count, autonomy_mode
FROM goals
WHERE state = 'ACTIVE';

-- Behavioral rules
SELECT id, rule_type, description, active, times_applied
FROM behavioral_rules
WHERE active = true;

-- Knowledge graph stats
SELECT entity_type, count(*)
FROM knowledge_nodes
GROUP BY entity_type
ORDER BY count(*) DESC;

-- Slow queries
SELECT query, calls, mean_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC LIMIT 10;
```

## Decision Trace forensics

The most powerful debug tool is `/traces`. Every response — fast-path, Decision Layer route, full LLM loop — emits a trace.

When you see surprising behavior:

1. Note the timestamp.
2. Open `/traces`, filter by chat or timestamp.
3. Inspect:
   - `request_tier` (simple/normal/complex) — what budget the response was given
   - `detected_language` and `detected_intent` — did the classifier read it correctly?
   - `allowed_skills` and `blocked_skills` (with reason)
   - `guard_actions` — every guard that fired and what it did

If the trace shows `intent_gate.no_explicit_intent` for a skill the user clearly intended, the regex needs work. If `enforce_factual_grounding.applied`, the LLM tried to fabricate a verdict and the guard caught it.

## Live event stream

`/live` opens an SSE feed of real-time events: skill calls, guard actions, scheduler ticks, model calls. Useful for "watch what the agent is doing right now" debugging.

## Browser session debugging

Per-session profile dirs live at `/data/browser_sessions/<name>/`. Inspect:

```bash
docker exec agent-core ls /data/browser_sessions/<name>/
docker exec agent-core ls /data/browser_sessions/<name>/
```

To wipe a session and start fresh:

```bash
docker exec agent-core rm -rf /data/browser_sessions/<name>
```

## Self-Improve proposal inspection

```bash
docker exec agent-redis redis-cli HGETALL self_improve:proposals
```

Each proposal has the diff, target file, and gate decision. The dashboard `/self-improve` page renders these with a syntax-highlighted diff viewer.

## Tracing a specific request through the pipeline

To follow a single request end-to-end:

1. Send the request (note the timestamp).
2. `docker compose logs agent-core --since=<timestamp> --tail=500 | grep <request_id>`.
3. The structlog event chain shows: `event_received` → `decision_layer.classified` → `auto_detect.matched` → `context_builder.assembled` → `model_manager.generate` → `skill_executor.execute` → `policy.guards.applied` → `outgoing_published`.

## Performance profiling

For deep performance investigation:

```bash
# Container CPU/memory
docker stats agent-core

# Event loop responsiveness
docker exec agent-core python -c "
import asyncio, time
async def measure():
    start = time.time()
    for _ in range(100): await asyncio.sleep(0)
    return (time.time() - start) * 1000 / 100
print(f'avg sleep(0) latency: {asyncio.run(measure()):.3f} ms')
"
```

The CPI metric (`agent:cpi`) gives you a rolling composite score; > 80 indicates pressure.

## Restart procedures

Targeted restart (least invasive):

```bash
docker compose up -d --force-recreate agent-core
```

Full stack restart:

```bash
docker compose restart
```

Clean rebuild (re-applies persisted patches at startup):

```bash
docker compose down
docker compose build --no-cache agent-core
docker compose up -d
```

## Hard reset (Panic Reset)

When memory is poisoned beyond cleanup:

1. Open `/reset` in the dashboard.
2. Type `RESET WASP` exactly.
3. Confirm. The 17 cognitive tables and 12+ Redis key patterns are wiped; `VACUUM FULL` runs.

API keys, custom skills, and `src_patches/` survive.

## See also

- [Common Errors](/troubleshooting/common-errors) — symptoms and recovery
- [Logs](/operations/logs) — log surfaces
- [Audit Logs](/security/audit-logs) — Decision Trace
