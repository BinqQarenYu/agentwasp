# WASP — Operational Status & Limits

This document is the operator-facing source of truth for what WASP does,
what it guarantees, and what it explicitly does NOT do.

## Current status

- **Truth enforcement**: active (response_binding v2 + memory truth model + URL substitution guard).
- **i18n**: English-internal canonicals. User language detected per chat. Translator on-demand with Redis cache (30-day TTL). **Final language guard** runs as last step of every publish — heuristic detection; if response language differs from user_lang, re-translates.
- **Security**: python_exec scan-time SSRF block + sandbox runtime block + URL substitution guard (works on follow-ups via `last_confirmed_domain`). No SSRF vector currently breaches.
- **Memory truth model**: `user_attributes` + `user_attribute_history`. Gaslighting blocked; contradictions surfaced as "antes me dijiste X, ahora dices Y, ¿cuál es correcto?".
- **Scheduler honesty**: timeout/exception writes back authoritative outcome. Circuit breaker auto-disables a task at 5 consecutive failures. Early warning at ≥3 failures.
- **Tests**: 357 pytest pass. New surfaces (translator, phrases, response_binding v2, hardening pass) covered by 30+ unit tests.
- **Telemetry**: `/metrics/api/i18n` (translation cache, hit rate, fallback rate) + `/metrics/api/truth` (honesty interventions, security violations, task outcomes).

## What 10/10 means operationally

A 10/10 system meets ALL these conditions over a sustained operating window:

| Axis | Condition |
|---|---|
| Truth violations to user | 0 |
| Forbidden execution attempts that succeed | 0 |
| URL substitutions accepted | 0 |
| Memory facts overwritten by gaslight | 0 |
| Scheduled tasks reporting false success | 0 |
| Language leaks (response in wrong language) | 0 (subject to detector resolution) |
| pytest failures | 0 |
| Empty user-facing responses | 0 |

Currently observed: all 8 conditions hold across the latest audit windows.

## Known operational limits

These are real, currently-imperfect behaviours. Document them honestly so
operators know what to monitor:

1. **Heuristic language detector resolution** — the final language guard
   uses regex marker counts (no model call). Very short responses
   (<12 chars) and ambiguous ones can pass through without translation.
   Large/clear responses are reliably routed.

2. **LLM-mediated tone drift** — gpt-4o-mini occasionally generates
   ad-hoc refusals in English when a session has been long and the
   user's language is non-English. The final guard catches the obvious
   cases; subtler drift (mixed language inside one sentence) may slip.

3. **Translator latency on cold cache** — ~200-500 ms per uncached
   phrase. Mitigation: aggressive caching (30-day TTL); after the first
   day in production, hit rate is typically >90%.

4. **Browser session storage growth** — `/data/browser_sessions/`
   accumulates one profile dir per named session. Not auto-pruned.
   Operator should periodically clear stale profiles.

5. **Decision trace `text` field** — empty for `perception` and
   `autonomous` paths (background jobs, no user message). User-driven
   traces are populated.

6. **Pre-existing test failures** — none. Suite is 100% green.

## Truth-enforcement layers (do not bypass)

1. **pre_execution_check** (`events/control_layer.py`)
   - SSRF target literal match
   - URL substitution check (uses `user_text` URLs OR `last_confirmed_domain`)
   - Domain lock + tool-level restrictions
2. **honesty layer v1** (topic grounding)
3. **honesty layer v2** (`response_binding.py`)
   - Capability claim verification
   - Numeric data grounding
   - Memory fabrication guard
   - User-attribute truth override
4. **i18n final guard** (post-policy translation when language mismatch detected)
5. **Scheduler outcome writeback** (timeout/exception still record failure)

Each layer logs a structured event when it intervenes. See:
- `honesty_layer.replaced`, `honesty_layer.stripped`, `honesty_layer_v2.*`
- `url_substitution.blocked`, `url_substitution.followup_lock`
- `python_exec.security_violation`
- `i18n.final_guard_applied`
- `custom_task.circuit_breaker_tripped`, `custom_task.early_warning_sent`

## Adding capability without breaking truth

When adding a new skill or response path:

1. Use `phrases.pick(intent, seed=...)` for canonical English templates.
2. Use `translator.apick(intent, lang, seed, model_manager, redis_url, ...)` for user-facing translated output.
3. Route every publish through `_safe_publish_response`. Do NOT call `bus.publish(stream_outgoing, ...)` directly with user text — that bypasses the honesty layer + final guard.
4. If a skill's error must influence honesty pattern matching, keep the English markers in the skill output (they're internal protocol — see `i18n_truth_layer.md`).

## Cache invalidation

```bash
# Single phrase across all languages
docker exec agent-redis redis-cli --scan --pattern "i18n:*:<sha256_16>" | xargs -r docker exec -i agent-redis redis-cli DEL

# Single language, all phrases
docker exec agent-redis redis-cli --scan --pattern "i18n:de:*" | xargs -r docker exec -i agent-redis redis-cli DEL

# Everything (translations only — does not touch metrics)
docker exec agent-redis redis-cli --scan --pattern "i18n:*" | grep -v "i18n:metrics" | xargs -r docker exec -i agent-redis redis-cli DEL
```

## Telemetry endpoints (auth-gated)

- `GET /metrics/api/i18n` — translator cache stats (hits/misses, fail rate, latency, per-lang cache size)
- `GET /metrics/api/truth` — honesty interventions, security violations, task outcomes, failure rate
- `GET /tasks` — operator dashboard for scheduled tasks (run_count, failure_count, enabled, last_result)
