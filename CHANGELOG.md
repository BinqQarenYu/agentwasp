# Changelog

All notable changes to WASP are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions: [SemVer](https://semver.org/). Full pre-OSS history (v2.3 → v2.6) lives at https://docs.agentwasp.com/changelog — the entries below cover the public-release work on top of that baseline.

## [2.7] — 2026-05-13 (first public OSS release)

### Fixed
- **Dashboard navigation**: same-path query-string clicks were silent no-ops. `/memory?tab=...`, `/tasks?status=...`, every `?page=N`, and every filter pill across the dashboard worked exactly once. Fix in `base.html::_navigate` compares full URL (path + query), not just path.
- **Tab bars broken on re-visit**: pages with top-level `const`/`let` (cognitive, world-model, skill-evolution, vector-memory, goals, tasks) threw `SyntaxError: redeclaration of const X` the second time you visited them, aborting their init scripts. SPA loader now wraps inline scripts in an IIFE before re-injection.
- **CheckIn job spammed fresh installs**: `¿Necesitas ayuda con algo?` could be the very first message a brand-new install ever sent, before the user said anything. The job now refuses to fire when there is zero episodic memory.
- **Installer cross-distro**: three real bugs caught by a smoke run against Debian 12, Alpine 3, and AlmaLinux 9:
  - `df -BG "${INSTALL_DIR%/*}"` crashed for top-level install dirs (`/wasp`) — added fallback to `/`.
  - `hostname -I` is GNU-only; BusyBox on Alpine exits non-zero — added `ip` and `localhost` fallbacks.
  - AlmaLinux's `curl-minimal` blocked `dnf install curl` — added `--allowerasing` to the rhel/fedora path.

### Added
- **Self-improve `dry_run`**: `write` and `patch` accept `dry_run="true"`. The skill returns the unified diff plus the AST validation verdict without touching the file, creating a backup, or persisting anything. Lets the agent (and operators) preview the impact of a change before committing.
- **Gmail recipient allowlist**: `gmail send` enforces `GMAIL_RECIPIENT_ALLOWLIST` when set (per-address `alice@example.com` or per-domain `@company.com`). Defense-in-depth against prompt injection asking the agent to email arbitrary recipients.
- **Tests for Experimental cognitive subsystems**: 38 new tests covering learning feedback detection, procedural sequence checks, behavioral conflict detection, formatting helpers, dream module load. Suite total: 622 passing.
- **Public release packaging script**: `release-prep/scripts/build-release.sh` builds the public tarball from an allowlist staging copy. Refuses to package if any forbidden operator-specific identifier (private host paths, mailbox, bot handle, IPs) appears in the staged tree.

### Changed
- **Telegram welcome message**: rewritten for warmth and clarity. `/start` shows a concise capability summary with a few well-placed emojis; the long command reference moved to `/help`. Translated EN / ES / PT / FR.
- **Docker socket policy** (public default): socket is no longer mounted into `agent-core`. Only `agent-broker` retains socket access for compose orchestration. The integration-manager's auto-restart path now prints a manual `docker restart` instruction instead of silently calling the API.
- **Self-repair prompts** use `${WASP_HOST_DIR}` (set by the installer / wizard) instead of any hardcoded path. The agent learns the right rebuild directory for its install.

### Removed
- **Public `TELEGRAM_ALLOW_PUBLIC` escape hatch**: there is no longer a way to start the Telegram bridge without a numeric allowlist. Empty `TELEGRAM_ALLOWED_USERS` makes the bridge refuse to start. Wizard requires the operator's numeric Telegram id (5–15 digits) and replicates it to both `TELEGRAM_ALLOWED_USERS` and `SCHEDULER_NOTIFY_CHAT_ID`.
- **Operator-only artifacts** from the public archive: internal `containers/agent-core/docs/reports/` audit reports, the production-specific `agent-nginx` container, and the operator secret-rotation tracker are all excluded from the tarball.

## [2.7-rc] — 2026-05-12 (public-release scaffolding work)

The work in this entry is what made v2.7 publishable as an OSS project on top of the v2.6 internal codebase. Bundled into the v2.7 release; called out separately here because it touches the public surface (installer, docs, hosting) rather than the agent's behavior.

### Added
- One-line installer (`install.sh`) with cross-distro support: Debian/Ubuntu, RHEL/AlmaLinux/Rocky/CentOS, Fedora, Arch, openSUSE, Alpine, macOS.
- PowerShell installer (`install.ps1`) for Windows via WSL2.
- `wasp` CLI: `onboard`, `start`, `stop`, `restart`, `status`, `logs`, `health`, `update`, `backup`, `restore`, `reset`, `uninstall`, `help`.
- Interactive onboarding wizard with format validation for Telegram tokens and provider keys.
- Self-hosted dashboard (151 HTTP endpoints across chat, traces, tasks, scheduler, memory, knowledge graph, world model, skills, agents, goals, integrations, metrics, audit log, self-improve, cognitive health).
- Telegram bridge with 15 commands and multi-language welcome (en/es/pt/fr).
- 40 built-in skills including browser (nodriver + Selenium), email (Gmail), shell, python_exec (subprocess sandbox), http_request, fetch_url, scrape, reminders, monitors, self_improve, skill_manager, web_search.
- OpenClaw external-skill registry: install third-party skills with `/openclaw install <slug>`.
- Goal orchestrator with replan budget, stability tracking, chain-break recovery, and circuit breaker.
- 41 scheduler jobs (perception, dream, autonomous, self-integrity, CPI monitor, behavioral learner, etc.) with persistent state and catch-up logic on restart.
- Truth/honesty layer: URL substitution guard, follow-up domain lock, numeric grounding, capability claim verification, action announcer, scheduler honesty, prompt-leak redaction.
- Centralized SSRF guard (`utils/network_safety.py`) with DNS rebinding protection and manual redirect re-validation.
- Volume-aware `wasp backup` / `wasp restore` covering Postgres + named Docker volumes (redis, ollama, memory, logs, screenshots, browser sessions, uploads).
- Public docs: README, INSTALL, QUICKSTART, DEPLOYMENT, TROUBLESHOOTING, SECURITY, CONTRIBUTING.
- BSL 1.1 license with Change Date 2029-05-13 → Apache 2.0. Production use permitted under USD $1M annual revenue threshold.

### Security
- Fail-closed defaults: Telegram refuses startup if `TELEGRAM_ALLOWED_USERS` is empty. There is no public-bot mode and no escape hatch.
- Dashboard generates strong temporary credentials on first boot if no `DASHBOARD_USER`/`DASHBOARD_PASSWORD` provided; credentials printed once to stderr.
- Path-traversal containment on dashboard self-improve apply (`os.path.realpath` against `/app/src/`).
- Argon2 password hashing, CSRF tokens session-bound, login rate limit (5/5min).
- Python_exec runtime sandbox via subprocess with RLIMIT_CPU/AS/NOFILE and import-blocker for network/ctypes/subprocess modules.
- Shell skill blocklist for destructive patterns (rm -rf /, fork bombs, /dev/tcp, LD_PRELOAD, metadata IPs, etc.).
- Redaction patterns for AWS/Stripe/Slack/SendGrid/OpenAI/Anthropic/Google/HuggingFace credentials in audit logs.

### Known limits
- Some cognitive systems (procedural memory, behavioral rules, learning examples, opportunities) require accumulated usage before they show data. See `docs/STATUS_AND_LIMITS.md`.
- BSL 1.1 prohibits production use by entities with >USD $1M annual revenue from WASP-incorporating products until 2029-05-13.
- Single-operator design: there is no built-in multi-tenancy. The dashboard supports multiple admin accounts but every operator shares the same memory, scheduler, and skill surface.

### Removed
- Site-preference hardcoding (17track, coinmarketcap, named news sites). Site resolution now happens at runtime via `web_search`.
- Dead installer flags (`--dev`, `firewall_open()`, `VPS_IP` env var).
