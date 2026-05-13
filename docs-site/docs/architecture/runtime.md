---
id: runtime
title: Runtime
description: Boot sequence, perception loop, runtime daemons, model layer.
---

# Runtime

This page documents the runtime surfaces: boot sequence, perception, the model layer, and the auxiliary daemons.

## Boot sequence

`_run_boot_sequence()` in `events/handlers.py` runs system checks on the first message after a fresh start (`agent:is_fresh` flag set):

1. **Telegram connectivity** — verifies the bot can reach Telegram API.
2. **Active model liveness ping** — 8 s timeout, `max_tokens=1`. Reports "live ✓" or "unreachable ✗" with explicit operator guidance when unreachable.
3. **Knowledge graph readiness** — verifies `kg:index` is queryable.
4. **Browser session capability** — verifies Chromium can launch.
5. **Memory subsystem** — verifies Postgres + Redis + memory tree.

The boot message also includes a cognitive-state warning when the system is post-reset, telling the operator that all knowledge has been cleared.

The `agent:is_fresh` flag is cleared only after the response is fully built — a crash mid-boot leaves the flag set so the next message retries the boot.

## Perception

```
Incoming event (Redis Streams)
   │
   ▼ XREADGROUP (consumer group: agent-core)
EventBus.consume()
   │
   ▼
EventHandler dispatches by event_type
   ├─ TELEGRAM_MESSAGE → handle_message()
   ├─ TELEGRAM_COMMAND → handle_command()
   ├─ DASHBOARD_ACTION → handle_dashboard()
   └─ SCHEDULED_JOB → no-op (jobs run inline)
```

Per-chat `asyncio.Lock` serializes concurrent messages from the same chat, preventing race conditions in chat-scoped state (memory writes, flow lock, last-action tracker).

## Auxiliary runtime daemons

### HealthState

`runtime/health_state.py`. Reads `agent:cpi` (computed by `cpi_monitor` job) and decides operating mode:

```
HealthState.evaluate(cpu, memory, latency) →
    HealthState(cpu_percent, memory_percent, latency_ms, mode="full"|"light")
```

When any threshold is exceeded (CPU > 80, memory > 80, latency > 500 ms), `mode="light"`. The handler injects a `[SYSTEM_CONSTRAINT: LIGHT_MODE]` hint into the LLM context so the model favors lightweight tools.

### SaccadicVision

`runtime/saccadic_vision.py`. Background daemon thread (synchronous Redis client, separate thread). Polls `browser:last_content` Redis key every 2 s; SHA-1 hash comparison detects content changes; emits to `events:saccadic` Redis stream (MAXLEN 500).

Heartbeat every 30 cycles (~60 s). Methods: `start()`, `stop()`. Fully fail-open — never blocks execution.

## Model layer

`src/models/manager.py`. 11 providers:

| Provider | Notes |
|----------|-------|
| Anthropic | Claude family |
| OpenAI | GPT family |
| Google | Gemini family |
| xAI | Grok family |
| Mistral | Mistral / Magistral / Devstral |
| DeepSeek | DeepSeek-V series |
| Moonshot / Kimi | Kimi K-series |
| OpenRouter | Aggregator |
| Perplexity | Sonar series |
| HuggingFace | Inference Endpoints |
| Ollama | Local LLM (optional) |

Each provider exposes a catalog. As of v2.5+, catalogs are returned in full regardless of provider health (so you see what *would* be available even if the API key is missing).

API keys live in Redis hash `apikeys`, encrypted by `SecretVault`. Loaded at startup and on provider registration.

### Model router

`models/router.py`. Classifies the request:

```
classify_task(request) → {vision, code, quick, complex, default}
suggest_model(task)    → provider/model
```

Detection rules:

- vision: image in message, screenshot keywords
- code: file extensions, script keywords
- quick: short queries
- complex: long analysis, multi-step

The user can pin a specific model with `/model <name>` from Telegram or `/models` in the dashboard.

### Compaction overflow recovery

`ModelManager.generate()` detects context-length errors via string matching across providers (`context_length_exceeded`, `prompt is too long`, `too many tokens`, etc.). Progressive retry:

```
full history → keep 4 exchanges → keep 2 → keep 1
```

System prompt is always preserved. Logged as `model_manager.overflow_recovered` on success.

### Sovereign Mode

`SOVEREIGN_MODE=true` (default) raises `MAX_SKILL_ROUNDS` to 12 and injects an explicit override block into every system prompt. Used to give the agent deeper reasoning room when needed.

## Process model

`agent-core` runs a single Python process (asyncio event loop). Concurrency:

- Per-chat lock serializes per-chat work.
- Cross-chat work runs concurrently in the event loop.
- CPU-bound work (Chromium, ffmpeg, Postgres queries) runs in subprocesses or thread pools.
- The daemon (`SaccadicVision`) runs in a dedicated thread to avoid coupling to the asyncio loop.

## Resource limits

| Resource | Limit (`docker-compose.yml`) |
|----------|------------------------------|
| Memory | 3 GB (agent-core), 12 GB (ollama if used) |
| CPU | 2.5 cores (agent-core) |
| `shm_size` | 2 GB (agent-core, for Chromium) |

Tune in `docker-compose.yml` for higher loads.

## See also

- [Agent Architecture](/core-concepts/agent-architecture) — services and pipeline
- [Execution Pipeline](/architecture/execution-pipeline) — message-to-response detail
- [Context Builder](/architecture/context-builder) — memory injection
- [Orchestration](/architecture/orchestration) — goal/agent runtime
- [Monitoring → Health](/operations/monitoring)
