---
id: dashboard
title: Dashboard
description: 151 HTTP endpoints organized into five sidebar sections — page-by-page reference.
---

# Dashboard

The dashboard is served by `agent-core` on port 8080. Authentication uses session cookies; the admin user is configured via `DASHBOARD_USER` / `DASHBOARD_PASSWORD`. For public-facing deployments, put your own reverse proxy (nginx, Caddy, Cloudflare Tunnel) in front of port 8080 for TLS.

**151 HTTP endpoints** across the dashboard, organized into five sidebar sections plus a handful of utility surfaces.

## Authentication

Sign in at `/login`. Sessions are signed by `DASHBOARD_SECRET` (the installer generates 64 random hex chars). Logout at `/auth/logout`.

CSRF tokens are session-bound — the validator compares the stored token with the session ID, not just the token value. Argon2 password hashing; login rate-limited to 5 attempts per 5 minutes.

If `DASHBOARD_USER` / `DASHBOARD_PASSWORD` are not set on first boot, a temporary password is generated and printed once to stderr — `wasp logs | grep dashboard.credentials` to retrieve it.

## Sections

### Dashboard

| Page | URL | Purpose |
|------|-----|---------|
| Overview | `/overview` | Top-level snapshot — active goals, recent traces, queue depths |
| Chat | `/chat` | Interactive chat (SSE streaming via `POST /chat/stream`) |
| Cmd | `/cmd` | Command palette — search across pages |

### Configurations

| Page | URL | Purpose |
|------|-----|---------|
| Identity | `/identity` | Agent name, born_at, total_xp, level, persona |
| Config Center | `/config` | `prime.md` editor + 12 feature flags + runtime parameters |
| Skills | `/skills` | Installed skills, capability levels, custom skill files |
| Models | `/models` | Providers, default model, full catalog (no health-gating) |
| Integrations | `/integrations` | 40+ connectors with circuit-breaker state |

### System

| Page | URL | Purpose |
|------|-----|---------|
| Scheduler | `/scheduler` | Recurring tasks, custom tasks, monitor list |
| Agents | `/agents` | Sub-agent CRUD, status, message log |
| Goals | `/goals` | Active and historical goals with TaskGraph visualization |
| Subscriptions | `/subscriptions` | RSS feeds and price alerts |
| Health | `/health` | CPI, memory, queues, model liveness |

### Governance

| Page | URL | Purpose |
|------|-----|---------|
| Self-Improve | `/self-improve` | Pending AI proposals — diff viewer, Apply/Reject, applied history |
| Behavioral Rules | `/behavioral-rules` | Learned rules — view, filter, toggle, delete |
| Audit | `/audit` | Per-action AuditLog with keyset pagination |
| Reset | `/reset` | Panic Reset (hard confirmation gate) |

### Observability

| Page | URL | Purpose |
|------|-----|---------|
| Traces | `/traces` | Per-response forensic trace |
| Live | `/live` | SSE feed of real-time events |
| Metrics | `/metrics` | Latency histograms, error rates, token usage |
| Cognitive | `/cognitive` | Self-model, epistemic state, integrity report |
| Memory | `/memory` | Memory tree browser |
| Knowledge Graph | `/knowledge-graph` | Force-directed canvas of entities and relations |
| World Model | `/world-model` | Temporal observations, entity timelines |
| Vector Memory | `/vector-memory` | Semantic similarity search |
| Brain | `/brain` | Stats and persona overview |
| Tasks | `/tasks` | Task execution log |
| Skill Evolution | `/skill-evolution` | Composite skill candidates + capability evolution |
| Opportunities | `/opportunities` | Detected automation opportunities |
| State | `/state` | Current execution state |

## Streaming chat (SSE)

`POST /chat/stream` opens a Server-Sent Events stream. Each event is a JSON object:

```
event: progress
data: {"step": 1, "skill": "web_search", "status": "running"}

event: chunk
data: {"text": "Found 12 results. Reading top 3..."}

event: done
data: {"final": "..."}
```

The chat UI (`/chat`) consumes the stream and renders progress + final text incrementally.

## Decision Trace forensics

`/traces` is the dashboard's most valuable forensic page. Every response — fast-path, Decision Layer route, or full LLM loop — emits a trace. The page lists traces with filters (chat_id, time range, request tier) and clicking a row shows the full guard chain output.

See [Audit Logs → Decision Trace](/security/audit-logs#decision-trace) for the schema.

## Self-Improve workflow

1. The agent (or you, manually) proposes a patch via `self_improve(action="propose")`.
2. The proposal lands at `/self-improve` with a diff viewer.
3. You review the diff. If acceptable, click **Apply**:
   - `ast.parse()` validates Python syntax.
   - Soft Safety Gate checks for critical-path weakening.
   - Timestamped backup is created at `/data/src_patches/backup_*`.
   - File is written.
   - The change persists across rebuilds via `apply_persisted_patches()` at startup.
4. Or click **Reject** — proposal is dismissed.
5. After Apply, rebuild and recreate `agent-core` for the change to take effect.

## Reset workflow

`/reset` requires hard confirmation:

- Operator types `RESET WASP` exactly into a `readonly` input (paste blocked at the DOM level).
- Submit → progress console streams each step:
  1. Truncate 17 cognitive tables.
  2. Wipe 12+ Redis key patterns.
  3. Reset agent identity (`born_at` → now, `total_xp` → 0).
  4. Reset self-model to empty `{}`.
  5. Run `VACUUM FULL`.
  6. Write AuditLog entry.
- On completion: green-bordered result card + WaspToast notification.

What survives: API keys, custom Python skills, `/data/src_patches/` backups, prime.md, subscriptions.

## Health page

The single most important page for daily operation. See [Monitoring → /health page](/operations/monitoring#health-page).

## See also

- [Telegram](/integrations/telegram) — alternative interface
- [Audit Logs](/security/audit-logs)
- [Configuration](/operations/configuration)
- [Monitoring](/operations/monitoring)
