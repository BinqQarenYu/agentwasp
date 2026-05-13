"""Goal Templates — save, version and reuse successful TaskGraphs.

Templates strip sensitive argument values and project-specific data
so they are safe to store and share between goals.

Storage: Redis HASH "goal_templates"
  key   = template_id
  value = GoalTemplate JSON

Templates never store:
  - Secret / credential argument values
  - Long project-specific argument strings (>= 200 chars)
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from uuid import uuid4

import structlog
from pydantic import BaseModel, Field

from .types import Goal, TaskGraph, TaskStatus

logger = structlog.get_logger()

REDIS_KEY = "goal_templates"
MAX_TEMPLATES = 100

# Argument keys whose values are always redacted
_SENSITIVE_KEYWORDS = frozenset([
    "password", "secret", "token", "key", "credential",
    "auth", "api_key", "access_key", "private",
])


# ---------------------------------------------------------------------------
# Template model
# ---------------------------------------------------------------------------


class GoalTemplate(BaseModel):
    """A reusable TaskGraph extracted from a successful goal execution."""

    id: str = Field(default_factory=lambda: str(uuid4())[:12])
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    # Serialized TaskGraph (arguments sanitized)
    task_graph_json: str

    # Versioning
    version: int = 1

    # Usage metrics
    total_uses: int = 0
    successful_uses: int = 0
    avg_completion_seconds: float = 0.0
    success_rate: float = 1.0

    # Timestamps
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_used: str | None = None

    @property
    def task_count(self) -> int:
        """Number of tasks in the template's TaskGraph."""
        try:
            graph = TaskGraph.model_validate_json(self.task_graph_json)
            return len(graph.nodes)
        except Exception:
            return 0

    @property
    def use_count(self) -> int:
        """Alias for total_uses (convenience for templates)."""
        return self.total_uses

    def load_task_graph(self) -> TaskGraph:
        """Deserialize and return a fresh TaskGraph with all tasks PENDING."""
        graph = TaskGraph.model_validate_json(self.task_graph_json)
        # Reset all statuses to PENDING (template is a blueprint)
        for node in graph.nodes:
            node.status = TaskStatus.PENDING
            node.retries = 0
            node.output_summary = ""
            node.error = ""
            node.started_at = None
            node.completed_at = None
        return graph

    def record_use(self, success: bool, completion_seconds: float = 0.0) -> None:
        """Update usage statistics after a goal using this template completes."""
        self.total_uses += 1
        if success:
            self.successful_uses += 1
        self.success_rate = (
            self.successful_uses / self.total_uses if self.total_uses > 0 else 0.0
        )
        if success and completion_seconds > 0:
            if self.avg_completion_seconds <= 0:
                self.avg_completion_seconds = completion_seconds
            else:
                self.avg_completion_seconds = (
                    self.avg_completion_seconds * 0.8 + completion_seconds * 0.2
                )
        self.last_used = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_template_from_goal(
    goal: Goal,
    name: str,
    description: str = "",
    tags: list[str] | None = None,
) -> GoalTemplate:
    """Create a GoalTemplate from a completed goal's TaskGraph.

    Sanitization rules:
    - Argument keys matching _SENSITIVE_KEYWORDS → value replaced with "<SECRET>"
    - Argument values >= 200 chars → replaced with "<VALUE>" (project-specific)
    - All task statuses reset to PENDING
    """
    clean_graph = copy.deepcopy(goal.task_graph)

    for node in clean_graph.nodes:
        # Reset execution state
        node.status = TaskStatus.PENDING
        node.retries = 0
        node.output_summary = ""
        node.error = ""
        node.started_at = None
        node.completed_at = None

        # Sanitize argument values
        clean_args: dict[str, str] = {}
        for k, v in node.arguments.items():
            k_lower = k.lower()
            if any(kw in k_lower for kw in _SENSITIVE_KEYWORDS):
                clean_args[k] = "<SECRET>"
            elif len(str(v)) >= 200:
                clean_args[k] = "<VALUE>"
            else:
                clean_args[k] = v
        node.arguments = clean_args

    return GoalTemplate(
        name=name[:100],
        description=(description or f"Template from: {goal.objective[:120]}")[:300],
        tags=tags or [],
        task_graph_json=clean_graph.model_dump_json(),
    )


# ---------------------------------------------------------------------------
# Redis CRUD
# ---------------------------------------------------------------------------


async def save_template(r, template: GoalTemplate) -> None:
    await r.hset(REDIS_KEY, template.id, template.model_dump_json())
    logger.info(
        "goal_template.saved",
        template_id=template.id,
        name=template.name,
        version=template.version,
    )


async def load_template(r, template_id: str) -> GoalTemplate | None:
    raw = await r.hget(REDIS_KEY, template_id)
    if not raw:
        return None
    try:
        return GoalTemplate.model_validate_json(raw)
    except Exception:
        logger.warning("goal_template.load_error", template_id=template_id)
        return None


async def list_templates(r) -> list[GoalTemplate]:
    raw_map = await r.hgetall(REDIS_KEY)
    templates: list[GoalTemplate] = []
    for v in raw_map.values():
        try:
            templates.append(GoalTemplate.model_validate_json(v))
        except Exception:
            pass
    return sorted(templates, key=lambda t: t.created_at, reverse=True)


async def delete_template(r, template_id: str) -> bool:
    result = await r.hdel(REDIS_KEY, template_id)
    if result:
        logger.info("goal_template.deleted", template_id=template_id)
    return bool(result)


async def update_template_stats(
    r,
    template_id: str,
    success: bool,
    completion_seconds: float = 0.0,
) -> None:
    """Update template usage statistics after a goal completes."""
    tmpl = await load_template(r, template_id)
    if tmpl is None:
        return
    tmpl.record_use(success, completion_seconds)
    await save_template(r, tmpl)
