from enum import Enum
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class EventType(str, Enum):
    # Human input
    TELEGRAM_MESSAGE = "telegram.message"
    TELEGRAM_COMMAND = "telegram.command"
    DASHBOARD_ACTION = "dashboard.action"

    # Agent responses
    TELEGRAM_RESPONSE = "telegram.response"
    TELEGRAM_PROGRESS = "telegram.progress"   # Live status update (edited by bridge)
    DASHBOARD_RESPONSE = "dashboard.response"

    # Scheduled
    SCHEDULED_JOB = "scheduled.job"
    REFLECTION_TRIGGER = "scheduled.reflection"
    HEALTH_CHECK = "scheduled.health"
    MEMORY_CLEANUP = "scheduled.memory_cleanup"

    # Skills
    SKILL_EXECUTION = "skill.execution"

    # System
    SERVICE_STARTED = "system.service_started"
    SERVICE_STOPPED = "system.service_stopped"
    ERROR_DETECTED = "system.error"
    SELF_HEAL_TRIGGER = "system.self_heal"

    # Memory
    MEMORY_SNAPSHOT = "memory.snapshot"
    MEMORY_ROLLBACK = "memory.rollback"

    # Autonomous Goal Engine
    GOAL_CREATED = "goal.created"
    GOAL_PLANNED = "goal.planned"
    GOAL_COMPLETED = "goal.completed"
    GOAL_FAILED = "goal.failed"
    GOAL_PAUSED = "goal.paused"
    GOAL_REPLANNED = "goal.replanned"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # Advanced control layer
    GOAL_BUDGET_EXCEEDED = "goal.budget_exceeded"
    GOAL_AUTONOMY_BLOCKED = "goal.autonomy_blocked"
    GOAL_AUTONOMY_CHANGED = "goal.autonomy_changed"
    GOAL_STABILITY_INTERVENTION = "goal.stability_intervention"
    GOAL_STABILITY_BACKOFF = "goal.stability_backoff"
    META_REFLECTION_ANALYSIS = "goal.meta_reflection"


class IncomingEvent(BaseModel):
    event_type: EventType
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    user_id: str = ""
    chat_id: str = ""
    text: str = ""
    metadata: dict = Field(default_factory=dict)


class OutgoingEvent(BaseModel):
    event_type: EventType
    correlation_id: str = ""
    chat_id: str = ""
    text: str = ""
    metadata: dict = Field(default_factory=dict)
