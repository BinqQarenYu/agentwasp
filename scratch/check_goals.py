import asyncio
import json
import sys
import os

async def main():
    try:
        import asyncpg
        dsn = os.environ['DATABASE_URL'].replace("postgresql+asyncpg", "postgresql")
        conn = await asyncpg.connect(dsn)
        rows = await conn.fetch("SELECT id, title, status, last_error FROM goals ORDER BY created_at ASC")
        print(json.dumps([dict(r) for r in rows], default=str))
        await conn.close()
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())
