---
id: configuration
title: Configuration
description: prime.md, feature flags, and runtime overrides.
---

# Configuration

WASP has three configuration entry points:

1. **`.env`** — startup-time environment variables (Pydantic-validated). See [Environment Variables](/getting-started/environment-variables).
2. **`prime.md`** — operator override prompt injected at the top of every system prompt. Writable at runtime.
3. **`config:overrides`** (Redis) — runtime feature-flag overrides applied at startup.

This page covers (2) and (3).

## prime.md

`prime.md` is mounted at `/data/config/prime.md` inside the container. It is injected at the **top** of every system prompt — before WASP's built-in identity block, before memory injection, before everything.

Use it to:

- Set persona / tone preferences
- Define operator-specific rules ("never auto-trade", "always quote sources", etc.)
- Override default behavior in edge cases

### Editing

Two ways:

**Dashboard:** open `/config`. Left column is the prime.md editor (Ctrl+S to save). Changes take effect on the next message — no restart required.

**Filesystem (inside the container):**

```bash
docker exec -it agent-core nano /data/config/prime.md
```

The volume `core-config` is writable at runtime; no rebuild needed for `.md` content (HTML/Jinja templates and `prime.md` reload from disk on each use).

### prime.default.md

`prime.default.md` is the canonical reference copy. The two files MUST be byte-identical at release time. Validate:

```bash
docker exec agent-core diff /data/config/prime.md /data/config/prime.default.md
```

Empty output is required. If you've edited `prime.md`, copy the changes back to `prime.default.md` so future installs ship in sync.

### Sections

`prime.md` is structured into 10 numbered sections covering: identity, intent boundaries (safety), tool rules, scheduling honesty (safety), side-effect policy (safety), failure honesty, task rules, language rules, output format, and the operator override slot.

The **Safety** sections are load-bearing — they back the deterministic policy guards in `src/policy/`. Modifying them affects how the agent reasons about side-effects, scheduling, and grounding.

## Feature flags

The `/config` page right column lists 12 boolean feature flags grouped into 4 categories.

### Autonomy

| Flag | Default | Effect |
|------|---------|--------|
| `dream_enabled` | `true` | Dream cycle (gated by inactivity) |
| `autonomous_goal_enabled` | `true` | Autonomous goal generator job |
| `perception_enabled` | `true` | Background crypto perception |

### Execution

| Flag | Default | Effect |
|------|---------|--------|
| `plan_critic_enabled` | `true` | LLM validation of TaskGraphs |
| `goal_meta_reflection_enabled` | `true` | Per-goal post-mortem |
| `opportunity_engine_enabled` | `true` | Opportunity detection |

### Learning

| Flag | Default | Effect |
|------|---------|--------|
| `behavioral_learning_enabled` | `true` | Behavioral rule learning loop |
| `skill_evolution_enabled` | `true` | Skill pattern synthesis |

### Memory & Safety

| Flag | Default | Effect |
|------|---------|--------|
| `vector_memory_enabled` | `true` | Embedding-based memory retrieval |
| `knowledge_graph_enabled` | `true` | KG extraction + injection |
| `cpi_monitor_enabled` | `true` | Cognitive Pressure Index monitor |
| `integrity_monitor_enabled` | `true` | Self-Integrity Monitor |

### Persistence

Flags toggled in the UI are written to Redis key `config:overrides` (JSON). On every startup, all 13 supported boolean flags are read from this key and applied to `settings` via `setattr()`. Changes survive restarts.

## Runtime parameters

The `/config` page also surfaces read-only runtime parameters:

- Active LLM model
- Active embedding provider
- Resource Governor caps (goals/day, agents, tasks/hour, LLM calls/min, API calls/min)
- Memory injection budgets
- Browser idle timeout
- Audit retention days

These are sourced from `settings`. To change them, edit `.env` and restart.

## Models

Open `/models` to:

- See the active default model and provider
- See the per-provider catalog (no health-gating — full catalog always shown)
- Test connectivity to each provider (sends a 1-token ping)
- Set the default model

The model router (`models/router.py`) classifies each request (vision / code / quick / complex / default) and suggests a model when none is pinned. You can override the suggestion per-request by mentioning the model name in `/model`.

## Integrations

Open `/integrations` to manage 44 named connectors:

- Slack, Discord, GitHub, Telegram (the bridge), Notion, Zapier, Webhook
- Gmail, Google Calendar
- Philips Hue, Sonos, Home Assistant
- Spotify, Shazam
- 1Password (secrets)
- Platform integrations (macOS, Linux, Windows, Android, iOS)
- 30+ more

Each connector exposes a `ConnectorManifest` with available actions and a `risk_level`. Calls are gated by the policy engine and circuit-broken on repeated failures. Circuit breaker state persists in Redis (`cb:state:{integration_id}`) and survives restarts.

## See also

- [Environment Variables](/getting-started/environment-variables) — startup-time config
- [Models](/integrations/api) — how providers are wired
- [Resource Governor](/core-concepts/resource-governor) — the limits
- [Dashboard → Identity](/integrations/dashboard) — agent persona page
