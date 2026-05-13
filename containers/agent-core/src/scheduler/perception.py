"""Background Perception Job — continuous ambient awareness.

Monitors the world for changes relevant to the user's interests.
Runs every 15 minutes, but only sends a Telegram notification when something
genuinely notable is detected (LLM judges notability).

Sources monitored:
- Crypto prices for assets the user has mentioned
- RSS feeds for topics the user cares about
- Key metrics the user tracks

Rate limits: max 3 perception-triggered notifications per day.
Respects quiet hours same as proactive job.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger()

PERCEPTION_DAILY_KEY = "perception:daily_count"
PERCEPTION_LAST_KEY = "perception:last_observations"
MAX_PER_DAY = 3
MIN_PRICE_CHANGE_PCT = 4.0  # Only surface if price changed >4%
QUIET_START = 23
QUIET_END = 8


class BackgroundPerceptionJob:
    """
    Polls user-relevant signals from the world and surfaces notable changes.
    Uses Knowledge Graph to discover what the user cares about.
    """

    def __init__(self, model_manager, redis_url: str, bus, notify_chat_id: str,
                 quiet_start: int = QUIET_START, quiet_end: int = QUIET_END):
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end

    async def __call__(self) -> None:
        from ..policy import with_trace
        async with with_trace(
            self.redis_url, path="perception",
            chat_id=self.notify_chat_id, user_text="perception_sweep",
        ) as _trace:
            await self._run(_trace)

    async def _run(self, _trace=None) -> None:
        def _g(label: str):
            if _trace is not None:
                _trace.add_guard(label)

        # Skip if CPI is high
        from ..agent.cpi import is_high as _cpi_high
        if await _cpi_high(self.redis_url):
            logger.debug("perception.cpi_throttled")
            _g("perception:cpi_throttled")
            return

        now = datetime.now(timezone.utc)
        # Quiet hours check
        hour = now.hour
        if self.quiet_start > self.quiet_end:
            in_quiet = hour >= self.quiet_start or hour < self.quiet_end
        else:
            in_quiet = self.quiet_start <= hour < self.quiet_end
        if in_quiet:
            logger.debug("perception.quiet_hours")
            _g("perception:quiet_hours")
            return

        # Rate limit check
        if not await self._check_rate_limit():
            logger.debug("perception.rate_limited")
            _g("perception:rate_limited")
            return

        # Gather signals
        findings = []
        findings.extend(await self._check_crypto_prices())
        findings.extend(await self._check_user_interests())

        if not findings:
            logger.debug("perception.no_notable_changes")
            _g("perception:no_changes")
            return

        # LLM judges if findings are worth surfacing
        notable = await self._judge_notability(findings)
        if not notable:
            logger.debug("perception.not_notable")
            _g("perception:not_notable")
            return

        # Format and send
        message = self._format_message(notable)
        await self._send(message)
        await self._increment_rate_limit()
        logger.info("perception.notification_sent", findings=len(notable))
        _g(f"perception:notification[{len(notable)}]")

    async def _check_crypto_prices(self) -> list[dict]:
        """Check prices for crypto assets the user has mentioned."""
        findings = []
        # Get user's crypto interests from KG
        tracked = await self._get_tracked_assets()
        if not tracked:
            tracked = ["BTC", "ETH"]  # Always monitor these as baseline

        async with httpx.AsyncClient(timeout=10) as client:
            for symbol in tracked[:5]:  # Max 5 assets
                try:
                    r = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot")
                    if r.status_code == 200:
                        data = r.json()
                        price = float(data["data"]["amount"])
                        change = await self._get_price_change(symbol, price)
                        if change and abs(change) >= MIN_PRICE_CHANGE_PCT:
                            direction = "rose" if change > 0 else "fell"
                            findings.append({
                                "type": "price_change",
                                "entity": symbol,
                                "value": f"${price:,.2f}",
                                "change_pct": change,
                                "detail": f"{symbol} {direction} {abs(change):.1f}% → ${price:,.2f}",
                            })
                            # Store in temporal model
                            from ..memory.temporal import record_observation
                            await record_observation(
                                entity=symbol,
                                observation_type="price",
                                value=f"${price:,.2f}",
                                source="perception_job",
                                expires_hours=24,
                            )
                except Exception:
                    pass
        return findings

    async def _check_user_interests(self) -> list[dict]:
        """Check news for topics the user has mentioned using DuckDuckGo."""
        findings = []
        try:
            topics = await self._get_user_topics()
            if not topics:
                return findings

            import asyncio
            from ddgs import DDGS

            for topic in topics[:3]:
                try:
                    results = await asyncio.to_thread(
                        lambda t=topic: list(DDGS().news(t, max_results=3))
                    )
                    if results:
                        latest = results[0]
                        title = latest.get("title", "").strip()
                        if title:
                            findings.append({
                                "type": "news",
                                "entity": topic,
                                "value": title,
                                "detail": f"Noticia sobre '{topic}': {title[:120]}",
                                "url": latest.get("url", ""),
                            })
                except Exception:
                    pass
        except Exception:
            pass
        return findings

    async def _get_tracked_assets(self) -> list[str]:
        """Get crypto assets the user has mentioned from KG + temporal model."""
        assets = []
        try:
            from ..memory.knowledge_graph import search_nodes
            nodes = await search_nodes("crypto", limit=10)
            for node in nodes:
                name = node.get("name", "").upper()
                if name in ("BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "MATIC", "AVAX"):
                    assets.append(name)
        except Exception:
            pass
        return list(set(assets)) or ["BTC", "ETH"]

    async def _get_user_topics(self) -> list[str]:
        """Get topics the user cares about from KG."""
        topics = []
        try:
            from ..memory.knowledge_graph import search_nodes
            nodes = await search_nodes("interés", limit=10)
            for node in nodes:
                if node.get("entity_type") in ("preference", "interest"):
                    topics.append(node.get("name", ""))
        except Exception:
            pass
        return [t for t in topics if t][:5]

    async def _get_price_change(self, symbol: str, current_price: float) -> float | None:
        """Get % change vs last recorded price for this symbol."""
        try:
            from ..memory.temporal import get_entity_history
            history = await get_entity_history(symbol, days=1, limit=2)
            if len(history) >= 2:
                # Parse previous price
                prev_str = history[1]["value"].replace("$", "").replace(",", "")
                prev_price = float(prev_str)
                if prev_price > 0:
                    return ((current_price - prev_price) / prev_price) * 100
        except Exception:
            pass
        return None

    async def _judge_notability(self, findings: list[dict]) -> list[dict]:
        """Use LLM to filter findings to only genuinely notable ones."""
        if not findings or not self.model_manager:
            return findings

        try:
            summary = "\n".join(f"- {f['detail']}" for f in findings)
            from ..models.types import Message, ModelRequest
            request = ModelRequest(messages=[
                Message(role="system", content="You decide what's notable enough to interrupt someone with. Be selective."),
                Message(role="user", content=f"""These signals were detected in the world:
{summary}

Which of these are genuinely notable enough to proactively inform the user about?
Consider: significant price movements, breaking news about their interests, important changes.
Return ONLY a JSON array of the notable finding indices (0-based): [0, 2] or [] if none.
Respond with ONLY the JSON array."""),
            ])
            response = await self.model_manager.generate(request)
            text = response.content.strip()
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            indices = json.loads(text)
            return [findings[i] for i in indices if 0 <= i < len(findings)]
        except Exception:
            # If LLM fails, only surface very large price moves
            return [f for f in findings if f.get("type") == "price_change" and abs(f.get("change_pct", 0)) >= 8.0]

    def _format_message(self, findings: list[dict]) -> str:
        """Format perception findings as a concise Telegram message."""
        lines = ["[Background perception]"]
        for f in findings:
            lines.append(f"• {f['detail']}")
        return "\n".join(lines)

    async def _send(self, message: str) -> None:
        """Publish message to Telegram via event bus."""
        try:
            from ..utils.safe_notify import safe_notify
            await safe_notify(
                self.bus,
                str(self.notify_chat_id),
                message,
                source="perception",
            )
        except Exception as e:
            logger.warning("perception.send_failed", error=str(e))

    async def _check_rate_limit(self) -> bool:
        """Returns True if we're under the daily limit."""
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                key = f"{PERCEPTION_DAILY_KEY}:{today}"
                count = await r.get(key)
                return int(count or 0) < MAX_PER_DAY
            finally:
                await r.aclose()
        except Exception:
            return True

    async def _increment_rate_limit(self) -> None:
        """Increment daily counter."""
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                key = f"{PERCEPTION_DAILY_KEY}:{today}"
                await r.incr(key)
                await r.expire(key, 86400)
            finally:
                await r.aclose()
        except Exception:
            pass
