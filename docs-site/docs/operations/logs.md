---
id: logs
title: Logs
description: Where logs live, what gets logged, and how to read them.
---

# Logs

WASP emits structured JSON logs via [structlog](https://www.structlog.org/) with consistent `event=` naming. There are four log surfaces:

| Surface | Where | Purpose |
|---------|-------|---------|
| Container logs | `docker compose logs <service>` | Stdout/stderr of each service |
| Structured logs | `/data/logs/` (volume `agent-logs`) | JSON event stream |
| AuditLog | Postgres `audit_log` table | Per-action audit trail |
| Decision Trace | Redis (TTL ~24h) → `/traces` | Per-response forensic record |

## Container logs

```bash
docker compose logs agent-core --tail=200
docker compose logs -f agent-core           # follow
docker compose logs --since=1h agent-core   # last hour
```

Pipe to `grep` to filter by event:

```bash
docker compose logs agent-core --since=1h | grep behavioral.queue_cap_trimmed
docker compose logs agent-core --tail=500 | grep model_manager.overflow_recovered
```

## Structured logs (`/data/logs/`)

The `agent-logs` volume mounts `/data/logs/` inside `agent-core`. Files there are JSON-line format. Each line has at least:

```json
{
  "timestamp": "2026-04-30T15:00:00Z",
  "level": "INFO",
  "event": "policy.intent_gate.blocked",
  "skill": "gmail",
  "reason": "no_explicit_intent",
  "chat_id": "..."
}
```

Common event names:

| Event | When |
|-------|------|
| `policy.intent_gate.blocked` | Intent gate dropped a side-effect skill |
| `policy.action_announcer.stripped` | Action announcer removed an unverified claim |
| `policy.response_guard.applied` | Response guard fired (schedule honesty / grounding / sanitizer) |
| `behavioral.queue_cap_trimmed` | Behavioral queue evicted items at the 50-cap |
| `behavioral.rule_conflict_detected` | New rule contradicts an existing one |
| `model_manager.overflow_recovered` | Compaction overflow successfully recovered |
| `self_improve.soft_gate_analysis` | Self-improve patch passed/blocked by soft gate |
| `goal_orchestrator.replan_storm` | Goal hit replan storm threshold |
| `cpi.high` | CPI exceeded 80; background jobs paused |
| `boot.model_unreachable` | Boot model liveness ping failed |
| `auto_detect.multi_url_exempt` | Multi-URL aggregator exemption applied |

## AuditLog

Postgres table; recorded for every CONTROLLED, RESTRICTED, and PRIVILEGED skill call. Query directly:

```sql
SELECT timestamp, action, input_summary, output_summary, error
FROM audit_log
WHERE chat_id = '<your-chat-id>'
ORDER BY timestamp DESC
LIMIT 50;
```

Or use the dashboard `/audit` page (keyset pagination, filterable by `chat_id` and date).

Action types recorded:

| Action | Triggered by |
|--------|--------------|
| `skill.shell` | Every `shell` skill call (with redacted command) |
| `skill.self_improve` | Every read/propose/apply/patch/install |
| `skill.gmail` | Every Gmail send/read/delete |
| `skill.task_manager` | Every task create/delete/trigger |
| `skill.agent_manager` | Every sub-agent CRUD |
| `skill.python_exec` | Every `python_exec` invocation |
| `skill.http_request` | Every `http_request` invocation |
| `agent.reset` | Every Panic Reset |
| `goal.created` / `goal.completed` / `goal.failed` | Goal lifecycle |

All input/output summaries pass through `redact()` to strip API keys, tokens, and `key=value` passwords. Shell commands additionally pass through `_redact_command()` before logging.

## Decision Trace

Every response — fast-path, Decision Layer route, or full LLM loop — emits a `DecisionTrace`:

```python
DecisionTrace(
    request_id, path, chat_id, user_text_hash,
    request_tier,                    # simple | normal | complex
    detected_language, detected_intent,
    allowed_skills, blocked_skills,
    guard_actions,                   # which guards fired and why
    notes, start_ts, end_ts, latency_ms,
)
```

Stored in Redis with TTL ~24h. Surfaced at `/traces`.

Each trace tells the full story: which fast-paths matched, which skills the LLM tried, which were blocked and why, which guards modified the response, total latency.

## Reading a trace

When you see a surprising response, open `/traces` and find the request. Look at:

| Field | What it tells you |
|-------|-------------------|
| `path` | `telegram` or `dashboard` |
| `request_tier` | Simple / normal / complex (drives the request budget) |
| `detected_language` | Should match user's language |
| `detected_intent` | Did the classifier read the request correctly? |
| `allowed_skills` | What the LLM tried to call |
| `blocked_skills` | What the policy layer dropped, with reason |
| `guard_actions` | List of `(guard_name, action, reason)` tuples |
| `notes` | Any free-form annotation from the pipeline |
| `latency_ms` | Total response time |

If you see `intent_gate.blocked` for `gmail`, the user message did not match the email-send regex. If you see `enforce_schedule_honesty.applied`, the user requested a clock time and a disclaimer was appended. If you see `factual_grounding.applied`, a fabricated verdict was replaced with an honest fallback.

## Log retention

| Layer | Retention | How |
|-------|-----------|-----|
| Container logs | Docker daemon (host config) | Configure `--log-opt max-size=...` |
| Structured logs (`/data/logs/`) | Operator-managed | Add log rotation cron |
| AuditLog | `AUDIT_RETENTION_DAYS` (default 30) | `AuditRetentionJob` runs daily, bounded batch deletes |
| Decision Trace | ~24h TTL | Redis automatic |
| Memory snapshots | Operator-managed | Backup cron (see [Scaling](/operations/scaling)) |

## See also

- [Monitoring](/operations/monitoring) — health and metrics
- [Audit Logs](/security/audit-logs) — what's logged + redaction
- [Common Errors](/troubleshooting/common-errors) — log patterns by failure
