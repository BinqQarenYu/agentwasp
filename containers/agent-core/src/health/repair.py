"""Self-healing: auto-reconnection and recovery procedures."""

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = structlog.get_logger()


class SelfHealer:
    def __init__(self, bus, memory, model_manager, notify_chat_id: str = "", broker_client=None):
        self.bus = bus
        self.memory = memory
        self.model_manager = model_manager
        self.notify_chat_id = notify_chat_id
        self.broker_client = broker_client

    async def handle_failure(self, service: str, error: str):
        """Dispatch to the appropriate repair method based on service name."""
        logger.warning("self_heal.triggered", service=service, error=error)

        if service == "redis":
            await self._repair_redis()
        elif service == "disk":
            await self._repair_disk()
        elif service == "ollama":
            await self._repair_ollama()
        # PostgreSQL: SQLAlchemy pool handles reconnection automatically

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _repair_redis(self):
        """Reconnect EventBus to Redis with exponential backoff."""
        logger.info("self_heal.redis_reconnecting")
        try:
            if self.bus.client:
                await self.bus.client.aclose()
        except Exception:
            pass
        await self.bus.connect()
        logger.info("self_heal.redis_reconnected")

    async def _repair_disk(self):
        """Emergency disk cleanup: trigger memory cleanup and alert."""
        logger.warning("self_heal.disk_cleanup_starting")
        from ..db.session import async_session
        from ..memory.types import MemoryQuery, MemoryType

        cleaned = 0
        async with async_session() as session:
            # Delete completed working memory
            working = await self.memory.retrieve(
                session, MemoryQuery(memory_type=MemoryType.WORKING, limit=100),
            )
            for entry in working:
                status = entry.content.get("status", "")
                if status in ("completed", "done", "cancelled"):
                    await self.memory.delete(session, MemoryType.WORKING, entry.id)
                    cleaned += 1

            # Aggressive episodic trim (keep 100 instead of 200)
            episodic = await self.memory.retrieve(
                session, MemoryQuery(memory_type=MemoryType.EPISODIC, limit=500),
            )
            if len(episodic) > 100:
                for entry in episodic[100:]:
                    await self.memory.delete(session, MemoryType.EPISODIC, entry.id)
                    cleaned += 1

        logger.info("self_heal.disk_cleanup_done", cleaned=cleaned)

        # Alert admin
        if self.notify_chat_id:
            try:
                from uuid import uuid4
                from ..utils.safe_notify import safe_notify
                await safe_notify(
                    self.bus,
                    self.notify_chat_id,
                    f"Self-Heal: Disk full alert. Emergency cleanup removed {cleaned} entries.",
                    source="self_heal",
                    correlation_id=str(uuid4()),
                )
            except Exception:
                logger.exception("self_heal.alert_failed")

    async def _repair_ollama(self):
        """If Ollama is down, try switching to a remote provider."""
        logger.info("self_heal.ollama_fallback")
        status = self.model_manager.get_status()
        providers = list(status["providers"].keys())

        for provider in providers:
            if provider != "ollama":
                success = await self.model_manager.switch_provider(provider)
                if success:
                    logger.info("self_heal.ollama_fallback_success", provider=provider)
                    return

        logger.warning("self_heal.ollama_no_fallback")

    async def restart_container(self, target: str) -> dict:
        """Request a container restart via the broker."""
        if not self.broker_client:
            return {"success": False, "error": "Broker client not available"}
        logger.info("self_heal.restart_requested", target=target)
        result = await self.broker_client.restart_container(target, requested_by="self_healer")
        logger.info("self_heal.restart_result", target=target, success=result.get("success"))
        return result
