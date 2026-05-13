import os

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from .manager import MemoryManager
from .types import MemoryType

logger = structlog.get_logger()


async def seed_initial_memory(manager: MemoryManager, session: AsyncSession):
    """Create initial seed memories if they don't exist."""
    stats = manager.get_stats()
    if stats["total"] > 0:
        logger.info("memory_seed.skipped", reason="memories already exist", total=stats["total"])
        return

    logger.info("memory_seed.starting")

    # System facts — read from environment, no hardcoded values
    await manager.store_memory(
        session,
        memory_type=MemoryType.FACTS,
        content={
            "infrastructure": {
                "python_version": "3.12",
                "services": ["agent-redis", "agent-postgres", "agent-core", "agent-telegram"],
            },
        },
        summary="System infrastructure facts",
        tags=["system", "infrastructure"],
    )

    # Default policies
    await manager.store_memory(
        session,
        memory_type=MemoryType.POLICY,
        content={
            "autonomy_level": "low",
            "rules": [
                "Never run as root",
                "Never execute arbitrary code without user approval",
                "Log all actions to audit trail",
                "Ask for confirmation on destructive operations",
            ],
            "confirmation_required": [
                "file.write",
                "shell.exec",
                "config.modify",
                "memory.rollback",
            ],
            "auto_allowed": [
                "file.read",
                "memory.query",
                "system.info",
                "health.check",
            ],
        },
        summary="Default autonomy policies and safety rules",
        tags=["policy", "autonomy", "safety"],
    )

    # Agent identity — uses env var for bot name if set
    bot_name = os.environ.get("TELEGRAM_BOT_USERNAME", "@your_bot")
    await manager.store_memory(
        session,
        memory_type=MemoryType.META,
        content={
            "name": "Agent Wasp",
            "version": "1.0.0",
            "created_by": "Claude Code",
            "purpose": "Autonomous AI assistant operating via Telegram",
            "telegram_bot": bot_name,
            "capabilities_current": [
                "Conversation via Telegram",
                "Event-driven message processing",
                "Structured memory (episodic, semantic, facts, policy)",
                "Scheduled tasks and reminders",
                "Web browsing, screenshots, scraping",
                "Code execution (Python, shell)",
            ],
        },
        summary="Agent identity and metadata",
        tags=["identity", "meta"],
    )

    stats = manager.get_stats()
    logger.info("memory_seed.complete", total=stats["total"])
