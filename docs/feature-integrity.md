# Feature Integrity Matrix — Agent WASP Workforce Management

Every interactive control in the UI maps to a real, server-validated `/api/v1/*` route, server-side RBAC check, and automated test coverage.

| UI Control / Surface | API Endpoint | Service / Store Method | RBAC Permission | Acceptance Test |
| --- | --- | --- | --- | --- |
| KPI Cards (`Active`, `Available`, `Blocked`, `Approval`, `Spend today`, `Resource pressure`) | `GET /api/v1/workforce/summary` | `WorkforceStore.computeSummary()` | Authenticated tenant read | Test #1 |
| Search, Status/Team/Owner/Provider Filters, Saved Views, Sort, Pagination | `GET /api/v1/agents` | `WorkforceStore.filterAgents()` + server sort/slice | Authenticated tenant read | Test #1, #2, #13 |
| Export CSV / JSON | `GET /api/v1/agents/export` | `WorkforceStore.filterAgents()` + audit record | `export:run` | Audited server route |
| `+ Register agent` Modal | `POST /api/v1/agents` | `WorkforceStore.agents.set()` + audit + SSE publish | `agent:write` | Server-validated |
| Bulk Actions (`Assign task`, `Pause`, `Request approval`) | `POST /api/v1/agents/bulk-actions` | `WorkforceStore.executeCommand()` / task creation | `agent:control` | Server-validated |
| Agent Detail Header & 10 Tabs (`Overview`..`Governance`) | `GET /api/v1/agents/:id` & `/agents/:id/{tab}` | Canonical `CanonicalAgent` + RBAC memory filter | Tenant read (`memory:read_restricted` for restricted items) | Test #3, #7, #9 |
| Agent Commands (`Pause`, `Resume`, `Safe stop`, `Retry`, `Request approval`, `Rollback`, `Disable`) | `POST /api/v1/agents/:id/commands` | `WorkforceStore.executeCommand()` + state machine + idempotency cache | `agent:control` | Test #4, #5, #15 |
| Agent Configuration Editor | `PATCH /api/v1/agents/:id/configuration` | Optimistic lock (`expected_version`) + audit | `agent:write` | Test #6 |
| Approval Decision (`Approve` / `Reject`) | `POST /api/v1/approvals/:id/decide` | Updates approval + unblocks agent + audit + SSE | `approval:decide` (Supervisor only) | Server-validated |
| Organization Graph & Team Filter | `GET /api/v1/organization/graph` | Canonical agent/supervisor/handoff graph | Tenant read | Test #7, #8 |
| Node Click Inspector | `GET /api/v1/graph/nodes/:node_id` | Tenant-scoped agent/task/supervisor/knowledge lookup | Tenant read | Test #8 |
| Task Flow & Unowned Task Assignment | `GET /api/v1/tasks/handoffs`, `PATCH /api/v1/tasks/:id` | Handoff cycle/overload detection + completion evidence gate | `agent:write` | Test #12 |
| Graphify Refresh / Atomic Failure Test / Cancel | `POST /api/v1/graph/refresh`, `GET /api/v1/graph/refresh/:job_id`, `POST .../cancel` | Async staging build + atomic snapshot promotion | `agent:write` | Test #10 |
| Integration Connector Sync & Replay | `GET /api/v1/integrations`, `POST /api/v1/integrations/:id/sync` | Idempotent dedup + unblocks dependent agents | `connector:manage` | Test #11 |
| Incident Resolution | `GET /api/v1/incidents`, `POST /api/v1/incidents/:id/resolve` | Updates incident + decrements agent `incidents_open` | `agent:control` | Server-validated |
| Audit Trail Search | `GET /api/v1/audit` | Tenant-scoped append-only audit query | `audit:read` | Test #15 |
| Resource Budgets & Backpressure Dashboard | `GET /api/v1/resources` | Live Node process RSS/heap, workers, queue, 70/80/90% ceilings | Tenant read | Test #14 |
| Real-time Live Sync | `GET /api/v1/events/stream` | Tenant-scoped SSE pub/sub with `entity_version` ordering | Tenant read | Live runtime |
