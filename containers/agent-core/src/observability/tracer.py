from __future__ import annotations
import hashlib
import json
import time
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

TRACE_TTL = 604800        # 7 days
TRACES_INDEX_KEY = "exec:traces:recent"
TRACES_INDEX_MAX = 1000


async def persist_trace(
    *,
    redis_url: str,
    trace_id: str,
    chat_id: str,
    user_id: str,
    spans: list[dict],
    start_ms: int,
    is_scheduled: bool,
    task_id: str,
    status: str,
    user_text: str = "",
) -> None:
    """Write execution trace to Redis. Fire-and-forget safe."""
    try:
        total_ms = int(time.monotonic() * 1000) - start_ms
        payload = {
            "trace_id": trace_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "spans": spans,
            "total_ms": total_ms,
            "span_count": len(spans),
            "status": status,
            "is_scheduled": is_scheduled,
            "task_id": task_id,
            "ts": time.time(),
            "user_text": user_text,
            "skill_names": list({s["skill"] for s in spans}),
            "success_rate": (
                sum(1 for s in spans if s["success"]) / len(spans)
                if spans else 1.0
            ),
        }
        raw = json.dumps(payload)
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            pipe = r.pipeline()
            pipe.setex(f"exec:trace:{trace_id}", TRACE_TTL, raw)
            pipe.zadd(TRACES_INDEX_KEY, {trace_id: payload["ts"]})
            pipe.zremrangebyrank(TRACES_INDEX_KEY, 0, -(TRACES_INDEX_MAX + 1))
            if task_id:
                pipe.setex(f"exec:trace:task:{task_id}", TRACE_TTL, trace_id)
            await pipe.execute()
        finally:
            await r.aclose()
    except Exception as exc:
        logger.debug("tracer.persist_failed", error=str(exc)[:80])


async def load_recent_traces(redis_url: str, limit: int = 200) -> list[dict]:
    """Return up to `limit` most-recent traces, newest first."""
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        ids = await r.zrevrange(TRACES_INDEX_KEY, 0, limit - 1)
        if not ids:
            return []
        raws = await r.mget([f"exec:trace:{tid}" for tid in ids])
        result = []
        for raw in raws:
            if raw:
                try:
                    result.append(json.loads(raw))
                except Exception:
                    pass
        return result
    finally:
        await r.aclose()
