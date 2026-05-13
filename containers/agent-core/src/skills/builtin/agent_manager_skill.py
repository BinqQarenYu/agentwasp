"""Agent Manager Skill — create and manage autonomous agents via the LLM.

The agent can:
- create: spawn a new named agent with a specific purpose and schedule
- list: see all agents and their status
- pause / resume / archive / delete: lifecycle control
- delete_all / archive_all: bulk operations
- send_message: send a task directive to another agent
"""

from __future__ import annotations

import structlog

from ..base import SkillBase
from ..types import SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

_SKILL_NAME = "agent_manager"


class AgentManagerSkill(SkillBase):
    """Create and manage autonomous sub-agents."""

    def __init__(self, agent_orchestrator=None):
        self._orch = agent_orchestrator

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=_SKILL_NAME,
            description=(
                "Create and manage autonomous agents. Use this to spawn a new agent "
                "that runs scheduled tasks independently, or to list, pause, resume, "
                "archive, or delete existing agents."
            ),
            params=[
                SkillParam(
                    name="action",
                    description=(
                        "Action: create, list, pause, resume, archive, delete, "
                        "delete_all, archive_all, send_message"
                    ),
                    required=True,
                ),
                SkillParam(
                    name="name",
                    description="Agent name (for create, or to identify by name instead of ID)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="description",
                    description="What this agent does (for create)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="identity_prompt",
                    description=(
                        "Custom system prompt / instructions for the new agent "
                        "(e.g. 'Check the USD/CLP exchange rate daily at 9:00 and send it to chat_id=XXX')"
                    ),
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="autonomy_mode",
                    description="Autonomy mode: assist, semi, full (default: semi)",
                    required=False,
                    default="semi",
                ),
                SkillParam(
                    name="agent_id",
                    description="Agent ID (for pause/resume/archive/delete/send_message). Also accepts agent name.",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="message",
                    description="Message content (for send_message action)",
                    required=False,
                    default="",
                ),
            ],
        )

    async def execute(self, **params) -> SkillResult:
        if self._orch is None:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="Agent orchestrator not available. Multi-agent system is not enabled.",
                success=False,
                error="agent_orchestrator not initialized",
            )

        action = params.get("action", "").strip().lower()

        try:
            if action == "create":
                return await self._create(params)
            elif action == "list":
                return await self._list()
            elif action in ("pause", "resume", "archive"):
                return await self._lifecycle(action, params)
            elif action == "delete":
                return await self._delete(params)
            elif action == "delete_all":
                return await self._delete_all()
            elif action in ("wipe_all", "reset_all", "clear_all", "borrar_todo", "eliminar_todo",
                            "delete_goals_and_agents", "eliminar_goals_y_agentes"):
                return await self._wipe_goals_and_agents()
            elif action == "archive_all":
                return await self._archive_all()
            elif action == "send_message":
                return await self._send_message(params)
            elif action in ("list_goals", "goals"):
                return await self._list_goals(params)
            elif action in ("delete_goal", "cancel_goal", "remove_goal"):
                return await self._delete_goal(params)
            elif action == "delete_all_goals":
                return await self._delete_all_goals()
            elif action in ("run_now", "ejecutar", "trigger", "execute_now"):
                return await self._run_now(params)
            else:
                return SkillResult(
                    skill_name=_SKILL_NAME,
                    output=(
                        f"Unknown action '{action}'. "
                        "Valid actions: create, list, pause, resume, archive, delete, "
                        "delete_all, wipe_all, archive_all, send_message, list_goals, delete_goal, delete_all_goals, run_now"
                    ),
                    success=False,
                )
        except Exception as e:
            logger.exception("agent_manager_skill.error", action=action)
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"Error: {e}",
                success=False,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_agent_id(self, params: dict) -> str | None:
        """Resolve agent_id from either the 'agent_id' param (UUID or name) or 'name' param."""
        agent_id = params.get("agent_id", "").strip()
        name = params.get("name", "").strip()

        # If agent_id looks like a full UUID, use it directly
        if agent_id and len(agent_id) > 20:
            return agent_id

        # Try name-based lookup (from agent_id param if it's a name, or name param)
        search_name = agent_id or name
        if search_name:
            agents = await self._orch.list_agents()
            # Exact match first
            for a in agents:
                if a.name.lower() == search_name.lower():
                    return a.id
            # Prefix match
            for a in agents:
                if a.name.lower().startswith(search_name.lower()):
                    return a.id

        return agent_id if agent_id else None

    # ------------------------------------------------------------------

    # Words that indicate the LLM passed instruction text instead of a proper name
    _GARBAGE_WORDS = {
        # Spanish instruction verbs / connectors
        "crea", "créalo", "crear", "debe", "debería", "debera", "monitorea",
        "monitorear", "revisa", "revisar", "especializado", "especializada",
        "quede", "queda", "dedicado", "dedicada", "haz", "hazlo", "usa", "utiliza",
        "que", "este", "esta", "ese", "esa", "para", "con", "del", "las", "los",
        "una", "uno", "por", "sin", "solo", "tambien", "también", "cuando",
        "donde", "como", "cual", "cuya", "hacer", "tener", "seguir", "mantener",
        # English instruction words
        "create", "make", "build", "use", "should", "will", "that", "which",
        "for", "the", "and", "with", "from", "this", "its", "have", "has",
        "monitor", "check", "track", "send", "get", "set", "run", "execute",
    }

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Strip punctuation/noise, hard-limit to 5 words / 40 chars.
        CamelCase names are split into words (e.g. 'CryptoAnalystAgent' → 'Crypto Analyst Agent').
        """
        import re
        # Split CamelCase into words before any other processing
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        name = re.sub(r"([^\w\s\-])", " ", name, flags=re.UNICODE).strip()
        name = re.sub(r"\s+", " ", name).strip()
        words = name.split()[:5]
        return " ".join(words)[:40].strip()

    @classmethod
    def _is_garbage_name(cls, name: str) -> bool:
        """Return True if the name looks like an instruction fragment, not a real label."""
        if not name:
            return True
        name_lower = name.lower()
        words = [w.rstrip("s") for w in name_lower.split()]

        # Single long word with no spaces: likely concatenated instruction text
        # e.g. "Especializadocréalodebe" — real names are 2+ words or short abbreviations
        if len(words) == 1 and len(name) > 12:
            # Check if any garbage word is a substring
            if any(gw in name_lower for gw in cls._GARBAGE_WORDS if len(gw) > 3):
                return True

        garbage_hits = sum(1 for w in words if w in cls._GARBAGE_WORDS)
        # Garbage if >50% of words are instruction words, or >= 2 hits
        return garbage_hits >= 2 or (len(words) > 0 and garbage_hits / len(words) > 0.5)

    @classmethod
    def _auto_name_from_text(cls, description: str, identity_prompt: str) -> str:
        """
        Derive a clean 2-4 word agent name from description or identity_prompt.
        Picks the first non-garbage content words and title-cases them.
        """
        import re
        source = description or identity_prompt or ""
        # Take only the first sentence
        first_sentence = re.split(r"[.\n!?]", source)[0].strip()
        # Remove punctuation
        cleaned = re.sub(r"[^\w\s]", " ", first_sentence, flags=re.UNICODE)
        words = cleaned.split()
        # Filter out garbage/filler words
        content = [
            w for w in words
            if w.lower().rstrip("s") not in cls._GARBAGE_WORDS and len(w) > 2
        ][:4]
        if not content:
            return ""
        return " ".join(w.title() for w in content)

    @staticmethod
    def _sanitize_description(desc: str) -> str:
        """Keep description to first sentence, max 120 chars."""
        if not desc:
            return ""
        first = desc.split(".")[0].split("!")[0].split("?")[0].strip()
        return first[:120]

    async def _create(self, params: dict) -> SkillResult:
        raw_name = params.get("name", "").strip()
        name = self._sanitize_name(raw_name)
        raw_desc = params.get("description", "").strip()
        raw_identity = params.get("identity_prompt", "").strip()

        # If the name is a garbage instruction fragment, auto-generate from description
        if not name or self._is_garbage_name(name):
            generated = self._auto_name_from_text(raw_desc, raw_identity)
            logger.warning(
                "agent_manager_skill.name_garbage_replaced",
                raw=raw_name[:80],
                generated=generated,
            )
            name = generated

        if not name:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=(
                    "Agent name is missing or invalid. "
                    "Provide a short, meaningful name (2-4 words), e.g. 'Crypto Financial Agent'."
                ),
                success=False,
            )

        description = self._sanitize_description(raw_desc)
        identity_prompt = raw_identity
        autonomy_mode_str = params.get("autonomy_mode", "semi").strip().lower()
        if autonomy_mode_str not in ("assist", "semi", "full"):
            autonomy_mode_str = "semi"

        agent = await self._orch.create_agent(
            name=name,
            description=description,
            identity_prompt=identity_prompt,
            autonomy_mode=autonomy_mode_str,
        )

        # Auto-populate intent from identity_prompt so AGENT_WAKEUP works immediately
        # even for freshly created agents — the agent owns its behavior from the start.
        if identity_prompt and agent.intent is None:
            from ...agent_manager.types import AgentIntent as _AgentIntent
            from ...agent_manager.store import save_agent as _save_agent
            import redis.asyncio as _aioredis
            _r = _aioredis.from_url(self._orch.redis_url, decode_responses=True)
            try:
                agent.intent = _AgentIntent(description=identity_prompt)
                await _save_agent(_r, agent)
            except Exception as _ie:
                logger.warning("agent_manager_skill.intent_auto_set_failed", error=str(_ie))
            finally:
                await _r.aclose()

        lines = [
            f"✅ Agent '{agent.name}' created successfully.",
            f"  ID: {agent.id}",
            f"  Status: {agent.status.value}",
            f"  Autonomy: {agent.autonomy_mode.value}",
        ]
        if description:
            lines.append(f"  Description: {description}")

        # Auto-start initial goal — objective is concise: "AgentName: short description"
        if identity_prompt:
            try:
                objective = f"{agent.name}: {description}" if description else agent.name
                await self._orch.create_agent_goal(agent.id, objective=objective)
                lines.append(f"  ▶️ Goal started: {objective}")
            except Exception as _e:
                logger.warning("agent_manager_skill.auto_start_failed", error=str(_e))
                lines.append(f"  ⚠️ Auto-start failed: {_e}")

        return SkillResult(skill_name=_SKILL_NAME, output="\n".join(lines), success=True)

    async def _list(self) -> SkillResult:
        agents = await self._orch.list_agents()
        if not agents:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="No agents found. Use agent_manager(action='create', name='...') to create one.",
                success=True,
            )
        lines = [f"Agents ({len(agents)} total):"]
        for a in agents:
            lines.append(
                f"  [{a.status.value.upper()}] {a.name} (id={a.id[:8]}…) "
                f"— autonomy={a.autonomy_mode.value}"
            )
            if a.description:
                lines.append(f"    {a.description}")
        return SkillResult(skill_name=_SKILL_NAME, output="\n".join(lines), success=True)

    async def _lifecycle(self, action: str, params: dict) -> SkillResult:
        agent_id = await self._resolve_agent_id(params)
        if not agent_id:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"Parameter 'agent_id' or 'name' is required for action '{action}'.",
                success=False,
            )
        if action == "pause":
            ok = await self._orch.pause_agent(agent_id)
        elif action == "resume":
            ok = await self._orch.resume_agent(agent_id)
        elif action == "archive":
            ok = await self._orch.archive_agent(agent_id)
        else:
            ok = False

        if ok:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"✅ Agent {agent_id[:8]}… {action}d.",
                success=True,
            )
        return SkillResult(
            skill_name=_SKILL_NAME,
            output=f"Agent {agent_id[:8]}… not found or action failed.",
            success=False,
        )

    async def _delete(self, params: dict) -> SkillResult:
        agent_id = await self._resolve_agent_id(params)
        if not agent_id:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="Parameter 'agent_id' or 'name' is required for delete.",
                success=False,
            )
        # Also delete all goals belonging to this agent
        goals_deleted = await self._delete_agent_goals(agent_id)
        ok = await self._orch.delete_agent(agent_id)
        if ok:
            suffix = f" + {goals_deleted} goal(s) deleted." if goals_deleted else "."
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"✅ Agent {agent_id[:8]}… deleted{suffix}",
                success=True,
            )
        return SkillResult(
            skill_name=_SKILL_NAME,
            output=f"Agent {agent_id[:8]}… not found.",
            success=False,
        )

    async def _delete_agent_goals(self, agent_id: str) -> int:
        """Delete all goals from Redis that belong to an agent. Returns count deleted."""
        try:
            import redis.asyncio as _redis
            r = _redis.from_url(self._orch.redis_url, decode_responses=True)
            try:
                goal_ids = await r.hkeys("goals")
                deleted = 0
                for gid in goal_ids:
                    raw = await r.hget("goals", gid)
                    if not raw:
                        continue
                    import json
                    data = json.loads(raw)
                    if data.get("agent_id") == agent_id:
                        await r.hdel("goals", gid)
                        deleted += 1
                return deleted
            finally:
                await r.aclose()
        except Exception as e:
            logger.warning("agent_manager_skill.delete_goals_failed", error=str(e))
            return 0

    async def _delete_all(self) -> SkillResult:
        count = await self._orch.delete_all_agents()
        if count == 0:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="No agents to delete.",
                success=True,
            )
        return SkillResult(
            skill_name=_SKILL_NAME,
            output=f"✅ {count} agent(s) deleted.",
            success=True,
        )

    async def _archive_all(self) -> SkillResult:
        agents = await self._orch.list_agents()
        if not agents:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="No agents to archive.",
                success=True,
            )
        count = 0
        for a in agents:
            ok = await self._orch.archive_agent(a.id)
            if ok:
                count += 1
        return SkillResult(
            skill_name=_SKILL_NAME,
            output=f"✅ {count} agent(s) archived.",
            success=True,
        )

    async def _send_message(self, params: dict) -> SkillResult:
        agent_id = await self._resolve_agent_id(params)
        message = params.get("message", "").strip()
        if not agent_id or not message:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="Parameters 'agent_id' and 'message' are required for send_message.",
                success=False,
            )

        # Create a goal on the target agent so the instruction is actually executed
        try:
            goal = await self._orch.create_agent_goal(agent_id, objective=message)
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"✅ Instruction sent to agent {agent_id[:8]}… — goal started (id={goal.id[:8]}): {message[:80]}",
                success=True,
            )
        except Exception as e:
            logger.warning("agent_manager_skill.send_message_goal_failed", error=str(e))
            # Fallback: bus message
            from ...agent_manager.bus import send_message as bus_send
            await bus_send(from_agent_id="wasp", to_agent_id=agent_id, content=message)
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"✅ Message queued for agent {agent_id[:8]}…: {message[:80]}",
                success=True,
            )

    # ------------------------------------------------------------------
    # Goal management
    # ------------------------------------------------------------------

    async def _load_all_goals(self) -> list[dict]:
        """Load all goals from Redis as dicts."""
        import json
        import redis.asyncio as _redis
        r = _redis.from_url(self._orch.redis_url, decode_responses=True)
        try:
            result = []
            for gid in await r.hkeys("goals"):
                raw = await r.hget("goals", gid)
                if raw:
                    try:
                        result.append(json.loads(raw))
                    except Exception:
                        pass
            return result
        finally:
            await r.aclose()

    async def _list_goals(self, params: dict) -> SkillResult:
        goals = await self._load_all_goals()
        if not goals:
            return SkillResult(skill_name=_SKILL_NAME, output="No hay goals activos.", success=True)
        lines = [f"Goals ({len(goals)} total):"]
        for g in goals:
            gid = g.get("id", "?")[:8]
            obj = g.get("objective", "?")[:60]
            state = g.get("state", "?")
            agent = g.get("agent_id", "")
            agent_str = f" [agent:{agent[:8]}]" if agent else ""
            lines.append(f"  {gid}… [{state}]{agent_str} — {obj}")
        return SkillResult(skill_name=_SKILL_NAME, output="\n".join(lines), success=True)

    async def _delete_goal(self, params: dict) -> SkillResult:
        """Delete a single goal by ID or by matching objective text."""
        import json
        import redis.asyncio as _redis
        goal_id = params.get("goal_id", "").strip()
        name = params.get("name", "").strip()
        if not goal_id and not name:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="Parameter 'goal_id' or 'name' is required.",
                success=False,
            )
        r = _redis.from_url(self._orch.redis_url, decode_responses=True)
        try:
            goal_ids = await r.hkeys("goals")
            deleted = []
            for gid in goal_ids:
                raw = await r.hget("goals", gid)
                if not raw:
                    continue
                data = json.loads(raw)
                match = (goal_id and (gid == goal_id or gid.startswith(goal_id))) or \
                        (name and name.lower() in data.get("objective", "").lower())
                if match:
                    await r.hdel("goals", gid)
                    deleted.append(gid[:8])
            if deleted:
                return SkillResult(skill_name=_SKILL_NAME, output=f"✅ Goal(s) deleted: {', '.join(deleted)}", success=True)
            return SkillResult(skill_name=_SKILL_NAME, output="No matching goal found.", success=False)
        finally:
            await r.aclose()

    async def _delete_all_goals(self) -> SkillResult:
        """Delete ALL goals from Redis."""
        import redis.asyncio as _redis
        r = _redis.from_url(self._orch.redis_url, decode_responses=True)
        try:
            count = len(await r.hkeys("goals"))
            await r.delete("goals")
            return SkillResult(skill_name=_SKILL_NAME, output=f"✅ {count} goal(s) deleted.", success=True)
        finally:
            await r.aclose()

    async def _wipe_goals_and_agents(self) -> SkillResult:
        """Delete all goals, agents, and custom tasks."""
        import redis.asyncio as _redis
        from ...scheduler.custom_tasks import list_tasks as _list_tasks, delete_task as _delete_task

        r = _redis.from_url(self._orch.redis_url, decode_responses=True)
        try:
            # Delete all goals
            goal_count = len(await r.hkeys("goals"))
            await r.delete("goals")

            # Delete all agents
            agent_count = await self._orch.delete_all_agents()

            # Delete all custom tasks
            tasks = await _list_tasks(r)
            task_count = 0
            for t in tasks:
                await _delete_task(r, t["task_id"])
                task_count += 1

            parts = [
                f"{goal_count} goal(s) eliminado(s)",
                f"{agent_count} agente(s) eliminado(s)",
                f"{task_count} tarea(s) eliminada(s)",
            ]

            return SkillResult(
                skill_name=_SKILL_NAME,
                output="✅ Goals, agentes y tareas eliminados.\n"
                       + "\n".join(f"  - {p}" for p in parts),
                success=True,
            )
        finally:
            await r.aclose()

    async def _run_now(self, params: dict) -> SkillResult:
        """Trigger immediate execution of an agent's objective."""
        agent_id = await self._resolve_agent_id(params)
        agents = await self._orch.list_agents()
        if not agents:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="No agents found. Create one first with agent_manager(action='create', ...).",
                success=False,
            )
        # Find by resolved id or use most recently created
        agent = None
        if agent_id:
            agent = next((a for a in agents if a.id == agent_id), None)
        if agent is None:
            agent = agents[0]

        try:
            objective = agent.identity_prompt or agent.description or agent.name
            goal = await self._orch.create_agent_goal(
                agent.id,
                objective=f"{agent.name}: {objective[:300]}",
            )
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=(
                    f"⏳ Ejecutando '{agent.name}' ahora.\n"
                    f"  Goal ID: {goal.id[:8]}\n"
                    f"  Te notificaré cuando termine."
                ),
                success=True,
            )
        except Exception as e:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"Error al ejecutar '{agent.name}': {e}",
                success=False,
                error=str(e),
            )
