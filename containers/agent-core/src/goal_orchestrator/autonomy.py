"""Autonomy mode enforcement for the Autonomous Goal Engine.

Autonomy mode determines which tasks require user confirmation before
execution.  PolicyEngine always overrides — autonomy never bypasses it.

Decision matrix:
                 LOW    MEDIUM   HIGH    CRITICAL
  ASSIST:        BLOCK  BLOCK    BLOCK   BLOCK
  SEMI:          AUTO   AUTO     BLOCK   BLOCK
  FULL:          AUTO   AUTO     AUTO    BLOCK

BLOCKED tasks set TaskStatus.BLOCKED and pause the goal, waiting for
the user to review via dashboard or Telegram and then Resume.
"""

from __future__ import annotations

import structlog

from .types import AutonomyMode, RiskLevel, TaskNode

logger = structlog.get_logger()

# Skills whose execution impact is high enough that LOW/MEDIUM LLM-assigned
# risk classifications are overridden to HIGH.  This prevents a crafted task
# description from auto-executing shell commands, file writes, or arbitrary
# code under SEMI autonomy mode.
_MINIMUM_HIGH_RISK_SKILLS: frozenset[str] = frozenset({
    # Direct code execution
    "shell",
    "bash",
    "python_exec",
    "execute_code",
    # File system
    "write_file",
    "read_file",
    # Self-modification
    "self_improve",
    # External HTTP / network
    "http_request",
    "web_request",
})


def _effective_risk(task: TaskNode) -> RiskLevel:
    """Return the effective risk level for a task.

    If the task skill is in _MINIMUM_HIGH_RISK_SKILLS and its LLM-assigned
    risk is below HIGH, promote it to HIGH.  CRITICAL is never downgraded.
    """
    if task.skill_name and task.skill_name.lower() in _MINIMUM_HIGH_RISK_SKILLS:
        if task.risk_level not in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            logger.info(
                "autonomy.risk_promoted",
                skill=task.skill_name,
                original_risk=task.risk_level.value,
                effective_risk="high",
            )
            return RiskLevel.HIGH
    return task.risk_level


def needs_confirmation(task: TaskNode, mode: AutonomyMode) -> bool:
    """Return True if this task requires user confirmation before execution.

    CRITICAL risk always requires confirmation regardless of mode.
    Dangerous skills (shell, write_file, python_exec, etc.) are always promoted
    to at least HIGH risk before the mode check — preventing LLM-assigned LOW/
    MEDIUM labels from auto-executing high-blast-radius operations under SEMI mode.
    PolicyEngine checks (CapabilityRegistry, RiskAssessor) are separate
    and always applied — autonomy mode does not bypass them.
    """
    effective = _effective_risk(task)

    # CRITICAL is always blocked regardless of autonomy mode
    if effective == RiskLevel.CRITICAL:
        return True

    if mode == AutonomyMode.ASSIST:
        # Every task needs confirmation in ASSIST mode
        return True

    if mode == AutonomyMode.SEMI:
        # HIGH and CRITICAL need confirmation; LOW and MEDIUM auto-execute
        return effective == RiskLevel.HIGH

    # FULL mode: only CRITICAL blocked (handled above)
    return False


def autonomy_label(task: TaskNode, mode: AutonomyMode) -> str:
    """Return 'auto' or 'blocked' for telemetry recording."""
    return "blocked" if needs_confirmation(task, mode) else "auto"


def confirmation_reason(task: TaskNode, mode: AutonomyMode) -> str:
    """Return a human-readable explanation for the confirmation requirement."""
    effective = _effective_risk(task)
    if effective == RiskLevel.CRITICAL:
        return f"Task '{task.id}' has CRITICAL risk level — always requires confirmation"
    if mode == AutonomyMode.ASSIST:
        return f"ASSIST mode: all tasks require manual approval (task '{task.id}')"
    if mode == AutonomyMode.SEMI and effective == RiskLevel.HIGH:
        skill_note = f" — skill '{task.skill_name}' is in high-risk list" if task.skill_name and task.skill_name.lower() in _MINIMUM_HIGH_RISK_SKILLS else ""
        return f"SEMI mode: HIGH risk task '{task.id}' ({task.skill_name}) requires confirmation{skill_note}"
    return ""
