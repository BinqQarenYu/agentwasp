import json
from enum import Enum

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()


class EventBus:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.client: redis.Redis | None = None

    async def connect(self):
        self.client = redis.from_url(self.redis_url, decode_responses=True)
        await self.client.ping()
        logger.info("event_bus.connected", redis_url=self.redis_url)

    async def reconnect(self):
        """Close and re-establish the Redis connection."""
        logger.info("event_bus.reconnecting")
        try:
            if self.client:
                await self.client.aclose()
        except Exception:
            pass
        self.client = None
        await self.connect()

    async def disconnect(self):
        if self.client:
            await self.client.aclose()
            logger.info("event_bus.disconnected")

    async def ensure_group(self, stream: str, group: str):
        try:
            await self.client.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("event_bus.group_created", stream=stream, group=group)
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                pass  # Group already exists
            else:
                raise

    async def publish(self, stream: str, data: dict) -> str:
        # Use .value for Enum members so "telegram.progress" is stored, not "EventType.TELEGRAM_PROGRESS"
        def _serialize(v):
            if isinstance(v, dict):
                return json.dumps(v)
            if isinstance(v, Enum):
                return v.value
            return str(v)

        payload = {k: _serialize(v) for k, v in data.items()}
        msg_id = await self.client.xadd(stream, payload, maxlen=10000, approximate=True)
        logger.debug("event_bus.published", stream=stream, msg_id=msg_id)
        return msg_id

    async def consume(
        self, stream: str, group: str, consumer: str, count: int = 1, block: int = 5000
    ) -> list[tuple[str, dict]]:
        try:
            results = await self.client.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=count,
                block=block,
            )
        except redis.ResponseError as e:
            # NOGROUP: stream and/or consumer group were deleted at runtime
            # (factory reset, FLUSHDB, manual XGROUP DESTROY, etc.).
            # Re-create the group in-place and retry once. Messages produced
            # while the group was gone are unrecoverable — '$' MKSTREAM means
            # we resume from the latest entry, not the wiped backlog.
            if "NOGROUP" in str(e):
                logger.warning(
                    "event_bus.nogroup_recovering",
                    stream=stream, group=group, consumer=consumer,
                )
                try:
                    await self.client.xgroup_create(
                        stream, group, id="$", mkstream=True,
                    )
                    logger.info(
                        "event_bus.group_recreated_after_nogroup",
                        stream=stream, group=group,
                    )
                except redis.ResponseError as ce:
                    if "BUSYGROUP" not in str(ce):
                        # Race: another worker beat us to recreate — that's fine.
                        # Anything else is a real failure.
                        raise
                # Retry the read — if it fails again, let the exception bubble
                # so the consumer-loop's outer handler logs and backs off.
                results = await self.client.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=count,
                    block=block,
                )
            else:
                raise
        messages = []
        if results:
            for _stream_name, stream_messages in results:
                for msg_id, raw_data in stream_messages:
                    data = {}
                    for k, v in raw_data.items():
                        try:
                            data[k] = json.loads(v)
                        except (json.JSONDecodeError, TypeError):
                            data[k] = v
                    messages.append((msg_id, data))
        return messages

    async def ack(self, stream: str, group: str, msg_id: str):
        try:
            await self.client.xack(stream, group, msg_id)
            logger.debug("event_bus.acked", stream=stream, msg_id=msg_id)
        except redis.ResponseError as e:
            # NOGROUP can fire here too if the group was wiped between
            # consume() and ack(). The message is lost regardless — log and
            # continue rather than crashing the loop.
            if "NOGROUP" in str(e):
                logger.warning(
                    "event_bus.ack_nogroup_skipped",
                    stream=stream, group=group, msg_id=msg_id,
                )
                return
            raise
