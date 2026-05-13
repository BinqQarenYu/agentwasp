"""DbMaintenanceJob — weekly VACUUM ANALYZE to keep PostgreSQL healthy.

VACUUM ANALYZE (non-FULL) is safe to run online: it reclaims dead row space
for the free-space map and updates planner statistics without locking tables.
Runs once per week (604800s). Must execute outside a transaction (AUTOCOMMIT).
"""

import structlog

logger = structlog.get_logger()


class DbMaintenanceJob:
    """Weekly PostgreSQL maintenance: VACUUM ANALYZE."""

    async def __call__(self) -> str:
        try:
            from ..db.session import engine
            from sqlalchemy import text

            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text("VACUUM ANALYZE"))

            logger.info("db_maintenance.vacuum_analyze_done")
            return "ok"
        except Exception as e:
            logger.exception("db_maintenance.vacuum_analyze_error")
            return f"error: {str(e).splitlines()[0][:80]}"
