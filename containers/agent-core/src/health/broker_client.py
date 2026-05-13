"""Client for communicating with agent-broker via Redis Streams."""

import asyncio
import json
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

STREAM_COMMANDS = "broker:commands"
STREAM_RESPONSES = "broker:responses"
RESPONSE_GROUP = "core-broker-group"
RESPONSE_CONSUMER = "core-1"


class BrokerClient:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: aioredis.Redis | None = None

    async def connect(self):
        self._client = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await self._client.xgroup_create(
                STREAM_RESPONSES, RESPONSE_GROUP, id="0", mkstream=True
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        logger.info("broker_client.connected")

    async def send_command(
        self, operation: str, target: str, requested_by: str = "core", timeout: float = 35.0
    ) -> dict:
        """Send a command to the broker and wait for the response."""
        if not self._client:
            return {"success": False, "error": "Broker client not connected"}

        correlation_id = str(uuid4())
        await self._client.xadd(STREAM_COMMANDS, {
            "correlation_id": correlation_id,
            "operation": operation,
            "target": target,
            "requested_by": requested_by,
        })

        logger.info(
            "broker_client.command_sent",
            operation=operation,
            target=target,
            correlation_id=correlation_id,
        )

        # Wait for matching response
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                results = await self._client.xreadgroup(
                    groupname=RESPONSE_GROUP,
                    consumername=RESPONSE_CONSUMER,
                    streams={STREAM_RESPONSES: ">"},
                    count=5,
                    block=2000,
                )
            except Exception:
                await asyncio.sleep(1)
                continue

            if not results:
                continue

            for _stream, messages in results:
                for msg_id, data in messages:
                    await self._client.xack(STREAM_RESPONSES, RESPONSE_GROUP, msg_id)
                    if data.get("correlation_id") == correlation_id:
                        success = data.get("success", "false").lower() == "true"
                        return {
                            "success": success,
                            "result": data.get("result", ""),
                            "operation": data.get("operation", ""),
                            "target": data.get("target", ""),
                        }

        return {"success": False, "error": "Broker response timeout"}

    async def restart_container(self, target: str, requested_by: str = "self_healer") -> dict:
        return await self.send_command("restart_container", target, requested_by)

    async def container_status(self, target: str) -> dict:
        return await self.send_command("container_status", target, requested_by="status_check")

    async def container_logs(self, target: str, requested_by: str = "admin") -> dict:
        return await self.send_command("container_logs", target, requested_by)
