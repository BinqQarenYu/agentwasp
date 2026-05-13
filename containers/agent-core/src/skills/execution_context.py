from __future__ import annotations
import re
import uuid

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

EXEC_CTX_TTL = 3600
_TEMPLATE_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class ExecutionContext:
    """Per-execution key-value store backed by Redis hash exec:ctx:{execution_id}."""

    def __init__(self, redis_url: str, execution_id: str) -> None:
        self._redis_url = redis_url
        self.execution_id = execution_id
        self._local: dict[str, str] = {}

    @classmethod
    def new(cls, redis_url: str) -> "ExecutionContext":
        return cls(redis_url, str(uuid.uuid4()))

    async def store(self, key: str, value: str) -> None:
        self._local[key] = value
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            rkey = f"exec:ctx:{self.execution_id}"
            await r.hset(rkey, key, value)
            await r.expire(rkey, EXEC_CTX_TTL)
        except Exception as exc:
            logger.debug("exec_ctx.store_failed", error=str(exc)[:60])
        finally:
            await r.aclose()

    async def get(self, key: str) -> str | None:
        if key in self._local:
            return self._local[key]
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            val = await r.hget(f"exec:ctx:{self.execution_id}", key)
            if val is not None:
                self._local[key] = val
            return val
        except Exception:
            return None
        finally:
            await r.aclose()

    async def load_all(self) -> dict[str, str]:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            data = await r.hgetall(f"exec:ctx:{self.execution_id}")
            self._local.update(data)
            return dict(self._local)
        except Exception:
            return dict(self._local)
        finally:
            await r.aclose()

    async def resolve(self, template_str: str) -> tuple[str, bool]:
        """Replace {{variable}} placeholders. Returns (resolved, all_found)."""
        ctx = await self.load_all()
        missing: list[str] = []
        result = template_str
        for m in _TEMPLATE_RE.finditer(template_str):
            var = m.group(1)
            if var in ctx:
                result = result.replace(m.group(0), ctx[var])
            else:
                missing.append(var)
        return result, len(missing) == 0

    def has_templates(self, text: str) -> bool:
        return bool(_TEMPLATE_RE.search(str(text)))
