"""ContextPacket builder — structured context assembly for LLM prompts.

Principles:
- No raw prompts stored in memory.
- No chain-of-thought stored.
- Memory is model-agnostic; same packet works for any provider.
- Each layer has a clear purpose and size budget.
- Context is assembled fresh from memory on each request.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from .types import MemoryContent, MemoryQuery, MemoryType

logger = structlog.get_logger()

# Token budget estimates per layer (conservative chars-per-token = 4)
BUDGET_POLICIES = 1200
BUDGET_FACTS = 1200
BUDGET_SEMANTIC = 2000
BUDGET_EPISODIC = 8000  # Raised for long-term memory injection
BUDGET_WORKING = 1000


@dataclass
class ContextPacket:
    """Structured context for LLM prompt assembly.

    Each field is a typed list of strings ready for prompt injection.
    The builder guarantees no field exceeds its size budget.
    """

    # Active policy rules extracted from policy/ memory
    active_policies: list[str] = field(default_factory=list)

    # Stable facts about the user / environment from facts/ memory
    user_facts: list[str] = field(default_factory=list)

    # Condensed learnings from semantic/ memory (reflections, patterns)
    learned_patterns: list[str] = field(default_factory=list)

    # Recent conversation pairs from episodic/ (user, agent)
    recent_interactions: list[dict] = field(default_factory=list)

    # Active working memory items (reminders, monitors, open tasks)
    working_items: list[dict] = field(default_factory=list)

    # Agent metadata (version, capabilities)
    agent_meta: dict = field(default_factory=dict)

    # Identity directive from IdentityManager (empty string = use default)
    identity_directive: str = ""

    # Project scope if isolation is active
    project_id: str | None = None

    def format_policies(self) -> str:
        """Format active policies for injection into system prompt."""
        if not self.active_policies:
            return ""
        lines = ["Active policies:"]
        for p in self.active_policies:
            lines.append(f"- {p}")
        return "\n".join(lines)

    def format_working(self) -> str:
        """Format working memory items as a concise status block."""
        if not self.working_items:
            return ""
        lines = ["Active items:"]
        for item in self.working_items:
            item_type = item.get("type", "item")
            status = item.get("status", "active")
            summary = item.get("summary", "")[:100]
            due = item.get("due", "")
            due_str = f" (due: {due})" if due else ""
            lines.append(f"- [{item_type}] {summary}{due_str} [{status}]")
        return "\n".join(lines)

    def format_semantic(self) -> str:
        """Format learned patterns for context injection."""
        if not self.learned_patterns:
            return ""
        lines = ["Learned patterns:"]
        for p in self.learned_patterns:
            lines.append(f"- {p}")
        return "\n".join(lines)

    def has_content(self) -> bool:
        return bool(
            self.active_policies
            or self.user_facts
            or self.learned_patterns
            or self.recent_interactions
            or self.working_items
        )


class ContextBuilder:
    """Builds a ContextPacket from memory, respecting size budgets."""

    def __init__(self, memory_manager=None):
        self._memory = memory_manager

    def set_memory(self, memory_manager) -> None:
        self._memory = memory_manager

    async def build(
        self,
        session,
        project_id: str | None = None,
        max_interactions: int = 20,
        max_working: int = 20,
    ) -> ContextPacket:
        """Assemble a ContextPacket for the given session.

        All 5 memory queries run in parallel via asyncio.gather() for speed.
        """
        if self._memory is None:
            return ContextPacket()

        packet = ContextPacket(project_id=project_id)

        # Run all 5 memory queries in parallel
        (
            policies_result,
            facts_result,
            semantic_result,
            episodic_result,
            working_result,
        ) = await asyncio.gather(
            self._memory.retrieve(session, MemoryQuery(memory_type=MemoryType.POLICY, project_id=project_id, limit=20)),
            self._memory.retrieve(session, MemoryQuery(memory_type=MemoryType.FACTS, project_id=project_id, limit=15)),
            self._memory.retrieve(session, MemoryQuery(memory_type=MemoryType.SEMANTIC, project_id=project_id, limit=10)),
            self._memory.retrieve(session, MemoryQuery(memory_type=MemoryType.EPISODIC, project_id=project_id, limit=max_interactions)),
            self._memory.retrieve(session, MemoryQuery(memory_type=MemoryType.WORKING, project_id=project_id, limit=max_working)),
            return_exceptions=True,
        )

        # 1. Policies
        if not isinstance(policies_result, Exception):
            budget = BUDGET_POLICIES
            for entry in policies_result:
                rule = entry.content.get("rule", entry.summary or "")
                if not rule:
                    continue
                rule = rule[:200]
                if budget - len(rule) < 0:
                    break
                packet.active_policies.append(rule)
                budget -= len(rule)
        else:
            logger.warning("context_builder.policy_load_failed")

        # 2. Facts
        if not isinstance(facts_result, Exception):
            budget = BUDGET_FACTS
            for entry in facts_result:
                fact = entry.summary or entry.content.get("value", "")
                if not fact:
                    continue
                fact = fact[:150]
                if budget - len(fact) < 0:
                    break
                packet.user_facts.append(fact)
                budget -= len(fact)
        else:
            logger.warning("context_builder.facts_load_failed")

        # 3. Semantic
        if not isinstance(semantic_result, Exception):
            budget = BUDGET_SEMANTIC
            for entry in sorted(semantic_result, key=lambda e: e.importance_score, reverse=True):
                learnings = entry.content.get("learnings", entry.summary or "")
                if not learnings:
                    continue
                learnings = learnings[:300]
                if budget - len(learnings) < 0:
                    break
                packet.learned_patterns.append(learnings)
                budget -= len(learnings)
        else:
            logger.warning("context_builder.semantic_load_failed")

        # 4. Episodic — recent conversation turns (budget-aware)
        if not isinstance(episodic_result, Exception):
            budget = BUDGET_EPISODIC
            for entry in reversed(episodic_result):
                user_input = entry.content.get("user_input", "")
                agent_response = entry.content.get("agent_response", "")
                if not user_input or not agent_response:
                    continue
                if agent_response == "(processing)":
                    continue
                u_trunc = user_input[:800]
                a_trunc = agent_response[:800]
                pair_len = len(u_trunc) + len(a_trunc)
                if budget - pair_len < 0:
                    break
                packet.recent_interactions.append({
                    "user": u_trunc,
                    "agent": a_trunc,
                    "ts": entry.created_at[:19],
                })
                budget -= pair_len
        else:
            logger.warning("context_builder.episodic_load_failed")

        # 5. Working memory — active reminders, tasks, monitors
        if not isinstance(working_result, Exception):
            for entry in working_result:
                status = entry.content.get("status", "active")
                if status in ("completed", "done", "cancelled"):
                    continue
                packet.working_items.append({
                    "type": entry.content.get("type", "task"),
                    "status": status,
                    "summary": entry.summary or entry.content.get("text", "")[:100],
                    "due": entry.content.get("due", ""),
                })
        else:
            logger.warning("context_builder.working_load_failed")

        return packet

    async def build_minimal(self, session, project_id: str | None = None) -> ContextPacket:
        """Build a lightweight packet with only policies + recent interactions.

        Used for fast-path processing where full context isn't needed.
        """
        if self._memory is None:
            return ContextPacket()

        packet = ContextPacket(project_id=project_id)

        try:
            episodic = await self._memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.EPISODIC, limit=3),
            )
            for entry in reversed(episodic):
                user_input = entry.content.get("user_input", "")
                agent_response = entry.content.get("agent_response", "")
                if user_input and agent_response and agent_response != "(processing)":
                    packet.recent_interactions.append({
                        "user": user_input[:200],
                        "agent": agent_response[:200],
                        "ts": entry.created_at[:19],
                    })
        except Exception:
            pass

        return packet
