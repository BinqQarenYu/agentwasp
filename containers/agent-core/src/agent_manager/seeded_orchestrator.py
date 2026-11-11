import time
from uuid import uuid4
from unittest.mock import MagicMock
from src.agent_manager.roster_seed import get_seeded_workforce
from src.agent_manager.types import Agent, AgentStatus, AgentIntent
from src.goal_orchestrator.types import AutonomyMode, CognitiveBudget

class SeededAgentOrchestrator:
    """Fully populated In-Memory Agent Orchestrator with Grok 15-Bot Mesh + Prometheus."""
    def __init__(self):
        self._agents = {}
        for a in get_seeded_workforce():
            self._agents[a.id] = a
        self._messages = {}

    async def list_agents(self):
        return list(self._agents.values())

    async def get_agent(self, agent_id: str):
        return self._agents.get(agent_id)

    async def pause_agent(self, agent_id: str):
        if agent_id in self._agents:
            self._agents[agent_id].status = AgentStatus.PAUSED
            return True
        return False

    async def resume_agent(self, agent_id: str):
        if agent_id in self._agents:
            self._agents[agent_id].status = AgentStatus.RUNNING
            return True
        return False

    async def archive_agent(self, agent_id: str):
        if agent_id in self._agents:
            self._agents[agent_id].status = AgentStatus.ARCHIVED
            return True
        return False

    async def delete_agent(self, agent_id: str):
        return self._agents.pop(agent_id, None) is not None

    async def get_messages(self, agent_id: str, limit: int = 50):
        return self._messages.get(agent_id, [])

    async def create_agent_goal(self, agent_id: str, objective: str):
        g = MagicMock()
        g.id = f"goal-{agent_id}-{int(time.time())}"
        if agent_id in self._agents:
            self._agents[agent_id].active_goal_ids.append(g.id)
            self._agents[agent_id].status = AgentStatus.RUNNING
        return g

    async def create_agent(
        self,
        name: str,
        description: str = "",
        model_provider: str = "",
        model_name: str = "",
        identity_prompt: str = "",
        autonomy_mode: str = "semi",
        allowed_capabilities: list[str] | None = None,
        memory_namespace: str = "",
        metadata: dict | None = None,
    ):
        aid = f"agent-{str(uuid4())[:8]}"
        mode = AutonomyMode.SEMI
        if autonomy_mode in AutonomyMode._value2member_map_:
            mode = AutonomyMode(autonomy_mode)
        agent = Agent(
            id=aid,
            name=name,
            description=description,
            model_provider=model_provider,
            model_name=model_name,
            identity_prompt=identity_prompt,
            autonomy_mode=mode,
            allowed_capabilities=allowed_capabilities or ["safe", "monitored"],
            memory_namespace=memory_namespace or aid,
            status=AgentStatus.IDLE,
            metadata={"badge": "🤖", "dept": "Custom Agent", "color": "#8b5cf6", "mesh_role": "Specialist"}
        )
        self._agents[aid] = agent
        return agent
