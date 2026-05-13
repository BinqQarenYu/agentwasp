"""Cognitive Budget enforcement for the Autonomous Goal Engine.

All budget checks are pure functions operating on CognitiveBudget.
They raise BudgetError on violation; the caller (executor / orchestrator)
handles the error by pausing the goal and emitting GOAL_BUDGET_EXCEEDED.

Design invariants:
  - No side effects on success (caller increments counters after success)
  - All functions are sync (no I/O)
  - BudgetError carries dimension, used, limit for structured events
"""

from __future__ import annotations

import json

from .types import CognitiveBudget


class BudgetError(Exception):
    """Raised when a cognitive budget limit would be exceeded."""

    def __init__(self, dimension: str, used: int | float, limit: int | float):
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(
            f"Budget exceeded [{dimension}]: used={used} limit={limit}"
        )


# ---------------------------------------------------------------------------
# Pre-execution checks
# ---------------------------------------------------------------------------


def check_planning_tokens(budget: CognitiveBudget, tokens_requested: int = 0) -> None:
    """Raise BudgetError if planning token budget is at or over limit."""
    if budget.budget_exceeded:
        raise BudgetError(
            budget.budget_exceeded_dimension,
            budget.tokens_used_planning,
            budget.max_tokens_planning,
        )
    if budget.tokens_used_planning + tokens_requested > budget.max_tokens_planning:
        raise BudgetError(
            "tokens_planning",
            budget.tokens_used_planning + tokens_requested,
            budget.max_tokens_planning,
        )


def check_replan(budget: CognitiveBudget) -> None:
    """Raise BudgetError if replanning budget is exhausted."""
    if budget.replans_used >= budget.max_replans:
        raise BudgetError("replans", budget.replans_used, budget.max_replans)


def check_steps(budget: CognitiveBudget) -> None:
    """Raise BudgetError if step budget is exhausted."""
    if budget.steps_executed >= budget.max_steps:
        raise BudgetError("max_steps", budget.steps_executed, budget.max_steps)


def check_memory(budget: CognitiveBudget, content: dict) -> None:
    """Raise BudgetError if writing content would exceed memory growth budget."""
    est = _estimate_bytes(content)
    if budget.memory_growth + est > budget.max_memory_growth_bytes:
        raise BudgetError(
            "memory_growth_bytes",
            budget.memory_growth + est,
            budget.max_memory_growth_bytes,
        )


# ---------------------------------------------------------------------------
# Post-execution recorders (mutate budget in-place; call only on success)
# ---------------------------------------------------------------------------


def record_planning_tokens(budget: CognitiveBudget, tokens: int) -> None:
    budget.tokens_used_planning = max(0, budget.tokens_used_planning + tokens)


def record_execution_step(budget: CognitiveBudget) -> None:
    budget.steps_executed += 1


def record_replan(budget: CognitiveBudget) -> None:
    budget.replans_used += 1


def record_memory_write(budget: CognitiveBudget, content: dict) -> None:
    budget.memory_growth += _estimate_bytes(content)


def mark_exceeded(budget: CognitiveBudget, dimension: str) -> None:
    """Mark budget as exceeded — goal must be paused before calling this."""
    budget.budget_exceeded = True
    budget.budget_exceeded_dimension = dimension


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _estimate_bytes(content: dict) -> int:
    """Estimate UTF-8 byte size of a dict for memory budget tracking."""
    try:
        return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 512  # Conservative fallback
