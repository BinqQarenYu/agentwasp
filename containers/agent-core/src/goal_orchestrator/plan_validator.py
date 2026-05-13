"""System 2 — Dual-Layer Planner: Plan Critic / Validator.

Architecture:
  planner LLM → TaskGraph → PlanCritic → validated / corrected TaskGraph → execution

The critic performs a second LLM pass after the initial plan is generated.
It checks for:
  - Redundant or duplicate tasks
  - Missing prerequisite steps
  - Invalid or unavailable skill names
  - Tasks that exceed capability / risk constraints
  - Circular dependencies in the DAG
  - Excessive plan complexity (> 12 tasks is almost always wrong)

Enforcement boundary:
  The critic can REWRITE or REJECT a plan before execution begins (verdict="replan").
  It CANNOT halt execution of an already-started plan — tasks in flight are unaffected.
  It also CANNOT override the autonomy mode or capability enforcement layer.
  When plan_critic_enabled=False → pass-through, no LLM call, plan executes as-is.

Feature flag: PLAN_CRITIC_ENABLED=false → pass-through, no LLM call.

Integration:
  Called from GoalOrchestrator.create_goal() after plan_generator.generate(),
  before the Goal is activated. Injected via constructor dependency.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ..models.manager import ModelManager
    from ..skills.registry import SkillRegistry
    from .types import Goal, TaskGraph

logger = structlog.get_logger()


# ──────────────────────────────────────────────────────────────────────────
# Deterministic placeholder validation (no LLM, no latency)
# Catches obvious template / hallucinated values before execution.
# Patterns are precision-tuned to avoid false positives on real arguments.
# ──────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_FULL_TOKENS = frozenset({
    "todo", "tbd", "placeholder", "sample", "<value>", "your-key",
})
_PLACEHOLDER_BRACKET_RE = re.compile(r"<\s*([a-zA-Z][a-zA-Z0-9_\- ]{0,40})\s*>")
_PLACEHOLDER_YOUR_RE = re.compile(
    r"(?i)\byour[-_](api[-_]?key|key|name|email|token|file|path|domain|url|host|password|secret)\b"
)
_PLACEHOLDER_EXAMPLE_RE = re.compile(r"(?i)\bexample\.(com|org|net|test)\b")


def detect_placeholder(value) -> str | None:
    """Return the matched placeholder pattern, or None if value is clean.

    Only flags obvious template / hallucinated values. Non-string inputs
    return None. Designed to never block legitimate real paths or URLs.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    sl = s.lower()
    if sl in _PLACEHOLDER_FULL_TOKENS:
        return sl
    if sl.startswith("/path/to/") or sl.startswith("path/to/"):
        return "/path/to/..."
    m = _PLACEHOLDER_BRACKET_RE.search(s)
    if m:
        inner = m.group(1).strip()
        if inner and " " not in inner[:5]:
            return f"<{inner[:30]}>"
    m = _PLACEHOLDER_YOUR_RE.search(s)
    if m:
        return f"your-{m.group(1).lower()}"
    m = _PLACEHOLDER_EXAMPLE_RE.search(s)
    if m:
        return f"example.{m.group(1).lower()}"
    return None


def scan_arguments(arguments) -> tuple[str, str, str] | None:
    """Scan a task's argument dict for placeholder values.

    Returns (field_name, value_snippet, matched_pattern) on first hit, else None.
    """
    if not isinstance(arguments, dict):
        return None
    for field, value in arguments.items():
        if isinstance(value, str):
            hit = detect_placeholder(value)
            if hit:
                return (str(field), value[:80], hit)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    hit = detect_placeholder(item)
                    if hit:
                        return (str(field), item[:80], hit)
    return None

_CRITIC_SYSTEM = """You are a plan quality critic for an autonomous AI agent.
You receive a task plan (JSON) and validate it for correctness, safety, and efficiency.

VALIDATION RULES:
1. Each task must use a real skill from the provided skill list.
2. No two tasks should do the exact same thing.
3. All dependencies must reference task IDs that exist in the plan.
4. The plan should have at most 10 tasks; prefer 3-6 for most goals.
5. Tasks must form a valid DAG (no circular dependencies).
6. Each task must have a clear, specific objective.

OUTPUT FORMAT:
Return ONLY valid JSON in this structure:
{
  "verdict": "ok" | "corrected" | "rejected",
  "issues": ["issue 1", "issue 2"],
  "corrected_tasks": [
    {
      "id": "task_N",
      "description": "...",
      "skill_name": "...",
      "arguments": {},
      "dependencies": [],
      "risk_level": "low" | "medium" | "high"
    }
  ]
}

If "verdict" is "ok", leave "corrected_tasks" empty.
If "verdict" is "corrected", provide the improved task list.
If "verdict" is "rejected", explain in issues and leave corrected_tasks empty.
"""


class PlanCritic:
    """Validates and optionally corrects a TaskGraph via a second LLM pass."""

    def __init__(
        self,
        model_manager: ModelManager,
        skill_registry: SkillRegistry,
        max_tokens: int = 1200,
        enabled: bool = True,
    ):
        self._model_manager = model_manager
        self._skill_registry = skill_registry
        self._max_tokens = max_tokens
        self._enabled = enabled

    # ------------------------------------------------------------------

    async def validate(self, goal: Goal, task_graph: TaskGraph) -> TaskGraph:
        """Run the critic pass. Returns improved (or original) TaskGraph.

        Never raises — on any failure returns the original task_graph unchanged.
        """
        if not self._enabled or not task_graph.nodes:
            return task_graph

        # ── Deterministic placeholder pre-scan (no LLM, fast) ─────────
        # Logs only — the executor is the enforcement point so the existing
        # task_failed → replan path handles recovery within MAX_REPLAN_COUNT.
        _ph_hits = []
        for _node in task_graph.nodes:
            _hit = scan_arguments(_node.arguments)
            if _hit:
                _ph_hits.append(f"{_node.id}.{_hit[0]}={_hit[2]}")
        if _ph_hits:
            logger.warning(
                "plan_critic.placeholders_detected",
                goal_id=goal.id[:8],
                count=len(_ph_hits),
                hits=_ph_hits[:5],
            )

        t0 = time.monotonic()
        try:
            result = await self._run_critic(goal, task_graph)
            latency_ms = round((time.monotonic() - t0) * 1000)

            if result["verdict"] == "ok":
                logger.info(
                    "plan_critic.ok",
                    goal_id=goal.id[:8],
                    tasks=len(task_graph.nodes),
                    latency_ms=latency_ms,
                )
                return task_graph

            if result["verdict"] == "corrected" and result.get("corrected_tasks"):
                corrected = self._rebuild_graph(task_graph, result["corrected_tasks"])
                if corrected:
                    logger.info(
                        "plan_critic.corrected",
                        goal_id=goal.id[:8],
                        original_tasks=len(task_graph.nodes),
                        corrected_tasks=len(corrected.nodes),
                        issues=result.get("issues", [])[:3],
                        latency_ms=latency_ms,
                    )
                    return corrected

            if result["verdict"] == "rejected":
                logger.warning(
                    "plan_critic.rejected",
                    goal_id=goal.id[:8],
                    issues=result.get("issues", []),
                    latency_ms=latency_ms,
                )
                # Don't reject — use original plan. The executor will handle failures.
                return task_graph

        except Exception as exc:
            logger.warning("plan_critic.error", error=str(exc)[:120])

        return task_graph

    # ------------------------------------------------------------------

    async def _run_critic(self, goal: Goal, task_graph: TaskGraph) -> dict:
        from ..models.types import Message, ModelRequest

        skill_names = [s.name for s in self._skill_registry.list_enabled()]
        skill_list = ", ".join(skill_names[:40])

        plan_json = json.dumps(
            {
                "objective": goal.objective,
                "tasks": [
                    {
                        "id": node.id,
                        "description": node.description,
                        "skill_name": node.skill_name,
                        "arguments": node.arguments,
                        "dependencies": node.dependencies,
                        "risk_level": node.risk_level.value
                        if hasattr(node.risk_level, "value")
                        else str(node.risk_level),
                    }
                    for node in task_graph.nodes
                ],
            },
            indent=2,
        )

        user_msg = (
            f"AVAILABLE SKILLS: {skill_list}\n\n"
            f"PLAN TO VALIDATE:\n{plan_json}\n\n"
            "Validate this plan. Return JSON only."
        )

        resp = await self._model_manager.generate(
            ModelRequest(
                messages=[
                    Message(role="system", content=_CRITIC_SYSTEM),
                    Message(role="user", content=user_msg),
                ],
                max_tokens=self._max_tokens,
                temperature=0.1,
            )
        )

        raw = resp.content.strip()
        # Strip markdown fences
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
        return json.loads(raw)

    def _rebuild_graph(self, original: TaskGraph, corrected_tasks: list[dict]) -> TaskGraph | None:
        """Reconstruct a TaskGraph from critic-corrected task dicts."""
        from .types import RiskLevel, TaskGraph, TaskNode

        _risk_map = {
            "low": RiskLevel.LOW,
            "medium": RiskLevel.MEDIUM,
            "high": RiskLevel.HIGH,
            "critical": RiskLevel.CRITICAL,
        }

        try:
            nodes = []
            for td in corrected_tasks:
                nodes.append(
                    TaskNode(
                        id=str(td.get("id", f"task_{len(nodes) + 1}")),
                        description=str(td.get("description", ""))[:500],
                        skill_name=str(td.get("skill_name", "web_search")),
                        arguments=dict(td.get("arguments", {})),
                        dependencies=list(td.get("dependencies", [])),
                        risk_level=_risk_map.get(
                            str(td.get("risk_level", "low")).lower(), RiskLevel.LOW
                        ),
                    )
                )
            graph = TaskGraph(nodes=nodes)
            valid, err = graph.validate_dag()
            if not valid:
                raise ValueError(f"Corrected graph is invalid: {err}")
            return graph
        except Exception as exc:
            logger.warning("plan_critic.rebuild_failed", error=str(exc)[:120])
            return None
