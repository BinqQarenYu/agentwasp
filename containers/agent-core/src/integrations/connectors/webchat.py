"""WebChat connector — bridges integration platform to WASP dashboard via Redis Streams."""

from __future__ import annotations

import json
import time
from typing import Any

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_DEFAULT_REDIS_URL = "redis://agent-redis:6379/0"
_OUTGOING_STREAM = "events:outgoing"
_INCOMING_STREAM = "events:incoming"


class WebChatConnector(BaseConnector):
    """Push notifications and read chat history via Redis Streams."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="webchat",
            version="1.0.0",
            name="WebChat",
            category="chat",
            description="Send notifications and read chat history through the WASP dashboard Redis Streams bus.",
            capabilities=["send_message", "read_messages", "broadcast"],
            risk_level=RiskLevel.MEDIUM,
            actions=[
                ActionSpec(
                    id="send_notification",
                    description="Push a system message to the outgoing events stream for dashboard display.",
                    params=[
                        ParamSpec(name="text", type="string", description="Message text to send."),
                        ParamSpec(name="chat_id", type="string", description="Target chat ID (overrides secret).", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_recent_chat",
                    description="Read the last N messages from the incoming events stream.",
                    params=[
                        ParamSpec(name="limit", type="integer", description="Number of recent messages to return.", required=False, default=10),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="broadcast_alert",
                    description="Send an alert message prefixed with [ALERT] to the outgoing stream.",
                    params=[
                        ParamSpec(name="text", type="string", description="Alert message text."),
                        ParamSpec(name="severity", type="string", description="Alert severity: info|warning|error.", required=False, default="info"),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
            ],
            required_secrets=[],
            config_schema={},
            rate_limits={
                "send_notification": RateLimit(requests_per_minute=30),
                "get_recent_chat":   RateLimit(requests_per_minute=60),
                "broadcast_alert":   RateLimit(requests_per_minute=10),
            },
        )

    async def health_check(self) -> bool:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(_DEFAULT_REDIS_URL, socket_connect_timeout=2)
            await r.ping()
            await r.aclose()
            return True
        except Exception:
            return False

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        import redis.asyncio as aioredis

        redis_url = secrets.get("redis_url") or _DEFAULT_REDIS_URL
        chat_id   = params.get("chat_id") or secrets.get("chat_id") or ""

        try:
            r = aioredis.from_url(redis_url, decode_responses=True)

            if action == "send_notification":
                text = params.get("text", "")
                if not text:
                    return self.err("text is required")
                event = {
                    "event_type": "telegram.message",
                    "text":       text,
                    "chat_id":    chat_id,
                    "source":     "webchat_connector",
                    "ts":         str(int(time.time())),
                }
                msg_id = await r.xadd(_OUTGOING_STREAM, event)
                await r.aclose()
                return self.ok({"stream_id": msg_id, "chat_id": chat_id})

            elif action == "get_recent_chat":
                limit = int(params.get("limit") or 10)
                entries = await r.xrevrange(_INCOMING_STREAM, count=limit)
                await r.aclose()
                messages = []
                for msg_id, fields in entries:
                    messages.append({"id": msg_id, **fields})
                return self.ok({"messages": messages, "count": len(messages)})

            elif action == "broadcast_alert":
                text     = params.get("text", "")
                severity = params.get("severity") or "info"
                if not text:
                    return self.err("text is required")
                alert_text = f"[ALERT:{severity.upper()}] {text}"
                event = {
                    "event_type": "telegram.message",
                    "text":       alert_text,
                    "chat_id":    chat_id,
                    "source":     "webchat_alert",
                    "ts":         str(int(time.time())),
                }
                msg_id = await r.xadd(_OUTGOING_STREAM, event)
                await r.aclose()
                return self.ok({"stream_id": msg_id, "severity": severity})

            else:
                await r.aclose()
                return self.err(f"Unknown action: {action}")

        except Exception as e:
            return self.err(str(e))
