"""Subscribe skill — subscribe to RSS feeds and price alerts.

Creates persistent subscriptions that check feeds periodically and notify
the user via Telegram when new items appear or price thresholds are crossed.

Subscriptions are stored in Redis HASH 'subscriptions'.
The SubscriptionCheckerJob runs every 5 minutes and processes due subscriptions.

Actions:
  subscribe(action="rss", url="https://...", name="My Feed", interval="30m")
  subscribe(action="price", symbol="BTC", above="50000", below="40000", name="BTC alert")
  subscribe(action="list")
  subscribe(action="delete", name="My Feed")
  subscribe(action="pause", name="My Feed")
  subscribe(action="resume", name="My Feed")
"""

import json
import time
import hashlib
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

SUBSCRIPTIONS_KEY = "subscriptions"


def _parse_interval(interval: str) -> int:
    """Parse interval string to seconds. Default: 1800 (30 minutes)."""
    interval = interval.strip().lower()
    if interval.endswith("m"):
        return int(interval[:-1]) * 60
    if interval.endswith("h"):
        return int(interval[:-1]) * 3600
    if interval.endswith("d"):
        return int(interval[:-1]) * 86400
    if interval.endswith("s"):
        return int(interval[:-1])
    try:
        return int(interval) * 60
    except ValueError:
        return 1800  # default 30 minutes


class SubscribeSkill(SkillBase):
    def __init__(self, redis_url: str = "redis://agent-redis:6379/0", default_chat_id: str = ""):
        self.redis_url = redis_url
        self.default_chat_id = default_chat_id

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="subscribe",
            description=(
                "Subscribe to RSS feeds or price alerts with automatic Telegram notifications. "
                "Actions: "
                "rss(url, name, interval='30m') — monitor an RSS feed for new items; "
                "price(symbol, name, above='', below='', interval='5m') — alert when price crosses threshold; "
                "list — show all subscriptions; "
                "delete(name) — remove subscription; "
                "pause(name) — pause without deleting; "
                "resume(name) — resume paused subscription. "
                "interval: '5m', '30m', '2h', '1d'"
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="Action: rss, price, list, delete, pause, resume",
                ),
                SkillParam(
                    name="url",
                    param_type=ParamType.STRING,
                    description="RSS feed URL",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="name",
                    param_type=ParamType.STRING,
                    description="Subscription name (unique identifier)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="symbol",
                    param_type=ParamType.STRING,
                    description="Crypto/stock symbol for price alerts (e.g. BTC, ETH, AAPL)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="above",
                    param_type=ParamType.STRING,
                    description="Alert when price goes above this value",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="below",
                    param_type=ParamType.STRING,
                    description="Alert when price goes below this value",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="interval",
                    param_type=ParamType.STRING,
                    description="Check interval: '5m', '30m', '2h', '1d'. Default: 30m",
                    required=False,
                    default="30m",
                ),
            ],
            category="monitoring",
            timeout_seconds=30.0,
            capability_level="controlled",
        )

    async def execute(
        self,
        action: str = "list",
        url: str = "",
        name: str = "",
        symbol: str = "",
        above: str = "",
        below: str = "",
        interval: str = "30m",
        chat_id: str = "",
        **kwargs,
    ) -> SkillResult:
        action = action.lower().strip()
        effective_chat_id = chat_id or self.default_chat_id
        r = aioredis.from_url(self.redis_url, decode_responses=True)

        try:
            if action == "rss":
                return await self._subscribe_rss(r, url, name, interval, effective_chat_id)
            elif action == "price":
                return await self._subscribe_price(r, symbol, name, above, below, interval, effective_chat_id)
            elif action == "list":
                return await self._list(r, effective_chat_id)
            elif action == "delete":
                return await self._delete(r, name, effective_chat_id)
            elif action == "pause":
                return await self._set_status(r, name, "paused", effective_chat_id)
            elif action == "resume":
                return await self._set_status(r, name, "active", effective_chat_id)
            else:
                return SkillResult(
                    skill_name="subscribe", success=False, output="",
                    error=f"Unknown action: {action}. Use: rss, price, list, delete, pause, resume"
                )
        except Exception as e:
            logger.exception("subscribe.error", action=action)
            return SkillResult(skill_name="subscribe", success=False, output="", error=str(e))
        finally:
            await r.aclose()

    async def _subscribe_rss(self, r, url: str, name: str, interval: str, chat_id: str) -> SkillResult:
        if not url:
            return SkillResult(skill_name="subscribe", success=False, output="", error="Provide url for RSS subscription")
        if not name:
            name = url.split("//")[-1].split("/")[0]  # use domain as default name
        sub_id = f"rss:{name}"
        interval_s = _parse_interval(interval)
        sub = {
            "id": sub_id,
            "type": "rss",
            "name": name,
            "url": url,
            "interval_seconds": interval_s,
            "chat_id": chat_id,
            "status": "active",
            "last_check": 0,
            "seen_ids": [],  # list of already-seen item GUIDs
            "created_at": time.time(),
        }
        await r.hset(SUBSCRIPTIONS_KEY, sub_id, json.dumps(sub))
        return SkillResult(
            skill_name="subscribe", success=True,
            output=(
                f"RSS subscription created: '{name}'\n"
                f"Feed: {url}\n"
                f"Check interval: every {interval}\n"
                f"I'll notify you when new items appear."
            )
        )

    async def _subscribe_price(self, r, symbol: str, name: str, above: str, below: str, interval: str, chat_id: str) -> SkillResult:
        if not symbol:
            return SkillResult(skill_name="subscribe", success=False, output="", error="Provide symbol for price alert")
        if not above and not below:
            return SkillResult(skill_name="subscribe", success=False, output="", error="Provide at least one threshold: above or below")
        if not name:
            name = f"{symbol.upper()} alert"
        sub_id = f"price:{name}"
        interval_s = _parse_interval(interval)
        sub = {
            "id": sub_id,
            "type": "price",
            "name": name,
            "symbol": symbol.upper(),
            "above": float(above) if above else None,
            "below": float(below) if below else None,
            "interval_seconds": interval_s,
            "chat_id": chat_id,
            "status": "active",
            "last_check": 0,
            "last_alert": 0,
            "created_at": time.time(),
        }
        await r.hset(SUBSCRIPTIONS_KEY, sub_id, json.dumps(sub))
        thresholds = []
        if above:
            thresholds.append(f"above ${float(above):,.2f}")
        if below:
            thresholds.append(f"below ${float(below):,.2f}")
        return SkillResult(
            skill_name="subscribe", success=True,
            output=(
                f"Price alert created: '{name}'\n"
                f"Symbol: {symbol.upper()}\n"
                f"Alert when: {' or '.join(thresholds)}\n"
                f"Check interval: every {interval}\n"
                f"I'll notify you when the threshold is crossed."
            )
        )

    async def _list(self, r, chat_id: str) -> SkillResult:
        all_subs = await r.hgetall(SUBSCRIPTIONS_KEY)
        if not all_subs:
            return SkillResult(skill_name="subscribe", success=True, output="No active subscriptions.")
        lines = [f"Subscriptions ({len(all_subs)}):"]
        for sub_id, raw in all_subs.items():
            try:
                s = json.loads(raw)
                status = s.get("status", "active")
                sub_type = s.get("type", "?")
                name = s.get("name", sub_id)
                interval = s.get("interval_seconds", 1800) // 60
                if sub_type == "rss":
                    lines.append(f"  [{status}] RSS '{name}' — {s.get('url', '')} — every {interval}m")
                elif sub_type == "price":
                    above = s.get("above")
                    below = s.get("below")
                    thresholds = []
                    if above:
                        thresholds.append(f">{above}")
                    if below:
                        thresholds.append(f"<{below}")
                    lines.append(f"  [{status}] PRICE '{name}' — {s.get('symbol', '')} {'/'.join(thresholds)} — every {interval}m")
            except Exception:
                lines.append(f"  [?] {sub_id}")
        return SkillResult(skill_name="subscribe", success=True, output="\n".join(lines))

    async def _delete(self, r, name: str, chat_id: str) -> SkillResult:
        if not name:
            return SkillResult(skill_name="subscribe", success=False, output="", error="Provide subscription name to delete")
        # Try both prefixes
        deleted = 0
        for prefix in ("rss:", "price:", ""):
            key = f"{prefix}{name}" if prefix else name
            deleted += await r.hdel(SUBSCRIPTIONS_KEY, key)
        if deleted:
            return SkillResult(skill_name="subscribe", success=True, output=f"Subscription '{name}' deleted.")
        return SkillResult(skill_name="subscribe", success=False, output="", error=f"Subscription '{name}' not found")

    async def _set_status(self, r, name: str, status: str, chat_id: str) -> SkillResult:
        if not name:
            return SkillResult(skill_name="subscribe", success=False, output="", error="Provide subscription name")
        for prefix in ("rss:", "price:", ""):
            key = f"{prefix}{name}" if prefix else name
            raw = await r.hget(SUBSCRIPTIONS_KEY, key)
            if raw:
                s = json.loads(raw)
                s["status"] = status
                await r.hset(SUBSCRIPTIONS_KEY, key, json.dumps(s))
                return SkillResult(skill_name="subscribe", success=True, output=f"Subscription '{name}' {status}.")
        return SkillResult(skill_name="subscribe", success=False, output="", error=f"Subscription '{name}' not found")
