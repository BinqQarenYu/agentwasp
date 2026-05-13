from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ..config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=15,         # Up from 5 — handles concurrent skill executions
    max_overflow=10,      # Up from 5 — burst headroom
    pool_pre_ping=True,   # Validate connections before use (avoids stale conn errors)
    pool_recycle=1800,    # Recycle connections every 30 min (prevents idle timeouts)
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


async def init_db():
    """Create tables if they don't exist (fallback for when Alembic hasn't run)."""
    from .models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_indexes():
    """Create any missing indexes that weren't present at table-creation time.

    Uses IF NOT EXISTS so it is safe to call on every startup.
    CONCURRENTLY is not available inside a transaction, so we use a raw connection.
    """
    from sqlalchemy import text
    async with engine.connect() as conn:
        await conn.execute(text("COMMIT"))  # exit implicit transaction
        await conn.execute(text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_log_chat_id_timestamp "
            "ON audit_log (chat_id, timestamp DESC)"
        ))
        await conn.execute(text("COMMIT"))


async def close_db():
    await engine.dispose()
