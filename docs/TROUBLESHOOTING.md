# Troubleshooting

## Install issues

### `Docker daemon not reachable`

The installer added you to the `docker` group, but the current shell still uses the old groups. Either:

```bash
newgrp docker
```

…or log out and back in. Then re-run `wasp start`.

### `Port 8080 already in use`

Another service has the dashboard port. Either:

```bash
# Free port 8080
sudo lsof -i :8080
sudo systemctl stop <whatever>

# OR change WASP's port in .env:
echo 'DASHBOARD_PORT=9090' >> /opt/wasp/.env
wasp restart
```

### Build fails: `failed to solve: ...`

Almost always a network / DNS issue while pulling base images. Try:

```bash
docker pull python:3.12-slim
# If that hangs, your DNS or network is the problem.
```

### `git clone` fails on private repo

You hit our placeholder URL. Either:
- Set `WASP_REPO_URL` to your fork: `WASP_REPO_URL=https://github.com/me/wasp.git curl ... | bash`
- Or use local source: `curl ... | bash -s -- --install-method local --local-source /path/to/wasp-source`

## Runtime issues

### Dashboard returns 502 / connection refused

```bash
wasp status
wasp logs agent-core | tail -50
```

Common causes:
- Postgres still starting up. Wait 30 s and try again.
- `DATABASE_URL` mis-set in `.env`. The installer should not touch this — only edit if you know what you're doing.
- `agent-core` crashed. Logs show why.

### Telegram bot doesn't respond

```bash
wasp logs agent-telegram | tail -30
```

Things to check:
- Token format: `NNN:XXXX...` (digits, colon, base64-ish).
- `TELEGRAM_ALLOWED_USERS` includes your numeric ID, OR is empty.
- Bot wasn't blocked by you in the Telegram client.

### Browser screenshots fail

Two engines: nodriver (default, stealth) and Selenium. If you see `[CLOUDFLARE_BLOCKED: ...]`, the site is behind Cloudflare and is challenging the VPS IP. That's a known limit — the agent is supposed to admit it and switch to an alternative source. There is **no fix** without a residential proxy.

For other errors:

```bash
# Reset browser sessions (clears cookies, breaks no logins)
docker volume rm "$(basename "$WASP_INSTALL_DIR")_core-browser-sessions"
wasp restart
```

### `wasp health` reports failures

The output names exactly what failed and a one-line fix. Run it and read the `fix:` lines. If still stuck:

```bash
wasp logs agent-core
wasp logs agent-postgres
wasp logs agent-redis
```

## Update issues

### `wasp update` fails halfway

```bash
# See what state we're in
wasp status

# Roll back to the previous git revision
cd /opt/wasp
git log --oneline -5    # find the previous commit
git checkout <prev-sha>
wasp restart
```

### Migrations fail

Postgres schema migrations run on `agent-core` startup. If they fail:

```bash
wasp logs agent-core | grep -i migrat
```

The fix depends on the error. Common one is a unique-constraint conflict from old data — `wasp backup` first, then `wasp reset` to wipe state and re-apply migrations from scratch.

## Total reset

If everything is broken and you want to start fresh **without losing data**:

```bash
wasp backup                  # save current state
wasp reset                   # stop + remove onboarding marker, KEEP volumes
wasp onboard
wasp start
```

If you want to start fresh **including data**:

```bash
wasp uninstall               # answer Y to "remove data volumes"
curl -fsSL https://agentwasp.com/install.sh | bash
```

## Getting help

- **GitHub issues**: please include `wasp health` output, last 50 lines of `wasp logs agent-core`, and the OS / Docker version.
- **Security**: see [SECURITY.md](SECURITY.md) for responsible disclosure (don't open public issues for security bugs).
