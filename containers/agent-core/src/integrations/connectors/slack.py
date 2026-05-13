"""Slack connector — incoming webhooks + Web API.

Secrets (stored in vault):
    webhook_url    — Slack Incoming Webhook URL (for posting messages)
    bot_token      — Optional: xoxb- Bot Token (for reading channels, users)

Actions:
    send_message    — Post message via incoming webhook          (MEDIUM)
    send_blocks     — Post Block Kit rich message                (MEDIUM)
    get_channels    — List workspace channels (requires bot_token)(LOW)
    get_users       — List workspace members (requires bot_token) (LOW)
    upload_snippet  — Post a code/text snippet                   (MEDIUM)
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
_API = "https://slack.com/api"


class SlackConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "slack",
            version     = "1.0.0",
            name        = "Slack",
            category    = "chat",
            description = "Send rich messages to Slack channels via incoming webhooks or Bot API.",
            capabilities = [
                "send_messages",
                "send_block_kit",
                "list_channels",
                "list_users",
                "upload_snippets",
            ],
            risk_level       = RiskLevel.MEDIUM,
            required_secrets = ["webhook_url"],
            config_schema    = {},
            rate_limits      = {
                "send_message":   RateLimit(requests_per_minute=60),
                "send_blocks":    RateLimit(requests_per_minute=60),
                "get_channels":   RateLimit(requests_per_minute=20),
                "get_users":      RateLimit(requests_per_minute=10),
                "upload_snippet": RateLimit(requests_per_minute=5),
            },
            actions = [
                ActionSpec(
                    id="send_message", description="Post a plain text message via incoming webhook",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("text",      "string", "Message text (mrkdwn supported)", required=True),
                        ParamSpec("channel",   "string", "Override channel (e.g. #general)", required=False),
                        ParamSpec("username",  "string", "Override bot display name",        required=False),
                        ParamSpec("icon_emoji","string", "Override bot icon (e.g. :robot_face:)", required=False),
                    ],
                ),
                ActionSpec(
                    id="send_blocks", description="Post a Block Kit rich message (JSON blocks array)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("blocks",   "array",  "Slack Block Kit blocks array",    required=True),
                        ParamSpec("text",     "string", "Fallback plain text",             required=False),
                        ParamSpec("channel",  "string", "Override channel",                required=False),
                    ],
                ),
                ActionSpec(
                    id="get_channels", description="List public workspace channels (requires bot_token)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("limit", "integer", "Max channels to return (default 50)", required=False),
                    ],
                ),
                ActionSpec(
                    id="get_users", description="List workspace members (requires bot_token)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("limit", "integer", "Max users to return (default 50)", required=False),
                    ],
                ),
                ActionSpec(
                    id="upload_snippet", description="Post code/text as a Slack snippet (requires bot_token)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("content",   "string", "Text content",               required=True),
                        ParamSpec("filename",  "string", "Filename (default: output.txt)", required=False),
                        ParamSpec("title",     "string", "Snippet title",              required=False),
                        ParamSpec("channel_id","string", "Channel ID to post into",    required=True),
                    ],
                ),
            ],
            homepage = "https://slack.com",
            docs_url = "https://api.slack.com/messaging/webhooks",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "send_message":   return await self._send_message(params, secrets)
        if action == "send_blocks":    return await self._send_blocks(params, secrets)
        if action == "get_channels":   return await self._get_channels(params, secrets)
        if action == "get_users":      return await self._get_users(params, secrets)
        if action == "upload_snippet": return await self._upload_snippet(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    async def _send_message(self, p: dict, secrets: dict) -> dict:
        url = secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured")
        body: dict[str, Any] = {"text": p.get("text", "")}
        if p.get("channel"):    body["channel"]    = p["channel"]
        if p.get("username"):   body["username"]   = p["username"]
        if p.get("icon_emoji"): body["icon_emoji"] = p["icon_emoji"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body)
        if r.status_code == 200 and r.text == "ok":
            return self.ok({"delivered": True})
        return self.err(f"Slack returned: {r.text[:200]}")

    async def _send_blocks(self, p: dict, secrets: dict) -> dict:
        url = secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured")
        body: dict[str, Any] = {
            "blocks": p.get("blocks", []),
            "text":   p.get("text", ""),
        }
        if p.get("channel"): body["channel"] = p["channel"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body)
        if r.status_code == 200 and r.text == "ok":
            return self.ok({"delivered": True})
        return self.err(f"Slack returned: {r.text[:200]}")

    async def _api_get(self, endpoint: str, params: dict, token: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{_API}/{endpoint}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        return r.json()

    async def _get_channels(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("bot_token", "")
        if not token:
            return self.err("bot_token not configured")
        limit = min(int(p.get("limit") or 50), 200)
        data  = await self._api_get("conversations.list", {"limit": limit, "types": "public_channel"}, token)
        if not data.get("ok"):
            return self.err(data.get("error", "unknown Slack error"))
        channels = [{"id": c["id"], "name": c["name"], "num_members": c.get("num_members")} for c in data.get("channels", [])]
        return self.ok({"channels": channels, "count": len(channels)})

    async def _get_users(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("bot_token", "")
        if not token:
            return self.err("bot_token not configured")
        limit = min(int(p.get("limit") or 50), 200)
        data  = await self._api_get("users.list", {"limit": limit}, token)
        if not data.get("ok"):
            return self.err(data.get("error", "unknown"))
        users = [
            {"id": u["id"], "name": u.get("real_name") or u.get("name"), "email": u.get("profile", {}).get("email")}
            for u in data.get("members", [])
            if not u.get("is_bot") and not u.get("deleted")
        ]
        return self.ok({"users": users, "count": len(users)})

    async def _upload_snippet(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("bot_token", "")
        if not token:
            return self.err("bot_token not configured")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{_API}/files.uploadV2",
                headers={"Authorization": f"Bearer {token}"},
                data={
                    "channel_id": p.get("channel_id", ""),
                    "filename":   p.get("filename") or "output.txt",
                    "title":      p.get("title") or "",
                },
                files={"file": (p.get("filename") or "output.txt", p.get("content", "").encode())},
            )
        data = r.json()
        if data.get("ok"):
            return self.ok({"file_id": data.get("file", {}).get("id")})
        return self.err(data.get("error", "upload failed"))
