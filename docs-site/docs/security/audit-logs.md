---
id: audit-logs
title: Audit Logs and Decision Traces
description: AuditLog schema, secret redaction, retention; DecisionTrace forensic record.
---

# Audit Logs and Decision Traces

Two complementary forensic records:

- **AuditLog** — Postgres table; one row per CONTROLLED+ skill call. Durable, queryable, retained for `AUDIT_RETENTION_DAYS` (default 30).
- **DecisionTrace** — Redis key per response, TTL ~24 h. Captures the full guard chain output and which skills were tried/blocked/allowed.

Together they answer: *what did the agent do, and why?*

## AuditLog

```sql
CREATE TABLE audit_log (
    id          UUID PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    event_type  VARCHAR(100),
    source      VARCHAR(50) DEFAULT '',
    action      VARCHAR(200) DEFAULT '',
    input_summary  TEXT DEFAULT '',
    output_summary TEXT DEFAULT '',
    user_id     VARCHAR(50) DEFAULT '',
    chat_id     VARCHAR(50) DEFAULT '',
    latency_ms  INTEGER DEFAULT 0,
    error       TEXT,
    metadata_json JSONB DEFAULT '{}'
);

CREATE INDEX ix_audit_log_chat_id_timestamp ON audit_log (chat_id, timestamp);
```

The composite index is created by `ensure_indexes()` at startup using `CREATE INDEX CONCURRENTLY IF NOT EXISTS`, so existing tables get index upgrades without downtime.

### What gets logged

| Action | Triggered by |
|--------|--------------|
| `skill.shell` | Every `shell` skill call (with redacted command) |
| `skill.self_improve` | Every read/propose/apply/patch/install |
| `skill.gmail` | Every Gmail send/read/delete |
| `skill.task_manager` | Every task create/delete/trigger |
| `skill.agent_manager` | Every sub-agent CRUD |
| `skill.python_exec` | Every `python_exec` invocation |
| `skill.http_request` | Every `http_request` invocation |
| `skill.reminders` | Every reminder create/delete |
| `agent.reset` | Every Panic Reset |
| `goal.created` / `.completed` / `.failed` / `.replanned` | Goal lifecycle |
| `task.started` / `.completed` / `.failed` | TaskGraph step lifecycle |

### Capability-based logging

| Level | Logged |
|-------|--------|
| SAFE | No |
| MONITORED | No |
| CONTROLLED | Yes |
| RESTRICTED | Yes |
| PRIVILEGED | Yes |

## Secret redaction

All input and output summaries pass through `redact()` (`utils/redaction.py`) before writing.

### Global patterns (every audit entry)

| Pattern | Example |
|---------|---------|
| OpenAI keys | `sk-[a-zA-Z0-9]{20,}` |
| Anthropic keys | `sk-ant-[a-zA-Z0-9-]{20,}` |
| Google keys | `AIza[a-zA-Z0-9-_]{25,}` |
| xAI keys | `xai-[a-zA-Z0-9]{20,}` |
| HuggingFace tokens | `hf_[a-zA-Z0-9]{20,}` |
| AWS access keys | `AKIA[A-Z0-9]{12,}` |
| Stripe live keys | `sk_live_[a-zA-Z0-9]{24}` |
| Slack tokens | `xox[bpoa]-[a-zA-Z0-9-]+` |
| SendGrid keys | `SG\.[a-zA-Z0-9]{22}\.[a-zA-Z0-9]{43}` |
| Bearer tokens | `Bearer [a-zA-Z0-9+/=]{20,}` |
| Password patterns | `password[=:]\S+` |

### Shell-specific redaction (v2.6)

The `shell` skill applies an additional command-level redaction via `_redact_command()` before the global redaction:

```python
_REDACT_RE = re.compile(
    r'(sk-[A-Za-z0-9\-_]{20,}|AIza[A-Za-z0-9\-_]{30,}|'
    r'xai-[A-Za-z0-9\-_]{20,}|hf_[A-Za-z0-9]{20,}|'
    r'(?:password|passwd|token|secret|key)\s*[=:]\s*\S+)',
    re.IGNORECASE,
)
```

This catches secrets embedded directly in shell commands (e.g., `curl -H "Authorization: Bearer sk-..."`).

## Automatic retention

`AuditRetentionJob` runs every 6 h. It hard-deletes rows older than `AUDIT_RETENTION_DAYS`:

```bash
AUDIT_RETENTION_DAYS=30  # default
```

Bounded batch deletion to avoid table locks:

```sql
DELETE FROM audit_log
WHERE id IN (
    SELECT id FROM audit_log
    WHERE timestamp < (NOW() - INTERVAL '30 days')
    LIMIT 5000
)
```

Logged as `audit_retention.deleted count=N`.

## Querying

### Dashboard

`/audit` — keyset paginator (cursor = `"{timestamp}|{id}"`). Filter by `chat_id` and date range.

### SQL (direct)

```sql
SELECT timestamp, action, input_summary, output_summary, error
FROM audit_log
WHERE chat_id = '<your-chat-id>'
  AND timestamp > NOW() - INTERVAL '7 days'
ORDER BY timestamp DESC
LIMIT 100;
```

```sql
-- Top 5 most-failing skills in the last 24h
SELECT action, COUNT(*) AS failures
FROM audit_log
WHERE error IS NOT NULL
  AND timestamp > NOW() - INTERVAL '24 hours'
GROUP BY action
ORDER BY failures DESC
LIMIT 5;
```

## Decision Trace

Every response — fast-path, Decision Layer route, or full LLM loop — emits a `DecisionTrace`:

```python
DecisionTrace(
    request_id      = uuid.uuid4(),
    path            = "telegram" | "dashboard",
    chat_id         = "<chat_id>",
    user_text_hash  = sha1(user_text)[:12],
    request_tier    = "simple" | "normal" | "complex",
    detected_language,
    detected_intent,
    allowed_skills,
    blocked_skills,
    guard_actions,             # which guards fired and why
    notes,
    start_ts, end_ts, latency_ms,
)
```

Stored in Redis with TTL ~24 h. Surfaced at `/traces`.

### Reading a trace

When you see surprising behavior, open `/traces` and find the request. Look at:

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

### Common guard reasons

| Reason | Guard |
|--------|-------|
| `intent_gate.no_explicit_intent` | Intent gate dropped a side-effect skill |
| `intent_gate.placeholder_subject` | Intent gate detected a placeholder email subject |
| `action_announcer.unverified_claim` | Announcer stripped a "I sent X" claim |
| `enforce_schedule_honesty.user_text` | Schedule honesty appended a clock-time disclaimer |
| `enforce_schedule_honesty.user_text_daypart` | Schedule honesty appended a daypart disclaimer |
| `enforce_factual_grounding.applied` | Factual grounding replaced a fabricated verdict |
| `sanitize_markdown.link` | Markdown link collapsed to `text (url)` |

## Tamper-evidence

Decision traces and audit log entries can be deleted from Redis/Postgres by anyone with database access. There is no signing or external attestation by default.

For high-stakes deployments, consider:

- OS-level filesystem audit (auditd, tripwire) on the host.
- Mirroring AuditLog entries to an append-only external sink (S3 Object Lock, syslog with checksum chain, etc.).
- Restricting Redis/Postgres access to the agent itself.

## See also

- [Skill Safety](/security/skill-safety) — what triggers an audit entry
- [Logs](/operations/logs) — log surfaces
- [Sandboxing](/security/sandboxing) — redaction patterns
- [Testing and Audit](/security/testing-and-audit) — audit methodology
