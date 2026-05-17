import asyncio
import logging

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import settings
from .bridge import TelegramBridge
from .handlers.commands import start_command, help_command

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        LOG_LEVELS.get(settings.log_level.upper(), logging.INFO)
    ),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()

bridge: TelegramBridge | None = None


def is_authorized(user_id: int) -> bool:
    allowed = settings.allowed_user_ids
    if not allowed:
        return True  # No restrictions if empty
    return user_id in allowed


async def handle_message(update: Update, context):
    """Handle free-text messages - forward to agent-core via Redis."""
    if not update.message or not update.message.text:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    await bridge.publish_incoming(
        event_type="telegram.message",
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        text=update.message.text,
    )


async def handle_photo(update: Update, context):
    """Handle photo messages — download and forward to agent-core."""
    if not update.message or not update.message.photo:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    photo = update.message.photo[-1]  # Largest available size
    caption = update.message.caption or "¿Qué hay en esta imagen?"
    try:
        photo_file = await photo.get_file()
        image_path = await bridge.download_photo(photo_file)
    except Exception as e:
        logger.warning("telegram.photo_download_failed", error=str(e))
        await update.message.reply_text("No pude descargar la imagen.")
        return

    await bridge.publish_incoming_with_media(
        event_type="telegram.message",
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        text=caption,
        image_path=image_path,
    )


async def handle_voice(update: Update, context):
    """Handle voice and audio messages — download and forward to agent-core."""
    if not update.message:
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    try:
        voice_file = await voice.get_file()
        audio_path = await bridge.download_voice(voice_file)
    except Exception as e:
        logger.warning("telegram.voice_download_failed", error=str(e))
        await update.message.reply_text("No pude descargar el audio.")
        return

    caption = update.message.caption or ""
    await bridge.publish_incoming_with_media(
        event_type="telegram.message",
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        text=caption,
        audio_path=audio_path,
    )


async def handle_video(update: Update, context):
    """Handle video messages — download and forward to agent-core."""
    if not update.message:
        return
    video = update.message.video or update.message.video_note
    if not video:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    try:
        video_file = await video.get_file()
        video_path = await bridge.download_video(video_file)
    except Exception as e:
        logger.warning("telegram.video_download_failed", error=str(e))
        await update.message.reply_text("No pude descargar el video.")
        return

    caption = update.message.caption or "¿Qué hay en este video?"
    await bridge.publish_incoming_with_media(
        event_type="telegram.message",
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        text=caption,
        video_path=video_path,
    )


async def handle_command(update: Update, context):
    """Handle commands that should be processed by agent-core."""
    if not update.message or not update.message.text:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    await bridge.publish_incoming(
        event_type="telegram.command",
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        text=update.message.text,
    )


async def post_init(application: Application):
    """Called after the application is initialized."""
    global bridge
    bridge = TelegramBridge(application.bot)
    await bridge.connect()

    # Start the outgoing event listener as a background task
    asyncio.create_task(bridge.start_outgoing_listener())
    logger.info("telegram.post_init.complete")


async def post_shutdown(application: Application):
    """Called during shutdown."""
    if bridge:
        await bridge.disconnect()
    logger.info("telegram.post_shutdown.complete")


def main():
    logger.info("telegram_bot.starting")

    if not settings.telegram_bot_token:
        logger.error("telegram_bot.no_token", msg="TELEGRAM_BOT_TOKEN not set")
        return

    # Fail-closed, NO escape hatch.
    # An empty TELEGRAM_ALLOWED_USERS would let ANY Telegram user invoke the
    # full skill surface (shell, python_exec, gmail, etc.) on the host — that's
    # a critical security hole. The bot refuses to start unless at least one
    # numeric Telegram user_id is whitelisted. There is no public-bot mode.
    if not settings.allowed_user_ids:
        logger.error(
            "telegram_bot.refused_empty_allowlist",
            msg=(
                "TELEGRAM_ALLOWED_USERS is empty. Refusing to start. "
                "Set TELEGRAM_ALLOWED_USERS=<your_numeric_telegram_id> in .env "
                "(get your id from @userinfobot on Telegram), then restart with: "
                "docker compose up -d agent-telegram"
            ),
        )
        return

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Local fast-response commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    # Commands routed through agent-core
    app.add_handler(CommandHandler("ping", handle_command))
    app.add_handler(CommandHandler("status", handle_command))
    app.add_handler(CommandHandler("memory", handle_command))
    app.add_handler(CommandHandler("snapshot", handle_command))
    app.add_handler(CommandHandler("model", handle_command))
    app.add_handler(CommandHandler("skills", handle_command))
    app.add_handler(CommandHandler("skill", handle_command))
    app.add_handler(CommandHandler("schedule", handle_command))
    app.add_handler(CommandHandler("introspect", handle_command))
    app.add_handler(CommandHandler("broker", handle_command))
    app.add_handler(CommandHandler("api", handle_command))
    app.add_handler(CommandHandler("openclaw", handle_command))
    app.add_handler(CommandHandler("monitor", handle_command))

    # All text messages go to agent-core
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Multimodal: photos, voice/audio, video
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))

    logger.info("telegram_bot.polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
