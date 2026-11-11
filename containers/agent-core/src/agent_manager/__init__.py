"""Multi-Agent Orchestration Layer.

Provides Agent entities with isolated memory namespaces, per-agent capability
sandboxes, per-agent cognitive budgets, and agent-to-agent messaging — all
layered non-breakingly above the existing GoalOrchestrator.

Public API:
  AgentOrchestrator           — coordinates all agent runtimes
  AgentRuntime                — per-agent execution context (wraps GoalOrchestrator)
  Agent                       — persistent agent entity (Pydantic model)
  AgentStatus                 — lifecycle state enum
  SubagentGenesis             — bounded ephemeral subagent spawner
  SubagentTaskReport          — report structure for ephemeral execution
  DepthLimitExceededError     — raised on recursive subagent spawn violation
  ConcurrencyLimitExceededError — raised on subagent concurrency saturation
  TriadConsensusEngine        — adversarial 2/3 consensus engine with Prometheus veto
  CodeProposal                — code change proposal structure
  ConsensusVerdict            — consensus evaluation outcome
  ConsensusStatus             — APPROVED, REJECTED, VETOED enum
  VoteType                    — APPROVE, REJECT, NEEDS_REVISION, VETO
"""

from .consensus import (
    CodeProposal,
    ConsensusStatus,
    ConsensusVerdict,
    TriadConsensusEngine,
    TriadVote,
    VoteType,
)
from .genesis import SubagentGenesis, SubagentTaskReport
from .orchestrator import AgentOrchestrator
from .runtime import AgentRuntime
from .types import (
    Agent,
    AgentStatus,
    ConcurrencyLimitExceededError,
    DepthLimitExceededError,
    GenesisError,
)

__all__ = [
    "AgentOrchestrator",
    "AgentRuntime",
    "Agent",
    "AgentStatus",
    "SubagentGenesis",
    "SubagentTaskReport",
    "DepthLimitExceededError",
    "ConcurrencyLimitExceededError",
    "GenesisError",
    "TriadConsensusEngine",
    "CodeProposal",
    "ConsensusVerdict",
    "ConsensusStatus",
    "TriadVote",
    "VoteType",
]
