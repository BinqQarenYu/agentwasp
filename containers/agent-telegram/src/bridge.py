import asyncio
import json
import os
from uuid import uuid4
from datetime import datetime, timezone

import redis.asyncio as redis
import structlog
from telegram import Bot, InputMediaPhoto
from telegram.constants import ChatAction

from .config import settings

logger = structlog.get_logger()


class TelegramBridge:
    """Bridges Telegram messages to/from Redis event streams."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.redis_client: redis.Redis | None = None
        self._running = False
        self._typing_tasks: dict[str, asyncio.Task] = {}
        # Live progress tracking: chat_id -> message_id of status message
        self._progress_msg_ids: dict[str, int] = {}
        # Phase 5: per-correlation response guard — correlation_id -> (chat_id, asyncio.Task)
        self._pending: dict[str, tuple[str, asyncio.Task]] = {}
        # Duplicate delivery guard — track delivered correlation_ids (last 500, no double-send)
        self._delivered: set[str] = set()
        self._delivered_order: list[str] = []  # for eviction (FIFO, max 500)
        # Media-group buffers: media_group_id -> {chat_id, photos: [(path, caption)], total, created_at}
        # When a published event carries media_group_id/index/total, buffer until all
        # photos arrive then send a single sendMediaGroup album. Buffers older than
        # _MEDIA_GROUP_TIMEOUT are flushed defensively (each photo as a fallback single).
        self._media_buffers: dict[str, dict] = {}
        self._media_lock = asyncio.Lock()

    async def connect(self):
        self.redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        await self.redis_client.ping()

        # Ensure consumer group for outgoing events
        try:
            await self.redis_client.xgroup_create(
                settings.stream_outgoing, settings.consumer_group, id="0", mkstream=True
            )
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        logger.info("bridge.connected")

    async def disconnect(self):
        self._running = False
        if self.redis_client:
            await self.redis_client.aclose()
        logger.info("bridge.disconnected")

    async def _typing_loop(self, chat_id: str):
        """Send typing action every 4 seconds until cancelled."""
        try:
            while True:
                await self.bot.send_chat_action(
                    chat_id=int(chat_id), action=ChatAction.TYPING
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    def _start_typing(self, chat_id: str):
        """Start typing indicator for a chat."""
        # Cancel existing typing task for this chat if any
        existing = self._typing_tasks.get(chat_id)
        if existing and not existing.done():
            existing.cancel()
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str):
        """Stop typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def download_photo(self, photo_file) -> str:
        """Download a Telegram photo to shared storage. Returns local path."""
        upload_dir = "/data/shared/uploads"
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, f"photo_{uuid4().hex}.jpg")
        await photo_file.download_to_drive(filepath)
        logger.info("bridge.photo_downloaded", path=filepath)
        return filepath

    async def download_voice(self, voice_file) -> str:
        """Download a Telegram voice/audio file to shared storage. Returns local path."""
        upload_dir = "/data/shared/uploads"
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, f"voice_{uuid4().hex}.ogg")
        await voice_file.download_to_drive(filepath)
        logger.info("bridge.voice_downloaded", path=filepath)
        return filepath

    async def download_video(self, video_file) -> str:
        """Download a Telegram video file to shared storage. Returns local path."""
        upload_dir = "/data/shared/uploads"
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, f"video_{uuid4().hex}.mp4")
        await video_file.download_to_drive(filepath)
        logger.info("bridge.video_downloaded", path=filepath)
        return filepath

    async def _is_delivered(self, correlation_id: str) -> bool:
        """Check duplicate delivery: fast in-memory path, then Redis for restart safety."""
        if correlation_id in self._delivered:
            return True
        if self.redis_client:
            try:
                return bool(await self.redis_client.exists(f"bridge:delivered:{correlation_id}"))
            except Exception:
                pass
        return False

    async def _mark_delivered(self, correlation_id: str) -> None:
        """Record delivery in memory + Redis (TTL 10 min) for restart-safe dedup."""
        self._delivered.add(correlation_id)
        self._delivered_order.append(correlation_id)
        if len(self._delivered_order) > 500:
            evicted = self._delivered_order.pop(0)
            self._delivered.discard(evicted)
        if self.redis_client:
            try:
                await self.redis_client.setex(f"bridge:delivered:{correlation_id}", 600, "1")
            except Exception:
                pass  # memory guard already active; Redis failure is non-fatal

    async def _response_timeout_guard(
        self, correlation_id: str, chat_id: str, timeout: float = 95.0
    ) -> None:
        """Phase 3/5: If no TELEGRAM_RESPONSE arrives within timeout, force stop + fallback."""
        try:
            await asyncio.sleep(timeout)
            if correlation_id not in self._pending:
                return  # already completed normally
            logger.error(
                "bridge.response_timeout",
                correlation_id=correlation_id,
                chat_id=chat_id,
                timeout_s=timeout,
            )
            self._pending.pop(correlation_id, None)
            self._stop_typing(chat_id)
            await self.bot.send_message(
                chat_id=int(chat_id),
                text="No pude completar la solicitud a tiempo. Intenta nuevamente.",
            )
        except asyncio.CancelledError:
            pass  # normal — cancelled when response arrived in time
        except Exception as e:
            logger.warning("bridge.timeout_guard_error", error=str(e)[:80])
            self._stop_typing(chat_id)  # always stop typing even if send fails

    async def _send_with_retry(self, chat_id: str, text: str, correlation_id: str, max_attempts: int = 3) -> None:
        """Send a Telegram message with retry on timeout."""
        import asyncio as _asyncio
        for attempt in range(max_attempts):
            try:
                await self.bot.send_message(chat_id=int(chat_id), text=text)
                logger.info(
                    "telegram_message_sent",
                    chat_id=chat_id,
                    correlation_id=correlation_id,
                    chars=len(text),
                )
                return
            except Exception as exc:
                if attempt < max_attempts - 1:
                    wait = 2 ** attempt  # 1s, 2s
                    logger.warning(
                        "bridge.send_retry",
                        chat_id=chat_id,
                        attempt=attempt + 1,
                        error=str(exc)[:80],
                        wait_s=wait,
                    )
                    await _asyncio.sleep(wait)
                else:
                    logger.error(
                        "bridge.send_failed",
                        chat_id=chat_id,
                        correlation_id=correlation_id,
                        error=str(exc)[:120],
                    )

    def _register_pending(self, correlation_id: str, chat_id: str, timeout: float = 95.0) -> None:
        """Register a correlation and start the safety timeout guard."""
        guard = asyncio.create_task(
            self._response_timeout_guard(correlation_id, chat_id, timeout)
        )
        self._pending[correlation_id] = (chat_id, guard)

    def _complete_pending(self, correlation_id: str) -> None:
        """Mark a correlation as completed and cancel its timeout guard."""
        entry = self._pending.pop(correlation_id, None)
        if entry:
            _, guard = entry
            guard.cancel()

    async def _flush_media_group(self, mid: str) -> None:
        """Send all buffered photos for media_group_id `mid` as a single album.

        Telegram API caps an album at 10 photos; longer groups are split into
        consecutive albums. The first photo's caption becomes the album caption.
        """
        async with self._media_lock:
            buf = self._media_buffers.pop(mid, None)
        if not buf:
            return
        chat_id = buf["chat_id"]
        photos = [p for p in buf["photos"] if p is not None]
        if not photos:
            return

        try:
            chat_id_int = int(chat_id)
        except ValueError:
            logger.warning("bridge.media_group_invalid_chat", chat_id=chat_id)
            return

        # Telegram albums are capped at 10 items
        chunks = [photos[i:i + 10] for i in range(0, len(photos), 10)]
        for chunk_idx, chunk in enumerate(chunks):
            media_items = []
            file_handles = []
            try:
                for i, (path, caption) in enumerate(chunk):
                    if not os.path.exists(path):
                        logger.warning("bridge.media_group_skip_missing", path=path)
                        continue
                    fh = open(path, "rb")
                    file_handles.append(fh)
                    cap = (caption or "")[:1024] if i == 0 and chunk_idx == 0 else None
                    media_items.append(InputMediaPhoto(media=fh, caption=cap))
                if not media_items:
                    continue
                if len(media_items) == 1:
                    # Album of 1 — fall back to send_photo for compatibility
                    item = media_items[0]
                    await self.bot.send_photo(
                        chat_id=chat_id_int,
                        photo=item.media,
                        caption=item.caption,
                    )
                else:
                    await self.bot.send_media_group(chat_id=chat_id_int, media=media_items)
                logger.info(
                    "bridge.media_group_sent",
                    chat_id=chat_id,
                    mid=mid,
                    count=len(media_items),
                    chunk=chunk_idx,
                )
            except Exception as _mg_err:
                logger.error(
                    "bridge.media_group_failed",
                    chat_id=chat_id,
                    error=str(_mg_err)[:200],
                    fallback="single_photo",
                )
                # Fallback: send each photo individually so user still sees them
                for path, caption in chunk:
                    if not os.path.exists(path):
                        continue
                    try:
                        with open(path, "rb") as f:
                            await self.bot.send_photo(
                                chat_id=chat_id_int,
                                photo=f,
                                caption=(caption or "")[:1024] if caption else None,
                            )
                    except Exception as _single_err:
                        logger.error("bridge.media_group_single_fallback_failed",
                                     path=path, error=str(_single_err)[:120])
            finally:
                for fh in file_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass

    async def publish_incoming(
        self, event_type: str, user_id: int, chat_id: int, text: str
    ):
        """Publish a Telegram event to the incoming stream for agent-core."""
        correlation_id = str(uuid4())

        # Start typing indicator while agent-core processes
        self._start_typing(str(chat_id))
        # Phase 5: register timeout guard — ensures stop_typing + fallback if no response
        self._register_pending(correlation_id, str(chat_id))

        data = {
            "event_type": event_type,
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": str(user_id),
            "chat_id": str(chat_id),
            "text": text,
            "metadata": json.dumps({}),
        }
        await self.redis_client.xadd(settings.stream_incoming, data)
        logger.info(
            "bridge.published",
            event_type=event_type,
            chat_id=chat_id,
            correlation_id=correlation_id,
        )

    async def publish_incoming_with_media(
        self,
        event_type: str,
        user_id: int,
        chat_id: int,
        text: str,
        image_path: str | None = None,
        audio_path: str | None = None,
        video_path: str | None = None,
    ):
        """Publish a Telegram event with media paths to the incoming stream."""
        correlation_id = str(uuid4())
        self._start_typing(str(chat_id))
        # Phase 5: register timeout guard for media requests too
        self._register_pending(correlation_id, str(chat_id))

        metadata: dict = {}
        if image_path:
            metadata["image_path"] = image_path
        if audio_path:
            metadata["audio_path"] = audio_path
        if video_path:
            metadata["video_path"] = video_path

        data = {
            "event_type": event_type,
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": str(user_id),
            "chat_id": str(chat_id),
            "text": text,
            "metadata": json.dumps(metadata),
        }
        await self.redis_client.xadd(settings.stream_incoming, data)
        logger.info(
            "bridge.published_with_media",
            event_type=event_type,
            chat_id=chat_id,
            correlation_id=correlation_id,
            has_image=bool(image_path),
            has_audio=bool(audio_path),
            has_video=bool(video_path),
        )

    async def start_outgoing_listener(self):
        """Listen for outgoing events from agent-core and send via Telegram."""
        self._running = True
        logger.info("bridge.outgoing_listener.started")

        while self._running:
            try:
                try:
                    results = await self.redis_client.xreadgroup(
                        groupname=settings.consumer_group,
                        consumername=settings.consumer_name,
                        streams={settings.stream_outgoing: ">"},
                        count=5,
                        block=3000,
                    )
                except redis.ResponseError as e:
                    # NOGROUP: stream/group destroyed at runtime (factory reset,
                    # FLUSHDB, etc.). Recreate from scratch with mkstream and
                    # resume from latest. Messages produced while we were dead
                    # are unrecoverable.
                    if "NOGROUP" in str(e):
                        logger.warning(
                            "bridge.nogroup_recovering",
                            stream=settings.stream_outgoing,
                            group=settings.consumer_group,
                        )
                        try:
                            await self.redis_client.xgroup_create(
                                settings.stream_outgoing,
                                settings.consumer_group,
                                id="$",
                                mkstream=True,
                            )
                            logger.info(
                                "bridge.group_recreated_after_nogroup",
                                stream=settings.stream_outgoing,
                                group=settings.consumer_group,
                            )
                        except redis.ResponseError as ce:
                            if "BUSYGROUP" not in str(ce):
                                raise
                        # Reset on next iteration — don't try to read with stale state
                        await asyncio.sleep(0.5)
                        continue
                    raise
                if not results:
                    continue

                for _stream, messages in results:
                    for msg_id, data in messages:
                        try:
                            await self._send_telegram_response(data)
                        except Exception:
                            logger.exception("bridge.send_error", msg_id=msg_id)
                        finally:
                            try:
                                await self.redis_client.xack(
                                    settings.stream_outgoing,
                                    settings.consumer_group,
                                    msg_id,
                                )
                            except redis.ResponseError as ack_e:
                                # NOGROUP between read and ack — message is lost.
                                # Log once at WARN, never crash the loop.
                                if "NOGROUP" in str(ack_e):
                                    logger.warning(
                                        "bridge.ack_nogroup_skipped",
                                        msg_id=msg_id,
                                    )
                                else:
                                    raise
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("bridge.listener_error")
                await asyncio.sleep(2)

        logger.info("bridge.outgoing_listener.stopped")

    async def _handle_progress(self, data: dict):
        """Progress events are suppressed in Telegram — the typing indicator is enough.

        Live thinking details are shown in the dashboard chat stream instead.
        """
        return

    async def _send_telegram_response(self, data: dict):
        event_type = data.get("event_type", "")
        chat_id = data.get("chat_id", "")
        text = data.get("text", "")
        photo_path = data.get("photo_path", "")
        correlation_id = data.get("correlation_id", "")

        if not chat_id:
            logger.warning("bridge.invalid_response", data=data)
            return

        # Route progress events to the live-status handler
        if event_type == "telegram.progress":
            await self._handle_progress(data)
            return

        # Duplicate delivery guard — in-memory fast path + Redis persistence (restart-safe)
        if correlation_id and await self._is_delivered(correlation_id):
            logger.warning("bridge.duplicate_delivery_skipped", correlation_id=correlation_id)
            return

        # Mark delivered BEFORE sending — prevents double-send if send raises and is retried.
        # The timeout guard is cancelled here too so typing stops even if send fails.
        if correlation_id:
            await self._mark_delivered(correlation_id)
        self._complete_pending(correlation_id)

        # Phase 2: _stop_typing in finally — ALWAYS runs even if send_message throws
        try:
            # Stop typing and delete any progress message
            self._stop_typing(chat_id)

            prog_id = self._progress_msg_ids.pop(chat_id, None)
            if prog_id:
                try:
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=prog_id)
                except Exception:
                    pass  # already gone

            if photo_path:
                # Media-group support: when handlers.py emits an album, each photo
                # event carries media_group_id, media_group_index, media_group_total.
                # Buffer until all photos arrive then call sendMediaGroup once.
                _mid = data.get("media_group_id", "")
                _midx = data.get("media_group_index", "")
                _mtotal = data.get("media_group_total", "")
                try:
                    _midx_i = int(_midx) if _midx != "" else -1
                    _mtotal_i = int(_mtotal) if _mtotal != "" else 0
                except (TypeError, ValueError):
                    _midx_i, _mtotal_i = -1, 0

                if _mid and _mtotal_i >= 2:
                    async with self._media_lock:
                        buf = self._media_buffers.get(_mid)
                        if buf is None:
                            buf = {
                                "chat_id": chat_id,
                                "photos": [None] * _mtotal_i,
                                "total": _mtotal_i,
                                "received": 0,
                            }
                            self._media_buffers[_mid] = buf
                        if 0 <= _midx_i < buf["total"]:
                            if buf["photos"][_midx_i] is None:
                                buf["photos"][_midx_i] = (photo_path, text or "")
                                buf["received"] += 1
                        ready = buf["received"] >= buf["total"]

                    if ready:
                        await self._flush_media_group(_mid)
                else:
                    try:
                        with open(photo_path, "rb") as f:
                            await self.bot.send_photo(
                                chat_id=int(chat_id),
                                photo=f,
                                caption=text[:1024] if text else None,
                            )
                        logger.info("bridge.photo_sent", chat_id=chat_id, path=photo_path)
                    except FileNotFoundError:
                        logger.error("bridge.photo_not_found", path=photo_path)
                        if text:
                            await self._send_with_retry(chat_id, text, correlation_id)
            elif text:
                await self._send_with_retry(chat_id, text, correlation_id)
            else:
                logger.warning("bridge.empty_response", data=data)
                return

        except Exception as _send_exc:
            # Phase 2: stop_typing guaranteed even on send failure
            self._stop_typing(chat_id)
            logger.error(
                "bridge.send_failed",
                chat_id=chat_id,
                correlation_id=correlation_id,
                error=str(_send_exc)[:120],
            )
