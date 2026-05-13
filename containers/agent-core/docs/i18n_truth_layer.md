# i18n + Truth Enforcement — Operator Reference

The agent's internal voice is English. Every system-emitted message
(refusals, fallbacks, success acknowledgements, contradiction prompts)
lives in `src/communication/phrases.py` as English-only canonicals.
Translation to the user's detected language happens at publish time via
`src/communication/translator.py` with Redis-cached results.

## Architecture in one paragraph

```
LLM-generated text         → already in user_lang        → publish (final-guard checks lang) → publish
phrases.pick() canonical   → English                     → translator → publish (final-guard verifies)
honesty layer replacement  → marks trace["__canonical_en"]→ translator → publish (final-guard verifies)
formatter LLM output       → asks LLM for user_lang      → publish (final-guard catches drift)
```

The **final language guard** runs at the END of `_safe_publish_response`,
after the honesty layer and the policy stack. It performs a heuristic
language detection on the final text; if the detected language doesn't
match user_lang, it routes through the translator. This catches LLM
ad-hoc refusals that escape the canonical paths.

## Adding a new canonical message (intent)

1. Pick a unique snake_case key, e.g. `weekly_summary_ready`.
2. Add it to `_CATALOGUE` in `phrases.py` with **2 English variants** that
   convey the **same meaning**.
3. Use `pick(intent, seed=..., **kwargs)` in sync code (returns English),
   or `translator.apick(intent, lang, seed, model_manager, redis_url, **kwargs)`
   in async code (returns user-language).
4. If the layer that produces this message is sync (e.g. honesty layer),
   set `trace["__canonical_en"] = True` so the async caller knows to
   translate.
5. Add a test in `tests/test_phrases.py` covering at least the `pick()`
   passthrough and one placeholder substitution.

## Adding a new language

You don't need to. The translator works for any language gpt-4o-mini
understands. The `_LANG_LABELS` map in `translator.py` is **optional** —
it gives the LLM a friendlier prompt label (e.g. `"de"` →
`"German"`). Languages not in the map pass the raw ISO code, which the
LLM still handles.

To add a friendlier label for a specific dialect (e.g. `pt-br` for
Brazilian Portuguese), add an entry to `_LANG_LABELS`:

```python
_LANG_LABELS["pt-br"] = "Portuguese (Brazil)"
```

## Cache invalidation

Translation cache keys: `i18n:{lang}:{sha256(text)[:16]}` — TTL 30 days.

To invalidate a single phrase across all languages:
```bash
docker exec agent-redis redis-cli --scan --pattern "i18n:*:<hash16>" | xargs -r docker exec -i agent-redis redis-cli DEL
```

To invalidate every translation in a single language:
```bash
docker exec agent-redis redis-cli --scan --pattern "i18n:de:*" | xargs -r docker exec -i agent-redis redis-cli DEL
```

To invalidate **everything** (regenerated on next request):
```bash
docker exec agent-redis redis-cli --scan --pattern "i18n:*" | grep -v "i18n:metrics" | xargs -r docker exec -i agent-redis redis-cli DEL
```

## Telemetry

`GET /metrics/api/i18n` returns:

```json
{
  "total":   {"cache_hits": 145, "cache_misses": 12, "llm_calls": 12, "llm_failures": 0,
              "hit_rate_pct": 92.36, "fail_rate_pct": 0.0, "avg_llm_latency_ms": 420},
  "today":   {...},
  "last_7_days": [{"date": "2026-05-06", ...}, ...],
  "cache":   {"total_entries": 47, "by_lang": {"de": 18, "fr": 12, "es": 9, "it": 8}}
}
```

Counters are stored in Redis hashes:
- `i18n:metrics:total` — all-time
- `i18n:metrics:day:YYYY-MM-DD` — daily, TTL 60 days

## Truth enforcement layers (do NOT bypass)

The honesty layer in `events/response_binding.py` runs three checks
**before** the message reaches the translator. None of them depend on
language:

1. **Topic grounding** — every topic mentioned in the response must have
   a successful skill in this turn supporting it.
2. **Capability override** — if response says "task_manager doesn't
   support X" but `at_time` was just persisted, the response is replaced.
3. **Numeric grounding** — every price / 4+digit number in the response
   must appear in at least one successful skill output, otherwise the
   sentence is stripped.
4. **Attribute truth override** — if response asserts a value for a
   tracked user attribute (cat name, favourite colour, etc.) that
   contradicts what's stored in `user_attributes`, the DB wins.

Each replacement sets `trace["__canonical_en"] = True`. The publish
wrapper translates the canonical English to the user's language. The
final user-visible message is **localised but still grounded in truth**.

## Adding a new tracked user attribute

1. Add a regex pattern to `_DECLARATIVE_PATTERNS` in
   `memory/user_attributes.py` (e.g. for "address", "phone", "birthday").
2. Add a label mapping in `response_binding._check_user_attribute_consistency`:

   ```python
   label = {"pet_cat": "cat's name", ..., "phone": "phone"}.get(key, key)
   ```

3. Test with `tests/test_user_attributes.py`.

The `user_attributes` and `user_attribute_history` tables are in the
factory-reset truncate list. A factory reset clears all declared
attributes — operator must re-declare per chat.

## Failure modes & fail-safes

| Failure | Behaviour |
|---|---|
| Translator LLM down | Falls back to canonical English. Logs `translator.llm_failed`. |
| Redis cache unavailable | Skips cache, calls LLM each time. Logs `translator.cache_*_failed`. |
| Unknown language code | Passed verbatim to LLM. Most ISO codes work. |
| Unknown intent passed to `pick()` | Returns `[missing intent: <name>]` — visible to user, never crashes. |
| Honesty layer raises | Caught in `_safe_publish_response`, original text published. |

## Required environment

- `model_manager` — instance with `.generate(ModelRequest)` async method.
  Provided by `EventHandler.__init__`.
- `redis_url` — connection string. If `None`, translator runs uncached.

## What does NOT get translated (and why)

Some English strings appear inside `skills/builtin/*.py` (e.g.
`⛔ Code execution blocked: ...`, `[CAPTURE_VALID: false]`,
`Skills blocked: ...`). These are **internal protocol markers**, not
user-facing UX:

- They flow into `skill_result.error` / `skill_result.output`.
- The honesty layer, procedural memory, behavioural learning, and the
  replan-message generator pattern-match on these exact English tokens
  (`"page blocked"`, `"capture_valid: false"`, `"Skills blocked"`, etc.).
- The LLM digests them into a natural-language response in the user's
  language before the message is published.

**Do not translate or rename these strings.** Changing them silently
breaks four downstream subsystems and their tests. They are wire
protocol; user-facing localisation happens at the publish boundary, not
inside the skill layer.

If you need to surface a *new* user-facing message from a skill, route
it through `phrases.pick()` + `translator.apick()` like any other
canonical message — do **not** add new ad-hoc English strings to skill
outputs.
