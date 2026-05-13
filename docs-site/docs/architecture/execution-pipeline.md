---
id: execution-pipeline
title: Execution Pipeline
description: Skill execution, SkillExecutor, parallel groups, and anticipatory simulation.
---

# Execution Pipeline

The execution pipeline processes skill calls from LLM responses through safety checks, parallel coordination, and result aggregation.

Before invoking the LLM, the Decision Layer (pre-LLM heuristic classifier) routes the request — 13 fast-paths handle common patterns directly. Requests are routed to one of 5 strategies: DIRECT_RESPONSE, GOAL, SCHEDULED_TASK, SUB_AGENT, or SCRIPT. Only when no fast-path matches does the full LLM skill loop run.

## Pipeline Order (v2.6)

```
Incoming message
   │
   ▼
1. Per-chat asyncio.Lock        ─ serializes concurrent messages
   │
   ▼
2. Low-Intent Cold-Start Guard  ─ NEW v2.6: clarification fast-path
   │                              for short / emoji / context-required input
   ▼
3. auto_detect.py               ─ 13 fast-path handlers (Gmail, reminders,
   │                              YouTube search, etc.)
   ▼
4. Decision Layer               ─ heuristic 5-strategy classifier
   │                              SCHEDULED_TASK / SUB_AGENT / GOAL / SCRIPT / DIRECT
   ▼
5. Capability Engine            ─ skipped when auto_detect already ran
   │
   ▼
6. Context Builder              ─ KG, self-model, epistemic, temporal,
   │                              procedural, behavioral, episodic, vector
   ▼
7. LLM Loop (≤12 rounds)        ─ skill parsing, parallel groups,
   │                              anticipatory simulation, recovery memory
   ▼
8. Multi-URL Aggregator         ─ NEW v2.6: deterministic per-URL outcome
   │                              when ≥2 browser URLs in single auto_detect
   ▼
9. ResponseValidator            ─ grounding_fail / incomplete / drift /
   │                              planning_mode_violation / multipart_incomplete
   ▼
10. response_guard chain        ─ schedule honesty (bidirectional v2.6),
   │                              factual grounding (entity-proximity v2.6),
   │                              markdown sanitizer (link form v2.6)
   ▼
11. handlers post-processing    ─ markdown strip, prompt-leak strip,
                                  Telegram-specific cleanup
   │
   ▼
Outgoing response
```

## SkillExecutor (`src/skills/executor.py`)

The `SkillExecutor` orchestrates all skill execution:

```python
executor = SkillExecutor(
    skill_registry,
    model_manager=model_manager,  # For anticipatory simulation
    redis_url=settings.redis_url,
)
```

### `execute_batch(skill_calls) → list[SkillResult]`

Main execution method. Handles both sequential and parallel execution:

```python
async def execute_batch(skill_calls: list[SkillCall]) -> list[SkillResult]:
    # Group by parallel_group
    groups = group_by_parallel(skill_calls)

    results = []
    for group in groups:
        if len(group) == 1:
            # Sequential execution
            result = await execute_one(group[0])
            results.append(result)
        else:
            # Parallel execution
            group_results = await asyncio.gather(*[execute_one(c) for c in group])
            results.extend(group_results)

    return results
```

### `execute_one(skill_call) → SkillResult`

Single skill execution with full safety pipeline:

```python
async def execute_one(call: SkillCall) -> SkillResult:
    # 1. Check skill exists and is enabled
    skill = registry.get(call.name)

    # 2. Get capability level
    level = capability_registry.get_level(call.name)

    # 3. Anticipatory simulation (RESTRICTED/PRIVILEGED only)
    if level in (RESTRICTED, PRIVILEGED):
        simulation = await anticipate(call, context)
        # Appended to result for agent self-reflection

    # 4. Execute skill
    try:
        result = await skill.execute(**call.arguments)
    except Exception as e:
        result = SkillResult(error=str(e)[:300])

    # 5. Audit log (CONTROLLED and above)
    if policy.requires_audit:
        await write_audit_log(call, result)

    # 6. Redact secrets from output
    result.output = redact(result.output or "")

    return result
```

## Skill Call Parsing

LLM responses are parsed for skill calls using `parse_skill_calls()`:

```python
# Pattern: <skill>name(param="value", param2=123)</skill>
_SKILL_CALL_RE = re.compile(
    r"<skill>(\w+)\(([^)]*)\)</skill>",
    re.DOTALL
)
```

Arguments are parsed as Python literals (safe eval):

```python
call = SkillCall(
    name="web_search",
    arguments={"query": "BTC price today", "max_results": 5},
)
```

## Parallel Execution

Skills in `<parallel>` blocks run concurrently:

```xml
<parallel>
  <skill>web_search(query="BTC price")</skill>
  <skill>web_search(query="ETH price")</skill>
</parallel>
```

All skills in a parallel group share the same `parallel_group` ID. `execute_batch()` processes them with `asyncio.gather()`.

## Anticipatory Simulation

Before executing RESTRICTED or PRIVILEGED skills, the simulation runs:

```python
async def anticipate(call: SkillCall, context: str) -> str:
    prompt = f"""
    About to execute: {call.name}({call.arguments})
    Context: {context[:500]}

    Predict the outcome and any risks in 2-3 sentences.
    """
    simulation = await model_manager.generate(prompt, max_tokens=300)
    return f"[ANTICIPATORY SIMULATION]: {simulation}"
```

The simulation result is appended to the skill output, allowing the LLM to course-correct on the next round if the predicted outcome is problematic.

Simulations are cached in Redis for 5 minutes (same call + context → same prediction).

## Audit Log

Every CONTROLLED, RESTRICTED, and PRIVILEGED skill call writes to `audit_log`:

| Column | Description |
|--------|-------------|
| `skill_name` | The skill that was called |
| `input_summary` | Arguments (secrets redacted) |
| `output_summary` | Result (secrets redacted) |
| `capability_level` | CONTROLLED/RESTRICTED/PRIVILEGED |
| `risk_level` | From RiskAssessor (RESTRICTED+ only) |
| `duration_ms` | Execution time |
| `chat_id` | Who triggered it |
| `created_at` | Timestamp |

Query recent audit entries:

```bash
docker exec agent-postgres psql -U agent -d agent -c "
  SELECT skill_name, input_summary, output_summary, created_at
  FROM audit_log
  ORDER BY created_at DESC
  LIMIT 20;
"
```

## Secret Redaction

Before writing to audit log, all outputs are passed through `redact()`:

```python
from src.utils.redaction import redact

redacted_output = redact(raw_output)
```

Patterns that are redacted:
- OpenAI keys: `sk-[a-zA-Z0-9]{20,}`
- Anthropic keys: `sk-ant-[a-zA-Z0-9-]{20,}`
- Google keys: `AIza[a-zA-Z0-9-_]{35}`
- AWS keys: `AKIA[A-Z0-9]{16}`
- Stripe keys: `sk_live_[a-zA-Z0-9]{24}`
- Bearer tokens, passwords in key=value pairs

## Error Handling

Skill failures return structured errors:

```python
SkillResult(
    output=None,
    error="TimeoutError: browser skill exceeded 60s timeout",
    metadata={"duration_ms": 60000}
)
```

The LLM receives the error and can:
- Try an alternative approach
- Use a different skill
- Report the error to the user

## Rate Limiting

PRIVILEGED skills have a hard rate limit of 20 calls per hour. Other levels are currently unlimited but logged for analysis.

Rate limit state is tracked in Redis:
```
skill:rate:{skill_name}:{hour_bucket}  → count
```

## Multi-URL Aggregator (v2.6)

When auto-detect resolves 2+ URLs in a single user message, all URLs are dispatched as parallel `browser` calls. The aggregator then builds a deterministic per-URL outcome list.

```python
# events/handlers.py — Multi-URL aggregator block
_browser_calls = [c for c in auto_calls if c.skill_name == "browser"]
if len(_browser_calls) >= 2:
    for _bc, _br in zip(auto_calls, auto_results):
        if _bc.skill_name != "browser":
            continue
        _u = (_bc.arguments or {}).get("url", "")
        if not getattr(_br, "success", False):
            _summaries.append(f"• {_u} → ❌ {error_first_line}")
            continue
        _out = getattr(_br, "output", "") or ""
        # NEW v2.6: Error: prefix detected even when success=True
        if _out.lstrip().lower().startswith("error:"):
            _summaries.append(f"• {_u} → ❌ {_out.splitlines()[0][:120]}")
        elif "[CAPTURE_VALID: false]" in _out:
            _summaries.append(f"• {_u} → 🚫 blocked")
        elif "[CAPTURE_VALID: true]" in _out:
            _summaries.append(f"• {_u} → ✅ screenshot sent")
        else:
            _summaries.append(f"• {_u} → ✅ navigated")
```

**Why deterministic aggregation matters:** without this layer, the LLM was occasionally summarizing only the first URL it processed. Each URL must appear in the response with a clear outcome.

**Why the `Error:` prefix check matters:** the browser skill returns `success=True` even when its output begins with `Error: URL blocked...` (SSRF block, file:// block). `success` only means "the skill itself didn't crash", not "the URL was reachable". Without this check, SSRF-blocked URLs were labeled ✅ navigated.

## Response Validation Chain

After the LLM produces a final response, it passes through a multi-stage validation chain before reaching the user.

### ResponseValidator (`policy/response_validator.py`)

Deterministic post-LLM checks that block weak or hallucinated outputs:

| Check | Trigger | Action |
|-------|---------|--------|
| `grounding_fail` | External-data query but no skill produced verified data | Replace with honest fallback |
| `incomplete` | Trace shows skill execution started but not completed | Trigger recovery round |
| `drift` | Response refers to a different domain than the request | Soft recovery via LLM correction |
| `planning_mode_violation` | Response asserts execution while planning mode is active | Block, return analysis-only response |
| `incomplete_multipart` | Multi-part request not fully answered | Trigger completeness retry |

When validation fails and `should_retry=True`, the agent gets one recovery round to correct the response.

### Response Grounding Engine — Checks 5–9 (v2.4+)

Layered post-LLM validation that eliminates weak outputs and hallucinated claims:

| Check | Description |
|-------|-------------|
| 5 | Universal weak-response rejection — fragments &lt;4 words with no structured marker / numeric evidence |
| 6 | Generic weak-phrase filter (20 multilingual patterns: "task completed", "done successfully", etc.) |
| 7 | Plain status-marker validation — `_PLAIN_STATUS_RE` whitelist |
| 8 | Intent evidence gate — `_requires_evidence(user_text)` semantic groups (STATUS_QUERIES, PRICE_QUERIES, VERIFICATION) |
| 9 | Anti-hallucination guard — `_FACTUAL_CLAIM_RE` blocks fabricated currency amounts, prices, dates, action claims |

### response_guard Chain (v2.6)

After ResponseValidator passes, three guards run in sequence:

1. **`enforce_schedule_honesty`** — strips clock-time lies; **bidirectional v2.6**: also appends a disclaimer when the user asked for a clock time (`at 9am`) or a daypart phrase (`in the morning`, `every evening`, `at dawn`) AND a real interval-only task was created. Branches: `user_text` (clock pattern) and `user_text_daypart` (daypart pattern). Trace records `had_real_create`, `claimed_time`, `origin`.
2. **`enforce_factual_grounding`** — replaces fabricated verdicts with honest fallback. **v2.6 entity-proximity check**: `_skill_output_supports_verdict()` requires the verdict word (`delivered`, `in transit`, etc.) to appear within 200 chars of a user-named entity (tracking code, ticker) extracted via `_USER_ENTITY_RE`. Without this check, the LLM could stitch unrelated UI labels from a tracking-site home page into a fabricated claim about the user's specific shipment.
3. **`sanitize_markdown`** — strips markdown leakage that prime.md §9 forbids. **v2.6 link form**: `_MARKDOWN_LINK_RE` collapses `[text](url)` to `text (url)` so the URL stays accessible without raw brackets rendering literally in Telegram. Negative lookbehind `(?<!!)` prevents double-handling of image syntax.

Each guard returns `(cleaned_text, trace)` where `trace["applied"]` indicates whether the guard fired. Traces ship in the response decision-trace for forensic review.

## Cold-Start Hallucination Guard (v2.6)

A short message arriving on a fresh chat with no prior context is one of the most reliable hallucination triggers. `_is_low_intent()` detects:

- Single ambiguous token (multilingual frozenset of confirmations and acknowledgements)
- Emoji / digit / punctuation-only message
- Context-required phrase without anchor (phrases that explicitly refer to prior interaction)
- ≤2 tokens AND every token is in the ambiguous set

When low-intent + no scheduled-language match + no `last_exchange` anchor in chat memory, the handler returns a clarification fast-path **without invoking the LLM**. Zero token cost; zero hallucination risk.

Bypassed for `[RETRY OF PREVIOUS:` messages and greetings (`hi`/`hello`/`hey`/`ping`) which use a dedicated friendly-response path.
