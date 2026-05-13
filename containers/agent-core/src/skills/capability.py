"""Skill Capability Registry — classifies skills by operational risk level.

Levels (ascending risk):
  SAFE        — pure computation, no side effects (calculate, datetime, translate)
  MONITORED   — read-only external access (fetch_url, web_search, browser, scrape)
  CONTROLLED  — scoped writes with bounded impact (reminders, notes, gmail, monitors)
  RESTRICTED  — arbitrary operations (shell, python_exec, http_request, file_ops)
  PRIVILEGED  — system-level / infrastructure (broker commands)

Architecture:
  - CapabilityLevel drives real enforcement at the executor layer.
  - Rate limits are enforced for CONTROLLED (100/hr), RESTRICTED (30/hr), PRIVILEGED (20/hr).
  - RESTRICTED skills additionally run risk_assess (warn-only) and require full audit.
  - Policy engine (memory/policy/) can escalate enforcement per deployment.
  - Advisory systems (anticipatory simulation, epistemic calibration) annotate outputs
    but do NOT block execution — they are clearly marked [ADVISORY] in their output.
"""

from dataclasses import dataclass
from enum import Enum


class CapabilityLevel(str, Enum):
    SAFE = "safe"
    MONITORED = "monitored"
    CONTROLLED = "controlled"
    RESTRICTED = "restricted"
    PRIVILEGED = "privileged"


@dataclass(frozen=True)
class CapabilityPolicy:
    """Per-level execution policy."""
    level: CapabilityLevel
    max_per_hour: int          # 0 = unlimited
    requires_audit: bool       # Always write to audit_log
    risk_assess: bool          # Run RiskAssessor before execution
    description: str


# Default policies per capability level
DEFAULT_POLICIES: dict[CapabilityLevel, CapabilityPolicy] = {
    CapabilityLevel.SAFE: CapabilityPolicy(
        level=CapabilityLevel.SAFE,
        max_per_hour=0,
        requires_audit=False,
        risk_assess=False,
        description="Pure computation. No side effects.",
    ),
    CapabilityLevel.MONITORED: CapabilityPolicy(
        level=CapabilityLevel.MONITORED,
        max_per_hour=0,
        requires_audit=False,
        risk_assess=False,
        description="Read-only external access.",
    ),
    CapabilityLevel.CONTROLLED: CapabilityPolicy(
        level=CapabilityLevel.CONTROLLED,
        max_per_hour=100,   # Real rate limit: 100 executions/hour
        requires_audit=True,
        risk_assess=False,
        description="Scoped writes with bounded impact. Rate-limited 100/hr.",
    ),
    CapabilityLevel.RESTRICTED: CapabilityPolicy(
        level=CapabilityLevel.RESTRICTED,
        max_per_hour=30,    # Real rate limit: 30 executions/hour
        requires_audit=True,
        risk_assess=True,   # Warn-only risk assessment (does not block)
        description="Arbitrary operations. Full audit + risk assessment. Rate-limited 30/hr.",
    ),
    CapabilityLevel.PRIVILEGED: CapabilityPolicy(
        level=CapabilityLevel.PRIVILEGED,
        max_per_hour=20,
        requires_audit=True,
        risk_assess=True,
        description="System-level / infrastructure operations.",
    ),
}


class CapabilityRegistry:
    """Maps skill names to their capability level and policy."""

    def __init__(self):
        # Built in defaults; overridden when skills register themselves
        self._levels: dict[str, CapabilityLevel] = {}

    def register(self, skill_name: str, level: CapabilityLevel) -> None:
        self._levels[skill_name] = level

    def get_level(self, skill_name: str) -> CapabilityLevel:
        return self._levels.get(skill_name, CapabilityLevel.CONTROLLED)

    def get_policy(self, skill_name: str) -> CapabilityPolicy:
        level = self.get_level(skill_name)
        return DEFAULT_POLICIES[level]

    def is_restricted_or_above(self, skill_name: str) -> bool:
        level = self.get_level(skill_name)
        return level in (CapabilityLevel.RESTRICTED, CapabilityLevel.PRIVILEGED)

    def summary(self) -> dict[str, str]:
        return {name: level.value for name, level in self._levels.items()}


# Singleton registry — populated by register_builtin_skills()
capability_registry = CapabilityRegistry()
