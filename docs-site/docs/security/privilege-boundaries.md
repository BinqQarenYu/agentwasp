---
id: privilege-boundaries
title: Privilege Boundaries
description: Broker allowlist, shell restrictions, self-improve safeguards, integration vault.
---

# Privilege Boundaries

This page documents the privilege boundaries between the agent runtime and the host, between the agent and Docker, and between the agent and its own source code.

## Container privileges

| Service | UID | Privileged | Notes |
|---------|-----|-----------|-------|
| `agent-redis` | non-root | No | Standard Redis container |
| `agent-postgres` | postgres user | No | Standard Postgres container |
| `agent-core` | UID 1000 | No | Main runtime; no Docker socket |
| `agent-telegram` | UID 1000 | No | Polling bridge |
| `agent-broker` | root | Yes (Docker socket) | Privileged sidecar with allowlist |
| `agent-nginx` | non-root | No | TLS termination |
| `agent-ollama` | non-root | No | Local LLM (optional) |

Only `agent-broker` runs as root, and only because it needs the Docker socket. Every other container is non-root.

## Broker allowlist

`agent-broker` is a Python service that mounts `/var/run/docker.sock` and proxies a small allowlist of Docker API endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /containers/list` | List containers |
| `GET /containers/{id}/inspect` | Inspect a container |
| `GET /containers/{id}/logs` | Read logs |
| `POST /containers/{id}/start` | Start a stopped container |
| `POST /containers/{id}/stop` | Stop a running container |

All other Docker API endpoints (create, exec, kill, attach, commit, etc.) are blocked. `agent-core` cannot create new containers, mount arbitrary volumes, or execute arbitrary commands inside other containers.

What the allowlist does NOT prevent:

- A compromised existing container can be exploited via `docker exec` if the agent has shell access in that container.
- Restart-loop denial of service via repeated `start`/`stop` calls.
- Container metadata leakage via `inspect`.

The broker is a defense-in-depth layer, not a complete sandbox. Keep `TELEGRAM_ALLOWED_USERS` tight.

## Shell skill (RESTRICTED)

The agent has no root inside the container. `shell` runs as UID 1000:

| Setting | Value |
|---------|-------|
| Default timeout | 60 s |
| Max timeout | 120 s |
| Output cap | 8 000 chars |
| Working dir | `/data` |
| User | `agent` (UID 1000) |

Every invocation:

1. Command passes through `_redact_command()` (strips API keys, passwords).
2. One `AuditLog` row written: `action="skill.shell"`, redacted command, exit code, error, `goal_id`.
3. AuditLog write is fire-and-forget via `asyncio.ensure_future()`.

Shell does NOT bypass the broker — `docker run` from shell still goes through the broker's allowlist (the agent container does NOT have direct Docker socket access).

## File system boundaries

| Path | Access |
|------|--------|
| `/data/` | Read-write (data volumes mounted here) |
| `/app/src/` | Read-only at runtime; `self_improve` can patch via dashboard apply endpoint |
| `/etc/`, `/usr/`, `/bin/`, `/sbin/` | Read-only (root-owned, agent runs as 1000) |
| `/var/run/docker.sock` | Not mounted in `agent-core` (only in `agent-broker`) |
| Host filesystem | Not visible from inside containers |

## Self-Improve skill (PRIVILEGED)

Boundaries:

### Path containment

`_list_files()` and `_read_file()` use `os.path.realpath()` and refuse paths outside the project source tree. Symlinks are resolved before the containment check.

### Syntax validation + backup (v2.6)

Before writing any `.py` file via `POST /api/{proposal_id}/apply`:

1. `ast.parse(content)` validates Python syntax. `SyntaxError` returns HTTP 400 with the error truncated to 120 chars; **no file is written**.
2. `shutil.copy2()` creates a timestamped backup at `/data/src_patches/backup_{ts}_{filename}` before overwrite.

### Soft Safety Gate

Three-tier decision for write/patch/apply_patch actions:

| Tier | Condition |
|------|-----------|
| **BLOCK** | Critical path (sandbox.py, control_layer.py, behavioral.py, response_grounder.py, etc.) AND weakening pattern AND (`is_large_patch` OR `is_dense_patch`) |
| **WARN** | Critical path AND (`is_large_patch` OR `is_dense_patch`) without weakening |
| **ALLOW** | Otherwise |

Diff signals: `patch_length > 2000` → `is_large_patch`; `avg_line_length > 120` → `is_dense_patch`.

Weakening patterns (13 of them) include: `disable sandbox`, `bypass guard`, `skip confirmation`, `_HIGH_RISK_ACTIONS=frozenset()`, etc.

### SHA-256 sidecar integrity

Every persisted patch in `/data/src_patches/` gets a `.sha256` sidecar. `apply_persisted_patches()` at startup verifies the sidecar; mismatch → log warning + skip.

What the safeguards do NOT prevent:

- A subtly-bad patch that passes all checks but introduces a logic regression.
- A patch to a non-critical file that still impacts safety transitively.
- A patch that adds a new attack surface (e.g., a new HTTP endpoint without auth).

## Integration vault

The integration registry (`integrations/registry.py`) enforces:

```
existence → action allowlist → PolicyEngine gate → SecretVault → CircuitBreaker → metrics
```

Secrets stored in `SecretVault` (Redis-backed, encrypted) are never exposed to the LLM. The vault key is derived from `DASHBOARD_SECRET`. The LLM cannot read connector credentials directly; it can only invoke `integration_skill(integration_id, action, params)`, and the registry injects credentials at call time.

Circuit breaker state persists in Redis (`cb:state:{integration_id}`). Failures over the threshold open the circuit; recovery timeout governs HALF_OPEN probing.

## Network boundaries

- `agent-net` is a private Docker network. Inter-service calls stay there.
- Only `agent-nginx` publishes ports (80, 443) to the host.
- No outbound traffic from any container is filtered by default. The agent can reach any external host. SSRF protection is enforced at the application level (not network level).

For air-gap scenarios, configure host-level egress filtering (iptables, host firewall) outside Docker.

## Capability levels recap

| Level | Skills | What it means |
|-------|--------|---------------|
| SAFE | `calculate`, `datetime_skill`, `system_info` | Pure read-only, no audit |
| MONITORED | `web_search`, `fetch_url`, `browser` (read), `http_request` (GET) | Read-only external, no audit |
| CONTROLLED | `gmail`, `reminders`, `task_manager`, `agent_manager`, `notes`, `subscribe`, `monitors` | Side-effects with audit |
| RESTRICTED | `shell`, `python_exec`, `http_request` (POST/DELETE) | Code/HTTP execution, audit + simulation |
| PRIVILEGED | `self_improve`, broker-mediated docker | Self-modification, audit + simulation + soft gate |

In SEMI autonomy mode (default), RESTRICTED and PRIVILEGED skills require operator confirmation. In MANUAL, all skills do. In FULL, none do.

## See also

- [Sandboxing](/security/sandboxing) — container isolation, SSRF, browser
- [Skill Safety](/security/skill-safety) — capability levels, intent gating, action announcer
- [Audit Logs](/security/audit-logs) — what's logged
- [Known Limitations](/known-limitations) — residual risks
