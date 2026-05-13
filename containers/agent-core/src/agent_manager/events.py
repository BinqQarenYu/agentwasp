"""AGENT_* event name constants for the Multi-Agent Orchestration Layer.

All events are published to the events:outgoing Redis stream via EventBus.
"""

AGENT_CREATED = "agent.created"
AGENT_STARTED = "agent.started"
AGENT_PAUSED = "agent.paused"
AGENT_RESUMED = "agent.resumed"
AGENT_ARCHIVED = "agent.archived"
AGENT_GOAL_CREATED = "agent.goal_created"
AGENT_GOAL_COMPLETED = "agent.goal_completed"
AGENT_GOAL_FAILED = "agent.goal_failed"
AGENT_BUDGET_EXCEEDED = "agent.budget_exceeded"
AGENT_SANDBOX_DENIED = "agent.sandbox_denied"
AGENT_MESSAGE_SENT = "agent.message_sent"
AGENT_GLOBAL_THROTTLE = "agent.global_throttle"
AGENT_CPU_THROTTLE = "agent.cpu_throttle"
