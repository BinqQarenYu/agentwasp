"""Promotion Engine — elevates recurring episodic topics to semantic memory.

Rules:
- A topic mentioned 3+ times in episodic becomes a semantic entry.
- Impact events (errors, failures, skill_use) get boosted importance.
- Duplicate semantic entries are merged, not duplicated.
- No chain-of-thought is stored; only extracted facts/patterns.
"""

import re
from datetime import datetime, timezone

import structlog

from .types import MemoryContent, MemoryQuery, MemoryType

logger = structlog.get_logger()

# Minimum mentions before promoting a topic to semantic memory
PROMOTION_THRESHOLD = 3

# Importance boost for events that have operational impact
IMPACT_TAGS = {"error", "failure", "skill_error", "alarm", "repair", "critical"}

# Minimum importance delta to trigger a semantic update
MIN_DELTA = 0.05


def _extract_topic_key(entry: MemoryContent) -> str | None:
    """Extract a short normalised topic key from an episodic entry.

    Returns None if the entry doesn't contain a meaningful extractable topic.
    """
    user_input = entry.content.get("user_input", "")
    agent_response = entry.content.get("agent_response", "")

    # Use first 120 chars of user input as the raw topic signal
    raw = (user_input or agent_response)[:120].lower().strip()
    if not raw:
        return None

    # Strip punctuation + collapse whitespace for key normalisation
    key = re.sub(r"[^\w\s]", " ", raw)
    key = re.sub(r"\s+", " ", key).strip()

    # Must be at least 5 chars to be worth tracking
    return key if len(key) >= 5 else None


def _compute_importance(entry: MemoryContent) -> float:
    """Score an episodic entry's importance based on its tags and content."""
    base = 0.5
    tags_set = set(t.lower() for t in entry.tags)

    if tags_set & IMPACT_TAGS:
        base = min(1.0, base + 0.3)

    # Skill executions are more important than plain conversation
    if "skill_used" in tags_set or entry.content.get("skills_used"):
        base = min(1.0, base + 0.1)

    # Entries that the user explicitly starred/marked
    if "important" in tags_set or "starred" in tags_set:
        base = 1.0

    return round(base, 3)


class PromotionEngine:
    """Scans episodic memories and promotes recurring patterns to semantic."""

    def __init__(self, memory_manager=None):
        # Injected at runtime to avoid circular imports
        self._memory = memory_manager

    def set_memory(self, memory_manager) -> None:
        self._memory = memory_manager

    async def process_new_episodic(
        self,
        session,
        entry: MemoryContent,
    ) -> None:
        """Called immediately after a new episodic entry is stored.

        Updates the importance_score and checks if the topic deserves
        a semantic promotion.
        """
        if self._memory is None:
            return

        # Compute importance and update entry if needed
        new_importance = _compute_importance(entry)
        if abs(new_importance - entry.importance_score) >= MIN_DELTA:
            entry.importance_score = new_importance
            self._memory.store.write(entry)

    async def run_promotion_cycle(self, session) -> int:
        """Full promotion scan.

        - Counts topic occurrences across recent episodic entries.
        - Promotes topics that exceed PROMOTION_THRESHOLD.
        - Merges with existing semantic entries.

        Returns number of promotions performed.
        """
        if self._memory is None:
            return 0

        recent = await self._memory.retrieve(
            session,
            MemoryQuery(memory_type=MemoryType.EPISODIC, limit=100),
        )

        if not recent:
            return 0

        # Count occurrences of each topic key
        topic_map: dict[str, list[MemoryContent]] = {}
        for entry in recent:
            key = _extract_topic_key(entry)
            if key:
                topic_map.setdefault(key, []).append(entry)

        promotions = 0
        for key, entries in topic_map.items():
            if len(entries) < PROMOTION_THRESHOLD:
                continue

            # Check if a semantic entry for this topic already exists
            existing = await self._memory.retrieve(
                session,
                MemoryQuery(
                    memory_type=MemoryType.SEMANTIC,
                    text_search=key[:60],
                    limit=3,
                ),
            )

            avg_importance = sum(e.importance_score for e in entries) / len(entries)
            topic_summary = f"Recurring topic ({len(entries)}x): {key[:100]}"

            if existing:
                # Merge: increment mention_count and refresh importance
                target = existing[0]
                new_count = target.mention_count + len(entries)
                new_importance = min(1.0, max(target.importance_score, avg_importance))
                if new_count != target.mention_count:
                    target.mention_count = new_count
                    target.importance_score = new_importance
                    target.updated_at = datetime.now(timezone.utc).isoformat()
                    self._memory.store.write(target)
                    logger.debug(
                        "promotion.merged",
                        key=key[:60],
                        mention_count=new_count,
                    )
            else:
                # Create new semantic entry
                await self._memory.store_memory(
                    session,
                    memory_type=MemoryType.SEMANTIC,
                    content={
                        "type": "promoted_topic",
                        "topic_key": key[:200],
                        "occurrence_count": len(entries),
                        "first_seen": entries[-1].created_at,
                        "last_seen": entries[0].created_at,
                        "sample": entries[0].content.get("user_input", "")[:200],
                    },
                    summary=topic_summary,
                    tags=["promoted", "auto"],
                    importance=avg_importance,
                    mention_count=len(entries),
                    source="promotion_engine",
                )
                promotions += 1
                logger.info(
                    "promotion.created",
                    key=key[:60],
                    occurrences=len(entries),
                    importance=avg_importance,
                )

        return promotions
