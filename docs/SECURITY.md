# Security

## Threat model

WASP is a self-hosted agent that:
- Holds your LLM provider API keys.
- Holds your Telegram bot token (if configured).
- Has a dashboard with login (HTTP, plain by default).
- Executes shell commands and Python code via skills.
- Reads/writes files in mounted volumes.
- Has access to a Docker socket (for skill orchestration).

If an attacker compromises a WASP instance, they get **everything WASP has access to**. Treat the host as production-sensitive.

## Default posture

The installer:
- Generates `POSTGRES_PASSWORD`, `DASHBOARD_SECRET`, and `MEDIA_SIGNING_SECRET` with `openssl rand -hex` (cryptographically strong).
- Writes `.env` with mode `600` (owner-read only).
- Does **not** mount the Docker socket into `agent-core`. Only the `agent-broker` sidecar has socket access (it runs compose orchestration). Add the socket back to `agent-core` only if you want the integration-manager's auto-restart fallback.
- Refuses to start the Telegram bridge unless `TELEGRAM_ALLOWED_USERS` is set to at least one numeric Telegram user id. There is no public-bot mode; you cannot accidentally expose the agent to anyone with the bot's username.
- Leaves `GMAIL_RECIPIENT_ALLOWLIST` empty by default. If you wire Gmail and don't set this, the agent can be tricked by prompt injection into emailing arbitrary recipients. Set it to your real allowed addresses.

## What the installer does NOT do (you must)

1. **Do not expose port 8080 to the public internet.** Put a reverse proxy (nginx, Caddy, Cloudflare Tunnel) in front, with TLS.
2. **Set a strong dashboard password.** The wizard will accept anything; pick something good.
3. **Rotate API keys** if you suspect compromise. WASP reads them from `.env` — replace and `wasp restart`.
4. **Set `GMAIL_RECIPIENT_ALLOWLIST`** before connecting Gmail. The `gmail send` skill refuses to send to addresses not on the list — leave it empty and any prompt injection can deliver mail to arbitrary recipients.

## Network exposure

Default `docker-compose.yml` exposes only port 8080 (dashboard) on the host. Postgres (5432) and Redis (6379) are on the internal Docker network only.

If you need them externally accessible (e.g. external Postgres client), you must explicitly add a `ports:` mapping. Don't.

## Skills with elevated capability

These skills run with broad permissions:

| Skill | Capability |
|---|---|
| `shell` | Runs arbitrary commands inside `agent-core`. |
| `python_exec` | Runs arbitrary Python inside `agent-core`. |
| `self_improve` | Reads/writes source under `/app/src`. |
| `http_request` | Makes outbound HTTP requests (with SSRF guard against RFC-1918 / metadata endpoints). |
| `browser` | Headless Chromium with full JS. |

The agent's policy layer (`agent/context.py`, `policy/*`) limits when these run, but the core defense is: **only let trusted users prompt the agent**.

## Secrets handling

- `.env` is the single source of truth. Mode `600`.
- Skills must never log API keys. `redaction.py` strips known patterns (Anthropic, OpenAI, Stripe, AWS, Slack, SendGrid) from outgoing text and logs.
- The dashboard never sends raw secrets to the browser; it shows masked values.

## Responsible disclosure

If you find a vulnerability, please **do not open a public GitHub issue**. Email the maintainer (contact in repo metadata) with:
- A description of the issue.
- Steps to reproduce.
- The affected commit/version.

We aim to acknowledge within 72 hours and patch within 7 days for high-severity issues.

## Hardening checklist for production

- [ ] Reverse proxy with TLS in front of dashboard
- [ ] Firewall: deny inbound except 22 (SSH), 80, 443
- [ ] SSH: key-only, no password auth
- [ ] Fail2ban on SSH and the reverse proxy
- [ ] `TELEGRAM_ALLOWED_USERS` set to your numeric IDs only
- [ ] Strong `DASHBOARD_PASSWORD`
- [ ] Regular `wasp backup` (cron the tarball offsite)
- [ ] Monitor `wasp health` (cron + alerting)
- [ ] Rotate API keys quarterly

## Known limits

- The host's user-namespace / SELinux / AppArmor profiles are not configured by the installer. If you need defense in depth, set them up manually.
- The agent has Docker socket access (read-only by default for `agent-core`, rw for `agent-broker`). A skill exploit could plausibly enumerate other containers via the socket.
- Memory tiers (Postgres + Redis) are not encrypted at rest by default. Use full-disk encryption on the host.
