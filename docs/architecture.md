# Agent WASP — Workforce Management Architecture

## Runtime Architecture
- **Single-process Full-Stack Runtime**: Express 5 (`server.ts`) serves versioned `/api/v1/*` REST endpoints, `/api/v1/events/stream` (SSE), `/healthz`, and the React 19 + TypeScript + Tailwind CSS v4 SPA (via Vite middleware in development and static `dist/` in production) on `0.0.0.0:3000`.
- **Canonical Data Model (`src/shared/types.ts`)**: Every surface (Workforce Command Center, Agent Detail Workspace, Organization & Handoff View, Tasks, Incidents, Audit) reads from one canonical `CanonicalAgent` record keyed by stable `agent_id` (`AG-001`..`AG-018`) and scoped by `tenant_id`.
- **Financial Precision**: All monetary values (`cost_today_cents`, `daily_cost_limit_cents`) are stored in integer minor units (USD cents) and formatted deterministically via `formatCentsUsd()`.

## State Machines & Safety Guarantees (`src/server/stateMachine.ts`)
- **Agent Lifecycle Transitions**: Explicit transition graph across `registering`, `running`, `idle`, `paused`, `blocked`, `degraded`, `failed`, `offline`, `awaiting_approval`, `safe_stopping`, and `disabled`. Invalid transitions return `409 INVALID_TRANSITION`.
- **Idempotency & Concurrency**:
  - `POST /api/v1/agents/:agent_id/commands` requires a unique `idempotency_key` and replays cached results without double execution.
  - `PATCH /api/v1/agents/:agent_id/configuration` requires `expected_version` and returns `409 VERSION_CONFLICT` on stale writers.
  - `POST /api/v1/integrations/:integration_id/sync` deduplicates repeated sync keys.
- **Task Completion Gate**: `PATCH /api/v1/tasks/:task_id` rejects `completed` transitions with `422 COMPLETION_EVIDENCE_REQUIRED` unless completion evidence is non-empty and all acceptance checks pass.
- **Explicit Stale Telemetry**: Agents whose heartbeat exceeds 5 minutes or report `telemetry_available: false` surface `health: 'stale'` rather than fabricated zero metrics.

## Asynchronous Graphify Refresh
- `POST /api/v1/graph/refresh` returns `202 Accepted` immediately and executes a staged async build (`running` → `validating` → `promoted` | `failed`).
- On validation failure, the previous valid `GraphifySnapshot` remains intact and `last_error` is recorded cleanly.

## Security & Access Control (`src/server/security.ts`)
- Every `/api/v1/*` endpoint extracts tenant and actor context (`X-Tenant-Id`, `X-Actor-Role`, `X-Actor-Id`, `X-Actor-Name`, `X-Correlation-Id`) and enforces server-side RBAC (`supervisor`, `operator`, `auditor`, `viewer`).
- Cross-tenant queries return `404` and never leak records across tenants (`tenant-wasp-ops` vs `tenant-external-beta`).
- Restricted memory entries are omitted server-side for roles without `memory:read_restricted`.
- Regex secret redaction strips bearer tokens and API key patterns from text fields and audit payloads.

## Persistence Boundary
- **Current AI Studio Runtime**: Uses an in-memory canonical `WorkforceStore` (`src/server/store.ts`) seeded with the 18-agent Agent WASP operational roster plus 1 isolated external-tenant agent for zero-config execution and deterministic automated tests.
- **Production Boundary**: Replace `WorkforceStore` maps with PostgreSQL (with Row-Level Security on `tenant_id`, append-only audit partitions, and transactional outbox) and Redis (for distributed idempotency keys, rate limits, and SSE fan-out across replicas).
