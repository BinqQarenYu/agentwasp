---
id: monitoring
title: Monitoring
description: Health checks, metrics, and the operational checklist.
---

# Monitoring

## Health endpoint

```bash
curl -s http://localhost:8080/health | jq
```

Returns a JSON snapshot covering: model liveness, Redis, PostgreSQL, browser pool, behavioral queue, CPI. Use this for an external uptime check (UptimeRobot, Pingdom, custom cron).

## /health page

The dashboard `/health` page is the live snapshot for the operator. Panels:

| Panel | What it tells you |
|-------|-------------------|
| Model | Active provider, model name, last ping result |
| CPI | Cognitive Pressure Index 0–100 (composite of active goals, error rate, latency, memory growth, CPU). > 80 sets `agent:cpi_high` and pauses non-critical jobs |
| Memory | Container RSS, growth rate |
| Queues | `events:incoming`, `events:outgoing`, `behavioral:pending` (with cap warnings) |
| Goals | Active count, paused count, error count |
| Integrations | Circuit breaker state per connector (CLOSED / OPEN / HALF_OPEN) |
| Learning Queue | `behavioral:pending` depth with thresholds (yellow ≥ 20, red ≥ 40 of 50 cap) |

## Cognitive Pressure Index (CPI)

CPI is a 0–100 composite metric updated by the `cpi_monitor` job (5 min). Components and weights:

| Component | Weight |
|-----------|--------|
| Active goals | 20% |
| Error rate (last 5 min audit log) | 25% |
| Latency p95 | 20% |
| Memory growth | 15% |
| CPU | 20% |

Thresholds:

- **HIGH > 80** → sets `agent:cpi_high` flag (TTL 10 min). Background jobs (autonomous, dream, perception) check this flag and skip if set.
- **CLEAR ≤ 60** → flag cleared.

Visible at `/health` and `/cognitive`.

## Self-Integrity Monitor

`scheduler/integrity.py` runs every 6 h. It cross-checks:

- Declared self-model `strengths` vs actual skill success rates (`skill_success_rates`)
- Epistemic state drift (sudden confidence changes)
- AuditLog error spikes

Writes a structured `agent:integrity_report` JSON in Redis. Visible at `/cognitive` (Integrity tab). Drift larger than threshold triggers a Telegram alert.

## /metrics page

The dashboard `/metrics` page aggregates the `audit_log` table:

- Latency histograms per skill
- Error rates per skill
- Token usage by provider
- Skill call frequencies

Use it to spot regressions ("yesterday `browser` was 8s p95, today it's 25s") and to catch token-spend anomalies.

## /traces page

For per-response forensic analysis. See [Logs](/operations/logs#decision-trace).

## Recommended weekly checks

1. **Audit log review** — `/audit` filtered to the last 7 days. Look for unexpected `skill.shell` or `skill.self_improve` actions.
2. **Behavioral rules** — `/behavioral-rules`. Disable rules that no longer reflect your preferences. Watch for `behavioral.rule_conflict_detected` warnings in logs.
3. **Model spend** — provider dashboards (Anthropic, OpenAI, etc.). Background jobs (dream, perception, autonomous goals, behavioral learner) consume tokens.
4. **Disk usage** — `docker system df -v` or check the Docker named volumes (`docker volume ls`). Screenshots and browser sessions can grow.
5. **Self-improve proposals** — `/self-improve`. Approve only what you've reviewed; reject the rest.
6. **Goal failures** — `/goals` filtered to status FAILED. Use `Replan` or close the goal.
7. **Container health** — `docker compose ps`; all services should be `healthy`.
8. **VACUUM ANALYZE** is run weekly automatically by `db_maintenance`. If you're seeing slow queries, manually run `VACUUM ANALYZE` and inspect `pg_stat_user_tables`.

## Operational checklist

| Activity | Frequency |
|----------|-----------|
| Verify all containers healthy | Daily |
| Inspect `/audit` for unexpected actions | Weekly |
| Review `/behavioral-rules`; disable stale ones | Weekly |
| Check model spend on provider dashboards | Weekly |
| Review `/self-improve` proposals | Weekly |
| Run external uptime check against `/health` | Always |
| Backup databases and memory | Daily |
| Update OS packages | Monthly |
| Rotate secrets (`DASHBOARD_PASSWORD`, `DASHBOARD_SECRET`, `MEDIA_SIGNING_SECRET`) | Quarterly |
| Rotate model API keys | Per provider's policy |
| Renew TLS certs | Automatic via certbot, verify quarterly |
| Run regression suite | Automatic on every rebuild |
| Forensic audit (mandatory tests) | Weekly |
| Forensic audit (edge + adversarial) | Monthly |
| Forensic audit (state + cross-layer + stress) | Quarterly |

## Alerting

WASP does not ship with a built-in alerting system. Recommended:

- External uptime check on `/health`.
- Telegram-side alerts: the agent itself sends 🤖 notifications for autonomous actions, integrity drift, and threshold breaches.
- For external infrastructure alerts (disk full, host down), use a separate monitoring agent (Prometheus, Datadog, etc.) on the host.

## See also

- [Logs](/operations/logs) — log surfaces and event names
- [Scaling](/operations/scaling) — capacity, backups, recovery
- [Audit Logs](/security/audit-logs) — what's logged
- [Health Dashboard](/integrations/dashboard) — page reference
