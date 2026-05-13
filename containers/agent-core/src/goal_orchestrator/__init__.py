"""Autonomous Goal Engine — goal_orchestrator package.

Public API:
    GoalOrchestrator  — top-level coordinator (create/tick/pause/resume/cancel)
    GoalTickJob       — Scheduler-compatible periodic tick callable
    PlanGenerator     — LLM-based task graph planner
    GoalStepExecutor  — single-step task executor
    GoalMetaReflectionJob — periodic anomaly analysis job
    Goal, TaskGraph, TaskNode, GoalState, TaskStatus, RiskLevel — data types
    AutonomyMode, CognitiveBudget, StabilityState, GoalTelemetry  — advanced types
    GoalTemplate      — reusable task graph template
    save_goal, load_goal, list_goals, list_active_goals  — persistence helpers
"""

from .executor import GoalStepExecutor
from .job import GoalTickJob
from .orchestrator import GoalOrchestrator
from .planner import PlanGenerator
from .reflection_job import GoalMetaReflectionJob
from .store import (
    delete_goal,
    list_active_goals,
    list_goals,
    load_goal,
    save_goal,
)
from .templates import GoalTemplate
from .types import (
    AutonomyMode,
    CognitiveBudget,
    Goal,
    GoalEvent,
    GoalState,
    GoalTelemetry,
    RiskLevel,
    StabilityState,
    TaskGraph,
    TaskNode,
    TaskStatus,
)

__all__ = [
    # Orchestration
    "GoalOrchestrator",
    "GoalTickJob",
    "PlanGenerator",
    "GoalStepExecutor",
    "GoalMetaReflectionJob",
    # Core types
    "Goal",
    "TaskGraph",
    "TaskNode",
    "GoalEvent",
    "GoalState",
    "TaskStatus",
    "RiskLevel",
    # Advanced control layer types
    "AutonomyMode",
    "CognitiveBudget",
    "StabilityState",
    "GoalTelemetry",
    "GoalTemplate",
    # Persistence
    "save_goal",
    "load_goal",
    "list_goals",
    "list_active_goals",
    "delete_goal",
]
