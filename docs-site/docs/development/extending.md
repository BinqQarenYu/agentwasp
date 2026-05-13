---
id: extending
title: Extending WASP
description: Adding scheduler jobs, integrations, memory layers, dashboard pages.
---

# Extending WASP

Beyond skills, you can extend WASP with new scheduler jobs, integration connectors, memory layers, and dashboard pages.

## Adding a scheduler job

### Step 1 — Create the job class

`src/scheduler/your_job.py`:

```python
import structlog

logger = structlog.get_logger()

class YourJob:
    """One-line description of what the job does."""

    def __init__(self, *, dependency_a, dependency_b):
        self.dep_a = dependency_a
        self.dep_b = dependency_b

    async def __call__(self) -> str:
        try:
            # ... your logic ...
            logger.info("your_job.tick_complete", count=42)
            return "ok"
        except Exception as e:
            logger.error("your_job.failed", error=str(e)[:200])
            raise
```

The `__call__` method must be async and return a string status (or raise on failure).

### Step 2 — Register in `main.py`

```python
from src.scheduler.your_job import YourJob

scheduler.register(
    "your_job",         # name (used for /scheduler URL)
    300,                # interval in seconds
    YourJob(dependency_a=..., dependency_b=...),
)
```

### Step 3 — Feature-flag (optional)

In `config.py`:

```python
your_job_enabled: bool = Field(default=True, description="...")
```

In `main.py`:

```python
if settings.your_job_enabled:
    scheduler.register("your_job", 300, YourJob(...))
```

This lets the operator toggle via `/config`.

### Step 4 — Add observability

The dashboard `/scheduler` page automatically shows registered jobs. Add structured log events with consistent `event=your_job.<verb>` naming so logs are filterable.

## Adding an integration connector

### Step 1 — Implement the connector

`src/integrations/connectors/your_connector.py`:

```python
from src.integrations.base import BaseConnector, ConnectorManifest, IntegrationError

class YourConnector(BaseConnector):
    integration_id = "your-service"

    manifest = ConnectorManifest(
        name="Your Service",
        description="...",
        actions={
            "send_message": {"params": ["channel", "text"], "risk_level": "medium"},
            "list_channels": {"params": [], "risk_level": "low"},
        },
    )

    async def execute(self, action: str, params: dict) -> dict:
        if action == "send_message":
            return await self._send(params["channel"], params["text"])
        elif action == "list_channels":
            return await self._list()
        else:
            raise IntegrationError(f"Unknown action: {action}")
```

### Step 2 — Register in `main.py`

```python
from src.integrations.connectors.your_connector import YourConnector

integration_registry.register(YourConnector())
```

### Step 3 — Vault setup

If the connector needs secrets:

```python
# In .env or via /integrations:
YOUR_SERVICE_API_KEY=...
```

The `SecretVault` automatically picks up env vars matching the connector's `integration_id`.

### Step 4 — Test

Through the agent: *"send a test message to channel #general using your-service integration"*. The agent will call `integration_skill(integration_id="your-service", action="send_message", params=...)`.

## Adding a memory layer

A new memory layer is more involved. You need:

1. A SQLAlchemy table in `db/models.py`.
2. A module in `src/memory/<layer>.py` with `add_*`, `query_*`, `format_for_context()` functions.
3. Hook into `MemoryManager` in `src/memory/manager.py`.
4. Inject into the system prompt via `Context Builder` in `memory/context_builder.py`.

See [Memory](/core-concepts/memory) for architecture; existing layers like `behavioral.py` or `procedural.py` are good templates.

## Adding a dashboard page

### Step 1 — Create the route

`src/dashboard/routes/your_page.py`:

```python
from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from src.dashboard.auth import require_admin

router = APIRouter()
templates = Jinja2Templates(directory="src/dashboard/templates")

@router.get("/your-page", response_class=HTMLResponse)
async def your_page(request: Request, _: dict = Depends(require_admin)):
    # ... gather data ...
    return templates.TemplateResponse(request, "your_page.html", {"data": ...})
```

### Step 2 — Register the router

In `src/dashboard/app.py`:

```python
from src.dashboard.routes.your_page import router as your_page_router
app.include_router(your_page_router)
```

### Step 3 — Add the template

`src/dashboard/templates/your_page.html` extending the base layout.

### Step 4 — Add to sidebar

Update the sidebar template (e.g., `src/dashboard/templates/_sidebar.html`) with a link.

### Step 5 — Rebuild

```bash
docker compose build agent-core
docker compose up -d agent-core
```

For static template/CSS changes only, the dashboard auto-reloads; rebuild is needed for new Python imports.

## Adding a regression case

When you fix a policy bug, add a regression for it. See [Testing and Audit](/security/testing-and-audit).

## Modifying `prime.md`

`prime.md` is the operator override prompt. Edit it via `/config` or directly in ``/data/config/prime.md` (inside agent-core) or the dashboard `/config` page`. Changes take effect on the next message — no rebuild required.

If your changes are policy-relevant, also update `prime.default.md` to keep them in sync. `diff prime.md prime.default.md` must return empty at release time.

## Adding a model provider

`src/models/manager.py`. Add a new provider class implementing the `ModelProvider` interface, then register in `ModelManager.__init__()`. See existing providers (`anthropic.py`, `openai.py`) as templates.

## Self-modification via `self_improve`

The `self_improve` skill (PRIVILEGED) lets the agent itself read, propose, and apply patches. Operator approval at `/self-improve` is required for every apply. See [Privilege Boundaries → Self-Improve](/security/privilege-boundaries#self-improve-skill-privileged).

## See also

- [Project Structure](/development/project-structure)
- [Creating Skills](/development/creating-skills)
- [Architecture → Orchestration](/architecture/orchestration)
- [Testing and Audit](/security/testing-and-audit)
