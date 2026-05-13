---
id: scaling
title: Scaling and Backups
description: Capacity planning, backup strategy, and disaster recovery.
---

# Scaling and Backups

## Capacity planning

For a single-operator workload (~100 messages/day, no heavy goal generation):

| Resource | Idle | Burst |
|----------|------|-------|
| CPU | 2 cores | 4 cores |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB after 6 months | — |
| Network | ~10 GB/month outbound | — |

For agent teams (5+ concurrent agents): scale CPU proportionally and watch CPI. **Token spend is the practical limit, not infrastructure.**

## What grows over time

| Volume | Growth driver | Mitigation |
|--------|---------------|------------|
| Postgres volume | `audit_log`, `memory_entries`, `world_timeline` | `audit_retention` job, `db_maintenance` (VACUUM ANALYZE), Panic Reset |
| Redis volume | Streams (capped) + state hashes | Streams capped at MAXLEN ~10000 |
| `core-screenshots` | Every browser capture | `screenshot_cleanup` job (30-day retention) |
| `core-browser-sessions` | Chrome profile data | Manual prune of unused named sessions; idle reaper runs every 300s |
| `core-logs` | structlog JSON | Log rotation via Docker daemon |

## Backups

### Easiest path: `wasp backup`

```bash
wasp backup
```

Creates a timestamped tarball under `$WASP_INSTALL_DIR/backups/` containing:

- `postgres.sql.gz` — `pg_dump` of the entire `agent` database
- `vol-*.tar.gz` — one archive per named Docker volume (`core-memory`, `core-config`, `core-skills`, `core-screenshots`, `core-browser-sessions`, `core-shared`, `core-uploads`)
- `manifest.json` — version, timestamp, and volume list

Set up a daily cron:

```bash
echo "30 3 * * * wasp backup >/dev/null 2>&1" | crontab -
```

### Restore

```bash
wasp restore $WASP_INSTALL_DIR/backups/wasp-2026-05-13T03-30.tar.gz
```

The script stops the stack, restores the Postgres dump and every named volume, then restarts. **Restore is destructive** — the current state is overwritten. Take a fresh backup before restoring if you might want the current state back.

### Manual backup script (for custom retention strategies)

```bash
#!/bin/bash
# /etc/cron.daily/wasp-backup-offsite
set -euo pipefail

DEST=/var/backups/wasp/$(date +%F)
mkdir -p "$DEST"

# Use the built-in wasp CLI; it knows the volume layout
wasp backup
LATEST=$(ls -1t "$WASP_INSTALL_DIR/backups"/wasp-*.tar.gz | head -1)
cp "$LATEST" "$DEST/"

# Retention: 30 days
find /var/backups/wasp -maxdepth 1 -mtime +30 -exec rm -rf {} \;

# Off-site copy (your choice — rclone, rsync, s3 cp, etc.)
rclone copy "$DEST" remote:wasp-backups/
```

## Disaster recovery

### If the host disk is lost

1. Restore the backup tarballs to a new host (same Docker version recommended).
2. `docker compose up -d` — Postgres + memory + config are restored from disk; Redis state is rebuilt at startup (PEL recovery, vector index backfill).
3. The boot sequence runs on the first message and reports any subsystems that failed to initialize.

### If a corrupted patch breaks startup

1. SSH to the host.
2. `docker exec agent-core ls /data/src_patches/` — find the offending patch.
3. Restore the most recent `backup_*` for the offending file (timestamped backups are created automatically by `self_improve`).
4. Rebuild + start.

### If memory is poisoned (behavioral runaway, KG contamination)

1. Open `/reset` in the dashboard.
2. Type `RESET WASP` (paste blocked).
3. Confirm. The 17 cognitive tables and Redis state are wiped; `VACUUM FULL` runs.
4. API keys, custom skills, and `src_patches/` survive.

## Upgrade path

```bash
wasp update
```

That command pulls the latest release tarball, rebuilds containers, restarts, and runs the health probe. The build's policy regression suite blocks the upgrade if a regression slipped in.

Manual fallback (if you cloned with `--install-method git`):

```bash
cd $WASP_INSTALL_DIR
git pull
docker compose build
docker compose up -d
```

After upgrade, verify:

- `wasp status` — all healthy
- `wasp health` — green
- A representative test message via Telegram or `/chat`

## Hardening checklist

| Item | Status by default |
|------|-------------------|
| Non-root containers | ✅ All app containers (UID 1000) except broker |
| Docker socket isolation | ✅ Broker allowlist; `agent-core` does NOT mount socket on public default |
| Network isolation | ✅ Private `wasp-net`; only port 8080 published |
| TLS termination | ⚠️ Bring your own reverse proxy (nginx / Caddy / Cloudflare Tunnel) |
| Strong `DASHBOARD_SECRET` | ✅ installer generates 64 random hex chars |
| Strong `MEDIA_SIGNING_SECRET` | ✅ installer generates 64 random hex chars |
| `media_signing_debug=false` | ✅ enforced by `@model_validator` |
| Telegram fail-closed | ✅ bridge refuses to start with empty allowlist |
| Gmail recipient allowlist | ⚠️ You should set `GMAIL_RECIPIENT_ALLOWLIST` before connecting Gmail |
| API key redaction in logs | ✅ via `utils/redaction.py` (covers Anthropic, OpenAI, Google, xAI, Slack, AWS, Stripe, SendGrid, etc.) |
| Shell command redaction | ✅ via `_redact_command()` |
| SSRF protection | ✅ centralized `utils/network_safety.py` with DNS-rebinding + redirect re-validation; applied to `http_request`, `fetch_url`, `scrape`, `browser`, `monitors`, `subscriptions` |
| Path containment in `self_improve` | ✅ `realpath` check |
| Soft Safety Gate on patches | ✅ on critical paths |
| SHA-256 patch integrity | ✅ on persisted patches |
| `self_improve` dry-run | ✅ `dry_run="true"` returns diff + AST verdict without writing |
| AuditLog | ✅ for CONTROLLED+ skills |
| Bounded Redis Streams | ✅ MAXLEN ~10000 on every xadd |
| PEL zombie recovery | ✅ at startup |
| CSRF protection | ✅ session-bound token, Argon2 hashing, 5/5min login rate limit |
| Rate limiting | ✅ via `governance/governor.py` |

## Multi-tenant scaling

WASP is **not** multi-tenant. To run multiple operators:

- Run separate instances on separate VPSes.
- Per-instance API keys, Telegram bots, and dashboards.
- No cross-instance memory sharing.

A multi-tenant rearchitecture would require: per-user authentication, per-user memory namespaces in every table, per-user rate limits, per-user audit logs, strict LLM context isolation. This is significant work, not a flag flip.

## See also

- [Configuration](/operations/configuration) — runtime settings
- [Monitoring](/operations/monitoring) — health and metrics
- [Known Limitations](/known-limitations) — what cannot be scaled
