---
id: commands
title: Operator Commands
description: Daily commands for using WASP via Telegram and shell.
---

# Operator Commands

Day-to-day operation of WASP happens through four surfaces: the `wasp` CLI, natural language via Telegram, the dashboard, and direct Docker commands. This page is the cheat-sheet.

## `wasp` CLI

Installed at `/usr/local/bin/wasp` by `install.sh`. Run any command from any directory.

```
wasp onboard         Re-run the configuration wizard
wasp start           Start the stack
wasp stop            Stop the stack
wasp restart         Restart all services
wasp status          Container status + health checks
wasp logs [service]  Stream logs (default: agent-core)
wasp health          Run the full health probe suite
wasp update          Pull latest tarball, rebuild, restart, verify
wasp backup          Create a timestamped backup archive (Postgres + volumes)
wasp restore <file>  Restore from a backup archive
wasp reset           Wipe runtime state but keep volumes
wasp uninstall       Remove WASP (asks before deleting data)
wasp help            Show command reference
```

## Telegram

WASP listens on the bot configured by `TELEGRAM_BOT_TOKEN`. Only Telegram user IDs in `TELEGRAM_ALLOWED_USERS` can interact.

### Asking the agent something

Send any message in your natural language. The agent will:

1. Try a deterministic fast-path (e.g., listing reminders, deleting a reminder, listing tasks).
2. Run the Decision Layer to classify intent (5 strategies: DIRECT_RESPONSE / GOAL / SCHEDULED_TASK / SUB_AGENT / SCRIPT).
3. If the request needs planning, create a Goal; if it is a one-shot answer, run the LLM loop directly.
4. Apply the policy guards before sending the reply.

### Built-in Telegram commands

| Command | Effect |
|---------|--------|
| `/start` | Concise welcome (EN/ES/PT/FR) with capability summary |
| `/help` | Full command reference |
| `/ping` | Reachability check |
| `/status` | System status |
| `/model` | Show active model + provider |
| `/skills` | List registered skills |
| `/skill <name>` | Invoke a skill directly |
| `/schedule` | Show scheduled tasks |
| `/memory` | Memory subsystem info |
| `/snapshot` | Save current state to a memory snapshot |
| `/introspect` | Capability + health snapshot |
| `/monitor <url>` | Watch a URL for changes |
| `/broker` | Integrations management |
| `/api set <provider> <key>` | Persist a model API key |
| `/openclaw <action>` | Manage dynamic skills from the ClawHub registry |

### Telegram input types

| Input | Handling |
|-------|----------|
| Text | Full pipeline |
| Photo | Vision-capable model receives the image bytes |
| Document | Treated as file; can be referenced in subsequent messages |
| Voice note | Transcribed via OpenAI Whisper, then passed through the text pipeline |
| Video / video note | First frame extracted with `ffmpeg`, then vision pipeline |

### Live progress

While the agent is working on a response, it edits a single status message in place (TELEGRAM_PROGRESS events) showing the current step. Only one progress message is created per turn; on edit failure, it is dropped silently.

## Common natural-language workflows

### Create a recurring task

> *every 6 hours, fetch BTC price and email me a report*

The agent detects the recurring keyword, routes to `task_manager`, creates a custom task with the literal instruction. The Custom Task Runner job (60 s) triggers it at the next interval.

:::warning Schedule semantics
`task_manager` only supports interval scheduling. Clock-time requests (`every Monday at 9am`) and daypart phrases (`in the morning`) are surfaced via an automatic disclaimer; they are NOT honored literally. To approximate a fixed clock time, create the task at the desired wall-clock time so the interval boundary aligns.
:::

### Create a reminder

> *remind me to back up the disk in 3 hours*

Reminders are one-shot or recurring. The Reminder Checker (30 s) fires due reminders. If a reminder is linked to an agent, firing it restarts that agent's goal cycle.

### Create a sub-agent

> *create an agent named News Watcher to monitor RSS feeds every hour*

The handler extracts the name (non-greedy regex with lookahead stop-set), the purpose, and the autonomy mode. Sub-agents have their own goal queues and Telegram chat-id default. Manage them at `/agents`.

### One-time screenshot

> *capture a screenshot of example.com*

The agent runs `browser(action="capture", url="...")`. The screenshot arrives as a Telegram photo with the page title as caption.

### Investigate logs

> *there are 503 errors in the audit log, find a pattern and propose a fix*

The agent creates a Goal: query audit log → analyze → propose patch via `self_improve(action="propose")`. Review the proposal at `/self-improve`. Apply only after reading the diff.

## Host shell

| Action | Command |
|--------|---------|
| Container status | `docker compose ps` |
| Recent agent logs | `docker compose logs agent-core --tail=200` |
| Follow logs | `docker compose logs -f agent-core` |
| Health check | `curl -s http://localhost:8080/health \| jq` |
| Queue depths | `docker exec agent-redis redis-cli XLEN events:incoming` |
| Behavioral queue | `docker exec agent-redis redis-cli LLEN behavioral:pending` |
| Postgres console | `docker exec -it agent-postgres psql -U agent -d agent` |
| Redis console | `docker exec -it agent-redis redis-cli` |
| Restart core | `docker compose up -d agent-core` |
| Rebuild core | `docker compose build agent-core && docker compose up -d agent-core` |

## Dashboard quick reference

| Page | Use it for |
|------|------------|
| `/overview` | Top-level snapshot |
| `/health` | CPU pressure (CPI), memory, queues, model liveness |
| `/traces` | Per-response forensic trace |
| `/audit` | Per-action AuditLog (keyset pagination) |
| `/cmd` | Command palette (search across pages) |
| `/scheduler` | Recurring tasks, custom tasks, monitor list |
| `/agents` | Sub-agent CRUD, status, message log |
| `/goals` | Active and historical goals with TaskGraph visualization |
| `/self-improve` | Pending AI proposals — diff viewer, Apply/Reject |
| `/behavioral-rules` | Learned rules — view, filter, toggle, delete |
| `/config` | `prime.md` editor + 12 feature flags |
| `/models` | Provider status, default model, catalog |
| `/integrations` | 40+ connectors with circuit-breaker state |
| `/cognitive` | Self-model, epistemic state, integrity report |
| `/knowledge-graph` | Force-directed canvas of entities and relations |
| `/world-model` | Temporal observations, entity timelines |
| `/memory` | Memory tree browser |
| `/vector-memory` | Semantic similarity search |
| `/reset` | Panic Reset (hard confirmation) |

## See also

- [Telegram Integration](/integrations/telegram) — full input handling reference
- [Dashboard Integration](/integrations/dashboard) — every dashboard page in detail
- [Configuration](/operations/configuration) — `prime.md` and feature flags
- [Monitoring](/operations/monitoring) — health and metrics
- [Logs](/operations/logs) — what to look for
