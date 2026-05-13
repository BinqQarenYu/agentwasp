"""Type definitions for the Multi-Agent Orchestration Layer.

Data model:
  Agent         — persistent agent entity (stored in Redis HASH 'agents')
  AgentStatus   — lifecycle state machine for agents
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from ..goal_orchestrator.types import AutonomyMode, CognitiveBudget


class AgentStatus(str, Enum):
    IDLE = "idle"           # Created but not running any goals
    RUNNING = "running"     # Has active goals being ticked
    PAUSED = "paused"       # Manually paused; goals preserved
    ARCHIVED = "archived"   # Soft-deleted; excluded from ticks


class AgentIntent(BaseModel):
    """Persistent intent owned by the agent — the source of truth for its behavior.

    The agent reconstructs its execution workflow from this model when triggered,
    without relying on injected task text.
    """
    description: str = ""
    """What this agent is trying to achieve (long-term goal)."""

    execution_strategy: str = ""
    """How the agent approaches each execution cycle (skills, sequence, approach)."""

    constraints: dict = {}
    """Rules and requirements: outputs expected, restrictions, config (e.g. email_to, no_scroll)."""

    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class Agent(BaseModel):
    """Persistent agent entity — stored as JSON in Redis HASH 'agents'.

    Each agent wraps the shared GoalOrchestrator with isolated:
    - Memory namespace (project_id scoping in MemoryManager)
    - Capability sandbox (allowed_capabilities allowlist)
    - Cognitive budget (tokens, replans, steps per goal)
    - Autonomy mode (assist/semi/full)
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""

    # Model override (empty = use active global provider/model)
    model_provider: str = ""     # e.g., "anthropic", "openai", "ollama"
    model_name: str = ""         # e.g., "claude-opus-4-6", "gpt-4o"

    # Custom system prompt for this agent (empty = use global system prompt)
    identity_prompt: str = ""

    # Autonomy mode (governs when human confirmation is required)
    autonomy_mode: AutonomyMode = AutonomyMode.SEMI

    # Capability sandbox — list of allowed capability levels
    # Empty list = unrestricted (inherits global CapabilityRegistry settings)
    # Example: ["safe", "monitored"] — restricts to read-only skills
    allowed_capabilities: list[str] = Field(default_factory=list)

    # Per-goal cognitive budget defaults (overrides global defaults)
    cognitive_budget: CognitiveBudget = Field(default_factory=CognitiveBudget)

    # Memory isolation namespace (defaults to agent.id at runtime)
    memory_namespace: str = ""

    # Lifecycle
    status: AgentStatus = AgentStatus.IDLE

    # Timestamps
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Active goals managed by this agent
    active_goal_ids: list[str] = Field(default_factory=list)

    # Arbitrary metadata
    metadata: dict = Field(default_factory=dict)

    # Persistent intent — the agent's source of behavioral truth.
    # When populated, the agent reconstructs its workflow from this model
    # instead of relying on injected task instructions.
    intent: AgentIntent | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    @property
    def effective_namespace(self) -> str:
        """Effective memory namespace — falls back to agent.id."""
        return self.memory_namespace or self.id
