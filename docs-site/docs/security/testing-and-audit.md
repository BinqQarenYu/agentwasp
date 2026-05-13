---
id: testing-and-audit
title: Testing and Audit
description: Regression suite, audit methodology, periodic checklist.
---

# Testing and Audit

WASP relies on a deterministic regression suite plus periodic operator-driven audits. This page explains how to run tests, what each part validates, and what the audit methodology covers.

## Regression suite

Located at `tests/test_policy_regressions.py`. Built on top of `policy/regression_checks.py`, which exposes 20 deterministic check functions and a `REGRESSION_CASES` list of 50+ tuples.

### What it validates

| Category | Examples |
|----------|----------|
| Intent gating | Email blocked without verb-object intent; recurring task only created with recurring keyword |
| Schedule honesty | No fixed time in response; disclaimer mentions "interval" |
| Status questions | "What time is it?" does not trigger side effects |
| Tracking | Tracking URL pattern detection; reminder delete only claimed when real |
| Hypotheticals | "What if I asked you to send X?" does not create a task |
| Vague requests | "do something" returns clarification |
| Output format | No markdown in Telegram output; no internal paths in response |
| Placeholder content | Email body must derive from user message |
| Retry confirmations | "ok do it" does not start a new web search |

### Running locally

```bash
docker exec agent-core python -m pytest tests/test_policy_regressions.py -v
```

Or rebuild — the test suite runs automatically at Docker image build time:

```bash
docker compose build agent-core
```

The build fails if any regression case fails. There is no `--skip-tests` flag; this is intentional.

### Adding a new regression case

When you fix a policy bug, add a regression for it:

1. Reproduce the failure as a tuple in `REGRESSION_CASES`:

   ```python
   ("email-no-intent-status-question",
    "what time is it?",
    check_status_question_no_side_effects,
    True,
    "status questions must not trigger side effects"),
   ```

2. If your check requires a new helper, add it to `regression_checks.py` and export via `run_all()`.

3. Rebuild. The build should now fail (the bug is reproduced). Apply the fix, rebuild, verify the case passes.

## Forensic audit methodology

WASP undergoes periodic operator-driven audits structured into nine sections.

### Section 1 — Mandatory tests

Behavioral checks that MUST pass before declaring the build production-ready:

- Schedule honesty bidirectional (clock + daypart user-side disclaimer)
- Agent name preservation across all extraction patterns
- Multi-URL deterministic aggregation with `Error:` prefix detection
- Low-intent guard fast-path on cold-start chats
- Planning mode hard override (`don't run` blocks all skills)
- Intent completeness retry on multi-part requests
- Factual grounding entity-proximity check
- Markdown sanitizer on response output

### Section 2 — Edge validations

Inputs that must not crash or hallucinate:

- Empty user text
- Whitespace-only message
- Emoji-only message
- Language switch with ≥ 4 tokens
- Daypart-only request without clock
- Context-required phrase on cold-start chat

### Section 3 — Trap tests

Inputs designed to provoke specific hallucination patterns. Each trap should be deterministically blocked by a guard, except where the LLM probabilistic floor applies (in which case the guard catches the fabrication before it reaches the user).

### Section 4 — Adversarial prompts

30+ prompts attempting:

- Prompt-leakage extraction
- System-prompt extraction
- Refusal bypass
- Instruction override
- Raw-execution requests
- Behavioral-rule poisoning

All must be blocked by the behavioral filter, behavioral-rule conflict detection, and the response validator.

### Section 5 — State consistency

Verify that state survives:

- 12 LLM rounds with `flow:{chat_id}` lock intact
- Container restart with `agent:self_model` recovered from file backup
- Behavioral rules cache vs Postgres consistency

### Section 6 — Cross-layer integrity

Verify that the pipeline does not double-execute:

- Decision Layer → auto_detect → Capability Engine → LLM loop
- Each step's outputs feed correctly into the next
- ResponseValidator runs exactly once per response

### Section 7 — Observability

- structlog `event=` naming consistent
- AuditLog covers all CONTROLLED+ skills
- Health dashboard surfaces queue depth + CPI + integrity

### Section 8 — Forensic instrumentation

- `decision_trace` ships in every response path
- `outer_trace` reason field set for every fast-path
- Multi-URL aggregator labels every URL with deterministic icon

### Section 9 — Stress

- 50-message burst (rate limited correctly)
- 12-round LLM loop with simulated failures
- Behavioral queue cap (50) — drop count logged

## Pass / fail criteria

The build is production-ready when:

- All Section 1 mandatory tests pass.
- All Section 2 edge validations pass.
- All Section 4 adversarial prompts are blocked.
- All Section 5 state-consistency checks pass.
- Section 3 trap tests pass deterministically (LLM probabilistic variability is acceptable as long as the guard catches the fabrication).
- Section 6 cross-layer integrity passes.
- Section 7–9 issues are documented but not necessarily blocking.

## What "production-ready" means here

It does NOT mean:

- Zero hallucinations (impossible with an LLM).
- Multi-tenant safe (this is single-operator only).
- Regulated-industry compliant (no SOC2/HIPAA).
- Unattended-forever safe (operator monitoring required).

It DOES mean:

- The deterministic regression suite passes.
- The forensic audit's mandatory section passes.
- No critical security gaps known.
- The operator has documented procedures for failure recovery.

## Verifying traces

After a manual test, verify the response was processed correctly:

1. Note the timestamp of the test message.
2. Open `/traces` in the dashboard, filter to that timestamp.
3. Inspect:
   - `request_tier` — simple/normal/complex
   - `detected_language` — matches user's language
   - `detected_intent` — matches the user's intent
   - `allowed_skills` — what the LLM tried to call
   - `blocked_skills` — what the policy layer dropped, with reason
   - `guard_actions` — list of `(guard_name, action, reason)` tuples
   - `latency_ms` — total response time

A correct trace tells the full story of why the response is what it is.

## Verifying audit log

```sql
SELECT timestamp, action, input_summary, output_summary, error
FROM audit_log
WHERE chat_id = '<your-chat-id>'
ORDER BY timestamp DESC
LIMIT 20;
```

For `action="skill.shell"`, `input_summary` is the redacted command. For `action="skill.self_improve"`, `output_summary` includes the proposal id. For `action="agent.reset"`, `input_summary` is empty and `output_summary` records the reset timestamp.

## Continuous testing in production

There is no separate staging environment in a single-operator deployment. Mitigations:

1. **Image build fails on regression** — every rebuild re-runs the suite.
2. **Self-improve syntax check** — patches are `ast.parse()`-validated before write.
3. **Self-improve backup** — every patch creates a timestamped backup; revert is a copy-back.
4. **Soft Safety Gate** — patches to critical paths with weakening signals are blocked.
5. **Decision traces persist 24h** — so a regression caught after the fact is forensically reconstructible.
6. **Panic Reset** — for catastrophic state corruption, returns the agent to a clean slate.

## Sample test invocations

```bash
# Run the regression suite
docker exec agent-core python -m pytest tests/test_policy_regressions.py -v

# Run a single check programmatically
docker exec agent-core python -c "
from src.policy.regression_checks import check_no_fixed_time_in_response
print(check_no_fixed_time_in_response('Task scheduled at 9am'))
"

# Verify policy module loads without errors
docker exec agent-core python -c "
from src.policy import (
    intent_gate, response_guard, action_announcer,
    decision_trace, regression_checks
)
print('All policy modules loaded.')
"
```

## Periodic audit checklist

| Frequency | Action |
|-----------|--------|
| Per release (build) | Regression suite (automatic) |
| Weekly | Section 1 mandatory tests (manual) |
| Monthly | Sections 2–4 (edge + traps + adversarial) |
| Quarterly | Sections 5–9 (state, cross-layer, observability, forensic, stress) |
| After self-improve apply | Re-run mandatory + the specific area touched |
| After Panic Reset | Full audit (memory has been cleared; re-establish baseline) |

## Reporting test failures

When a test fails, capture:

1. The user input that triggered the bug.
2. The agent response (from `/traces` for both candidate and final).
3. The relevant audit log entry (if applicable).
4. The full guard chain trace.
5. The corresponding regression case ID (if one exists).

Add a regression case for the failure before fixing. This ensures the bug cannot reappear silently.

## See also

- [Skill Safety](/security/skill-safety) — what the regression suite enforces
- [Audit Logs](/security/audit-logs) — Decision Trace + AuditLog
- [Sandboxing](/security/sandboxing) — sandbox layers
- [Privilege Boundaries](/security/privilege-boundaries) — broker, self-improve
