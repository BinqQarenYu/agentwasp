"""SubscriptionCheckerJob — processes RSS and price alert subscriptions.

Runs every 5 minutes, checks due subscriptions, sends Telegram notifications.
"""

import json
import time
import hashlib

import redis.asyncio as aioredis
import structlog

from ..events.bus import EventBus
from ..events.types import EventType
from ..utils.safe_notify import safe_notify

logger = structlog.get_logger()

SUBSCRIPTIONS_KEY = "subscriptions"
MIN_ALERT_INTERVAL = 3600  # don't repeat same price alert within 1 hour


class SubscriptionCheckerJob:
    """Checks RSS feeds and price thresholds, sends Telegram alerts."""

    def __init__(self, bus: EventBus, redis_url: str, chat_id: str):
        self.bus = bus
        self.redis_url = redis_url
        self.chat_id = chat_id

    async def __call__(self) -> str:
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        now = time.time()
        checked = 0
        notified = 0

        try:
            all_subs = await r.hgetall(SUBSCRIPTIONS_KEY)
            for sub_id, raw in all_subs.items():
                try:
                    sub = json.loads(raw)
                except Exception:
                    continue

                if sub.get("status") != "active":
                    continue

                interval = sub.get("interval_seconds", 1800)
                last_check = sub.get("last_check", 0)
                if now - last_check < interval:
                    continue

                checked += 1
                sub_type = sub.get("type", "")
                target_chat = sub.get("chat_id") or self.chat_id

                try:
                    if sub_type == "rss":
                        alert = await self._check_rss(sub)
                    elif sub_type == "price":
                        alert = await self._check_price(sub, now)
                    else:
                        alert = None
                except Exception as e:
                    logger.exception("subscription.check_error", sub_id=sub_id)
                    alert = None

                # Update last_check
                sub["last_check"] = now
                await r.hset(SUBSCRIPTIONS_KEY, sub_id, json.dumps(sub))

                if alert and target_chat:
                    await safe_notify(
                        self.bus,
                        str(target_chat),
                        alert,
                        source="subscription_checker",
                    )
                    notified += 1
                    logger.info("subscription.notified", sub_id=sub_id, type=sub_type)

        except Exception:
            logger.exception("subscription_checker.error")
        finally:
            await r.aclose()

        return f"Subscriptions: {checked} checked, {notified} notifications sent"

    async def _check_rss(self, sub: dict) -> str | None:
        """Check RSS feed for new items. Returns notification text or None."""
        import xml.etree.ElementTree as ET
        import httpx
        from ..utils.network_safety import validate_url_for_request

        url = sub.get("url", "")
        if not url:
            return None

        seen_ids: list = sub.get("seen_ids", [])

        reason = await validate_url_for_request(url)
        if reason is not None:
            logger.warning("rss.blocked", url=url, reason=reason)
            return None

        try:
            # follow_redirects=False — revalidate each Location target.
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                _current = url
                for _ in range(6):
                    resp = await client.get(_current, headers={"User-Agent": "Mozilla/5.0"})
                    if resp.is_redirect and resp.headers.get("location"):
                        _current = str(httpx.URL(_current).join(resp.headers["location"]))
                        r2 = await validate_url_for_request(_current)
                        if r2 is not None:
                            logger.warning("rss.blocked_redirect", url=_current, reason=r2)
                            return None
                        continue
                    break
                if resp.status_code != 200:
                    return None
                content = resp.text
        except Exception as e:
            logger.warning("rss.fetch_error", url=url, error=str(e))
            return None

        try:
            root = ET.fromstring(content)
        except Exception:
            return None

        # Handle both RSS 2.0 and Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        new_items = []
        new_seen = list(seen_ids)

        for item in items[:20]:  # check last 20 items
            # Get item ID (guid, link, or hash of title)
            guid = (
                _el_text(item, "guid") or
                _el_text(item, "link") or
                _el_text(item, "atom:link", ns) or
                hashlib.md5((_el_text(item, "title") or "").encode()).hexdigest()
            )
            if guid in seen_ids:
                continue

            title = _el_text(item, "title") or _el_text(item, "atom:title", ns) or "Sin título"
            link = _el_text(item, "link") or _el_text(item, "atom:link", ns) or ""

            new_items.append((title, link))
            if guid not in new_seen:
                new_seen.append(guid)

        if not new_items:
            return None

        # Update seen_ids (keep last 200)
        sub["seen_ids"] = new_seen[-200:]

        name = sub.get("name", "Feed")
        lines = [f"[{name}] {len(new_items)} nuevo(s):"]
        for title, link in new_items[:5]:
            lines.append(f"• {title}")
            if link:
                lines.append(f"  {link}")

        return "\n".join(lines)

    async def _check_price(self, sub: dict, now: float) -> str | None:
        """Check price threshold. Returns notification text or None."""
        import httpx

        symbol = sub.get("symbol", "")
        above = sub.get("above")
        below = sub.get("below")
        last_alert = sub.get("last_alert", 0)

        if not symbol or (above is None and below is None):
            return None

        # Rate limit: don't alert more than once per hour
        if now - last_alert < MIN_ALERT_INTERVAL:
            return None

        # Fetch price from Coinbase or Binance API
        price = None
        try:
            urls = [
                f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot",
                f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT",
            ]
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                for url in urls:
                    try:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            data = resp.json()
                            # Coinbase format
                            if "data" in data and "amount" in data["data"]:
                                price = float(data["data"]["amount"])
                                break
                            # Binance format
                            if "price" in data:
                                price = float(data["price"])
                                break
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("price_alert.fetch_error", symbol=symbol, error=str(e))
            return None

        if price is None:
            return None

        triggered = False
        direction = ""
        threshold = 0.0

        if above is not None and price > above:
            triggered = True
            direction = "superó"
            threshold = above

        if below is not None and price < below:
            triggered = True
            direction = "bajó de"
            threshold = below

        if not triggered:
            return None

        # Update last_alert timestamp
        sub["last_alert"] = now
        name = sub.get("name", f"{symbol} alert")

        return (
            f"[Alerta de precio] {name}\n"
            f"{symbol}: ${price:,.2f}\n"
            f"El precio {direction} ${threshold:,.2f}"
        )


def _el_text(el, tag: str, ns: dict | None = None) -> str:
    """Safely extract element text."""
    try:
        child = el.find(tag, ns or {})
        if child is not None and child.text:
            return child.text.strip()
    except Exception:
        pass
    return ""
