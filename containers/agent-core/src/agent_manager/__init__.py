"""Multi-Agent Orchestration Layer.

Provides Agent entities with isolated memory namespaces, per-agent capability
sandboxes, per-agent cognitive budgets, and agent-to-agent messaging — all
layered non-breakingly above the existing GoalOrchestrator.

Public API:
  AgentOrchestrator   — coordinates all agent runtimes
  AgentRuntime        — per-agent execution context (wraps GoalOrchestrator)
  Agent               — persistent agent entity (Pydantic model)
  AgentStatus         — lifecycle state enum
"""

from .orchestrator import AgentOrchestrator
from .runtime import AgentRuntime
from .types import Agent, AgentStatus

__all__ = ["AgentOrchestrator", "AgentRuntime", "Agent", "AgentStatus"]
