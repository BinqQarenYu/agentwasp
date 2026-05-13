"""Telegram Bot API connector.

This is the ACTIVE Telegram integration for WASP — it uses the same
TELEGRAM_BOT_TOKEN that the agent-telegram polling container uses.
The token is auto-configured from env at startup; no manual setup needed.

Configuration (single source of truth — Integrations page):
    bot_token      — Telegram Bot token (from @BotFather); auto-populated from env
    allowed_users  — Comma-separated Telegram user IDs allowed to interact (optional)

Actions:
    send_message     — Send text to a chat                             (MEDIUM)
    send_photo       — Send photo from URL or file_id                  (MEDIUM)
    send_document    — Send document from URL or file_id               (MEDIUM)
    forward_message  — Forward a message to another chat               (MEDIUM)
    get_chat         — Get chat metadata                               (LOW)
    get_me           — Get bot info (also used as health check)        (LOW)
    get_updates      — Poll for recent updates                         (LOW)
    pin_message      — Pin a message in a chat                        (MEDIUM)
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel, SecretSpec

logger = structlog.get_logger()
_API = "https://api.telegram.org/bot{token}"
_TIMEOUT = 15.0


class TelegramConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="telegram", version="1.1.0", name="Telegram", category="chat",
            description=(
                "Active Telegram bot integration — same token as the polling service. "
                "Auto-configured from TELEGRAM_BOT_TOKEN env var. "
                "Send messages, media, and interact with any chat the bot has access to."
            ),
            capabilities=["send_messages", "send_media", "forward_messages", "read_chat_info", "poll_updates"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["bot_token", "allowed_users"],
            secret_specs=[
                SecretSpec(
                    key="bot_token",
                    label="Bot token",
                    help="Get this from @BotFather on Telegram (send /newbot to create one).",
                    placeholder="123456789:ABCdefGhIJKlmNoPQRsTuvWxYZ",
                    example="123456789:ABCdef...",
                    required=True,
                ),
                SecretSpec(
                    key="allowed_users",
                    label="Allowed user IDs",
                    help="Numeric Telegram user IDs (NOT @usernames). Get yours from @userinfobot. Comma-separate multiple IDs. Leave blank to allow ANY user (not recommended).",
                    placeholder="123456789,987654321",
                    example="123456789",
                    required=False,
                ),
            ],
            config_schema={},
            rate_limits={
                "send_message":    RateLimit(requests_per_minute=30),
                "send_photo":      RateLimit(requests_per_minute=20),
                "send_document":   RateLimit(requests_per_minute=20),
                "forward_message": RateLimit(requests_per_minute=20),
                "get_chat":        RateLimit(requests_per_minute=30),
                "get_me":          RateLimit(requests_per_minute=30),
                "get_updates":     RateLimit(requests_per_minute=10),
                "pin_message":     RateLimit(requests_per_minute=10),
            },
            actions=[
                ActionSpec(id="send_message", description="Send a text message to a Telegram chat",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("chat_id", "string", "Chat ID or @username", required=True),
                        ParamSpec("text", "string", "Message text (Markdown or HTML supported)", required=True),
                        ParamSpec("parse_mode", "string", "Markdown|HTML (optional)", required=False),
                        ParamSpec("reply_to_message_id", "integer", "Reply to message ID", required=False),
                    ]),
                ActionSpec(id="send_photo", description="Send a photo to a Telegram chat",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("chat_id", "string", "Chat ID or @username", required=True),
                        ParamSpec("photo", "string", "Photo URL or file_id", required=True),
                        ParamSpec("caption", "string", "Optional caption", required=False),
                    ]),
                ActionSpec(id="send_document", description="Send a document to a Telegram chat",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("chat_id", "string", "Chat ID or @username", required=True),
                        ParamSpec("document", "string", "Document URL or file_id", required=True),
                        ParamSpec("caption", "string", "Optional caption", required=False),
                    ]),
                ActionSpec(id="forward_message", description="Forward a message to another chat",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("chat_id", "string", "Target chat ID", required=True),
                        ParamSpec("from_chat_id", "string", "Source chat ID", required=True),
                        ParamSpec("message_id", "integer", "Message ID to forward", required=True),
                    ]),
                ActionSpec(id="get_chat", description="Get metadata about a chat",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("chat_id", "string", "Chat ID or @username", required=True)]),
                ActionSpec(id="get_me", description="Get bot info and verify token",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_updates", description="Poll for recent bot updates",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("limit", "integer", "Max updates to return (default 10)", required=False),
                        ParamSpec("offset", "integer", "Update offset for pagination", required=False),
                    ]),
                ActionSpec(id="pin_message", description="Pin a message in a chat",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("chat_id", "string", "Chat ID", required=True),
                        ParamSpec("message_id", "integer", "Message ID to pin", required=True),
                    ]),
            ],
            homepage="https://core.telegram.org/bots/api",
            docs_url="https://core.telegram.org/bots/api",
        )

    async def health_check(self) -> bool:
        """Call getMe to verify the bot token is valid and the API is reachable.

        Reads bot_token from TELEGRAM_BOT_TOKEN env var (canonical source).
        Returns True only if Telegram API confirms the token is valid.
        """
        import os
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
            data = r.json()
            ok = bool(data.get("ok"))
            if ok:
                bot = data.get("result", {})
                logger.info(
                    "telegram.health_check_ok",
                    username=bot.get("username"),
                    bot_id=bot.get("id"),
                )
            else:
                logger.warning("telegram.health_check_failed", description=data.get("description"))
            return ok
        except Exception as e:
            logger.warning("telegram.health_check_error", error=str(e)[:120])
            return False

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        token = secrets.get("bot_token", "")
        if not token:
            return self.err("bot_token not configured")
        base = f"https://api.telegram.org/bot{token}"
        if action == "send_message":   return await self._call(base, "sendMessage",   self._msg_body(params), params)
        if action == "send_photo":     return await self._call(base, "sendPhoto",     {"chat_id": params["chat_id"], "photo": params["photo"], "caption": params.get("caption", "")}, params)
        if action == "send_document":  return await self._call(base, "sendDocument",  {"chat_id": params["chat_id"], "document": params["document"], "caption": params.get("caption", "")}, params)
        if action == "forward_message":return await self._call(base, "forwardMessage",{"chat_id": params["chat_id"], "from_chat_id": params["from_chat_id"], "message_id": int(params["message_id"])}, params)
        if action == "get_chat":       return await self._call(base, "getChat",       {"chat_id": params["chat_id"]}, params)
        if action == "get_me":         return await self._call(base, "getMe",         {}, params)
        if action == "get_updates":    return await self._call(base, "getUpdates",    {"limit": int(params.get("limit") or 10), "offset": int(params.get("offset") or 0)}, params)
        if action == "pin_message":    return await self._call(base, "pinChatMessage",{"chat_id": params["chat_id"], "message_id": int(params["message_id"])}, params)
        return self.err(f"Unknown action: {action}")

    def _msg_body(self, p: dict) -> dict:
        body: dict[str, Any] = {"chat_id": p["chat_id"], "text": p["text"]}
        if p.get("parse_mode"): body["parse_mode"] = p["parse_mode"]
        if p.get("reply_to_message_id"): body["reply_to_message_id"] = int(p["reply_to_message_id"])
        return body

    async def _call(self, base: str, method: str, body: dict, _params: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{base}/{method}", json=body)
        d = r.json()
        if d.get("ok"):
            return self.ok(d.get("result"))
        return self.err(f"Telegram {d.get('error_code')}: {d.get('description')}")
