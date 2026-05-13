"""ExecutionReflectionPrunerJob — keeps execution_reflections table bounded.

Runs every 6 hours.  Deletes oldest rows when count exceeds MAX_ROWS (1000).
Safe to run concurrently with writes — uses indexed timestamp column.
"""
from __future__ import annotations

import structlog
from sqlalchemy import delete, func, select

from ..db.models import ExecutionReflection
from ..db.session import async_session
from ..reflection_engine import EXEC_REFLECTION_MAX_ROWS

logger = structlog.get_logger()


class ExecutionReflectionPrunerJob:
    """Delete oldest execution_reflections rows when table exceeds MAX_ROWS."""

    async def __call__(self) -> str:
        try:
            async with async_session() as session:
                total = (
                    await session.execute(select(func.count(ExecutionReflection.id)))
                ).scalar() or 0

                if total <= EXEC_REFLECTION_MAX_ROWS:
                    return f"execution_reflection_pruner: {total} rows — no pruning needed"

                excess = total - EXEC_REFLECTION_MAX_ROWS
                # Delete oldest rows via timestamp-ordered subquery
                id_subq = (
                    select(ExecutionReflection.id)
                    .order_by(ExecutionReflection.timestamp.asc())
                    .limit(excess)
                    .scalar_subquery()
                )
                del_result = await session.execute(
                    delete(ExecutionReflection).where(ExecutionReflection.id.in_(id_subq))
                )
                deleted = del_result.rowcount
                await session.commit()

            logger.info(
                "execution_reflection_pruner.deleted",
                deleted=deleted,
                total_before=total,
                remaining=total - deleted,
            )
            return f"execution_reflection_pruner: deleted={deleted} total_before={total}"

        except Exception as exc:
            logger.exception("execution_reflection_pruner.failed", error=str(exc))
            return f"execution_reflection_pruner: failed — {exc}"
