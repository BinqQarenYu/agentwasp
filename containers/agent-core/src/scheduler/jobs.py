"""Scheduled job implementations.

Each job is a callable class that returns a summary string.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

from ..config import now_local, get_tz
from ..db.session import async_session
from ..events.bus import EventBus
from ..events.types import EventType
from ..memory.manager import MemoryManager
from ..memory.types import MemoryQuery, MemoryType
from ..models.manager import ModelManager
from ..models.types import Message, ModelRequest

logger = structlog.get_logger()


class HealthCheckJob:
    """Checks all services via HealthMonitor, stores results, triggers self-healing.

    Only sends Telegram notification on state CHANGES.
    """

    def __init__(
        self, bus: EventBus, chat_id: str, health_monitor, self_healer=None,
    ):
        self.bus = bus
        self.chat_id = chat_id
        self.health_monitor = health_monitor
        self.self_healer = self_healer
        self._prev_state: dict[str, bool] = {}

    async def __call__(self) -> str:
        # Use centralized HealthMonitor for all checks
        health = await self.health_monitor.check_all()
        await self.health_monitor.store_results(health)

        # Flatten to simple healthy/unhealthy map
        results = {}
        for svc, info in health.get("services", {}).items():
            results[svc] = info.get("healthy", False)
        for res, info in health.get("system", {}).items():
            if isinstance(info, dict) and "healthy" in info:
                results[res] = info["healthy"]

        disk = health.get("system", {}).get("disk", {})
        ram = health.get("system", {}).get("ram", {})
        disk_info = f"{disk.get('percent', '?')}% ({disk.get('used_gb', '?')}/{disk.get('total_gb', '?')}GB)"
        ram_info = f"{ram.get('percent', '?')}% ({ram.get('used_mb', '?')}/{ram.get('total_mb', '?')}MB)"

        # Detect state changes
        alerts = []
        recoveries = []
        for service, healthy in results.items():
            prev = self._prev_state.get(service)
            if prev is not None and prev != healthy:
                if not healthy:
                    alerts.append(service)
                else:
                    recoveries.append(service)
            elif prev is None and not healthy:
                alerts.append(service)

        self._prev_state = results

        # Self-heal on failures
        if self.self_healer and alerts:
            for svc in alerts:
                try:
                    await self.self_healer.handle_failure(svc, f"{svc} check failed")
                except Exception:
                    logger.exception("health.self_heal_failed", service=svc)

        # Notify on state changes only
        if (alerts or recoveries) and self.chat_id:
            lines = ["Health Alert\n"]
            if alerts:
                lines.append("DEGRADED:")
                for svc in alerts:
                    lines.append(f"  - {svc}")
            if recoveries:
                lines.append("RECOVERED:")
                for svc in recoveries:
                    lines.append(f"  - {svc}")
            lines.append(f"\nDisk: {disk_info}")
            lines.append(f"RAM: {ram_info}")

            try:
                await self.bus.publish("events:outgoing", {
                    "event_type": EventType.TELEGRAM_RESPONSE,
                    "correlation_id": str(uuid4()),
                    "chat_id": self.chat_id,
                    "text": "\n".join(lines),
                })
            except Exception:
                logger.exception("health.notification_failed")

        status_parts = []
        for svc, healthy in results.items():
            mark = "OK" if healthy else "FAIL"
            status_parts.append(f"{svc}={mark}")

        return f"Health: {', '.join(status_parts)} | Disk: {disk_info} | RAM: {ram_info}"


class ReflectionJob:
    """Reviews recent episodic memories and extracts learnings via LLM."""

    def __init__(self, memory: MemoryManager, model_manager: ModelManager):
        self.memory = memory
        self.model_manager = model_manager

    async def __call__(self) -> str:
        if not self.model_manager.active_model:
            return "Skipped: no active model"

        async with async_session() as session:
            recent = await self.memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.EPISODIC, limit=20),
            )

        if len(recent) < 3:
            return f"Skipped: only {len(recent)} episodic memories (need 3+)"

        summaries = []
        for entry in recent:
            user_input = entry.content.get("user_input", "")[:150]
            agent_response = entry.content.get("agent_response", "")[:150]
            ts = entry.created_at[:19]
            summaries.append(f"[{ts}] User: {user_input}\nAgent: {agent_response}")

        conversations_text = "\n---\n".join(summaries)

        messages = [
            Message(
                role="system",
                content=(
                    "You are an introspective AI agent analyzing your recent conversations. "
                    "Extract 1-3 key learnings, patterns, or insights. Be concise. "
                    "Focus on: user preferences, recurring topics, mistakes to avoid, "
                    "knowledge gaps. Output as a bulleted list."
                ),
            ),
            Message(
                role="user",
                content=f"Recent conversations:\n\n{conversations_text}\n\n"
                        "What are the key learnings and patterns?",
            ),
        ]

        try:
            request = ModelRequest(messages=messages, max_tokens=512, temperature=0.3)
            response = await self.model_manager.generate(request)
            reflection_text = response.content

            async with async_session() as session:
                await self.memory.store_memory(
                    session,
                    memory_type=MemoryType.SEMANTIC,
                    content={
                        "type": "reflection",
                        "source": "scheduled_reflection",
                        "learnings": reflection_text,
                        "episodes_reviewed": len(recent),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    summary=f"Reflection: {reflection_text[:100]}",
                    tags=["reflection", "automated"],
                )

            return f"Reflected on {len(recent)} episodes. Stored semantic memory."
        except Exception as e:
            return f"Reflection failed: {e}"


class MemoryCleanupJob:
    """Memory lifecycle management: TTL expiry, importance decay, volume caps.

    Uses ForgettingEngine for structured, importance-aware cleanup.
    """

    def __init__(self, memory: MemoryManager):
        self.memory = memory

    async def __call__(self) -> str:
        async with async_session() as session:
            summary = await self.memory.forgetting.run_full_cycle(session)

        return (
            f"Cleanup: ttl_expired={summary['ttl_expired']} "
            f"working_cleaned={summary['working_cleaned']} "
            f"episodic_trimmed={summary['episodic_trimmed']} "
            f"total={summary['total']}"
        )


class PromotionJob:
    """Promotes recurring episodic topics to semantic memory.

    Runs less frequently than cleanup; meaningful after substantial
    episodic accumulation.
    """

    def __init__(self, memory: MemoryManager):
        self.memory = memory

    async def __call__(self) -> str:
        async with async_session() as session:
            promoted = await self.memory.promotion.run_promotion_cycle(session)

        if promoted:
            return f"Promotion: elevated {promoted} new topic(s) to semantic memory."
        return "Promotion: no new topics met the promotion threshold."


class SnapshotJob:
    """Creates an automatic daily memory snapshot."""

    def __init__(self, memory: MemoryManager):
        self.memory = memory

    async def __call__(self) -> str:
        try:
            info = self.memory.create_snapshot(
                label=f"auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}",
                trigger="scheduled",
            )
            return f"Snapshot: {info.label} ({info.entry_count} entries, {info.size_bytes} bytes)"
        except Exception as e:
            return f"Snapshot failed: {e}"


# First-person -> second-person Spanish replacements (order matters: longer first)
_PERSON_SWAPS = [
    (r"\btengo que\b", "tienes que"),
    (r"\btengo\b", "tienes"),
    (r"\bdebo\b", "debes"),
    (r"\bvoy a\b", "vas a"),
    (r"\bnecesito\b", "necesitas"),
    (r"\bquiero\b", "quieres"),
    (r"\bpuedo\b", "puedes"),
    (r"\bestoy\b", "estás"),
    (r"\bme toca\b", "te toca"),
    (r"\bme falta\b", "te falta"),
    (r"\bsalgo\b", "sales"),
    (r"\bhago\b", "haces"),
]


def _to_second_person(text: str) -> str:
    """Convert first-person Spanish text to second-person for reminder notifications."""
    result = text
    for pattern, replacement in _PERSON_SWAPS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


class ReminderCheckerJob:
    """Checks due reminders every 30s and sends Telegram notifications.

    When a reminder has an ``agent_id`` field, the reminder was created by an
    autonomous agent.  In that case we also create a new goal for the agent so
    it can repeat the recurring task automatically — no manual intervention needed.
    """

    def __init__(self, bus: EventBus, chat_id: str, memory: MemoryManager,
                 agent_orchestrator=None):
        self.bus = bus
        self.chat_id = chat_id
        self.memory = memory
        self.agent_orchestrator = agent_orchestrator  # Optional AgentOrchestrator

    async def __call__(self) -> str:
        async with async_session() as session:
            entries = await self.memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.WORKING, tags=["reminder", "active"], limit=50),
            )

        if not entries:
            return "No active reminders"

        local_now = now_local()
        sent = 0

        for entry in entries:
            due_str = entry.content.get("due", "")
            if not due_str:
                continue

            try:
                due_dt = datetime.fromisoformat(due_str)
                if due_dt.tzinfo is None:
                    from ..config import get_tz
                    due_dt = due_dt.replace(tzinfo=get_tz())
            except (ValueError, TypeError):
                continue

            if local_now >= due_dt:
                target_chat = entry.content.get("chat_id", "") or self.chat_id
                reminder_text = entry.content.get("reminder_text", "Reminder")
                agent_id = entry.content.get("agent_id", "")
                agent_objective = entry.content.get("agent_objective", "")

                # ── Agent-linked reminder: restart the agent goal ──────────
                if agent_id and self.agent_orchestrator:
                    objective = agent_objective or reminder_text
                    try:
                        goal = await self.agent_orchestrator.create_agent_goal(
                            agent_id=agent_id,
                            objective=objective,
                            chat_id=target_chat,
                        )
                        logger.info(
                            "reminder.agent_goal_restarted",
                            agent_id=agent_id[:8],
                            goal_id=goal.id[:8],
                            objective=objective[:80],
                        )
                        # Notify user that the recurring task is running
                        if target_chat:
                            await self.bus.publish("events:outgoing", {
                                "event_type": EventType.TELEGRAM_RESPONSE,
                                "correlation_id": str(uuid4()),
                                "chat_id": target_chat,
                                "text": f"🤖 Starting scheduled task: _{objective[:120]}_",
                            })
                    except Exception:
                        logger.exception("reminder.agent_restart_failed", agent_id=agent_id[:8])
                        # Keep reminder alive so it retries on next tick — do NOT delete
                        if target_chat:
                            friendly_text = _to_second_person(reminder_text)
                            await self.bus.publish("events:outgoing", {
                                "event_type": EventType.TELEGRAM_RESPONSE,
                                "correlation_id": str(uuid4()),
                                "chat_id": target_chat,
                                "text": f"⏰ {friendly_text}",
                            })
                        continue  # Skip deletion — preserve reminder for retry
                elif target_chat:
                    # ── Regular reminder: just send notification ───────────
                    friendly_text = _to_second_person(reminder_text)
                    # Build a natural notification message
                    rt = friendly_text.strip()
                    if len(rt) <= 5 or rt.lower() in {"más", "mas", "eso", "esto", "ya", "ok", "si", "sí"}:
                        # Vague/empty text — generic time notification
                        notification = f"⏰ El tiempo que pediste ya se cumplió."
                    elif rt.endswith("?") or rt.endswith("!") or len(rt) > 40:
                        notification = f"⏰ {rt}"
                    else:
                        notification = f"⏰ Recordatorio: {rt}"
                    try:
                        await self.bus.publish("events:outgoing", {
                            "event_type": EventType.TELEGRAM_RESPONSE,
                            "correlation_id": str(uuid4()),
                            "chat_id": target_chat,
                            "text": notification,
                        })
                        logger.info("reminder.sent", text=reminder_text[:50])
                    except Exception:
                        logger.exception("reminder.send_failed")
                        continue

                # Remove from active reminders
                async with async_session() as session:
                    await self.memory.delete(session, MemoryType.WORKING, entry.id)
                sent += 1

        return f"Checked {len(entries)} reminders, sent {sent}"


def _extract_price_from_page(text: str) -> tuple[float | None, str]:
    """Try to extract a price and symbol from page text/title.

    Returns (price_float, symbol_str) or (None, "").
    Handles Binance page titles like '1,922.69 | ETH USDT | Ethereum...'
    """
    # Binance page title pattern: "1,922.69 | ETH USDT | ..."
    m = re.search(r"([\d,]+\.?\d*)\s*\|\s*([A-Z]{2,6})\s+(?:USDT?|USD)", text)
    if m:
        try:
            price = float(m.group(1).replace(",", ""))
            symbol = m.group(2)
            return price, symbol
        except ValueError:
            pass

    # Generic price pattern: "$1,922.69" or "1922.69 USD"
    m = re.search(r"\$?([\d,]+\.?\d{2})\s*(?:USD|USDT)?", text)
    if m:
        try:
            price = float(m.group(1).replace(",", ""))
            return price, ""
        except ValueError:
            pass

    return None, ""


def _format_change_notification(label: str, url: str, old_text: str, new_text: str) -> str:
    """Format a change notification. For price/crypto APIs returns a clean price message."""
    import json as _json

    # --- JSON API responses (Binance ticker, Coinbase spot) ---
    try:
        new_data = _json.loads(new_text.strip())
        old_data = _json.loads(old_text.strip()) if old_text.strip() else {}

        # Binance ticker: {"symbol":"ETHUSDT","price":"1934.65000000"}
        if isinstance(new_data, dict) and "price" in new_data and "symbol" in new_data:
            symbol = new_data["symbol"]
            new_price = float(new_data["price"])
            old_price = float(old_data.get("price", new_price))
            diff = new_price - old_price
            arrow = "▲" if diff > 0 else "▼" if diff < 0 else "="
            base = symbol.replace("USDT", "").replace("USD", "")
            return f"{base}: ${new_price:,.2f} USDT {arrow} ({diff:+.2f})"

        # Coinbase spot: {"data":{"base":"ETH","currency":"USD","amount":"1934.65"}}
        if isinstance(new_data, dict) and "data" in new_data:
            d = new_data["data"]
            if "amount" in d and "base" in d:
                new_price = float(d["amount"])
                old_price = float((old_data.get("data") or {}).get("amount", new_price))
                diff = new_price - old_price
                arrow = "▲" if diff > 0 else "▼" if diff < 0 else "="
                return f"{d['base']}: ${new_price:,.2f} {d.get('currency','USD')} {arrow} ({diff:+.2f})"
    except Exception:
        pass

    # --- HTML page — try to extract price from title/text ---
    new_price, new_sym = _extract_price_from_page(new_text[:500])
    old_price_val, _ = _extract_price_from_page(old_text[:500])

    if new_price is not None:
        symbol = new_sym or label
        if old_price_val is not None and old_price_val != new_price:
            diff = new_price - old_price_val
            arrow = "▲" if diff > 0 else "▼" if diff < 0 else "="
            return f"{symbol}: ${new_price:,.2f} {arrow} ({diff:+.2f})\n{url}"
        return f"{symbol}: ${new_price:,.2f}\n{url}"

    # --- Default: show compact diff (not full page dump) ---
    # Extract only first meaningful line from each snippet
    def _first_line(text: str) -> str:
        for line in text.split("\n"):
            line = line.strip()
            if len(line) > 10:
                return line[:200]
        return text[:200]

    return (
        f"Cambio en {label}\n"
        f"Antes: {_first_line(old_text)}\n"
        f"Ahora: {_first_line(new_text)}\n"
        f"{url}"
    )


class MonitorCheckerJob:
    """Checks web monitors periodically and sends notifications on detected changes."""

    def __init__(self, bus: EventBus, chat_id: str, memory: MemoryManager):
        self.bus = bus
        self.chat_id = chat_id
        self.memory = memory

    async def __call__(self) -> str:
        async with async_session() as session:
            entries = await self.memory.retrieve(
                session,
                MemoryQuery(memory_type=MemoryType.WORKING, tags=["monitor", "active"], limit=50),
            )

        if not entries:
            return "No active monitors"

        local_now = now_local()
        checked = 0
        notified = 0

        for entry in entries:
            # Rate limiting: skip if interval hasn't elapsed
            interval_minutes = entry.content.get("interval_minutes", 60)
            last_checked = entry.content.get("last_checked_at", "")
            if last_checked:
                try:
                    last_dt = datetime.fromisoformat(last_checked)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=get_tz())
                    elapsed_minutes = (local_now - last_dt).total_seconds() / 60
                    if elapsed_minutes < interval_minutes:
                        continue
                except (ValueError, TypeError):
                    pass

            url = entry.content.get("url", "")
            if not url:
                continue

            selector = entry.content.get("selector", "")
            use_browser = entry.content.get("use_browser", False)

            # Fetch content
            from ..skills.builtin.monitors import fetch_page_content
            text, error = await fetch_page_content(url, selector, use_browser)

            if error:
                consec = entry.content.get("consecutive_errors", 0) + 1
                entry.content["consecutive_errors"] = consec
                entry.content["last_error"] = error
                entry.content["last_checked_at"] = local_now.isoformat()
                entry.content["check_count"] = entry.content.get("check_count", 0) + 1

                new_tags = ["monitor", "active"]
                if consec >= 5:
                    entry.content["status"] = "error"
                    new_tags = ["monitor", "error"]

                async with async_session() as session:
                    await self.memory.delete(session, MemoryType.WORKING, entry.id)
                    await self.memory.store_memory(
                        session, MemoryType.WORKING,
                        content=entry.content, summary=entry.summary, tags=new_tags,
                    )

                if consec >= 5:
                    target_chat = entry.content.get("chat_id", "") or self.chat_id
                    if target_chat:
                        label = entry.content.get("label", url)
                        await self.bus.publish("events:outgoing", {
                            "event_type": EventType.TELEGRAM_RESPONSE,
                            "correlation_id": str(uuid4()),
                            "chat_id": target_chat,
                            "text": f"Monitor pausado (5 errores consecutivos): {label}\nError: {error}",
                        })
                continue

            new_hash = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
            old_hash = entry.content.get("last_content_hash", "")
            monitor_type = entry.content.get("monitor_type", "change")
            should_notify = False
            notification_text = ""

            if monitor_type == "change":
                if old_hash and new_hash != old_hash:
                    should_notify = True
                    label = entry.content.get("label", url)
                    old_snippet = entry.content.get("last_content_snippet", "")
                    # If content looks like a price JSON (Binance/Coinbase style), format cleanly
                    notification_text = _format_change_notification(label, url, old_snippet, text)

            elif monitor_type == "keyword":
                keyword = entry.content.get("keyword", "").lower()
                if keyword and keyword in text.lower():
                    old_snippet = entry.content.get("last_content_snippet", "")
                    if keyword not in old_snippet.lower():
                        should_notify = True
                        idx = text.lower().find(keyword)
                        start = max(0, idx - 100)
                        end = min(len(text), idx + len(keyword) + 100)
                        context = text[start:end]
                        label = entry.content.get("label", url)
                        notification_text = (
                            f"Keyword \"{keyword}\" encontrado en {label}\n{url}\n\n"
                            f"...{context}..."
                        )

            elif monitor_type == "new_content":
                items = [line for line in text.split("\n") if line.strip() and len(line.strip()) > 20]
                new_count = len(items)
                old_count = entry.content.get("last_item_count", 0)
                if old_count > 0 and new_count > old_count:
                    diff = new_count - old_count
                    should_notify = True
                    label = entry.content.get("label", url)
                    notification_text = (
                        f"Nuevo contenido en {label}\n{url}\n"
                        f"~{diff} items nuevos (antes: {old_count}, ahora: {new_count})"
                    )
                entry.content["last_item_count"] = new_count

            # Update monitor state
            entry.content["last_checked_at"] = local_now.isoformat()
            entry.content["last_content_hash"] = new_hash
            entry.content["last_content_snippet"] = text[:500]
            entry.content["check_count"] = entry.content.get("check_count", 0) + 1
            entry.content["consecutive_errors"] = 0
            entry.content["last_error"] = ""
            if should_notify:
                entry.content["change_count"] = entry.content.get("change_count", 0) + 1

            async with async_session() as session:
                await self.memory.delete(session, MemoryType.WORKING, entry.id)
                await self.memory.store_memory(
                    session, MemoryType.WORKING,
                    content=entry.content, summary=entry.summary, tags=["monitor", "active"],
                )
            checked += 1

            if should_notify:
                target_chat = entry.content.get("chat_id", "") or self.chat_id
                if target_chat:
                    await self.bus.publish("events:outgoing", {
                        "event_type": EventType.TELEGRAM_RESPONSE,
                        "correlation_id": str(uuid4()),
                        "chat_id": target_chat,
                        "text": notification_text[:4000],
                    })
                    notified += 1
                    logger.info("monitor.notified", url=url, type=monitor_type)

        return f"Checked {checked}/{len(entries)} monitors, {notified} notifications"


class ProactiveJob:
    """Periodically uses LLM to decide whether to send a proactive message to the user."""

    REDIS_DAILY_KEY = "proactive:daily_count:{date}"
    REDIS_TOPICS_KEY = "proactive:recent_topics"

    _WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

    def __init__(
        self,
        bus: EventBus,
        chat_id: str,
        memory: MemoryManager,
        model_manager: ModelManager,
        redis_url: str,
        quiet_start: int = 23,
        quiet_end: int = 8,
        max_daily: int = 3,
    ):
        self.bus = bus
        self.chat_id = chat_id
        self.memory = memory
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.max_daily = max_daily

    async def __call__(self) -> str:
        if not self.chat_id:
            return "Skipped: no chat_id configured"

        if not self.model_manager.active_model:
            return "Skipped: no active model"

        local_now = now_local()
        current_hour = local_now.hour

        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            # Check for manual-trigger force flag (set by dashboard quick action)
            force_raw = await r.get("proactive:manual_force")
            if force_raw:
                await r.delete("proactive:manual_force")
            force = bool(force_raw)

            # Quiet hours check (bypassed when forced)
            if not force and self._is_quiet_hour(current_hour):
                return f"Skipped: quiet hours ({current_hour}:00)"

            # Daily limit check (bypassed when forced)
            date_key = self.REDIS_DAILY_KEY.format(date=local_now.strftime("%Y-%m-%d"))
            daily_count = int(await r.get(date_key) or 0)
            if not force and daily_count >= self.max_daily:
                return f"Skipped: daily limit ({daily_count}/{self.max_daily})"

            # Gather context
            context = await self._build_context(local_now)
            if not context:
                return "Skipped: insufficient context"

            # Recent topics to avoid repetition
            recent_topics_raw = await r.lrange(self.REDIS_TOPICS_KEY, 0, 4)
            recent_topics = ", ".join(recent_topics_raw) if recent_topics_raw else "ninguno"

            # Ask the LLM
            decision = await self._ask_llm(context, local_now, recent_topics)

            if not decision or not decision.get("send"):
                return "LLM decided: no message needed"

            message_text = decision.get("message", "").strip()
            topic = decision.get("topic", "general").strip()

            if not message_text or len(message_text) < 10:
                return "Skipped: LLM returned empty/short message"

            # Send the message
            await self.bus.publish("events:outgoing", {
                "event_type": EventType.TELEGRAM_RESPONSE,
                "correlation_id": str(uuid4()),
                "chat_id": self.chat_id,
                "text": message_text,
            })

            # Update daily counter
            await r.incr(date_key)
            await r.expire(date_key, 86400)

            # Track topic
            await r.lpush(self.REDIS_TOPICS_KEY, topic)
            await r.ltrim(self.REDIS_TOPICS_KEY, 0, 4)
            await r.expire(self.REDIS_TOPICS_KEY, 172800)

            # Store as episodic memory so agent remembers it sent this
            try:
                async with async_session() as session:
                    await self.memory.store_episodic(
                        session,
                        event_type="proactive.message",
                        user_input="",
                        agent_response=message_text,
                        chat_id=self.chat_id,
                        tags=["proactive"],
                    )
            except Exception:
                logger.warning("proactive.memory_store_failed")

            logger.info("proactive.sent", topic=topic, length=len(message_text))
            return f"Sent proactive message (topic={topic}, count={daily_count + 1}/{self.max_daily})"

        except Exception as e:
            logger.exception("proactive.error")
            return f"Error: {e}"
        finally:
            await r.aclose()

    def _is_quiet_hour(self, hour: int) -> bool:
        if self.quiet_start > self.quiet_end:
            return hour >= self.quiet_start or hour < self.quiet_end
        return self.quiet_start <= hour < self.quiet_end

    async def _build_context(self, local_now: datetime) -> dict | None:
        async with async_session() as session:
            episodic = await self.memory.retrieve(
                session, MemoryQuery(memory_type=MemoryType.EPISODIC, limit=10),
            )
            semantic = await self.memory.retrieve(
                session, MemoryQuery(memory_type=MemoryType.SEMANTIC, limit=5),
            )
            working = await self.memory.retrieve(
                session, MemoryQuery(memory_type=MemoryType.WORKING, limit=10),
            )
            facts = await self.memory.retrieve(
                session, MemoryQuery(memory_type=MemoryType.FACTS, limit=5),
            )

        if len(episodic) < 2:
            return None

        return {"episodic": episodic, "semantic": semantic, "working": working, "facts": facts}

    async def _ask_llm(self, context: dict, local_now: datetime, recent_topics: str) -> dict | None:
        # Build conversation summaries
        conv_lines = []
        for entry in context["episodic"]:
            user_input = entry.content.get("user_input", "")[:200]
            agent_response = entry.content.get("agent_response", "")[:200]
            ts = entry.created_at[:16]
            if user_input:
                conv_lines.append(f"[{ts}] User: {user_input}\nAgent: {agent_response}")
            else:
                conv_lines.append(f"[{ts}] (Proactive agent message): {agent_response}")
        conversations = "\n---\n".join(conv_lines) if conv_lines else "No recent conversations."

        # Semantic memories
        sem_lines = []
        for entry in context["semantic"]:
            sem_lines.append(entry.summary[:150] if entry.summary else str(entry.content)[:150])
        semantics = "\n".join(sem_lines) if sem_lines else "No semantic memories."

        # Working memory
        work_lines = []
        for entry in context["working"]:
            tags = ", ".join(entry.tags)
            summary = entry.summary[:100] if entry.summary else str(entry.content)[:100]
            work_lines.append(f"[{tags}] {summary}")
        working = "\n".join(work_lines) if work_lines else "No active tasks."

        # Facts
        fact_lines = []
        for entry in context["facts"]:
            fact_lines.append(entry.summary[:100] if entry.summary else str(entry.content)[:100])
        facts = "\n".join(fact_lines) if fact_lines else "No user data."

        weekday = self._WEEKDAYS_ES[local_now.weekday()]
        time_str = local_now.strftime("%H:%M")
        date_str = local_now.strftime("%d/%m/%Y")

        prompt = f"""You are Agent Wasp, an autonomous agent running on Telegram. Your user talks to you regularly.
You have the opportunity to send ONE proactive message — to start the conversation yourself.

CURRENT DATE AND TIME: {weekday} {date_str}, {time_str} (Chile)

RECENT CONVERSATIONS:
{conversations}

LEARNINGS AND REFLECTIONS:
{semantics}

ACTIVE TASKS/REMINDERS/MONITORS:
{working}

USER DATA:
{facts}

RECENT PROACTIVE MESSAGE TOPICS (avoid repeating):
{recent_topics}

INSTRUCTIONS:
Decide whether it makes sense to send a proactive message NOW. Consider:

1. Is there anything pending from a recent conversation worth following up on? ("How did X go?")
2. Does today's date have anything special? (holiday, event, notable day)
3. Is there an interesting fact related to the user's interests?
4. Could you share something useful based on what you know about the user?
5. Has a long time passed since the last conversation? (a casual greeting may be welcome)

CRITICAL RULES:
- DO NOT send if the last conversation was less than 2 hours ago.
- DO NOT repeat topics you've already sent recently.
- DO NOT be generic ("Hi, how are you?" is NOT enough). Be SPECIFIC and relevant.
- DO NOT be invasive or clingy. If there's no good reason, DO NOT send anything.
- The message must be SHORT (1-3 sentences maximum), natural, and in the user's language (default English, casual register).
- It must sound like a friend remembering something, NOT like an automated notification.
- NEVER mention that you are an AI, that this is automated, or that you "checked your memories".
- NEVER use emojis.

Respond EXACTLY in this JSON format (no markdown, no code blocks):
{{"send": true, "topic": "brief topic", "message": "the message for the user"}}

If you decide NOT to send:
{{"send": false, "topic": "", "message": ""}}"""

        messages = [
            Message(
                role="system",
                content="You are an assistant that decides whether to send proactive messages. Respond ONLY with valid JSON, no markdown.",
            ),
            Message(role="user", content=prompt),
        ]

        text = ""
        try:
            request = ModelRequest(messages=messages, max_tokens=256, temperature=0.7)
            response = await self.model_manager.generate(request)
            text = response.content.strip()

            # Handle markdown-wrapped JSON
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            text = text.strip()

            return json.loads(text)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("proactive.llm_parse_failed", error=str(e), raw=text[:200])
            return None


class CheckInJob:
    """Sends a brief hourly check-in message when the user has been inactive.

    Only sends during active hours (not quiet hours). Skips if the user has
    been active recently or if a check-in was sent recently.
    """

    REDIS_LAST_KEY = "checkin:last_sent"
    MIN_INTERVAL_MINUTES = 55  # Avoid re-sending within same hour

    def __init__(
        self,
        bus: EventBus,
        chat_id: str,
        memory: MemoryManager,
        redis_url: str,
        quiet_start: int = 23,
        quiet_end: int = 8,
        inactive_threshold_minutes: int = 55,
    ):
        self.bus = bus
        self.chat_id = chat_id
        self.memory = memory
        self.redis_url = redis_url
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.inactive_threshold_minutes = inactive_threshold_minutes

    async def __call__(self) -> str:
        if not self.chat_id:
            return "Skipped: no chat_id configured"

        local_now = now_local()
        current_hour = local_now.hour

        # Quiet hours check
        if self._is_quiet_hour(current_hour):
            return f"Skipped: quiet hours ({current_hour}:00)"

        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            # Check if we sent a check-in recently
            last_sent = await r.get(self.REDIS_LAST_KEY)
            if last_sent:
                try:
                    last_dt = datetime.fromisoformat(last_sent)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                    if elapsed < self.MIN_INTERVAL_MINUTES:
                        return f"Skipped: last check-in {elapsed:.0f}m ago"
                except Exception:
                    pass

            # Check episodic history.
            #  - Empty → fresh install, user has never spoken to the agent.
            #    Do NOT fire a check-in: it would be the very first message
            #    the user receives, which looks like spam and steals the
            #    /start welcome moment from them.
            #  - Recent → skip (already engaged).
            #  - Old → proceed.
            try:
                async with async_session() as session:
                    recent = await self.memory.retrieve(
                        session, MemoryQuery(memory_type=MemoryType.EPISODIC, limit=1),
                    )
                if not recent:
                    return "Skipped: no prior user interaction (fresh install)"
                last_conv = datetime.fromisoformat(recent[0].created_at)
                if last_conv.tzinfo is None:
                    last_conv = last_conv.replace(tzinfo=timezone.utc)
                minutes_since = (datetime.now(timezone.utc) - last_conv).total_seconds() / 60
                if minutes_since < self.inactive_threshold_minutes:
                    return f"Skipped: user was active {minutes_since:.0f}m ago"
            except Exception:
                # If we can't read episodic memory for any reason, default to
                # the safe choice: don't proactively message the user.
                return "Skipped: could not read episodic memory"

            # Send check-in
            await self.bus.publish("events:outgoing", {
                "event_type": EventType.TELEGRAM_RESPONSE,
                "correlation_id": str(uuid4()),
                "chat_id": self.chat_id,
                "text": "¿Necesitas ayuda con algo?",
            })

            # Track sent time (expires after 2 hours)
            await r.set(self.REDIS_LAST_KEY, datetime.now(timezone.utc).isoformat(), ex=7200)

            logger.info("checkin.sent")
            return "Sent check-in message"

        except Exception as e:
            logger.exception("checkin.error")
            return f"Error: {e}"
        finally:
            await r.aclose()

    def _is_quiet_hour(self, hour: int) -> bool:
        if self.quiet_start > self.quiet_end:
            return hour >= self.quiet_start or hour < self.quiet_end
        return self.quiet_start <= hour < self.quiet_end


class CustomTaskRunnerJob:
    """Checks all custom scheduled tasks and triggers due ones via the agent pipeline."""

    def __init__(self, bus: EventBus, default_chat_id: str, redis_url: str):
        self.bus = bus
        self.default_chat_id = default_chat_id
        self.redis_url = redis_url

    async def __call__(self) -> str:
        from .custom_tasks import list_tasks, save_task, next_run_from_now

        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            tasks = await list_tasks(r)
            if not tasks:
                return "No custom tasks configured"

            now = datetime.now(timezone.utc)
            triggered = 0
            skipped = 0

            for task in tasks:
                if not task.get("enabled", True):
                    skipped += 1
                    continue

                next_run_str = task.get("next_run")
                if not next_run_str:
                    skipped += 1
                    continue

                try:
                    next_run = datetime.fromisoformat(next_run_str)
                    # Ensure timezone-aware
                    if next_run.tzinfo is None:
                        next_run = next_run.replace(tzinfo=timezone.utc)
                except Exception:
                    skipped += 1
                    continue

                if next_run > now:
                    skipped += 1
                    continue

                chat_id = task.get("chat_id") or self.default_chat_id
                if not chat_id:
                    logger.warning("custom_task.no_chat_id", task=task.get("name"))
                    skipped += 1
                    continue

                # If this task is linked to a sub-agent, emit a wake-up signal instead of
                # injecting the full instruction. The agent reconstructs its behavior from
                # its stored intent — the task is only the trigger.
                # Fallback: if no agent_id, use the legacy full-instruction path.
                linked_agent_id = task.get("agent_id", "")
                if linked_agent_id:
                    trigger_text = (
                        f"[AGENT_WAKEUP: agent_id={linked_agent_id}]\n"
                        f"task={task['name']} triggered_at={now.isoformat()}"
                    )
                else:
                    # Phase 7: embed task_id alongside name so the handler can
                    # writeback an authoritative outcome (success/failure) by id,
                    # not by name lookup. Name extraction is fragile when names
                    # collide or change.
                    trigger_text = (
                        f"[TAREA PROGRAMADA: {task['name']} | id={task['task_id']}]\n"
                        f"EJECUTA AHORA step by step. Do not create new tasks or agents.\n"
                        f"INSTRUCCION:\n{task['instruction']}\n"
                        f"MANDATORY VALIDATION before responding:\n"
                        f"- If the task requires screenshots: at least 2 distinct /data/screenshots/*.png paths must exist in your context\n"
                        f"- If the task requires sending email: the gmail skill must have executed successfully BEFORE writing your final reply\n"
                        f"- If the task requires a Telegram summary: your final response must be ONLY that summary (5 lines max, no markdown)\n"
                        f"- BLOCKED SOURCES: If ALL captures have [CAPTURE_VALID: false], try alternative public sources (APIs, aggregators) before sending. NEVER send fabricated data or invalid captures as if they were valid.\n"
                        f"- DEGRADED REPORT: If you could not obtain all required data, mark the report as PARTIAL and state which sources failed.\n"
                        f"If any validation fails: execute the missing step before responding."
                    )

                # Publish as incoming Telegram message — full agent pipeline handles it
                await self.bus.publish("events:incoming", {
                    "event_type": "telegram.message",
                    "chat_id": str(chat_id),
                    "text": trigger_text,
                    "user_id": "scheduled_task",
                    "correlation_id": str(uuid4()),
                    "metadata": "{}",
                })

                # Phase 7: NEVER mark optimistic last_success=True. The scheduler
                # only knows it triggered; the handler writes back the real outcome
                # on completion (handlers.py:7431) or on timeout/exception
                # (handlers.py outer_timeout_fallback / outer_exception_fallback).
                # If the handler dies completely without a writeback, last_success
                # stays None — that itself is honest signal.
                task["last_run"] = now.isoformat()
                _at_time_after = task.get("at_time", "")
                if _at_time_after:
                    from .custom_tasks import compute_next_run_at_time as _crt
                    _next_iso = _crt(_at_time_after)
                    task["next_run"] = _next_iso or next_run_from_now(task["interval_seconds"])
                else:
                    task["next_run"] = next_run_from_now(task["interval_seconds"])
                task["run_count"] = task.get("run_count", 0) + 1
                task["last_result"] = "Running"
                task["last_success"] = None  # unknown until handler completes
                await save_task(r, task)

                triggered += 1
                logger.info("custom_task.triggered", name=task.get("name"), chat_id=chat_id)

            return f"Checked {len(tasks)} custom tasks: {triggered} triggered, {skipped} skipped"
        except Exception as e:
            logger.exception("custom_task_runner.error")
            return f"Error: {e}"
        finally:
            await r.aclose()


class ExecutionKnowledgeSyncJob:
    """Syncs in-memory execution knowledge (strategy scores, selectors, global stats)
    from Redis to PostgreSQL every 5 minutes.

    Redis is the runtime write target (sync, <1ms per write).
    This job provides durable backup — knowledge survives Redis flush or migration.
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url

    async def __call__(self) -> str:
        try:
            from ..intent.execution_persistence import sync_to_postgres
            summary = await sync_to_postgres()
            if summary.get("error"):
                return f"execution_knowledge_sync: error — {summary['error']}"
            return (
                f"execution_knowledge_sync: "
                f"scores={summary['scores']} selectors={summary['selectors']} "
                f"globals={summary['global_stats']} errors={summary['errors']}"
            )
        except Exception as exc:
            return f"execution_knowledge_sync: failed — {str(exc)[:80]}"
