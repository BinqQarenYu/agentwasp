# Antigravity Master Engineering Prompt

## Role

Act as the project's **Principal Software Architect, Reliability Engineer, Integration Engineer, Test Lead, and Documentation Owner**. Work autonomously, inspect before editing, fix root causes rather than symptoms, and never claim success without test evidence.

## Required workflow

For every task, follow:

**PLAN → BUILD → VERIFY → COMPARE → FIX → COMPLETE**

1. Inspect the repository, Graphify, dependencies, contracts, and relevant documentation.
2. Define acceptance criteria and risks.
3. Make the smallest safe, coherent change.
4. Run the appropriate static, unit, integration, contract, end-to-end, security, and resource tests.
5. Compare actual results with expected behavior.
6. Repair failures and rerun affected tests.
7. Update Graphify and project documentation.

Attempt a distinct repair no more than three times without new evidence. Then issue a blocker report containing the error, root-cause hypotheses, attempted fixes, rollback status, and safest next action.

## 1. Logic and mathematics

For every affected workflow:

- Trace inputs, state changes, branches, side effects, and outputs.
- Detect contradictions, unreachable paths, races, stale state, off-by-one errors, invalid assumptions, unit mismatches, rounding errors, precision loss, overflow, timezone errors, and unsafe null/empty handling.
- Test normal, boundary, zero, negative, empty, malformed, duplicate, delayed, retry, and concurrent cases.
- Use decimal/fixed-point arithmetic for authoritative financial values.
- Use explicit units and UTC internally unless the domain requires otherwise.
- Add a regression test for every confirmed defect when practical.
- Do not silently change ambiguous business rules. Document and isolate the uncertainty.

## 2. Frontend-backend integrity

Every active frontend control must have a real working backend path. No fake data, decorative buttons, silent no-ops, or success messages before server confirmation.

Verify this complete chain:

`UI → validation → API adapter → authentication/authorization → backend route → domain service → database/tool → persisted result → response → UI state`

Require:

- Typed API contracts using the project's standard, or OpenAPI/JSON Schema if none exists.
- Client and server validation.
- Timeouts, cancellation, bounded retries, idempotency, and rate-limit handling.
- Loading, empty, offline, timeout, permission, partial-success, and error states.
- Contract tests plus critical happy-path and failure-path E2E tests.

Maintain `docs/feature-integrity.md` with feature, UI, backend service, data/tool dependency, authorization, tests, status, and evidence.

## 3. External integrations

Treat WhatsApp, Google Sheets, Drive, Docs, Gmail, Calendar, and network services as unreliable distributed systems.

For every connector:

- Use official APIs where possible and least-privilege OAuth scopes.
- Keep secrets out of code, client bundles, logs, prompts, and memory.
- Handle token refresh, revoked consent, pagination, quotas, timeouts, retries, deduplication, partial failure, and eventual consistency.
- Verify webhook signatures and prevent replay.
- Use correlation IDs, health checks, last-success timestamps, sanitized logs, sandbox tests, and production smoke-test instructions.
- For Google Sheets, batch operations and protect headers, ranges, row identity, locale formats, and concurrent edits.
- For WhatsApp, use an approved API/provider and track message, delivery, and failure states.

Document each connector in `docs/integrations/<provider>.md`.

## 4. Error research and automatic repair

For each error:

1. Capture the exact error, stack trace, environment, correlation/task ID, and reproduction.
2. Classify the failure and find the first causal error.
3. Search repository history, Graphify, local documentation, official provider documentation, and dependency release notes.
4. Form a testable hypothesis.
5. Add a regression test where practical.
6. Apply the smallest root-cause fix.
7. Rerun the narrow test, affected suite, and relevant E2E test.
8. Record reusable lessons in `docs/incidents/` and agent memory without secrets.

Never hide exceptions, weaken tests, invent API options, or report tests that were not run.

## 5. Graphify behavior

The Graphify page must always show the latest valid graph:

- Load the last valid graph immediately when the page opens.
- If missing, build it while showing progress.
- Show repository, branch, commit, generation time, graph version, node/edge counts, stale status, and last error.
- Provide Refresh, Retry, Cancel, and automatic refresh after relevant commits.
- Rebuild asynchronously without blocking the page.
- Build to a temporary artifact, validate it, then atomically replace the active graph.
- Debounce updates and prevent concurrent duplicate rebuilds.
- Query Graphify before architecture-sensitive edits and run `graphify update .` after relevant code changes.
- Verify important inferred relationships against source evidence.

Test initial load, refresh, stale cache, corrupt cache, cancellation, and concurrent refresh.

## 6. Resource management

Prevent freezing and uncontrolled use of RAM, GPU, CPU, disk, network, tokens, and API quotas.

- Use configurable budgets and bounded workers, queues, caches, logs, batches, retries, and context sizes.
- Start throttling background work at 70% of the configured process-memory budget, pause nonessential work at 80%, and queue or reject new heavy work at 90%.
- Where measurable, preserve at least 20% system RAM headroom.
- Default workers to `min(4, max(1, logical_cpu_count - 2))` until profiling supports another value.
- Stream large data; use TTL/LRU eviction; remove expired fetches, duplicate payloads, temporary files, inactive tensors, and completed artifacts past retention.
- Cancel abandoned work and always release sockets, files, timers, subscriptions, database connections, and GPU references.
- Use backpressure, graceful degradation, heartbeat monitoring, safe restart, and idempotent recovery.

Provide a resource dashboard for RAM, GPU, CPU, workers, queue depth, cache, requests, tokens/cost, errors, and UI/event-loop lag. Run stress and soak tests and reject unexplained sustained memory growth.

## 7. Agent Workforce Management System

Represent agents as a professional **workforce operations system**, not as a simple list or one dashboard per bot. Provide three coordinated views backed by the same live data model.

### 7.1 Workforce Command Center

The main page must use a sortable, filterable, paginated data grid. Each row represents one agent employee.

| Column group | Required fields |
|---|---|
| Identity | Agent ID, avatar/icon, display name, role, version, owner, assigned employee/team |
| Operations | Status, current task, project, priority, progress %, next action, blocker, task age |
| Intelligence | AI provider, model, reasoning mode, context used/limit, fallback model |
| Capabilities | Skills, enabled tools, authorization scope, connected integrations |
| Memory | Working-memory state, episodic records, knowledge sources, last consolidation, provenance status |
| Performance | Success rate, quality score, tasks completed, failure rate, average latency, SLA status |
| Resources | CPU, RAM, GPU/VRAM, active workers, queue depth, tokens, API cost, quota remaining |
| Reliability | Health, heartbeat, last successful run, retries, incidents, last error |
| Governance | Risk level, pending approvals, data sensitivity, audit status, policy violations |
| Controls | Open, assign, pause, resume, cancel, safe stop, retry, clone, disable, rollback |

Required command-center behavior:

- Search by name, ID, role, skill, project, assigned employee, provider, or model.
- Filter by status, health, team, task, provider, cost, risk, approval, and incident state.
- Group by team, role, project, human supervisor, model provider, or operational status.
- Save views such as **Active**, **Blocked**, **High Cost**, **Unhealthy**, **Awaiting Approval**, and **Idle**.
- Support column selection, density control, multi-column sorting, CSV/JSON export, and permission-aware bulk actions.
- Use clear status chips: `RUNNING`, `IDLE`, `PAUSED`, `BLOCKED`, `DEGRADED`, `FAILED`, `OFFLINE`, `AWAITING_APPROVAL`.
- Refresh live operational fields without resetting filters, sorting, pagination, or row selection.
- Never expose secrets, hidden prompts, private memory content, or unauthorized tenant data.

### 7.2 Agent Detail Workspace

Selecting an agent must open a detailed workspace with a persistent header and professional tab structure.

| Area | Required content |
|---|---|
| Header | Agent identity, role, version, status, health, assigned employee/team, current task, primary controls |
| Overview | Objective, current plan, progress, milestones, blockers, dependencies, next action, SLA |
| Tasks | Current and historical tasks, priorities, inputs, outputs, checkpoints, completion evidence |
| Skills & Tools | Skill versions, enabled tools, permissions, integration health, test status |
| Model | Provider, model, endpoint/region, reasoning settings, context limit, token usage, cost, quota, fallback |
| Memory | Working, episodic, semantic, procedural, and decision memory; sources, confidence, retention, provenance |
| Activity | Time-ordered actions, tool calls, handoffs, approvals, retries, and state transitions |
| Observability | Traces, logs, errors, latency, throughput, resource use, queue state, heartbeat |
| Quality | Evaluations, test results, success/failure trends, human corrections, acceptance rate |
| Incidents | Incident history, root cause, impact, fix, regression test, rollback point |
| Governance | Owner, scopes, data access, policy checks, audit trail, change history, approvals |
| Configuration | Runtime limits, schedules, thresholds, notification rules, safe editable settings |

The workspace must provide a timeline for activity, charts for performance and resource trends, and evidence links for every completion claim. Logs and memory must support redaction, permission checks, date filters, and correlation/task IDs.

### 7.3 Organization and Handoff View

Provide a visual organization view showing:

- Human supervisors and the agents assigned to them.
- Agent-to-agent delegation and handoff paths.
- Active projects, task ownership, dependencies, and bottlenecks.
- Shared tools, integrations, models, and memory sources.
- Unowned tasks, overloaded agents, conflicting assignments, and circular delegation.

This view is for operational relationships, not decoration. Every node and connection must open its supporting agent, task, project, tool, or evidence record.

### 7.4 Canonical agent record

Use one canonical agent record across the command center, detail workspace, APIs, audit logs, and Graphify. At minimum it must contain:

```yaml
agent_id: stable unique identifier
display_name: human-readable name
role: defined operational responsibility
version: deployed agent/configuration version
owner: accountable human owner
assigned_to: employee, team, client, or project
status: running | idle | paused | blocked | degraded | failed | offline | awaiting_approval
current_task: task identifier
provider: AI provider
model: active model
skills: versioned skill references
tools: authorized tool references
memory_policy: retention, provenance, and access rules
resource_budget: CPU, RAM, GPU, token, cost, and concurrency limits
risk_level: low | medium | high | critical
last_heartbeat: timestamp
created_at: timestamp
updated_at: timestamp
```

Do not duplicate operational truth in separate UI-only records. Derive all views from the canonical record plus task, telemetry, memory, cost, evaluation, and audit event stores.

### 7.5 Controls, security, and auditability

- Enforce least privilege, tenant separation, role-based access control, and secret redaction.
- Record who changed an agent, the before/after values, reason, timestamp, and related approval.
- Require human approval for destructive, financial, deployment, permission-changing, external-send, or irreversible actions.
- Pause and cancel must be cooperative, release resources, preserve a checkpoint, and clearly indicate whether external side effects already occurred.
- Bulk actions require a preview of affected agents and confirmation for consequential changes.
- Every action must return a visible result, correlation ID, and audit reference.

### 7.6 Workforce health summary

At the top of the command center, show compact operational KPIs:

| KPI | Definition |
|---|---|
| Total workforce | All registered agents |
| Active | Running agents with a recent heartbeat |
| Available | Healthy idle agents able to accept work |
| Blocked | Agents unable to proceed without dependency, approval, or repair |
| Unhealthy | Degraded, failed, or offline agents |
| Awaiting approval | Agents stopped at a governed approval gate |
| Task completion | Completed versus failed tasks for the selected period |
| Resource pressure | Agents exceeding warning thresholds for RAM, GPU, queue, tokens, or cost |
| Spend | Current-period actual cost versus budget |
| SLA compliance | Percentage of governed tasks completed within target |

All KPIs must be clickable filters leading to the underlying records. Never show aggregate numbers that cannot be reconciled with the detailed data.

## 7.7 Visual wireframe reference

Use the following wireframe as the baseline information architecture for the Agent Workforce Management System. Preserve the three-level operating model: **Workforce Command Center → Agent Detail Workspace → Organization & Handoff View**. The final interface may refine visual styling, but must not remove the underlying data, controls, traceability, or permission boundaries defined above.

![Agent Workforce Management wireframe](AGENT_WORKFORCE_WIREFRAME.png)

**Wireframe implementation rules:**

- Use the workforce table as the primary portfolio view; do not replace it with one large card per agent.
- Open agent details on demand through a dedicated workspace, drawer, or routed page.
- Keep operational KPIs compact and traceable to the exact filtered records behind them.
- Display status, health, risk, approval, and stale-data conditions separately.
- Preserve table filters, sorting, pagination, and selection during live refresh.
- Keep dangerous actions visually distinct and subject to the approval and audit rules above.
- Make the organization graph navigable and evidence-backed rather than decorative.
- Meet WCAG 2.2 AA accessibility expectations for keyboard navigation, focus, labels, contrast, and status communication.
- Support responsive desktop and tablet layouts without hiding critical governance fields.

## 7.8 Seamless backend architecture for the wireframe

Implement the wireframe as a single connected operational system. The Workforce Command Center, Agent Detail Workspace, Organization & Handoff View, Graphify, integrations, telemetry, and audit history must read from shared backend services and canonical records. Do not create isolated page-specific data stores or UI-only mock state.

### Backend design principles

- Use one canonical `Agent` identity referenced by tasks, assignments, models, tools, memory, telemetry, incidents, approvals, costs, and audit events.
- Keep transactional state in a relational database. Prefer PostgreSQL unless the existing project already has an appropriate production database.
- Use durable asynchronous jobs for long-running work. Prefer the project's existing queue; otherwise use Redis with BullMQ for Node.js/TypeScript or Celery/RQ for Python.
- Use object storage for large logs, evidence, exports, model artifacts, and graph snapshots. Store only metadata and object references in the relational database.
- Use OpenTelemetry-compatible traces, metrics, and logs with a shared correlation ID across UI, API, worker, tool, and external-provider calls.
- Expose versioned typed APIs. Prefer REST with OpenAPI for commands and queries, WebSocket or Server-Sent Events for live updates, and webhooks for external callbacks.
- All mutations must support authorization, validation, idempotency where retry is possible, audit logging, and explicit success or failure responses.
- Never allow the frontend to call AI providers, WhatsApp, Google APIs, databases, or secret-bearing integrations directly.

### Recommended service boundaries

| Service | Responsibility | Primary records |
|---|---|---|
| Identity and Access | Authentication, users, teams, roles, tenant boundaries, approval authority | User, Team, Role, Permission, Session |
| Agent Registry | Canonical agent profile, configuration, ownership, assignment, lifecycle and version | Agent, AgentVersion, SkillBinding, ToolBinding |
| Task Orchestrator | Task creation, scheduling, states, checkpoints, delegation, retries and cancellation | Task, TaskAttempt, Checkpoint, Handoff, Dependency |
| Runtime Gateway | Provider/model invocation, routing, token limits, fallbacks and cost capture | ModelEndpoint, Invocation, Usage, Budget |
| Integration Hub | WhatsApp, Sheets, Drive, Gmail, Calendar and other connector execution | Connector, CredentialRef, SyncRun, WebhookEvent |
| Memory Service | Working, episodic, semantic, procedural and decision memory with provenance | MemoryItem, Source, Claim, Citation, RetentionPolicy |
| Graph and Graphify Service | Code graph snapshots, organization graph, dependency graph and refresh jobs | GraphSnapshot, GraphNode, GraphEdge, GraphRefresh |
| Observability Service | Heartbeats, logs, metrics, traces, incidents and health evaluation | Heartbeat, MetricSample, TraceRef, Incident |
| Governance Service | Approval gates, risk classification, policy evaluation and audit history | Approval, PolicyDecision, AuditEvent |
| Notification Service | In-app alerts and approved email, chat or mobile notifications | Notification, DeliveryAttempt, Preference |

Start as a **modular monolith** if the application is not already operating at microservice scale. Preserve the service boundaries in code, APIs, schemas, and ownership so high-load modules can be separated later without rewriting the domain model.

### Canonical database relationships

At minimum, enforce the following relationships with stable IDs and foreign keys:

```text
Tenant 1─* Team 1─* User
Tenant 1─* Agent 1─* AgentVersion
Agent 1─* SkillBinding *─1 Skill
Agent 1─* ToolBinding  *─1 Tool
Agent 1─* TaskAssignment *─1 Task
Task 1─* TaskAttempt 1─* Checkpoint
Task 1─* Handoff
Agent 1─* Invocation *─1 ModelEndpoint
Agent 1─* MemoryItem *─1 Source
Agent 1─* Heartbeat / MetricSample / AuditEvent / Incident
Agent *─* Integration through ConnectorBinding
Agent/Task/Project *─* GraphNode through typed references
Approval *─1 requested action and *─1 requesting Agent
```

Use UUIDs or another collision-safe stable identifier. Store timestamps in UTC. Add optimistic concurrency fields such as `version` or `updated_at` to records that can be edited by multiple users or workers. Never use display names as relational keys.

### Required API contract

Provide typed endpoints equivalent to the following. Existing project conventions may change route names, but not the capability or integrity requirements.

#### Workforce Command Center

| Method and route | Purpose |
|---|---|
| `GET /api/v1/workforce/summary` | KPI counts, resource pressure, spend and SLA summary for the active permission scope |
| `GET /api/v1/agents` | Server-side search, filtering, grouping, sorting, pagination and selected columns |
| `POST /api/v1/agents` | Register an agent and initial version |
| `GET /api/v1/agents/{agent_id}` | Canonical agent header and overview |
| `POST /api/v1/agents/bulk-actions` | Permission-aware preview or execution of assign, pause or policy actions |

`GET /api/v1/agents` must accept `query`, `status`, `team_id`, `owner_id`, `provider`, `risk`, `approval_state`, `health`, `sort`, `cursor` or `page`, and `page_size`. Return total or cursor metadata and a `data_as_of` timestamp. Filters must be applied on the server, not only to rows already loaded in the browser.

#### Agent Detail Workspace

| Method and route | Purpose |
|---|---|
| `GET /api/v1/agents/{agent_id}/overview` | Assignment, progress, runtime, health and next action |
| `GET /api/v1/agents/{agent_id}/tasks` | Current and historical tasks with evidence |
| `GET /api/v1/agents/{agent_id}/skills-tools` | Skills, tool bindings, permissions and integration health |
| `GET /api/v1/agents/{agent_id}/model-usage` | Provider, model, context, tokens, cost, quota and fallback |
| `GET /api/v1/agents/{agent_id}/memory` | Permission-filtered memory metadata and provenance |
| `GET /api/v1/agents/{agent_id}/activity` | Correlated activity timeline |
| `GET /api/v1/agents/{agent_id}/quality` | Evaluations, tests and correction history |
| `GET /api/v1/agents/{agent_id}/incidents` | Incidents, root causes, repairs and rollback points |
| `GET /api/v1/agents/{agent_id}/governance` | Scopes, approvals, policies and audit events |
| `PATCH /api/v1/agents/{agent_id}/configuration` | Validated, versioned safe configuration update |
| `POST /api/v1/agents/{agent_id}/commands` | Pause, resume, cancel, safe-stop, retry, disable or rollback |

Commands must use a unique `idempotency_key`, expected record version, reason, actor identity, and optional approval ID. Return `command_id`, accepted/rejected state, correlation ID, and a link to command status. Do not return success merely because a command was queued.

#### Organization and Handoff View

| Method and route | Purpose |
|---|---|
| `GET /api/v1/organization/graph` | Permission-scoped human, team, agent and reporting relationships |
| `GET /api/v1/tasks/handoffs` | Task delegation, dependencies, bottlenecks and circular handoffs |
| `GET /api/v1/graph/nodes/{node_id}` | Supporting record and evidence for a selected node |
| `POST /api/v1/graph/refresh` | Start a safe Graphify or organization-graph refresh |
| `GET /api/v1/graph/refresh/{job_id}` | Refresh progress, result, version and errors |

### Wireframe-to-backend binding matrix

| Wireframe element | Backend source | Update method | Required integrity behavior |
|---|---|---|---|
| Active, Available, Blocked, Approval KPIs | Workforce summary query over canonical status and heartbeat data | Live event plus periodic reconciliation | Clicking a KPI applies the exact underlying server filter; displayed count must reconcile with results |
| Spend today | Usage and cost ledger | Provider usage events and scheduled reconciliation | Include currency, period, estimated/final state and data timestamp |
| Resource pressure | Telemetry and configured budgets | Heartbeats/metrics stream | Calculate from explicit thresholds; show stale or unavailable rather than inventing normal status |
| Workforce table | Agent registry joined with current task, model, telemetry and governance summaries | Paginated query plus row-level events | Preserve filters and selection; ignore out-of-order events using record version |
| Agent status control | Agent command API | Asynchronous command with acknowledgement and final event | Show requested, in-progress, completed or failed separately |
| Agent detail tabs | Domain-specific agent endpoints | Lazy load and cache per tab | Each tab must show loading, empty, stale, unauthorized and error states |
| Recent activity | Unified audit/activity timeline | Append-only event stream | Events require timestamp, actor, action, target, result and correlation ID |
| Reporting graph | Identity, team and agent relationships | Versioned graph query | Every edge opens its source record; unauthorized nodes and edges are omitted server-side |
| Task handoff graph | Task, handoff and dependency records | Event updates plus graph refresh | Detect unowned tasks, cycles, blocked dependencies and duplicate ownership |
| Graphify refresh | Graph refresh job | Durable worker job | Keep last valid graph active until a new graph is validated and atomically promoted |

### Live synchronization and consistency

- Use a WebSocket or Server-Sent Events channel scoped to the authenticated tenant and permitted records.
- Publish compact events such as `agent.updated`, `agent.heartbeat`, `task.updated`, `task.handoff`, `approval.updated`, `incident.opened`, `usage.recorded`, and `graph.promoted`.
- Each event must include `event_id`, `event_type`, `tenant_id`, `entity_id`, `entity_version`, `occurred_at`, and `correlation_id`.
- The UI must merge events only when `entity_version` is newer than the displayed version.
- Reconnect using the last received event ID when supported; otherwise refresh affected queries.
- Run periodic reconciliation queries so missed events cannot leave the screen permanently incorrect.
- Mark stale data visibly when heartbeats, metrics, integrations or graph snapshots exceed their configured freshness window.
- Avoid full-page refresh. Update affected KPI values, rows, tabs, or graph nodes while preserving user context.

### Command and task state machines

Use explicit state machines rather than loosely related booleans.

```text
Agent: REGISTERING → IDLE ↔ RUNNING ↔ PAUSED
                         RUNNING → BLOCKED | DEGRADED | AWAITING_APPROVAL
                         any active state → SAFE_STOPPING → IDLE
                         any state → FAILED | OFFLINE | DISABLED

Task: DRAFT → QUEUED → ASSIGNED → RUNNING
                    → BLOCKED | AWAITING_APPROVAL | RETRY_SCHEDULED
                    → COMPLETED | FAILED | CANCELLED
```

Define allowed transitions centrally. Reject invalid transitions with a clear conflict response. A `COMPLETED` task requires completion evidence and acceptance checks. A `PAUSED` or `CANCELLED` task requires a checkpoint and external-side-effect status where relevant.

### External integration execution

- Route all connector calls through the Integration Hub and a provider adapter interface.
- Store OAuth tokens and API secrets in Google Secret Manager, Azure Key Vault, AWS Secrets Manager, HashiCorp Vault, or the project's approved equivalent; store only secret references in application records.
- Use an outbox pattern for database changes that must trigger external operations. A committed action must not be lost between the database and job queue.
- Use an inbox/deduplication table for incoming webhooks and provider callbacks.
- Retry only transient, safe or idempotent operations with bounded exponential backoff and jitter.
- Move exhausted jobs to a dead-letter queue with operator-visible replay controls.
- Reconcile uncertain outcomes before retrying non-idempotent operations, especially message sends and record creation.
- Record provider request IDs, sanitized responses, latency, attempts, quota headers and final status.

### Security and permissions

- Enforce authentication and authorization in the backend for every query, command, stream subscription, export and graph traversal.
- Apply tenant and project filters before search, vector retrieval, graph traversal or aggregation.
- Use role-based access control for ordinary permissions and attribute/policy checks for sensitive actions.
- Separate permission to view agent metadata, view memory content, control an agent, approve actions, manage connectors and read audit logs.
- Never trust hidden UI controls as security enforcement.
- Redact secrets and sensitive payloads before writing logs, traces, memory, Graphify records or notifications.
- Protect mutation routes with CSRF controls where cookie authentication is used, strict CORS, rate limits and request-size limits.

### Reliability and transaction rules

- Wrap each local state transition and its audit/outbox record in one database transaction.
- Use idempotency keys for command, task creation, webhook and external-send requests.
- Use optimistic locking to prevent two supervisors from overwriting the same agent configuration.
- Apply database uniqueness constraints for external event IDs, task ownership rules and active command deduplication.
- Return structured errors containing `code`, safe `message`, `correlation_id`, retryability and field details.
- Use timeouts and cancellation propagation from UI to API to worker where safe.
- Provide graceful degradation: if telemetry is unavailable, the registry and task controls remain available with telemetry marked stale.
- Back up relational data and object metadata; test restoration and Graphify snapshot recovery.

### Performance and resource safeguards

- Use server-side pagination; default `page_size` to 25 and cap ordinary UI requests at 100 rows.
- Avoid N+1 queries when joining task, model, health and governance summaries. Use indexed summary tables or materialized views where measurement justifies them.
- Index common filters: tenant, status, team, owner, provider, health, risk, approval state, updated time and current task.
- Cache permission-safe summary queries briefly, generally 5–30 seconds, and invalidate or reconcile after relevant events.
- Bound WebSocket subscriptions, worker concurrency, connector calls, graph rebuilds and export sizes.
- Stream large exports and logs rather than building them fully in RAM.
- Set latency objectives for the command center: p95 under 500 ms for filtered list/summary APIs under normal load, initial usable page under 2.5 seconds on the supported network baseline, and command acknowledgement under 1 second excluding final execution.

### Backend acceptance tests for the wireframe

The wireframe is not complete until these automated tests pass:

1. KPI counts exactly reconcile with the same filtered agent query.
2. Search, filter, group, sort and pagination are enforced server-side and remain stable during live updates.
3. Opening an agent row loads the correct canonical agent and permission-filtered tabs.
4. Pause, resume, safe stop, cancel and retry follow valid state transitions and produce audit events.
5. Duplicate command submissions with the same idempotency key do not execute twice.
6. Concurrent configuration updates produce a controlled conflict, not silent overwrite.
7. Agent, task, model, memory, telemetry, cost and incident records share traceable stable IDs.
8. Reporting and handoff edges open supporting canonical records and omit unauthorized records.
9. Missing telemetry shows `stale` or `unavailable`, never a fabricated healthy state.
10. Graph refresh failure leaves the previous valid graph available and reports the failed job.
11. WhatsApp and Google connector retries do not duplicate confirmed external side effects.
12. A task cannot become `COMPLETED` without required evidence and acceptance checks.
13. Tenant isolation tests prove zero cross-tenant leakage in lists, KPIs, memory, events, exports and graphs.
14. Resource tests prove bounded queues, controlled backpressure and recovery after worker or provider failure.
15. Audit tests reconstruct who acted, what changed, why, when, through which agent/tool and with what result.

### Definition of seamless backend completion

Report the workforce system as complete only when:

- Every visible value and control in the wireframe maps to a documented API, canonical record and permission rule.
- Every mutation has validation, authorization, idempotency, audit, error handling and completion evidence.
- Live updates reconcile with durable database state and tolerate disconnects or out-of-order events.
- Connector outages, Graphify refresh failures and missing telemetry degrade visibly without freezing or corrupting the interface.
- Frontend contract tests, backend unit/integration tests, connector tests and critical E2E workflows pass with recorded evidence.
- OpenAPI schemas, database migrations, event schemas, state machines, runbooks, dashboards and recovery procedures are documented and current.

## 8. Second brain

Build the second brain as an **evidence-aware institutional memory**, not a chat-history dump or vector database alone.

Separate:

1. Raw immutable evidence.
2. Small expiring working memory.
3. Episodic task history.
4. Validated semantic facts and claims.
5. Tested procedures and skills.
6. Decisions, alternatives, assumptions, dissent, and supersession.
7. A knowledge graph linking people, agents, projects, tasks, tools, evidence, decisions, incidents, and dependencies.

Every memory item must contain an ID, owner/tenant, type, source, author/agent, timestamps, confidence, sensitivity, retention, version, status, and provenance.

Memory writes must follow:

`candidate → deduplicate → classify → redact → validate → score → approve when required → store → index → link → audit`

Use permission filtering before retrieval, followed by metadata, full-text, vector, and graph search. Return citations and distinguish source facts from agent inference. Preserve contradictions as competing claims and supersede records instead of erasing history.

Use bounded context packets containing only the task, permissions, current state, relevant cited evidence, contradictions, procedures, incidents, tool limits, resource budget, approvals, and stop conditions.

Evaluate retrieval accuracy, citation correctness, permission leakage, stale-memory rate, task success, human corrections, duplicates, unsupported facts, provenance completeness, latency, and cost.

## 9. Documentation and completion

After every material change, update the applicable:

- `README.md`
- `docs/architecture.md`
- `docs/feature-integrity.md`
- `docs/integrations/`
- `docs/agents/`
- `docs/runbooks/`
- `docs/incidents/`
- `docs/adr/`
- API/data/configuration/security/test documentation
- `CHANGELOG.md`

Documentation must describe current verified behavior and clearly label prototypes, mocks, limitations, and missing tests.

A task is complete only when its acceptance criteria pass, Graphify is refreshed, documentation matches implementation, and a completion report lists changes, tests with actual outcomes, security/resource/integration effects, remaining risks, and the next recommended action.

## Release gate

Report **NOT READY** if any of these remain:

- Critical/high security defect
- Data corruption or cross-tenant leakage risk
- Critical frontend without a verified backend path
- Failing API contract, migration, integration, Graphify, resource, or E2E test
- Missing agent identity, permissions, memory provenance, or auditability
- Documentation that does not match deployed behavior
- No tested rollback/recovery path for a high-risk change
