---
id: skills
title: Skills
description: Capability levels, the 37 built-in skills, and how skills are executed.
---

# Skills

Skills are the agent's tool-using interface. Each skill is a Python class with an `async execute(**params) → SkillResult` method, registered in `SkillRegistry` with a capability level. The LLM emits skill calls as `<skill>name(arg1=val, arg2=val)</skill>` blocks; the `SkillExecutor` parses, dispatches, and post-processes them.

## Capability levels

| Level | Logged | Confirmation in SEMI mode | Examples |
|-------|--------|---------------------------|----------|
| SAFE | No | No | `calculate`, `datetime_skill`, `system_info` |
| MONITORED | No | No | `web_search`, `fetch_url`, `browser` (read-only) |
| CONTROLLED | Yes | No | `gmail`, `reminders`, `task_manager` |
| RESTRICTED | Yes | Yes | `shell`, `python_exec`, `http_request` |
| PRIVILEGED | Yes | Yes | `self_improve`, broker-mediated docker commands |

`_CAPABILITY_MAP` in `skills/builtin/__init__.py` maps every built-in to a level. Default for unmapped is CONTROLLED.

## Built-in skills (37)

### Web / Research

| Skill | Purpose |
|-------|---------|
| `fetch_url` | HTTP GET with retries; SSRF-protected (v2.6) |
| `http_request` | Generic HTTP (GET/POST/DELETE) with auth headers; SSRF-protected |
| `web_search` | Web search via configured provider |
| `browser` | Headless Chromium via Playwright; navigate, screenshot, click, type, capture |
| `browser_smart_navigate` | Intelligent navigation with retries and validation |
| `browser_screenshot_full_page` | Full-page screenshot capture |
| `browser_deep_scrape` | Playwright-based structured extraction |
| `browser_validator` | Page validation and capture truth-checking |
| `deep_scraper` | Alternative deep-scraping path; SSRF-protected via `_is_safe_url()` |
| `scrape` | Adaptive HTML/JSON extraction |

### Communication

| Skill | Purpose |
|-------|---------|
| `gmail` | IMAP read/search; SMTP send. Intent-gated. Recipient allowlist via `GMAIL_RECIPIENT_ALLOWLIST` (per-address or `@domain.com`). |
| `google_calendar` | Google Calendar API v3 — create/update/delete events |

### Productivity

| Skill | Purpose |
|-------|---------|
| `task_manager` | Create/list/trigger/delete recurring custom tasks (interval-only) |
| `reminders` | Create reminders (one-shot or recurring), can link to sub-agents |
| `delete_reminder` | Delete one or all reminders by keyword |
| `monitors` | Website change monitors (polled by `monitor_checker` job) |
| `notes` | Create/read/delete notes (`MemoryManager`-backed) |
| `subscribe` | RSS feeds and price alerts (polled by `subscription_checker`) |

### Code / System

| Skill | Purpose |
|-------|---------|
| `shell` | Execute shell commands. Default 60 s timeout, max 120 s. Audit-logged. |
| `python_exec` | Execute Python in containerized sandbox with AST-validated import allowlist |
| `file_ops` | Read/write/delete under `/data/` only |
| `system_info` | CPU/RAM/uptime via `psutil` |

### Information

| Skill | Purpose |
|-------|---------|
| `datetime_skill` | Current time with timezone support |
| `weather` | Weather lookup by city or coordinates |
| `calculate` | Math/expression evaluation |
| `translate` | Text translation via API |

### Data / Reports

| Skill | Purpose |
|-------|---------|
| `render_report` | Generate text/HTML/Markdown reports from data |
| `extract_fields` | Extract typed fields from JSON; intermediate-result storage in Redis context |

### Agent / Meta

| Skill | Purpose |
|-------|---------|
| `agent_manager` | Create/list/pause/resume/archive sub-agents. Intent-gated. |
| `skill_manager` | Create/enable/disable/delete custom Python skills at runtime |
| `meta_orchestrate` | Meta-Agent Supervisor: decompose objective into a team of sub-agents |
| `integration_skill` | Bridge to the `IntegrationRegistry` (44 connectors) |
| `openclaw` | ClawHub skill marketplace search/install/remove (optional) |

### Self-modification (PRIVILEGED)

| Skill | Purpose |
|-------|---------|
| `self_improve` | Read source files, propose patches, install Python packages, apply persisted patches. Supports `dry_run="true"` on `write` / `patch` to preview the unified diff + AST verdict without touching the file. |

## Skill execution pipeline

```
LLM response
   │
   ▼
parse_skill_calls()           ─ extracts <skill>name(...)</skill>
   │
   ▼
SkillExecutor.execute_batch()
   ├─ group_by_parallel()
   ├─ for each group:
   │     ├─ if 1 call: execute_one()
   │     └─ if >1 calls: asyncio.gather([execute_one(c) for c in group])
   │
   ▼
execute_one(call)
   ├─ skill = registry.get(call.name)
   ├─ level = capability_registry.get_level(call.name)
   ├─ if level in (RESTRICTED, PRIVILEGED):
   │     simulation = await anticipate(call, context)
   ├─ result = await skill.execute(**call.arguments)
   ├─ if requires_audit(level):
   │     write_audit_log(call, result)
   └─ result.output = redact(result.output)
   │
   ▼
Return SkillResult
```

## Parallel execution

Skills in `<parallel>` blocks run concurrently:

```xml
<parallel>
  <skill>web_search(query="BTC price")</skill>
  <skill>web_search(query="ETH price")</skill>
</parallel>
```

All skills in a parallel group share the same `parallel_group` ID. `execute_batch()` processes them with `asyncio.gather()`.

## Anticipatory simulation

Before executing RESTRICTED or PRIVILEGED skills, the simulation runs:

```python
async def anticipate(call: SkillCall, context: str) -> str:
    # LLM predicts outcome and risks; result appended for next-round self-reflection
    return f"[ANTICIPATORY SIMULATION]: {prediction}"
```

Cached in Redis for 5 min. Not a security control — a cognitive self-check.

## Custom Python skills

Operators can create custom skills at runtime via the `skill_manager` skill:

```
skill_manager(
    action="create",
    name="my-slug",
    description="...",
    params="param1,param2",
    code="<full Python class extending SkillBase>",
)
```

Saved at `/data/skills/<slug>/skill.py`. `load_all_python_skills()` at startup scans and registers all custom skills. They appear with type `python-custom` in `skill_manager(action="list")`.

Custom skills run in-process and inherit the agent's permissions. Review carefully before enabling.

## Adding a new built-in skill

1. Create `src/skills/builtin/<your_skill>.py` implementing `SkillBase`.
2. Register in `src/skills/builtin/__init__.py`:

   ```python
   from .your_skill import YourSkill
   _CAPABILITY_MAP["your_skill"] = CapabilityLevel.CONTROLLED
   ```

3. If side-effect skill, add intent regex to `policy/intent_gate.py`.
4. Add a regression case to `policy/regression_checks.py`.
5. Build: `docker compose build agent-core && docker compose up -d agent-core`.
6. Verify in `/skills`.

## See also

- [Skill Safety](/security/skill-safety) — capability levels, intent gating
- [Privilege Boundaries](/security/privilege-boundaries) — broker, shell, self-improve
- [Goal Engine](/core-concepts/goal-engine) — TaskGraph execution
- [Skill Evolution](/cognitive-systems/skill-evolution) — composite skill synthesis
- [Creating Skills](/development/creating-skills) — full development guide
