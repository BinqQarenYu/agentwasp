---
id: docker-setup
title: Docker Setup
description: Service inventory, volumes, and network architecture.
---

# Docker Setup

WASP runs as a Docker Compose stack. The public install ships six services; you can optionally place your own reverse proxy in front (any nginx / Caddy / Cloudflare Tunnel works — the project itself doesn't ship one for public installs because it would require your domain and your TLS certs).

## Service inventory

| Service | Image | Default port | Role |
|---------|-------|--------------|------|
| `agent-redis` | `redis:7-alpine` | 6379 (internal) | Event bus (Streams) + state cache (KV) |
| `agent-postgres` | `postgres:16-alpine` | 5432 (internal) | Durable storage — 28 tables |
| `agent-core` | built locally | 8080 → host | Agent runtime — events, LLM, skills, scheduler, dashboard |
| `agent-telegram` | built locally | (none, long-polls Telegram) | Telegram bridge ↔ Redis Streams |
| `agent-broker` | built locally (root) | (internal) | Privileged Docker-API proxy with endpoint allowlist |
| `agent-ollama` | `ollama/ollama:latest` | (internal) | Local LLM runtime (always present; no models pulled by default) |

Only `agent-core` publishes a port (8080) to the host by default. Everything else stays on the private `wasp-net` Docker network.

All app containers run as non-root (UID 1000) except `agent-broker`, which needs root for Docker socket access. The broker enforces an endpoint allowlist (`/containers/*/start`, `/stop`, `/restart`, `/logs`, `/inspect`, `/list`) — other Docker API endpoints are blocked. See [Privilege Boundaries](/security/privilege-boundaries).

:::info Operator-only `agent-nginx`
The operator-controlled production deployment at `agentwasp.com` also ships an `agent-nginx` container that terminates TLS and serves the landing page + docs. That container is **not** included in the public OSS tarball because it bakes in operator-specific SSL cert paths and the `agentwasp.com` server name. For your own public-facing dashboard, place your own reverse proxy in front of port 8080.
:::

## Volumes

The public installer uses Docker named volumes (not host bind mounts). Operators can switch to bind mounts by editing `docker-compose.yml`.

| Volume | Mount path (in container) | Purpose |
|---|---|---|
| `redis-data` | `/data` | Redis state |
| `postgres-data` | `/var/lib/postgresql/data` | Postgres data files |
| `core-memory` | `/data/memory` | Memory tree, `src_patches/` backups |
| `core-logs` | `/data/logs` | Structured logs |
| `core-config` | `/data/config` | `prime.md` (writable at runtime) |
| `core-backups` | `/data/backups` | `wasp backup` snapshots |
| `core-shared` | `/data/shared` | Shared file uploads |
| `core-screenshots` | `/data/screenshots` | Browser captures |
| `core-uploads` | `/data/chat-uploads` | Dashboard uploads |
| `core-browser-sessions` | `/data/browser_sessions` | Persistent Chromium profiles |
| `core-skills` | `/data/skills` | Custom Python skills |
| `ollama-models` | (Ollama default) | Local LLM weights |

## Network

- All inter-service traffic stays on the `wasp-net` Docker network.
- Only `agent-core` publishes a host-facing port (8080 → 8080 by default).
- The broker container has access to `/var/run/docker.sock` but enforces an allowlist on the Docker API.
- Public default does **not** mount `/var/run/docker.sock` into `agent-core`. The integration-manager's auto-restart fallback prints a manual `docker restart` instruction instead.

## Build

```
agent-core image build:
  Stage 1 — Tailwind CSS build (node-based)
  Stage 2 — Python 3.12-slim base + Chromium (for browser skill) + Docker CLI
  Stage 3 — Run tests/test_policy_regressions.py at build time
```

The build fails if any policy regression check fails. This is intentional — it's the production gate that prevents shipping a build with broken response-binding or truth-layer code.

## Reverse proxy (optional but recommended)

For any deployment exposed to the public internet:

```nginx
# Example nginx config (place on your host or upstream)
server {
    listen 443 ssl http2;
    server_name your-domain.example.com;
    ssl_certificate     /etc/letsencrypt/live/your-domain.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.example.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then set `DASHBOARD_PUBLIC_URL=https://your-domain.example.com` in `.env` so the dashboard knows its external address (used for media-link signing).

## Common operations

### Rebuild after a code change

```bash
docker compose build agent-core
docker compose up -d agent-core
```

`docker compose restart` does NOT pick up new image content — always `up -d` after a rebuild. (HTML/Jinja templates are an exception: they reload from disk per request, so a hot-copy + restart is enough for template changes.)

### Recreate one service

```bash
docker compose up -d --force-recreate <service>
```

### View logs

```bash
wasp logs                            # tail agent-core
docker compose logs <service> --tail=200
docker compose logs -f <service>     # follow
```

### Inspect Redis

```bash
docker exec agent-redis redis-cli
> XLEN events:incoming
> HGETALL agents
> KEYS "agent:*"
```

### Inspect Postgres

```bash
docker exec agent-postgres psql -U agent -d agent
> \dt              # list 28 tables
> SELECT count(*) FROM audit_log;
```

## See also

- [Environment Variables](/getting-started/environment-variables)
- [Configuration](/operations/configuration)
- [Scaling](/operations/scaling)
