"""Audit log retention job.

Deletes audit_log rows older than a configurable retention window.
Uses bounded batch deletion to avoid long table locks on large tables.
"""

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, func, select

from ..db.models import AuditLog
from ..db.session import async_session

logger = structlog.get_logger()

# Default retention: 30 days
_DEFAULT_RETENTION_DAYS = 30
# Batch size: delete at most this many rows per run to avoid long locks.
# Raised from 5_000 → 50_000: daily write volume is ~19k rows so 5k/day
# could never clear the backlog.  50k per 6-hour run = 200k/day capacity,
# which comfortably absorbs growth and clears the existing backlog.
_BATCH_SIZE = 50_000


class AuditRetentionJob:
    """Runs every 6 hours; removes audit_log rows older than retention_days.

    Uses bounded batch deletion:
    - Deletes at most _BATCH_SIZE rows per execution
    - Logs how many rows were deleted and how many remain eligible
    - Never touches rows younger than retention_days
    - Completely safe to run with concurrent writes (no table lock)
    - Uses the indexed timestamp column for all WHERE/ORDER clauses
    """

    def __init__(self, retention_days: int = _DEFAULT_RETENTION_DAYS):
        self.retention_days = retention_days

    async def __call__(self) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)

        try:
            async with async_session() as session:
                # Count eligible rows first (cheap index scan on timestamp)
                count_stmt = select(func.count(AuditLog.id)).where(
                    AuditLog.timestamp < cutoff
                )
                eligible: int = (await session.execute(count_stmt)).scalar() or 0

                if eligible == 0:
                    logger.info(
                        "audit_retention.no_rows_eligible",
                        cutoff=cutoff.isoformat(),
                        retention_days=self.retention_days,
                    )
                    return f"audit_retention: 0 rows eligible (cutoff={cutoff.date()})"

                # Delete via subquery — avoids passing 50k IDs as bind parameters
                # (asyncpg hard-limits bind params to 32767) and avoids a full-
                # table lock.  The subquery uses the indexed timestamp column so
                # the inner SELECT is an index scan, not a seq scan.
                id_subquery = (
                    select(AuditLog.id)
                    .where(AuditLog.timestamp < cutoff)
                    .order_by(AuditLog.timestamp.asc())
                    .limit(_BATCH_SIZE)
                    .scalar_subquery()
                )
                del_stmt = delete(AuditLog).where(AuditLog.id.in_(id_subquery))
                del_result = await session.execute(del_stmt)
                deleted = del_result.rowcount
                await session.commit()

            remaining_eligible = max(0, eligible - deleted)
            logger.info(
                "audit_retention.deleted",
                deleted=deleted,
                eligible_before=eligible,
                remaining_eligible=remaining_eligible,
                cutoff=cutoff.isoformat(),
                retention_days=self.retention_days,
            )
            msg = (
                f"audit_retention: deleted={deleted} "
                f"eligible_before={eligible} "
                f"remaining_eligible={remaining_eligible} "
                f"cutoff={cutoff.date()}"
            )
            return msg

        except Exception as exc:
            logger.exception("audit_retention.failed", error=str(exc))
            return f"audit_retention: failed — {exc}"
