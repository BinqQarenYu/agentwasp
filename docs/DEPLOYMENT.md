# Deployment

Two scenarios: local laptop and remote VPS. Same installer, different post-install setup.

## Local (laptop / desktop)

```bash
curl -fsSL https://agentwasp.com/install.sh | bash
```

After install, dashboard is at `http://localhost:8080`. Telegram still works if you configured it — the bot reaches Telegram's API outbound, no inbound webhook needed.

If you close your laptop, WASP stops. To make it run on boot:

```bash
sudo systemctl enable docker
# Containers have restart: unless-stopped, so they come up with Docker.
```

## VPS (Ubuntu/Debian)

```bash
ssh root@your-vps
curl -fsSL https://agentwasp.com/install.sh | bash
```

Onboarding asks for the dashboard URL. If you have a domain pointing at the VPS:

```
DASHBOARD_PUBLIC_URL=https://wasp.yourdomain.com
```

…and put a reverse proxy in front of port 8080 with TLS. Quick recipe (Caddy):

```caddy
wasp.yourdomain.com {
    reverse_proxy localhost:8080
}
```

Restart Caddy. WASP keeps running on internal port 8080; only Caddy is internet-facing.

### Firewall

```bash
sudo ufw allow 22/tcp        # SSH
sudo ufw allow 80/tcp        # HTTP (Caddy / certbot)
sudo ufw allow 443/tcp       # HTTPS
sudo ufw enable
```

Don't expose 8080 directly. Don't expose 5432 or 6379 ever.

## Backup strategy

`wasp backup` creates a tarball with `.env`, postgres dump, and `data/`. Cron it:

```cron
0 3 * * * /usr/local/bin/wasp backup >> /var/log/wasp-backup.log 2>&1
```

Then sync the tarball offsite:

```bash
# Example with rclone to S3
rclone copy /opt/wasp/backups remote:wasp-backups --max-age 30d
```

## Resource sizing

| RAM | Notes |
|---|---|
| 4 GB | Minimum. Tight when nodriver + Postgres + agent-core all hot. |
| 8 GB | Comfortable. Default profile fits. |
| 16 GB+ | Required if using local LLM via the `local-llm` compose profile. |

| CPU | Notes |
|---|---|
| 2 vCPU | Works. Browser screenshots are the bottleneck. |
| 4 vCPU | Recommended. |

| Disk | Notes |
|---|---|
| 10 GB | Minimum (Docker images + small data). |
| 50 GB+ | Recommended for long-running instances (memory tier grows, screenshots accumulate). |

## Updating

```bash
wasp update
```

Pulls the git branch (no force, no rebase), rebuilds containers, restarts, runs `wasp health`. Volumes and `.env` are preserved.

If `wasp update` fails: see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#update-issues).

## Multi-instance

You can run more than one WASP on the same host by setting `WASP_INSTALL_DIR` to different paths and using different `DASHBOARD_PORT` values:

```bash
# Instance 1
curl ... | bash -s -- --install-dir /opt/wasp-prod
# Edit /opt/wasp-prod/.env: DASHBOARD_PORT=8080

# Instance 2
curl ... | bash -s -- --install-dir /opt/wasp-staging
# Edit /opt/wasp-staging/.env: DASHBOARD_PORT=8081
```

Each instance has its own compose project (named after its directory), so volumes don't collide.

## Migrating from prior agent-* deployment

If you already have an `agent-*` deployment (e.g. cloned the source manually before this installer existed), the easiest path:

```bash
# 1. Backup the old install
cd "$OLD_INSTALL_DIR" && docker compose exec agent-postgres pg_dump -U agent agent > /tmp/agent.sql

# 2. Install fresh
curl -fsSL https://agentwasp.com/install.sh | bash

# 3. Stop fresh install's postgres, restore data
wasp stop
docker compose --project-directory /opt/wasp up -d agent-postgres
sleep 5
docker compose --project-directory /opt/wasp exec -T agent-postgres psql -U agent -d agent < /tmp/agent.sql
wasp start
```

Container names will differ (`wasp-agent-core-1` instead of `agent-core`) but data and behavior are identical.
