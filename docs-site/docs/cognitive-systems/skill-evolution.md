---
id: skill-evolution
title: Skill Evolution
description: Synthesis of composite skills from recurring execution patterns.
---

# Skill Evolution

`src/scheduler/skill_evolution_job.py`. Feature-flagged via `skill_evolution_enabled` (default `true`).

## Concept

When the same skill sequence runs repeatedly across goals, Skill Evolution synthesizes a composite skill that captures the pattern. The composite saves planning tokens (one skill call instead of N) and codifies the best-known approach to the recurring problem.

## Storage

```
SkillPattern(
    pattern_key,            -- hash of skill sequence
    skill_sequence,         -- ordered list of (skill, args)
    occurrence_count,       -- how many times this pattern has run
    last_seen_at,
    synthesized,            -- True after composite generated
    composite_skill_name,   -- name of the generated skill (when synthesized)
    created_at,
)
```

## Detection

The job runs every 6 h. It scans the `skill_patterns` table for `occurrence_count >= MIN_OCCURRENCES` (default 5) AND `synthesized = false`.

## Synthesis

For each unsynthesized pattern:

1. The LLM generates a Python class implementing `SkillBase` whose `execute(**params)` runs the skill sequence with parameter mapping.
2. The generated code passes the same AST validation as `python_exec` (no `subprocess`, `os`, `eval`, etc. in dangerous positions).
3. The skill is saved at `/data/skills/<slug>/skill.py`.
4. The `skill_patterns.synthesized = true`.

## Operator approval

Synthesized skills do **not** activate automatically. They appear at `/skill-evolution` for review. The operator can:

- Inspect the generated code
- Test it via the dashboard
- Approve → registers in `SkillRegistry`
- Reject → removes the file

## Risk

A composite skill embeds a specific argument-mapping logic. If the recurring pattern was *coincidental* (different goals happened to use the same skills), the composite may be useless or wrong. Always inspect the generated diff.

## Disabling

To turn off composite synthesis entirely:

```bash
SKILL_EVOLUTION_ENABLED=false
```

Or toggle in `/config`. Existing composites stay registered.

## See also

- [Skills](/core-concepts/skills) — built-in catalog
- [Capability Evolution Engine](/advanced/capability-evolution-engine) — separate but related: discovers new capabilities from execution traces
- [Self-Improve](/integrations/dashboard) — manual code modification
