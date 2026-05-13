"""Capability sandbox for per-agent allowed_capabilities enforcement.

The sandbox is checked BEFORE the global CapabilityRegistry in GoalStepExecutor.
If an agent has an empty allowed_capabilities list, it is unrestricted (inherits
the global capability policy). If the list is non-empty, only those capability
levels are allowed for goals created by that agent.

Capability level names (from SkillExecutor / CapabilityLevel):
  "safe"        — pure computation, no side effects
  "monitored"   — read-only external access
  "controlled"  — scoped writes with bounded impact
  "restricted"  — arbitrary operations (shell, python_exec)
  "privileged"  — system-level / infrastructure
"""

from __future__ import annotations

from .types import Agent


class CapabilityDeniedError(Exception):
    """Raised when an agent's sandbox blocks a required capability."""

    def __init__(self, agent_name: str, capability: str) -> None:
        self.agent_name = agent_name
        self.capability = capability
        super().__init__(
            f"Agent '{agent_name}' sandbox: capability '{capability}' not in allowed list"
        )


class CapabilitySandbox:
    """Evaluates per-agent capability allowlists.

    Thread-safe: stateless, all data comes from the Agent object.
    """

    def check(self, agent: Agent, capability_name: str) -> bool:
        """Return True if agent is permitted to use capability_name.

        Empty allowed_capabilities → unrestricted (inherits global).
        Non-empty → exact match required.
        """
        if not agent.allowed_capabilities:
            return True
        return capability_name in agent.allowed_capabilities

    def gate(self, agent: Agent, capability_name: str) -> None:
        """Raise CapabilityDeniedError if capability is not allowed.

        Callers should emit AGENT_SANDBOX_DENIED event after catching this.
        """
        if not self.check(agent, capability_name):
            raise CapabilityDeniedError(agent.name, capability_name)
