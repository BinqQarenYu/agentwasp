"""Memory growth control — pruner jobs for unbounded tables.

Three jobs target tables that have NO existing pruning:

KnowledgeGraphPrunerJob (daily)
  - Keeps the top 5,000 KnowledgeNode rows by updated_at (most-recently-active)
  - Deletes the excess — purely LRU, no content judgement needed

LearningExamplesPrunerJob (weekly)
  - Keeps the top 2,000 LearningExample rows by use_count DESC
  - Low-use/stale examples beyond that cap are deleted

BehavioralRulesPrunerJob (monthly)
  - Soft-archives (active=False) rules not triggered in >30 days
  - Hard-deletes rules that have been inactive >90 days
  - DB rows are kept for audit; just deactivated first

All jobs use bounded DELETE with subqueries to avoid long table locks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

logger = structlog.get_logger()

# ── Tunable caps ──────────────────────────────────────────────────────────────
KG_MAX_NODES         = 5_000
LEARNING_MAX_EXAMPLES = 2_000
BEHAVIORAL_INACTIVE_ARCHIVE_DAYS = 30   # soft-archive after this many days unused
BEHAVIORAL_HARD_DELETE_DAYS      = 90   # hard-delete after this many days archived


class KnowledgeGraphPrunerJob:
    """Daily: keep top KG_MAX_NODES knowledge_nodes by updated_at, delete the rest."""

    async def __call__(self) -> str:
        from ..db.models import KnowledgeNode
        from ..db.session import async_session
        from sqlalchemy import select, func, delete

        try:
            async with async_session() as session:
                total: int = (await session.execute(
                    select(func.count(KnowledgeNode.id))
                )).scalar() or 0

                if total <= KG_MAX_NODES:
                    logger.info("kg_pruner.under_cap", total=total, cap=KG_MAX_NODES)
                    return f"kg_pruner: total={total} under cap ({KG_MAX_NODES}), nothing deleted"

                excess = total - KG_MAX_NODES
                # Subquery: IDs of the OLDEST rows (lowest updated_at) beyond the cap
                id_subquery = (
                    select(KnowledgeNode.id)
                    .order_by(KnowledgeNode.updated_at.asc())
                    .limit(excess)
                    .scalar_subquery()
                )
                del_stmt = delete(KnowledgeNode).where(KnowledgeNode.id.in_(id_subquery))
                del_result = await session.execute(del_stmt)
                deleted = del_result.rowcount
                await session.commit()

            logger.info(
                "kg_pruner.done",
                total_before=total,
                deleted=deleted,
                remaining=total - deleted,
                cap=KG_MAX_NODES,
            )
            return f"kg_pruner: deleted={deleted} remaining={total - deleted} cap={KG_MAX_NODES}"

        except Exception as exc:
            logger.exception("kg_pruner.failed", error=str(exc))
            return f"kg_pruner: failed — {exc}"


class LearningExamplesPrunerJob:
    """Weekly: keep top LEARNING_MAX_EXAMPLES learning_examples by use_count DESC."""

    async def __call__(self) -> str:
        from ..db.models import LearningExample
        from ..db.session import async_session
        from sqlalchemy import select, func, delete

        try:
            async with async_session() as session:
                total: int = (await session.execute(
                    select(func.count(LearningExample.id))
                )).scalar() or 0

                if total <= LEARNING_MAX_EXAMPLES:
                    logger.info("learning_pruner.under_cap", total=total, cap=LEARNING_MAX_EXAMPLES)
                    return f"learning_pruner: total={total} under cap ({LEARNING_MAX_EXAMPLES}), nothing deleted"

                excess = total - LEARNING_MAX_EXAMPLES
                # Subquery: IDs of the LOWEST-use rows beyond the cap
                id_subquery = (
                    select(LearningExample.id)
                    .order_by(LearningExample.use_count.asc(), LearningExample.created_at.asc())
                    .limit(excess)
                    .scalar_subquery()
                )
                del_stmt = delete(LearningExample).where(LearningExample.id.in_(id_subquery))
                del_result = await session.execute(del_stmt)
                deleted = del_result.rowcount
                await session.commit()

            logger.info(
                "learning_pruner.done",
                total_before=total,
                deleted=deleted,
                remaining=total - deleted,
                cap=LEARNING_MAX_EXAMPLES,
            )
            return f"learning_pruner: deleted={deleted} remaining={total - deleted} cap={LEARNING_MAX_EXAMPLES}"

        except Exception as exc:
            logger.exception("learning_pruner.failed", error=str(exc))
            return f"learning_pruner: failed — {exc}"


class BehavioralRulesPrunerJob:
    """Monthly: archive unused rules, hard-delete long-archived rules.

    Phase 1 — Soft-archive: set active=False for rules whose times_applied=0
               AND created_at is older than BEHAVIORAL_INACTIVE_ARCHIVE_DAYS.
               (Rules applied even once are kept active indefinitely.)

    Phase 2 — Hard-delete: delete rules where active=False AND created_at is
               older than BEHAVIORAL_HARD_DELETE_DAYS.
               These have been inactive long enough to be audited if needed.
    """

    async def __call__(self) -> str:
        from ..db.models import BehavioralRule
        from ..db.session import async_session
        from sqlalchemy import select, update, delete

        now = datetime.now(timezone.utc)
        archive_cutoff = now - timedelta(days=BEHAVIORAL_INACTIVE_ARCHIVE_DAYS)
        hard_delete_cutoff = now - timedelta(days=BEHAVIORAL_HARD_DELETE_DAYS)

        archived = 0
        hard_deleted = 0

        try:
            async with async_session() as session:
                # Phase 1: soft-archive rules never triggered + old enough
                archive_stmt = (
                    update(BehavioralRule)
                    .where(
                        BehavioralRule.active == True,      # noqa: E712
                        BehavioralRule.times_applied == 0,
                        BehavioralRule.created_at < archive_cutoff,
                    )
                    .values(active=False)
                )
                archive_result = await session.execute(archive_stmt)
                archived = archive_result.rowcount

                # Phase 2: hard-delete rules that have been inactive long enough
                delete_stmt = delete(BehavioralRule).where(
                    BehavioralRule.active == False,     # noqa: E712
                    BehavioralRule.created_at < hard_delete_cutoff,
                )
                delete_result = await session.execute(delete_stmt)
                hard_deleted = delete_result.rowcount

                await session.commit()

            logger.info(
                "behavioral_pruner.done",
                archived=archived,
                hard_deleted=hard_deleted,
                archive_cutoff_days=BEHAVIORAL_INACTIVE_ARCHIVE_DAYS,
                hard_delete_cutoff_days=BEHAVIORAL_HARD_DELETE_DAYS,
            )
            return (
                f"behavioral_pruner: "
                f"archived={archived} (unused >{BEHAVIORAL_INACTIVE_ARCHIVE_DAYS}d) "
                f"hard_deleted={hard_deleted} (inactive >{BEHAVIORAL_HARD_DELETE_DAYS}d)"
            )

        except Exception as exc:
            logger.exception("behavioral_pruner.failed", error=str(exc))
            return f"behavioral_pruner: failed — {exc}"
