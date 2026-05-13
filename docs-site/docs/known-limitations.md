---
id: known-limitations
title: Known Limitations
description: Honest list of what WASP cannot do and where the operator must compensate.
---

# Known Limitations

This is the honest list of what WASP cannot do, what it does poorly, and where the operator must compensate. Read this before depending on WASP for high-stakes work.

## The LLM is probabilistic

WASP wraps a foundation model (Anthropic / OpenAI / Google / etc.). The model is not deterministic. The same prompt can produce different outputs on different turns. The Policy Layer mitigates this:

- The Intent Gate is deterministic regex.
- The Action Announcer is deterministic string matching.
- The Response Guard's grounding check is deterministic proximity matching.
- The Response Validator runs deterministic checks before the LLM gets a corrective round.

What the policy layer does NOT eliminate:

- Subtle factual errors that pass grounding (e.g., the LLM cites a real source but misquotes it).
- Tone drift from a single prompt-engineered nudge.
- Off-by-one errors in narrated counts.
- Reasoning errors that produce a plausible-but-wrong plan.

**Mitigation:** Use `/traces` to verify any high-stakes response. For irreversible actions, require a manual confirmation step.

## Factual grounding is strong but not perfect

The grounding guard requires:

- A successful skill output to substantiate any verdict.
- For user-named entities (tracking codes, tickers), the verdict word must appear within 200 chars of the entity.

What it catches:

- Fabricated delivery statuses without browser/API evidence.
- Hallucinated prices, dates, action claims when no supporting skill ran.
- UI labels stitched into responses about specific entities.

What it misses:

- Verdicts about subjective state ("the market sentiment is bearish") — the guard targets factual states, not interpretations.
- Numeric values within the 200-char window but inverted ("delivered" was actually "not delivered" two words later).
- Verdicts in languages not covered by the verdict-keyword set.

**Mitigation:** Critical fact-check responses by re-running the skill and reading the raw output. For non-supported languages, the regression suite needs additional verdict keywords.

## No guarantee of trading or financial profit

WASP can fetch crypto prices, monitor RSS feeds, and call exchange APIs. It does not model risk, cannot guarantee profit, and is not financial advice. Markets are adversarial and the agent has no edge.

What you can use it for:

- Notifying you when a price moves more than X%.
- Aggregating news headlines.
- Running pre-defined trading rules with manual confirmation.

What you should NOT use it for:

- Autonomous trading without a hard stop-loss and operator review.
- Tax decisions.
- Anything where a hallucinated number causes a real loss.

**Mitigation:** Keep `agent_manager.create` for trading agents in MANUAL autonomy mode. Require explicit confirmation for any order-placing skill. Read every Telegram alert before acting.

## Not multi-tenant ready

WASP is designed for one operator:

- The dashboard supports multiple admin accounts, but every operator shares the same memory, scheduler, and skill surface.
- Telegram access is restricted to `TELEGRAM_ALLOWED_USERS`.
- Per-chat memory is namespaced, but the agent process is shared.
- Behavioral rules learned from one operator apply to all chats.
- The knowledge graph aggregates entities from all conversations.

Multi-tenant hardening would require: per-user authentication (SSO, OAuth), per-user memory namespaces in every table, per-user rate limits, per-user audit logs, strict isolation between users in the LLM context. This is significant rework, not a flag flip.

**Mitigation:** Run separate WASP instances on separate VPSes for separate users.

## Background jobs consume tokens

The dream cycle, perception, autonomous goal generator, and behavioral learner all call the LLM:

| Job | Frequency | Token cost |
|-----|-----------|------------|
| Dream | 1/h (gated) | Medium — LLM reflection ~2k tokens |
| Perception | 4/h | Low — short LLM judgments |
| Autonomous goals | 2/h | Low — short evaluations + occasional plan generation |
| Behavioral learner | 30/h | Medium — rule extraction per correction |
| Goal tick | 240/h | High — every active goal step is an LLM call |

For a quiet single-operator setup, expect ~$0.50–$2.00/day with default models.

**Mitigation:**

- Open `/metrics` for actual usage.
- Disable jobs you don't need via `/config` flags.
- Use a cheaper default model for routine work; switch to a stronger model only for complex tasks.

## Long-term unattended operation needs monitoring

WASP runs 24/7, but it is not fire-and-forget. After weeks of operation:

- Behavioral rules may accumulate contradictions.
- The knowledge graph may absorb misconceptions.
- The self-model may drift from reality.
- Background jobs may exhaust your model budget.
- Disk volumes may grow unboundedly without retention.

**Mitigation:**

- External uptime check on `/health`.
- Weekly review of `/audit`, `/behavioral-rules`, `/self-improve`.
- Monthly `docker system df -v` to spot growth.
- Quarterly review of the self-model at `/cognitive`.
- Use Panic Reset if contamination is suspected.

## Docker socket access is powerful and risky

`agent-broker` mounts `/var/run/docker.sock` and proxies a small allowlist of endpoints. The public default does **not** mount the socket into `agent-core` — only the broker has Docker access. This is enough for the agent to manage Docker containers, but not enough to create new containers with arbitrary capabilities.

What the allowlist does NOT prevent:

- A compromised existing container can be exploited via `docker exec` (if the agent has shell access in that container).
- Container metadata leakage via `inspect`.
- Restart-loop denial of service via repeated `start`/`stop`.

**Mitigation:** Keep `TELEGRAM_ALLOWED_USERS` tight. Review `/audit` for any `skill.shell` activity that interacts with Docker. Use the broker as a defense-in-depth layer, not a complete sandbox.

## Self-modification has limits

The `self_improve` skill safeguards include path containment, syntax validation, timestamped backups, soft safety gate, and SHA-256 sidecars.

What these do NOT prevent:

- A subtly-bad patch that passes all checks but introduces a logic regression.
- A patch to a non-critical file that still impacts safety transitively.
- A patch that adds a new attack surface (new HTTP endpoint without auth).

**Mitigation:**

- Always review the diff at `/self-improve` before applying.
- Run the regression suite after applying.
- Keep a snapshot of `/data/src_patches/` so you can revert wholesale.

## Browser automation is fragile

WASP uses Playwright + Chromium for `browser.py`. Real-world browsing is adversarial:

- Sites change DOM structure → CSS selectors break.
- Anti-bot systems escalate (captchas, rate limits, IP blocks).
- Login sessions expire.
- Some sites detect headless Chromium and refuse to serve.

The Universal Interaction Validation Layer mitigates the false-success cases (e.g., misleading screenshots when the site actually blocked us), but cannot substitute for a working site.

**Mitigation:**

- Use named sessions for sites you regularly access (cookie persistence).
- Prefer official APIs over scraping when available.
- Treat browser failures as expected — handle them with manual fallback.

## Email handling has narrow guardrails

The Gmail skill works through IMAP/SMTP with App Passwords. The intent gate blocks sends without explicit content. But:

- Attachments are not supported by the built-in `send` action.
- Rate limits are enforced at Gmail's side, not by the skill.
- A compromised App Password can be used by an attacker who gains access to Redis.
- Outbound emails to large lists may trigger Gmail's anti-abuse rules.

**Mitigation:**

- Use an App Password specific to WASP, revocable independently.
- Set `GMAIL_RECIPIENT_ALLOWLIST` to restrict who the agent can email (per-address or `@domain.com`) — defense-in-depth vs prompt injection.
- Monitor Gmail's "Sent" folder for unexpected sends.
- For bulk email, use a transactional provider via the integration layer instead of Gmail.

## Scheduling is interval-only

`task_manager` does NOT support fixed clock times or daypart phrases. The bidirectional schedule honesty guard surfaces this clearly to the user, but cannot make `task_manager` honor a clock time.

**Workarounds:**

- Create the task at the desired wall-clock time so the interval boundary aligns.
- Use the `cron` integration (registered connector) for true cron semantics.
- Use a goal triggered by a reminder (reminders accept absolute UTC timestamps).

## Memory is not infinite

The memory layers grow over time. Bounds:

- Episodic: pruned by `MemoryCleanupJob` based on importance and age.
- Audit log: trimmed by `AuditRetentionJob` (default 30 days).
- Behavioral: capped at 50 in queue, no cap on rules table.
- Knowledge graph: no cap, but composite TTL via `confidence` and last access.
- World timeline: rows expire via `expires_at` column.
- Vector embeddings: no cap (rebuild via `vector_index` job).

**Mitigation:**

- Run Panic Reset if memory is contaminated.
- Use `/memory` to delete specific entries.
- Lower `AUDIT_RETENTION_DAYS` for high-volume deployments.

## The agent has no concept of money

Skills can call APIs that cost money (model providers, paid integrations, exchange APIs), but the agent does not track its own spending. There is no "stop spending" mechanism beyond:

- The Resource Governor's per-minute LLM call cap.
- The per-day goal cap.
- Manual operator intervention via `/config` flag toggles.

**Mitigation:**

- Set hard spending limits at your model providers.
- Monitor `/metrics` token usage daily.
- Keep autonomous job intervals conservative.

## Trace and audit are not tamper-evident

Decision traces and audit log entries can be deleted from Redis/Postgres by anyone with database access. There is no signing or external attestation by default.

**Mitigation:**

- Treat the host OS as part of the trust perimeter.
- Use OS-level filesystem audit (auditd, tripwire) if you need tamper-evidence.
- For high-stakes deployments, mirror AuditLog entries to an append-only external sink.

## Time and timezone are best-effort

The agent uses UTC internally. User-facing times are rendered in `USER_TIMEZONE`. DST transitions, system clock skew, and NTP failures can cause confusion in scheduling. The agent does not have a strict consensus clock.

**Mitigation:**

- Run NTP on the host.
- Avoid rebooting during DST transitions.

## Boot sequence is best-effort

The boot sequence at first-message-after-fresh-start checks Telegram, model, knowledge graph, browser, memory. Failures are reported in the boot message but do not prevent the agent from accepting messages. The operator must read the boot output and act.

## Final note

WASP is a powerful, opinionated, single-operator agent. Every feature listed in the docs works as documented. The limitations are real and documented because they exist. Operating WASP successfully requires reading this document, the [Skill Safety](/security/skill-safety) document, and treating the agent as a capable but fallible assistant — not as an oracle.

The right mental model: *senior junior engineer who reads my Slack messages, has access to all my tools, and works 24/7. I trust them with most things, but I review the audit log before payday.*
