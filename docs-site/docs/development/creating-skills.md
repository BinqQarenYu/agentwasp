---
id: creating-skills
title: Creating Skills
description: Adding built-in or custom Python skills.
---

# Creating Skills

Two paths:

1. **Built-in skill** — adding a Python file to the source tree, requires rebuild.
2. **Custom Python skill** — runtime registration via `skill_manager`, no rebuild needed.

## Path 1: Built-in skill

### Step 1 — Implement `SkillBase`

Create `src/skills/builtin/your_skill.py`:

```python
from src.skills.base import SkillBase, SkillResult

class YourSkill(SkillBase):
    name = "your_skill"
    description = "What the skill does, briefly."
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_chars": {"type": "integer", "default": 8000},
        },
        "required": ["url"],
    }

    async def execute(self, **params) -> SkillResult:
        url = params.get("url", "")
        max_chars = params.get("max_chars", 8000)
        if not url:
            return SkillResult(skill_name=self.name, success=False,
                               output="", error="url is required")
        try:
            # ... your logic ...
            return SkillResult(skill_name=self.name, success=True, output="...")
        except Exception as e:
            return SkillResult(skill_name=self.name, success=False,
                               output="", error=str(e)[:300])
```

### Step 2 — Register the skill

In `src/skills/builtin/__init__.py`:

```python
from .your_skill import YourSkill

# Add to register_builtin_skills() or the relevant aggregation
registry.register(YourSkill())

# Add to the capability map
_CAPABILITY_MAP["your_skill"] = CapabilityLevel.CONTROLLED
```

### Step 3 — Side-effect skills: add intent regex

If your skill has side-effects (sends data, writes external state), add an intent regex to `src/policy/intent_gate.py`:

```python
SIDE_EFFECT_SKILLS = {"gmail", "agent_manager", "task_manager", "your_skill"}

INTENT_GATE_PATTERNS["your_skill"] = re.compile(
    r"(?:verb-pattern-here)",
    re.IGNORECASE,
)
```

### Step 4 — Add a regression case

In `src/policy/regression_checks.py`, add a case asserting expected behavior:

```python
("your-skill-needs-explicit-intent",
 "do something",                 # user input
 check_no_inferred_side_effect,
 True,
 "your_skill must not run on inferred intent"),
```

### Step 5 — Build and verify

```bash
docker compose build agent-core
docker compose up -d agent-core
```

The build runs the regression suite. If your check fails, fix the implementation. After the build succeeds, verify in the dashboard at `/skills`.

## Path 2: Custom Python skill (runtime)

For skills you want to add without rebuilding:

```
skill_manager(
    action="create",
    name="my-slug",
    description="What the skill does",
    params="param1,param2",
    code="""
from src.skills.base import SkillBase, SkillResult

class MySkill(SkillBase):
    name = "my-slug"
    description = "..."
    async def execute(self, **params) -> SkillResult:
        return SkillResult(skill_name=self.name, success=True, output="...")
""")
```

The skill is saved at `/data/skills/my-slug/skill.py`. `load_all_python_skills()` at startup scans `/data/skills/` and registers each one.

Custom skills:

- Run in-process and inherit the agent's permissions.
- Cannot extend `SIDE_EFFECT_SKILLS` (that requires a code-level change).
- Default to CONTROLLED capability level.
- Show up in `skill_manager(action="list")` with type `python-custom`.

**Review the code carefully before enabling.** Any bug or security flaw runs with the agent's full permissions.

## SkillResult

```python
@dataclass
class SkillResult:
    skill_name: str
    success: bool
    output: str = ""        # rendered for LLM consumption (and via redact() for audit)
    error: str | None = None
    metadata: dict | None = None  # arbitrary structured data
```

Always set `skill_name`. Use `success=False` for any non-success path; populate `error` with the cause (≤ 300 chars).

`output` should be human-readable for the LLM — avoid raw JSON unless that's the explicit contract.

## Anticipatory simulation

For RESTRICTED and PRIVILEGED skills, the executor runs a pre-execution LLM simulation. The result is appended to `output` for the next round of self-reflection. This is automatic; no skill code changes needed.

## Audit logging

CONTROLLED, RESTRICTED, and PRIVILEGED skills are auto-audited. Audit entries pass through `redact()` to strip secrets. Shell skill commands also pass through `_redact_command()`.

## Testing

```bash
# Test a skill in isolation
docker exec agent-core python -c "
import asyncio
from src.skills.builtin.your_skill import YourSkill
async def t():
    r = await YourSkill().execute(url='https://example.com')
    print(r)
asyncio.run(t())
"
```

Or trigger via Telegram / dashboard chat with a precise instruction:

> *call your_skill with url=https://example.com*

## See also

- [Skills](/core-concepts/skills) — built-in catalog
- [Skill Safety](/security/skill-safety) — capability levels and intent gating
- [Project Structure](/development/project-structure)
- [Extending](/development/extending) — beyond skills
