---
id: agent-architecture
title: Agent Architecture
description: Six services, event-driven flow, and request pipeline.
---

# Agent Architecture

WASP is event-driven and Docker-based. The public install ships six services; place your own reverse proxy in front for TLS.

## Service map

| Service | Image | Host port | Role |
|---------|-------|-----------|------|
| `agent-redis` | `redis:7-alpine` | (internal) | Event bus (Streams), state cache (KV), session store |
| `agent-postgres` | `postgres:16-alpine` | (internal) | Durable storage — **28 tables** |
| `agent-core` | built locally | **8080** | Agent runtime: events, LLM, skills, scheduler, dashboard, 151 HTTP endpoints |
| `agent-telegram` | built locally | (none, long-polls Telegram) | Telegram bridge ↔ Redis Streams |
| `agent-broker` | built locally (root) | (internal) | Privileged Docker-API proxy with endpoint allowlist |
| `agent-ollama` | `ollama/ollama:latest` | (internal) | Local LLM runtime (always present; no models pulled by default) |

Only `agent-core` publishes a port to the host (8080). All other inter-service traffic stays on the private `wasp-net` Docker network.

All app containers run as non-root (UID 1000) except `agent-broker`, which needs root for Docker socket access. The broker enforces an allowlist of Docker API endpoints (`/containers/*/start`, `/stop`, `/restart`, `/logs`, `/inspect`, `/list`); other endpoints are blocked. See [Privilege Boundaries](/security/privilege-boundaries).

:::info Operator `agent-nginx`
The operator-controlled production deployment at agentwasp.com adds an `agent-nginx` container that terminates TLS and serves the landing page + docs. That container is not part of the public OSS install because it bakes in operator-specific SSL paths and server names. For your own setup, put your reverse proxy in front of port 8080.
:::

## Event-driven flow

```
User message (Telegram)
   │
   ▼
agent-telegram polls Telegram API
   │
   ▼ XADD events:incoming
agent-redis (Streams)
   │
   ▼ XREADGROUP (consumer group: agent-core)
agent-core EventBus.consume()
   │
   ▼
EventHandler.handle_message()
   │
   ▼ (per-chat asyncio.Lock serializes concurrent messages)
   │
   ▼ Pipeline (see below)
   │
   ▼ XADD events:outgoing
agent-telegram XREADGROUP
   │
   ▼ Telegram sendMessage API
User receives reply
```

The dashboard chat path runs the same pipeline but bypasses the streams — `chat_direct()` calls `handle_message()` in-process.

## Request pipeline

```
Incoming message
   │
   ▼
1. Per-chat asyncio.Lock                ─ serializes concurrent messages
   ▼
2. Low-Intent Cold-Start Guard          ─ clarification fast-path for
   │                                       short/emoji/context-required input
   ▼
3. auto_detect.py                       ─ 13 deterministic fast-paths
   │                                       (Gmail inbox, reminder list/delete,
   │                                        agent CRUD, YouTube search, etc.)
   ▼
4. Decision Layer                       ─ heuristic classifier:
   │                                       DIRECT_RESPONSE / GOAL /
   │                                       SCHEDULED_TASK / SUB_AGENT / SCRIPT
   ▼
5. Capability Engine                    ─ skipped if auto_detect already ran
   ▼
6. Context Builder                      ─ injects:
   │                                       prime.md, knowledge graph,
   │                                       self-model, epistemic state,
   │                                       temporal observations,
   │                                       procedural memory hints,
   │                                       behavioral rules,
   │                                       episodic history,
   │                                       vector-memory neighbors
   ▼
7. LLM Loop (≤12 rounds)                ─ skill parsing, parallel groups,
   │                                       anticipatory simulation,
   │                                       recovery memory consultation
   ▼
8. Multi-URL Aggregator                 ─ deterministic per-URL outcome
   │                                       when ≥2 browser URLs in batch
   ▼
9. Response Validator                   ─ deterministic post-LLM check
   ▼
10. Response Guard chain                ─ schedule honesty, factual grounding,
   │                                       markdown sanitizer
   ▼
11. Action Announcer                    ─ strips unverified action claims,
   │                                       surfaces hidden failures
   ▼
12. handlers post-processing            ─ final cleanup
   │
   ▼
Outgoing response
```

Each step writes to a `DecisionTrace`. Traces persist in Redis (TTL ~24 h) and are surfaced at `/traces` in the dashboard.

## Telegram and dashboard paths

| Aspect | Telegram | Dashboard |
|--------|----------|-----------|
| Entry | `agent-telegram` polls Telegram API, publishes to Redis | `dashboard/routes/chat.py` (HTTP POST) |
| Authentication | `TELEGRAM_ALLOWED_USERS` allowlist (fail-closed; empty = bridge refuses to start) | session cookie, `DASHBOARD_SECRET`, CSRF token bound to session |
| Concurrency | per-chat `asyncio.Lock` | per-chat `asyncio.Lock` (shared) |
| Pipeline | identical | identical |
| Streaming | message-edit progress (TELEGRAM_PROGRESS) | SSE via `POST /chat/stream` |

Both paths converge at `EventHandler.handle_message()`, so a single regression suite covers both.

## Per-chat lock and request budget

A per-chat `asyncio.Lock` ensures concurrent messages from the same chat are processed serially. This prevents race conditions in chat-scoped state (memory writes, flow lock, last-action tracker).

`_REQUEST_BUDGET` enforces a per-execution cap on LLM rounds based on request tier:

| Tier | Cap (rounds) |
|------|--------------|
| simple | 10 |
| normal | 20 |
| complex | 36 |

Tier is derived from `_COMPLEX_MARKERS_RE` in the user text (agent / daily / schedule / report / etc.). The budget is reset per request.

## Failure recovery

| Failure | Recovery |
|---------|----------|
| LLM error | Retry with progressively shorter history |
| Skill error | Error returned to LLM; can replan |
| DB error | Graceful degradation; in-memory fallback for self-model |
| Redis error | In-memory fallback for non-essential state |
| Context overflow | Progressive truncation in `ModelManager.generate()` |
| Pre-commit syntax error in self_improve | Patch rejected with HTTP 400; no file written |
| Self-improve regression | Timestamped backup at `/data/src_patches/backup_*` allows rollback |
| Container crash mid-message | PEL zombie recovery at startup re-delivers idle messages |
| Goal replan storm | Goal flipped to FAILED with partial output |

## See also

- [Goal Engine](/core-concepts/goal-engine) — TaskGraph execution
- [Skills](/core-concepts/skills) — skill catalog and capability levels
- [Memory](/core-concepts/memory) — memory layers
- [Scheduler](/core-concepts/scheduler) — background jobs
- [Resource Governor](/core-concepts/resource-governor) — rate limits
- [Architecture → Runtime](/architecture/runtime) — boot, perception, execution surfaces
- [Architecture → Execution Pipeline](/architecture/execution-pipeline) — pipeline detail
