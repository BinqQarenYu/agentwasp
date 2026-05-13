---
id: common-errors
title: Common Errors
description: Production failure modes — symptoms, causes, and recovery.
---

# Common Errors

This page covers the failures most likely to occur in production. For each: the symptom, the likely cause, and the recovery steps.

## First diagnostics

When something is wrong, start here:

```bash
# 1. Container status
docker compose ps
# Every service should be "healthy" (or "up" for ones without health checks)

# 2. Recent agent logs
docker compose logs agent-core --tail=200

# 3. Health endpoint
curl -s http://localhost:8080/health | jq

# 4. Queue depths
docker exec agent-redis redis-cli XLEN events:incoming
docker exec agent-redis redis-cli XLEN events:outgoing
docker exec agent-redis redis-cli LLEN behavioral:pending
```

If `docker compose ps` shows a service as unhealthy or restarting, jump to the relevant section below.

## Empty or no response

### Agent reads the message but never replies

Likely causes:

1. Model unreachable — API key invalid, provider rate-limited, or network issue.
2. Per-chat lock stuck.
3. Goal/agent loop blocked.

Diagnose:

```bash
docker compose logs agent-core --tail=100 | grep -E "model_manager|generate_failed|TimeoutError"
```

Fix:

- Test the model: open `/models`, find the active provider, click "Test". The dashboard sends a 1-token ping.
- If the test fails, set a working API key: Telegram `/api set <provider> <key>` or in `/models`.
- If goals/agents are stuck, force-tick from `/goals` or `/agents`. As a last resort, restart `agent-core`.

### Agent replies "I cannot help with that" to a clearly valid request

Likely cause: a behavioral rule learned from a prior correction now over-blocks. Or the intent gate rejected a side-effect skill.

Diagnose: open `/traces`, find the request, look at `blocked_skills` and `guard_actions`.

Fix:

- If a behavioral rule is the cause: open `/behavioral-rules`, identify the rule, toggle it OFF or delete.
- If the intent gate is the cause: rephrase the request to include explicit intent (verb + object).

## Failed screenshots

### Browser skill returns `[CAPTURE_VALID: false]` or 🚫 blocked

Likely cause: login wall, captcha, or post-click interference.

Fix:

- For sites that require auth, use a named session: `browser(action="capture", url="...", session="my-session")`. Sign in once via the persistent session; future calls reuse cookies.
- If the capture wall is a captcha, use `deep_scraper` which handles JS-heavy pages with retry.
- Check `/data/screenshots/` for the actual capture file — even when blocked, the file is written for inspection.

### Browser session uses high CPU

Likely cause: stale Chromium session not closed.

Fix: the Idle Reaper daemon closes sessions idle > 300 s. If CPU is still high, manually:

```bash
docker exec agent-core pkill -f chromium || true
```

Then send a new browser request to recreate the pool.

### Browser fails with "shm_size too small"

Fix: verify `docker-compose.yml`:

```yaml
agent-core:
  shm_size: '2gb'
```

Restart `agent-core`.

## SSRF-blocked URL

### User asked for an internal address but got ❌ in the multi-URL aggregator

This is correct behavior. RFC-1918 ranges, loopback, and cloud-metadata addresses are blocked at the SSRF guard in `http_request.py`. The browser, `fetch_url`, and `http_request` skills all enforce this.

If you need to query an internal service, run it from the host shell, not through the agent.

## Schedule confusion

### Asked for "every Monday at 9am", task runs at random times

This is by design. `task_manager` only supports interval scheduling. The response includes a disclaimer ("the task does not run at 9am specifically — `task_manager` only supports interval scheduling"). Workarounds:

- Create the task at the desired wall-clock time so the interval boundary aligns.
- For true cron-style scheduling, use the `cron` integration.
- Use a goal triggered by a reminder (reminders accept absolute timestamps).

### Same task runs twice immediately after creation

Fixed in v2.0+. `next_run_at = created_at + interval` (not `now`). If you still see this on a current build, check `docker exec agent-redis redis-cli HGET custom_tasks <id>` and verify `next_run_at` is in the future.

### Duplicate task created with the same name

Likely cause: deduplication didn't catch it because the names differ in whitespace or case.

Fix: delete duplicates via *"delete the X task"* in Telegram, or per-row in `/scheduler`.

## Email not sent

### Agent says "I'll send the email" but no email arrives

Likely cause: the intent gate or action announcer blocked the send. The agent is narrating an action that was actually dropped.

Diagnose:

- Open `/traces` and find the request. Check `blocked_skills` for `gmail`.
- Open `/audit` filtered to `skill.gmail` — if no row, the skill never executed.

Fix:

- Rephrase the request with explicit intent and content: *"send an email to alice@example.com with subject 'X' and body 'Y'"*.
- If the body was a placeholder, the gate blocked it. Provide the actual content in the user message.
- The action announcer also strips false success claims. Check the latest response — it should now say what actually happened.

### Gmail credentials don't work

Diagnose:

```bash
docker exec agent-redis redis-cli HGETALL gmail:credentials
```

Fix:

- Verify the App Password is correct (16 chars, generated at https://myaccount.google.com/apppasswords).
- IMAP must be enabled in Gmail settings.
- Set credentials via `.env` and restart, or via `/integrations` in the dashboard.

## Goals stuck

### Goal in ACTIVE status for hours with no progress

Likely causes:

1. Replan storm — goal hit `MAX_REPLAN_COUNT` and is stuck.
2. Stability backoff — last execution failed, waiting for backoff.
3. Autonomy gate — step requires confirmation in SEMI/MANUAL mode.

Diagnose:

```bash
docker exec agent-redis redis-cli HGET goals <goal-id>
```

Check `state`, `replan_count`, `stability.backoff_until`, `autonomy_mode`.

Fix:

- For replan storms: archive the goal at `/goals` and recreate with clearer objective.
- For stability backoff: wait for `backoff_until` or manually clear the field.
- For autonomy gate: confirm or change autonomy mode at `/goals`.

### PAUSED goal blocks the agent forever

Fixed in v1.6+. `runtime.tick()` auto-resumes after backoff expires; fails after 10 min stuck. If you see this on a current build, manually delete the goal:

```bash
docker exec agent-redis redis-cli HDEL goals <goal-id>
```

## Docker / container issues

### `agent-core` keeps restarting

```bash
docker compose logs agent-core --tail=80
```

Common causes:

| Error | Fix |
|-------|-----|
| `DASHBOARD_SECRET must be at least 16 characters` | Set in `.env`, restart |
| `Connection refused: postgres` | Wait for `agent-postgres` to be healthy; check its logs |
| `Module not found` | Rebuild image: `docker compose build agent-core --no-cache` |
| `Sandbox initialization failed` | Check `/data/skills/` permissions (UID 1000) |

### All containers up but bot doesn't respond

Likely causes:

1. `TELEGRAM_BOT_TOKEN` invalid.
2. Your Telegram user ID not in `TELEGRAM_ALLOWED_USERS`.
3. `agent-telegram` polling is paused (rare).

Diagnose:

```bash
# Validate token
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"

# Get your user ID
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | jq '.result[].message.from.id'
```

Fix: update `.env`, `docker compose restart agent-telegram`.

### SSL certificate errors

```bash
ls /etc/letsencrypt/live/<domain>/
docker exec agent-nginx nginx -t
```

Fix:

```bash
certbot renew --nginx
docker compose restart agent-nginx
```

## Redis issues

### `events:incoming` backlog growing

Likely cause: `agent-core` not consuming fast enough — usually a crash or stuck loop.

Diagnose:

```bash
docker exec agent-redis redis-cli XLEN events:incoming
docker exec agent-redis redis-cli XPENDING events:incoming agent-core-group
```

Fix: restart `agent-core`. PEL zombie recovery (`xautoclaim` at startup) recovers idle pending entries.

If the backlog is huge and you want to drop:

```bash
docker exec agent-redis redis-cli XTRIM events:incoming MAXLEN 1000
```

### Behavioral queue near cap (≥ 40 of 50)

Symptom on `/health`: Learning Queue panel yellow or red.

Cause: burst of corrections faster than `BehavioralLearnerJob` can process (default 120s).

Diagnose:

```bash
docker compose logs agent-core --since=1h | grep behavioral.queue_cap_trimmed
```

If `dropped > 0`, items were silently evicted.

Fix: wait — the queue self-drains within 5–10 min. If chronic, raise `BehavioralLearnerJob` frequency in code (default 120s → 60s) and rebuild.

## PostgreSQL issues

### Slow queries

Diagnose:

```bash
docker exec agent-postgres psql -U agent -d agent -c "
  SELECT query, calls, mean_exec_time
  FROM pg_stat_statements
  ORDER BY mean_exec_time DESC LIMIT 10;
"
```

Fix:

- The weekly `db_maintenance` job runs `VACUUM ANALYZE`. If you can't wait, run it manually:

  ```bash
  docker exec agent-postgres psql -U agent -d agent -c "VACUUM ANALYZE;"
  ```

- For `audit_log` specifically, the composite index `ix_audit_log_chat_id_timestamp` is created at startup. If missing, run `ensure_indexes()`:

  ```bash
  docker exec agent-core python -c "from src.db.session import ensure_indexes; import asyncio; asyncio.run(ensure_indexes())"
  ```

### `audit_log` is huge

Cause: `AuditRetentionJob` not running or retention too long.

Fix:

- Verify `AUDIT_RETENTION_DAYS` in `.env` (default 30).
- Force a retention pass:

  ```bash
  docker exec agent-core python -c "
  from src.scheduler.audit_retention import AuditRetentionJob
  import asyncio; asyncio.run(AuditRetentionJob()())
  "
  ```

## Model / token usage

### High monthly token bill

Likely causes:

- Background jobs (dream, perception, autonomous) calling the LLM.
- Multi-agent setup with each agent ticking every 15 s.
- Long conversation histories not being compacted.

Diagnose:

- Open `/metrics` for token usage breakdown.
- Open provider dashboard.

Fix:

- Set a cheaper default model.
- Pause background jobs by toggling feature flags in `/config`:
  - `dream_enabled = False`
  - `autonomous_goal_enabled = False`
  - `perception_enabled = False`
- Cap concurrent agents in `governance/governor.py` (default 5).
- Compact long histories: `/wipe_all` for the chat in question.

### `context_length_exceeded` errors

Fixed automatically. `ModelManager.generate()` retries progressively (full → 4 exchanges → 2 → 1). System prompt always preserved. Logged as `model_manager.overflow_recovered`.

If it persists, switch to a larger-context model.

## Disk / filesystem

### `df -h` shows `/home/agent` near full

Diagnose:

```bash
docker system df -v
```

Common culprits:

- `the `core-screenshots` volume/` — browser captures
- `the `core-browser-sessions` volume/` — Chromium profiles
- `the `core-logs` volume/` — structured logs
- `the `postgres-data` volume/` — DB files

Fix:

- Run `disk_cleanup` job (if registered) or manually:

  ```bash
  find the `core-screenshots` volume -mtime +30 -delete
  docker exec agent-postgres psql -U agent -d agent -c "VACUUM FULL;"
  ```

- Check screenshot retention policy in `disk_cleanup.py`.

## Behavioral / Cognitive

### Agent fabricated information on a fresh chat

Symptom: user sends a single-token confirmation, an emoji, or a context-required phrase ("do the same", "again") on a brand-new chat with no prior context, and the agent returns a fabricated answer.

Fixed in v2.6 with the Low-Intent Cold-Start Guard (`_is_low_intent()`). Short messages without an anchor return a clarification fast-path **without invoking the LLM**.

If it persists on v2.6:

- Check the message wasn't prefixed with `[RETRY OF PREVIOUS:` (those bypass the guard).
- Check the chat doesn't have a `last_exchange` anchor in chat memory.

```bash
docker exec agent-redis redis-cli HGET chat:{chat_id}:last_exchange text
```

### Tracking-code lookup hallucinated a delivery status

Symptom: user pastes a postal/courier tracking code; agent responds with a fabricated delivery date or status that's not in the actual page.

Fixed in v2.6 with the entity-proximity verdict check (200-char window). The verdict word (`delivered`, `in transit`, etc.) must appear within 200 chars of the user's tracking code in the actual skill body. UI labels on tracking-site home pages no longer count as evidence.

If it persists on v2.6: verify `enforce_factual_grounding` is reaching the response — check `decision_trace` in `/traces`.

### Multi-URL aggregator marked SSRF-blocked URL as ✅

Was a v2.5 bug — fixed in v2.6. The browser skill returns `success=True` even when its output starts with `Error: URL blocked...`. The aggregator now detects the `Error:` prefix and labels the URL ❌ correctly.

If you still see this on v2.6, file a bug.

### Agent name extracted incorrectly from natural language

Symptom: a request like *"create an agent named News Watcher that monitors RSS feeds every hour"* results in an agent whose name swallows the rest of the sentence.

Fixed in v2.6 with non-greedy `_AGENT_NAME_PATTERNS` and a lookahead stop-set on clause connectors.

Workaround if the regex still misses: quote the name explicitly: *"create an agent named "News Watcher" that monitors RSS feeds"*.

## Self-Improve / patches

### A patch was applied but the change doesn't take effect

Cause: Python modules are loaded once at startup. Hot-copying source files alone is not enough.

Fix: always rebuild + restart after self-improve apply:

```bash
docker compose build agent-core && docker compose up -d agent-core
```

`apply_persisted_patches()` re-applies all `/data/src_patches/` patches at startup, with SHA-256 sidecar verification.

### Self-improve patch was rejected with HTTP 400 syntax error

Fixed in v2.6. `ast.parse()` validates Python syntax before write; the error is returned in the proposal `apply` response. The patch is NOT written. Review the error, fix the proposal, re-submit.

### Critical patch needs to be reverted

Recovery:

1. Find the timestamped backup at `/data/src_patches/backup_*`:

   ```bash
   docker exec agent-core ls /data/src_patches/ | grep backup_
   ```

2. Restore the backup:

   ```bash
   docker exec agent-core cp /data/src_patches/backup_<ts>_<filename> /data/src_patches/<filename>
   ```

3. Rebuild + restart.

## Self-repair recovery

If `agent-core` cannot start:

1. Inspect logs:

   ```bash
   docker compose logs agent-core --tail=200
   ```

2. If a recent patch broke it: list and restore `/data/src_patches/backup_*`.

3. If the database is corrupted: restore from `the `core-backups` volume/`.

4. As a last resort — clean rebuild:

   ```bash
   docker compose down
   docker compose build agent-core --no-cache
   docker compose up -d
   ```

   Persistent data in `the Docker named volumes (see Docker Setup) ` survives.

5. Nuclear option — Panic Reset: open `/reset`, type `RESET WASP`. Wipes all cognitive state, runs `VACUUM FULL`, preserves API keys + custom skills + patches.

## See also

- [Debugging](/troubleshooting/debugging) — diagnostic tools
- [Logs](/operations/logs) — log surfaces
- [Decision Trace](/security/audit-logs#decision-trace) — per-response forensic record
