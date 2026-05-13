"""Discord connector — webhooks + Bot API.

Secrets (stored in vault):
    webhook_url   — Discord Webhook URL (no auth required for sends)
    bot_token     — Optional: Discord Bot Token (for reading channels, DMs)

Actions:
    send_message     — Send text via webhook                           (MEDIUM)
    send_embed       — Send rich embed card via webhook                (MEDIUM)
    send_file        — POST file content as a message attachment       (HIGH)
    get_guild_info   — Fetch guild metadata via Bot API                (LOW)
    create_thread    — Create a thread in a channel via Bot API        (HIGH)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import (
    ActionSpec, BaseConnector, ConnectorManifest,
    ParamSpec, RateLimit, RiskLevel,
)

logger = structlog.get_logger()
_TIMEOUT = 15.0
_API = "https://discord.com/api/v10"


class DiscordConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "discord",
            version     = "1.0.0",
            name        = "Discord",
            category    = "chat",
            description = "Send messages and embeds to Discord channels via webhooks or Bot API.",
            capabilities = [
                "send_messages",
                "send_rich_embeds",
                "read_guild_info",
                "create_threads",
            ],
            risk_level       = RiskLevel.HIGH,
            required_secrets = ["webhook_url"],
            config_schema    = {},
            rate_limits      = {
                "send_message":   RateLimit(requests_per_minute=30),
                "send_embed":     RateLimit(requests_per_minute=30),
                "send_file":      RateLimit(requests_per_minute=5),
                "get_guild_info": RateLimit(requests_per_minute=10),
                "create_thread":  RateLimit(requests_per_minute=5),
            },
            actions = [
                ActionSpec(
                    id="send_message", description="Send a plain text message to a Discord channel",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("content",    "string", "Message text (max 2000 chars)", required=True),
                        ParamSpec("username",   "string", "Override webhook display name", required=False),
                        ParamSpec("avatar_url", "string", "Override webhook avatar URL",   required=False),
                    ],
                ),
                ActionSpec(
                    id="send_embed", description="Send a rich embed card with title, description, color, and fields",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("title",       "string",  "Embed title",                required=True),
                        ParamSpec("description", "string",  "Embed description",          required=False),
                        ParamSpec("color",       "integer", "Decimal color code (default: WASP yellow=16776960)", required=False),
                        ParamSpec("url",         "string",  "Title hyperlink URL",        required=False),
                        ParamSpec("footer",      "string",  "Footer text",                required=False),
                        ParamSpec("fields",      "array",   "List of {name, value, inline} objects", required=False),
                    ],
                ),
                ActionSpec(
                    id="send_file", description="Upload a text file as a Discord message attachment",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("content",   "string", "File content (text)",  required=True),
                        ParamSpec("filename",  "string", "Filename (default: wasp_output.txt)", required=False),
                        ParamSpec("caption",   "string", "Optional message text alongside file", required=False),
                    ],
                ),
                ActionSpec(
                    id="get_guild_info", description="Fetch guild/server metadata (requires bot_token)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("guild_id", "string", "Discord Guild (server) ID", required=True),
                    ],
                ),
                ActionSpec(
                    id="create_thread", description="Create a thread in a channel (requires bot_token)",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("channel_id",    "string",  "Channel ID",                                  required=True),
                        ParamSpec("name",          "string",  "Thread name",                                 required=True),
                        ParamSpec("message",       "string",  "Starter message text",                        required=True),
                        ParamSpec("auto_archive_m","integer", "Auto-archive after N minutes (60/1440/4320)", required=False),
                    ],
                ),
            ],
            homepage = "https://discord.com",
            docs_url = "https://discord.com/developers/docs/resources/webhook",
        )

    async def health_check(self) -> bool:
        return True  # Webhook URLs validated at use time

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "send_message":  return await self._send_message(params, secrets)
        if action == "send_embed":    return await self._send_embed(params, secrets)
        if action == "send_file":     return await self._send_file(params, secrets)
        if action == "get_guild_info":return await self._get_guild(params, secrets)
        if action == "create_thread": return await self._create_thread(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    async def _send_message(self, p: dict, secrets: dict) -> dict:
        url = secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured")
        body: dict[str, Any] = {"content": str(p.get("content", ""))[:2000]}
        if p.get("username"):   body["username"]   = p["username"]
        if p.get("avatar_url"): body["avatar_url"] = p["avatar_url"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body)
        ok = r.status_code in (200, 204)
        return self.ok({"status_code": r.status_code}) if ok else self.err(f"Discord HTTP {r.status_code}: {r.text[:200]}")

    async def _send_embed(self, p: dict, secrets: dict) -> dict:
        url = secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured")
        embed: dict[str, Any] = {
            "title":       p.get("title", ""),
            "description": p.get("description", ""),
            "color":       int(p.get("color") or 16_776_960),  # Yellow
        }
        if p.get("url"):    embed["url"]    = p["url"]
        if p.get("footer"): embed["footer"] = {"text": p["footer"]}
        if p.get("fields"): embed["fields"] = p["fields"]
        body: dict[str, Any] = {"embeds": [embed]}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body)
        ok = r.status_code in (200, 204)
        return self.ok({"status_code": r.status_code}) if ok else self.err(f"Discord HTTP {r.status_code}: {r.text[:200]}")

    async def _send_file(self, p: dict, secrets: dict) -> dict:
        url = secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured")
        filename = p.get("filename") or "wasp_output.txt"
        content  = p.get("content", "").encode()
        caption  = p.get("caption", "")
        files    = {"file": (filename, content, "text/plain")}
        data     = {"content": caption} if caption else {}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, data=data, files=files)
        ok = r.status_code in (200, 204)
        return self.ok({"filename": filename}) if ok else self.err(f"Discord HTTP {r.status_code}")

    async def _get_guild(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("bot_token", "")
        if not token:
            return self.err("bot_token not configured — required for get_guild_info")
        guild_id = p.get("guild_id", "")
        headers  = {"Authorization": f"Bot {token}"}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/guilds/{guild_id}", headers=headers)
        if r.status_code == 200:
            d = r.json()
            return self.ok({"name": d.get("name"), "id": d.get("id"), "member_count": d.get("approximate_member_count")})
        return self.err(f"Discord HTTP {r.status_code}")

    async def _create_thread(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("bot_token", "")
        if not token:
            return self.err("bot_token not configured — required for create_thread")
        channel_id = p.get("channel_id", "")
        headers    = {"Authorization": f"Bot {token}"}
        body = {
            "name":              p.get("name", "New Thread"),
            "auto_archive_duration": int(p.get("auto_archive_m") or 1440),
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{_API}/channels/{channel_id}/threads",
                json=body,
                headers=headers,
            )
            if r.status_code != 201:
                return self.err(f"Discord HTTP {r.status_code}: {r.text[:200]}")
            thread = r.json()
            thread_id = thread.get("id")
            # Post the starter message
            msg_r = await c.post(
                f"{_API}/channels/{thread_id}/messages",
                json={"content": p.get("message", "")[:2000]},
                headers=headers,
            )
        return self.ok({"thread_id": thread_id, "message_status": msg_r.status_code})
