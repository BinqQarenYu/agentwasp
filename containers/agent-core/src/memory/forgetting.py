"""Forgetting Engine — implements structured memory decay and TTL cleanup.

Rules:
- Working memory entries with expires_at are deleted when past due.
- Old episodic entries with low importance are decayed first.
- Nothing critical (importance >= 0.9) is ever auto-deleted.
- Semantic and Policy memories are never auto-deleted.
- Facts are only deleted when expires_at is explicitly set.
"""

from datetime import datetime, timedelta, timezone

import structlog

from .types import MemoryContent, MemoryQuery, MemoryType

logger = structlog.get_logger()

# Episodic entries older than this with low importance are candidates for deletion
EPISODIC_MAX_AGE_DAYS = 3650  # 10 years — near-infinite retention

# Importance threshold below which old episodic entries are trimmed
DECAY_IMPORTANCE_THRESHOLD = 0.05  # Only drop truly irrelevant entries

# Hard cap on episodic entries (importance-sorted; excess is trimmed from bottom)
MAX_EPISODIC_ENTRIES = 10_000  # Store up to 10,000 episodic memories

# Types subject to TTL cleanup
TTL_TYPES = {MemoryType.WORKING, MemoryType.EPISODIC, MemoryType.META}

# Types that are never auto-deleted (only manual)
PROTECTED_TYPES = {MemoryType.SEMANTIC, MemoryType.POLICY, MemoryType.FACTS}


class ForgettingEngine:
    """Manages memory lifecycle: TTL expiry, importance decay, volume caps."""

    def __init__(self, memory_manager=None):
        self._memory = memory_manager

    def set_memory(self, memory_manager) -> None:
        self._memory = memory_manager

    async def apply_ttl(self, session) -> int:
        """Delete entries whose expires_at has passed.

        Only applies to WORKING, EPISODIC, and META types.
        Returns number of entries deleted.
        """
        if self._memory is None:
            return 0

        now = datetime.now(timezone.utc)
        deleted = 0

        for mem_type in TTL_TYPES:
            entries = await self._memory.retrieve(
                session,
                MemoryQuery(memory_type=mem_type, limit=500),
            )
            for entry in entries:
                if not entry.expires_at:
                    continue
                try:
                    exp = datetime.fromisoformat(entry.expires_at)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue

                if now > exp:
                    # Never delete critical entries even if TTL expired
                    if entry.importance_score >= 0.9:
                        logger.debug(
                            "forgetting.ttl_skipped_critical",
                            id=entry.id,
                            type=mem_type,
                        )
                        continue
                    await self._memory.delete(session, mem_type, entry.id)
                    deleted += 1

        if deleted:
            logger.info("forgetting.ttl_cleanup", deleted=deleted)
        return deleted

    async def decay_and_trim_episodic(self, session) -> int:
        """Remove low-importance old episodic entries, then enforce MAX_EPISODIC_ENTRIES.

        Strategy:
        1. Delete entries older than EPISODIC_MAX_AGE_DAYS with importance < DECAY_IMPORTANCE_THRESHOLD.
        2. Sort remaining by importance desc; delete beyond MAX_EPISODIC_ENTRIES.

        Returns total entries deleted.
        """
        if self._memory is None:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=EPISODIC_MAX_AGE_DAYS)
        all_episodic = await self._memory.retrieve(
            session,
            MemoryQuery(memory_type=MemoryType.EPISODIC, limit=15_000),
        )

        deleted = 0

        # Pass 1: Age-based decay of low-importance entries
        survivors = []
        for entry in all_episodic:
            try:
                created = datetime.fromisoformat(entry.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                survivors.append(entry)
                continue

            is_old = created < cutoff
            is_low_importance = entry.importance_score < DECAY_IMPORTANCE_THRESHOLD
            is_critical = entry.importance_score >= 0.9

            if is_old and is_low_importance and not is_critical:
                await self._memory.delete(session, MemoryType.EPISODIC, entry.id)
                deleted += 1
            else:
                survivors.append(entry)

        # Pass 2: Volume cap — keep top MAX_EPISODIC_ENTRIES by importance
        if len(survivors) > MAX_EPISODIC_ENTRIES:
            sorted_entries = sorted(
                survivors,
                key=lambda e: (e.importance_score, e.created_at),
                reverse=True,
            )
            for entry in sorted_entries[MAX_EPISODIC_ENTRIES:]:
                if entry.importance_score >= 0.9:
                    continue  # Never drop critical even over cap
                await self._memory.delete(session, MemoryType.EPISODIC, entry.id)
                deleted += 1

        if deleted:
            logger.info("forgetting.episodic_trimmed", deleted=deleted)
        return deleted

    async def cleanup_completed_working(self, session) -> int:
        """Remove working memory entries that are done/cancelled.

        Returns number deleted.
        """
        if self._memory is None:
            return 0

        working = await self._memory.retrieve(
            session,
            MemoryQuery(memory_type=MemoryType.WORKING, limit=200),
        )

        deleted = 0
        done_statuses = {"completed", "done", "cancelled", "expired", "dismissed"}
        for entry in working:
            status = entry.content.get("status", "")
            if status in done_statuses:
                await self._memory.delete(session, MemoryType.WORKING, entry.id)
                deleted += 1

        return deleted

    async def run_full_cycle(self, session) -> dict:
        """Run all forgetting operations. Returns summary dict."""
        ttl_deleted = await self.apply_ttl(session)
        working_cleaned = await self.cleanup_completed_working(session)
        episodic_trimmed = await self.decay_and_trim_episodic(session)

        summary = {
            "ttl_expired": ttl_deleted,
            "working_cleaned": working_cleaned,
            "episodic_trimmed": episodic_trimmed,
            "total": ttl_deleted + working_cleaned + episodic_trimmed,
        }
        logger.info("forgetting.cycle_complete", **summary)
        return summary
