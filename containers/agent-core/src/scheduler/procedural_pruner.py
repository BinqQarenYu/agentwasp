"""Memory Maintenance Pruner — daily cleanup for procedural, timeline, and dream data.

Pruning rules:
  Procedures:
    - success_count = 0  AND created_at < now-30d  →  delete (never proven useful)
    - success_count < 2  AND created_at < now-60d  →  delete (barely used, old)

  WorldTimeline:
    - price/metric observations: delete where expires_at < now-7d
    - all other types (mention, state, event): delete where expires_at < now-90d
    - rows without expires_at: delete where observed_at < now-90d

  DreamLog:
    - keep last 90 days, delete older (log is audit-only; consolidations already applied)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

logger = structlog.get_logger()


class ProceduralPrunerJob:
    """Daily maintenance: prunes stale procedural memory, timeline observations, and dream logs."""

    # ── Procedures ────────────────────────────────────────────────────────
    PROC_ZERO_TTL_DAYS = 30
    PROC_LOW_THRESHOLD = 2
    PROC_LOW_TTL_DAYS = 60

    # ── WorldTimeline ──────────────────────────────────────────────────────
    TIMELINE_PRICE_TTL_DAYS = 7     # price data stales fast
    TIMELINE_OTHER_TTL_DAYS = 90    # mentions/states/events kept longer

    # ── DreamLog ──────────────────────────────────────────────────────────
    DREAM_TTL_DAYS = 90

    async def __call__(self) -> str:
        parts = []
        parts.append(await self._prune_procedures())
        parts.append(await self._prune_timeline())
        parts.append(await self._prune_dreams())
        return " | ".join(parts)

    # ── Procedures ────────────────────────────────────────────────────────

    async def _prune_procedures(self) -> str:
        from ..db.models import ProceduralMemory
        from ..db.session import async_session
        from sqlalchemy import select

        now = datetime.now(timezone.utc)
        zero_cutoff = now - timedelta(days=self.PROC_ZERO_TTL_DAYS)
        low_cutoff = now - timedelta(days=self.PROC_LOW_TTL_DAYS)

        deleted_zero = deleted_low = 0
        try:
            async with async_session() as session:
                res = await session.execute(
                    select(ProceduralMemory).where(
                        ProceduralMemory.success_count == 0,
                        ProceduralMemory.created_at < zero_cutoff,
                    )
                )
                for row in res.scalars().all():
                    await session.delete(row)
                    deleted_zero += 1

                res2 = await session.execute(
                    select(ProceduralMemory).where(
                        ProceduralMemory.success_count > 0,
                        ProceduralMemory.success_count < self.PROC_LOW_THRESHOLD,
                        ProceduralMemory.created_at < low_cutoff,
                    )
                )
                for row in res2.scalars().all():
                    await session.delete(row)
                    deleted_low += 1

                await session.commit()
        except Exception:
            logger.exception("procedural_pruner.procedures_error")
            return "procedures: error"

        total = deleted_zero + deleted_low
        logger.info("procedural_pruner.procedures_done", deleted_zero=deleted_zero, deleted_low=deleted_low)
        return f"procedures: -{total}"

    # ── WorldTimeline ──────────────────────────────────────────────────────

    async def _prune_timeline(self) -> str:
        from ..db.models import WorldTimeline
        from ..db.session import async_session
        from sqlalchemy import select

        now = datetime.now(timezone.utc)
        price_cutoff = now - timedelta(days=self.TIMELINE_PRICE_TTL_DAYS)
        other_cutoff = now - timedelta(days=self.TIMELINE_OTHER_TTL_DAYS)

        deleted = 0
        try:
            async with async_session() as session:
                # Price/metric: delete if expires_at older than 7 days
                res = await session.execute(
                    select(WorldTimeline).where(
                        WorldTimeline.observation_type.in_(["price", "metric"]),
                        WorldTimeline.expires_at < price_cutoff,
                    )
                )
                for row in res.scalars().all():
                    await session.delete(row)
                    deleted += 1

                # Everything else: delete if expires_at older than 90 days
                res2 = await session.execute(
                    select(WorldTimeline).where(
                        WorldTimeline.observation_type.notin_(["price", "metric"]),
                        WorldTimeline.expires_at < other_cutoff,
                    )
                )
                for row in res2.scalars().all():
                    await session.delete(row)
                    deleted += 1

                # No expires_at: treat as other_cutoff based on observed_at
                res3 = await session.execute(
                    select(WorldTimeline).where(
                        WorldTimeline.expires_at.is_(None),
                        WorldTimeline.observed_at < other_cutoff,
                    )
                )
                for row in res3.scalars().all():
                    await session.delete(row)
                    deleted += 1

                await session.commit()
        except Exception:
            logger.exception("procedural_pruner.timeline_error")
            return "timeline: error"

        logger.info("procedural_pruner.timeline_done", deleted=deleted)
        return f"timeline: -{deleted}"

    # ── DreamLog ──────────────────────────────────────────────────────────

    async def _prune_dreams(self) -> str:
        from ..db.models import DreamLog
        from ..db.session import async_session
        from sqlalchemy import select

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.DREAM_TTL_DAYS)

        deleted = 0
        try:
            async with async_session() as session:
                res = await session.execute(
                    select(DreamLog).where(DreamLog.started_at < cutoff)
                )
                for row in res.scalars().all():
                    await session.delete(row)
                    deleted += 1
                await session.commit()
        except Exception:
            logger.exception("procedural_pruner.dreams_error")
            return "dreams: error"

        logger.info("procedural_pruner.dreams_done", deleted=deleted)
        return f"dreams: -{deleted}"
