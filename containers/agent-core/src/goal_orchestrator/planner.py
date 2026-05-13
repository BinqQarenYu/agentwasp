"""PlanGenerator — uses ModelManager to build a validated TaskGraph from a Goal.

Design constraints:
  - Hard token cap (MAX_PLAN_TOKENS) to bound cost
  - Available skills listed within PLAN_SKILL_BUDGET chars
  - Retry up to MAX_PLAN_RETRIES times on invalid output
  - Returns strict (TaskGraph, "") on success, (None, error) on failure
  - Generated arguments are pre-filled so execution needs no secondary LLM call
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import structlog

from ..models.manager import ModelManager
from ..models.types import Message, ModelRequest
from ..skills.registry import SkillRegistry
from .types import Goal, RiskLevel, TaskGraph, TaskNode

logger = structlog.get_logger()

MAX_PLAN_RETRIES = 3
MAX_PLAN_TOKENS = 4000
PLAN_SKILL_BUDGET = 2000  # characters for skill list in prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_skills_for_prompt(registry: SkillRegistry) -> str:
    """Build a budget-capped skill reference for the planning prompt.

    Format:
      - skill_name(param1, param2): Description. Example: skill_name(param1="value")
    """
    lines: list[str] = []
    total = 0

    for defn in registry.list_enabled():
        # Build parameter signature
        params = ", ".join(
            f'{p.name}{"" if p.required else "?"}'
            for p in defn.params
            if p.name not in ("chat_id", "user_id")
        )
        sig = f"{defn.name}({params})"

        # Example arguments: up to 2 key params (required or commonly needed)
        ex_args: list[str] = []
        for p in defn.params:
            if p.name in ("chat_id", "user_id"):
                continue
            if p.required or p.name in ("url", "query", "code", "command", "text", "message"):
                ex_args.append(f'{p.name}="<value>"')
            if len(ex_args) >= 2:
                break

        ex = f'  e.g.: arguments={{{", ".join(ex_args)}}}' if ex_args else ""
        line = f"- {sig}: {defn.description[:80]}{ex}"
        lines.append(line)
        total += len(line)

        if total >= PLAN_SKILL_BUDGET:
            lines.append("... (more skills available)")
            break

    return "\n".join(lines)


def _build_plan_prompt(goal: Goal, skills_text: str) -> str:
    return f"""You are a deterministic planning engine for WASP, an autonomous AI agent.

GOAL OBJECTIVE:
{goal.objective}

CONSTRAINTS:
{goal.constraints or "None"}

SUCCESS CRITERIA:
{goal.success_criteria or "Objective successfully completed"}

AVAILABLE SKILLS:
{skills_text}

AUTONOMOUS SYSTEM SETUP — READ THIS FIRST:
If the objective describes setting up a recurring autonomous monitoring/analysis system (contains keywords like "cada hora", "cada día", "every hour", "automáticamente", "sistema autónomo", "monitoreo continuo", "executa cada") AND involves complex multi-step work per cycle (fetch + analyze + report + email OR analyze + generate + send):
→ THIS IS A SETUP GOAL. Plan ONLY the setup steps, NOT the execution steps.
→ ALWAYS use this exact 2-task pattern:
  t1: agent_manager(action="create", name="<AgentName>", description="<role>", identity_prompt="<full instructions for what to do each cycle>")
  t2: task_manager(action="create", name="<task_name>", instruction="Execute <AgentName> objective: <brief summary>", interval="cada hora")
→ Put ALL the execution logic (fetch, analyze, report, send email) into the identity_prompt of t1.
→ DO NOT create http_request, web_search, python_exec, browser, or gmail tasks in this plan — those belong inside the agent's scheduled execution, not in the setup plan.
→ If the objective says "crea un agente especializado" or "agente dedicado" — USE agent_manager. Not a goal. Not http_request.

INSTRUCTIONS:
Generate a JSON execution plan. Break the objective into 2-6 concrete tasks.
Each task must:
1. Use exactly ONE skill from the available list (exact name)
2. Include all required arguments pre-filled with real values (not placeholders)
3. Reference only valid task IDs in dependencies
4. Use risk_level: "low" (read-only), "medium" (scoped writes), "high" (system ops)
5. Have 0 max_retries for simple tasks, up to 2 for critical ones

CRITICAL RULES FOR SCHEDULED TASKS:
- If the objective starts with "[TAREA PROGRAMADA:" — this is a recurring scheduled execution. Keep it SIMPLE: 1-3 tasks maximum. No loops, no "monitor continuously", no creating new tasks or reminders.
- "Monitorea continuamente" in a scheduled task = just do ONE fetch+send cycle right now. Not a loop.
- NEVER create task_manager or create_reminder tasks inside a scheduled task execution.
- NEVER create agent_manager tasks inside a scheduled task execution.

CRITICAL PLANNING RULES:
- NEVER create a plan with circular dependencies.
- NEVER plan tasks that "monitor", "loop", or "repeat" — do the action ONCE.
- If the goal says "do X and send" — plan: t1=do X, t2=send result. Done.
- Prefer fewer tasks over many. 2 tasks that work > 6 tasks that might fail.

SKILL SELECTION RULES — use the RIGHT skill for each action type:

FETCH DATA (prices, APIs, web content):
  → fetch_url(url="https://...") or http_request(method="GET", url="https://...")
  CRYPTO APIS (free, no key, no rate limits):
    Binance BTC price: https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT
    Binance ETH price: https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT
    Binance 24h stats: https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT
    CoinGecko (may rate-limit): https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd,usd&include_24hr_change=true&include_market_cap=true
  USE BINANCE AS PRIMARY for crypto prices — it has no rate limits and is more reliable.

PROCESS DATA / GENERATE REPORTS / CREATE FILES / CHARTS:
  → python_exec(code="...") — write Python code that processes data, generates text, saves files
  For historical storage: write JSON to /data/memory/crypto_history.json
  IMPORTANT: Always initialize files with try/except — the file may not exist on first run:
    import json, os
    history = []
    if os.path.exists("/data/memory/crypto_history.json"):
        with open("/data/memory/crypto_history.json") as f: history = json.load(f)
  For charts: use matplotlib, save to /data/screenshots/crypto_chart.png
  IMPORTANT: All numeric data from APIs may be strings or None — always cast safely:
    price = float(str(data.get("price", 0) or 0))
    change = float(str(data.get("priceChangePercent", 0) or 0))

SEND EMAIL:
  → gmail(action="send", to="address@example.com", subject="Subject", body="Body text here")
  Gmail credentials are shared across ALL agents via Redis — NO configuration step needed.
  Sub-agents inherit the same Gmail account as Agent Wasp automatically.
  Just call gmail(action="send", ...) directly.
  NEVER use create_reminder or fetch_url for sending email.
  NEVER call gmail(action="configure") unless the user explicitly provides new credentials.

TAKE SCREENSHOT of a webpage:
  → browser(action="capture", url="https://...", session="s1")
  Use for: https://www.coingecko.com/en/coins/bitcoin or https://www.coingecko.com/en/coins/ethereum

DELETE ALL TASKS / GOALS / AGENTS (when user asks to clear/wipe/delete everything):
  → agent_manager(action="wipe_all")  — ONE call that deletes ALL tasks + ALL goals + ALL agents at once
  "elimina todo", "borra todo", "delete everything", "elimina todas las tareas", "elimina todos los goals",
  "elimina todos los agentes" — use ONLY: agent_manager(action="wipe_all")
  NEVER use 3 separate delete calls. ONE call to wipe_all handles everything.

SCHEDULE RECURRING TASK (run every N hours/minutes):
  → task_manager(action="create", name="Task name", instruction="what to do", interval="cada hora")
  The 'interval' parameter MUST be a human-readable string. Valid values:
    "cada hora" (hourly), "cada 2h" (every 2 hours), "cada 30 minutos" (every 30 min)
    "diario" (daily), "semanal" (weekly)
  NEVER use interval_seconds — the parameter name is exactly "interval" with a human-readable value.
  Use this for "every hour", "every day", "automatically repeat" tasks.
  NEVER use create_reminder for recurring scheduling.
  NEVER use "custom_task" — the correct skill name is task_manager.

SEARCH THE WEB:
  → web_search(query="...", max_results="5")

RUN SHELL COMMANDS:
  → shell(command="...")

CREATE AGENT (use when objective explicitly requests it OR task requires autonomous recurring specialized behavior):
  → agent_manager(action="create", name="...", description="...", identity_prompt="...")
  WHEN TO CREATE AN AGENT instead of (or in addition to) a custom_task:
    • Objective says "crea un agente", "create an agent", "agente especializado", "agente dedicado"
    • Task requires a persistent autonomous entity with its own identity, memory, and decision-making
    • The recurring task is complex (multi-step analysis, report generation, decision-making) — not just a simple fetch
    • The user wants an independent system that can adapt and self-correct, not just a scheduled command
  AGENT CREATION PATTERN for complex recurring tasks:
    t1: agent_manager(action="create", name="CryptoAnalystAgent", description="...", identity_prompt="...")
    t2: task_manager(action="create", name="crypto_hourly", instruction="Run CryptoAnalystAgent objective: ...", interval="cada hora")
  NOTE: For SIMPLE recurring tasks (just fetch a price and notify), use ONLY task_manager.
        For COMPLEX recurring tasks (analyze, compare, generate reports, email), create agent + task_manager.

DELETE REMINDER:
  → delete_reminder(keyword="text to match") or delete_reminder(keyword="all")
  Use when: user asks to cancel, delete, or remove a reminder.
  NEVER claim to delete a reminder without calling this skill.

STEP PREFERENCE ORDER (use the FIRST applicable approach, not LLM reasoning):
1. DETERMINISTIC TOOL: shell(), python_exec(), http_request() — if the task can be done with a script
2. EXISTING SKILL: web_search(), gmail(), browser(), subscribe() — if a built-in skill handles it
3. LLM ONLY as last resort — only when no tool or skill can accomplish the task

IMPORTANT RULES:
- create_reminder is ONLY for one-time user notifications (e.g. "remind me at 3pm"). NEVER for data storage, email, or scheduling.
- NEVER use placeholder URLs like "example.com". Use REAL working URLs.
- If objective is to delete something already gone (404/not found): treat as SUCCESS, no retry tasks.
- No cycles in dependencies. Task IDs: "t1", "t2", etc.
- Keep plans COMPACT: prefer 3-5 steps. Split into sub-goals rather than making plans longer than 8 steps.

OUTPUT (JSON only — no markdown, no explanation, no code blocks):
{{
  "nodes": [
    {{
      "id": "t1",
      "description": "What this task accomplishes",
      "skill_name": "exact_skill_name",
      "arguments": {{"param1": "value1", "param2": "value2"}},
      "required_capability": "monitored",
      "risk_level": "low",
      "dependencies": [],
      "max_retries": 2
    }},
    {{
      "id": "t2",
      "description": "Next task that depends on t1",
      "skill_name": "another_skill",
      "arguments": {{"key": "value"}},
      "required_capability": "controlled",
      "risk_level": "medium",
      "dependencies": ["t1"],
      "max_retries": 1
    }}
  ]
}}"""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract and parse JSON from LLM response.

    Handles markdown code blocks and leading/trailing whitespace.
    """
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.strip().rstrip("`").strip()

    # Locate outermost JSON object
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        return None

    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def _validate_and_build_graph(
    data: dict[str, Any],
    registry: SkillRegistry,
) -> tuple[TaskGraph | None, str]:
    """Validate plan data and construct a TaskGraph.

    Returns (graph, "") on success, (None, error_message) on failure.
    """
    if not isinstance(data.get("nodes"), list):
        return None, "Missing 'nodes' array in plan"

    nodes_raw = data["nodes"]

    if len(nodes_raw) == 0:
        return None, "Plan has no tasks"

    if len(nodes_raw) > 20:
        return None, f"Plan has too many tasks ({len(nodes_raw)} > 20 max)"

    enabled_skills = {d.name for d in registry.list_enabled()}
    nodes: list[TaskNode] = []
    seen_ids: set[str] = set()

    for i, raw in enumerate(nodes_raw):
        if not isinstance(raw, dict):
            return None, f"Node #{i} is not a JSON object"

        node_id = str(raw.get("id", f"auto_{i}")).strip()
        if not node_id:
            return None, f"Node #{i} has empty id"
        if node_id in seen_ids:
            return None, f"Duplicate task id: '{node_id}'"
        seen_ids.add(node_id)

        skill_name = str(raw.get("skill_name", "")).strip()
        if not skill_name:
            return None, f"Task '{node_id}' has no skill_name"
        if skill_name not in enabled_skills:
            return None, f"Task '{node_id}' references unknown/disabled skill '{skill_name}'"

        description = str(raw.get("description", ""))[:300]
        if not description:
            return None, f"Task '{node_id}' has empty description"

        # Parse arguments — must be dict of strings
        raw_args = raw.get("arguments", {})
        if not isinstance(raw_args, dict):
            raw_args = {}
        arguments = {str(k): str(v) for k, v in raw_args.items()}

        # Parse risk level
        try:
            risk = RiskLevel(raw.get("risk_level", "low"))
        except ValueError:
            risk = RiskLevel.LOW

        deps = [str(d) for d in raw.get("dependencies", []) if d]
        max_retries = max(0, min(int(raw.get("max_retries", 2)), 5))

        nodes.append(
            TaskNode(
                id=node_id,
                description=description,
                skill_name=skill_name,
                arguments=arguments,
                required_capability=str(raw.get("required_capability", "controlled")),
                risk_level=risk,
                dependencies=deps,
                max_retries=max_retries,
            )
        )

    graph = TaskGraph(nodes=nodes)
    valid, err = graph.validate_dag()
    if not valid:
        return None, err

    return graph, ""


# ---------------------------------------------------------------------------
# PlanGenerator
# ---------------------------------------------------------------------------


class PlanGenerator:
    """Generates execution plans via ModelManager.

    Flow:
      build prompt → call LLM → extract JSON → validate graph
      retry up to MAX_PLAN_RETRIES times on failure
    """

    def __init__(self, model_manager: ModelManager, skill_registry: SkillRegistry):
        self.model_manager = model_manager
        self.skill_registry = skill_registry

    async def generate(self, goal: Goal) -> tuple[TaskGraph | None, str, int]:
        """Generate and validate a TaskGraph for the given goal.

        Returns (task_graph, "", tokens_used) on success.
        Returns (None, error_message, tokens_used) on failure after all retries.

        tokens_used accumulates across all attempts so the caller can update
        the cognitive budget accurately.
        """
        skills_text = _format_skills_for_prompt(self.skill_registry)
        base_prompt = _build_plan_prompt(goal, skills_text)
        prompt = base_prompt
        tokens_total = 0

        for attempt in range(MAX_PLAN_RETRIES + 1):
            t0 = time.monotonic()

            try:
                response = await self.model_manager.generate(
                    ModelRequest(
                        messages=[
                            Message(
                                role="system",
                                content=(
                                    "You are a precise JSON planning engine. "
                                    "Output ONLY valid JSON. No explanation. "
                                    "No markdown. No code blocks."
                                ),
                            ),
                            Message(role="user", content=prompt),
                        ],
                        temperature=0.1,
                        max_tokens=MAX_PLAN_TOKENS,
                    )
                )
            except Exception as e:
                logger.exception(
                    "planner.llm_error", goal_id=goal.id, attempt=attempt + 1
                )
                if attempt < MAX_PLAN_RETRIES:
                    prompt = base_prompt + f"\n\n[Previous attempt failed: {e}. Try again.]"
                    continue
                return None, f"LLM error during planning: {e}", tokens_total

            tokens_this = getattr(getattr(response, "usage", None), "total_tokens", 0)
            tokens_total += tokens_this
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "planner.llm_response",
                goal_id=goal.id,
                attempt=attempt + 1,
                tokens=tokens_this,
                tokens_total=tokens_total,
                latency_ms=latency_ms,
                model=response.model_used,
            )

            data = _extract_json(response.content)
            if data is None:
                logger.warning(
                    "planner.invalid_json",
                    goal_id=goal.id,
                    attempt=attempt + 1,
                    preview=response.content[:200],
                )
                if attempt < MAX_PLAN_RETRIES:
                    prompt = (
                        base_prompt
                        + "\n\n[Previous response was not valid JSON. "
                        "Output ONLY the JSON object, nothing else.]"
                    )
                    continue
                return None, "LLM returned invalid JSON after all retries", tokens_total

            graph, error = _validate_and_build_graph(data, self.skill_registry)
            if graph is None:
                logger.warning(
                    "planner.invalid_plan",
                    goal_id=goal.id,
                    attempt=attempt + 1,
                    error=error,
                )
                if attempt < MAX_PLAN_RETRIES:
                    prompt = (
                        base_prompt
                        + f"\n\n[Previous plan was invalid: {error}. Fix this issue and try again.]"
                    )
                    continue
                return None, f"Plan validation failed: {error}", tokens_total

            logger.info(
                "planner.plan_generated",
                goal_id=goal.id,
                tasks=graph.total_tasks,
                attempt=attempt + 1,
                tokens_total=tokens_total,
            )
            return graph, "", tokens_total

        return None, "Plan generation exhausted all retries", tokens_total
