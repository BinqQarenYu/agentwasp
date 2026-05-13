"""Nextcloud Talk connector — messaging via Nextcloud Talk REST API.

Nextcloud Talk (Spreed) provides chat rooms, direct messages, and file sharing.
Uses Basic Auth (username + app password recommended).

Secrets:
    base_url  — Nextcloud instance URL (e.g. https://cloud.example.com)
    username  — Nextcloud username
    password  — Nextcloud password or app password

Actions:
    list_rooms       — List all conversations/rooms                     (LOW)
    get_room         — Get details of a specific room                   (LOW)
    send_message     — Send a message to a room                        (MEDIUM)
    get_messages     — Get recent messages from a room                  (LOW)
    create_room      — Create a new conversation room                   (MEDIUM)
    delete_room      — Delete a room                                    (HIGH)
    join_room        — Join a conversation                              (MEDIUM)
    leave_room       — Leave a conversation                             (MEDIUM)
    get_participants — List participants in a room                      (LOW)
    share_file       — Share a file into a conversation                 (HIGH)
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_API_PATH = "/ocs/v2.php/apps/spreed/api/v4"
_TIMEOUT  = 15


class NextcloudTalkConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="nextcloud-talk", version="1.0.0", name="Nextcloud Talk", category="chat",
            description=(
                "Send and receive messages via Nextcloud Talk (Spreed). "
                "Supports rooms, direct messages, and file sharing within Nextcloud."
            ),
            capabilities=["send_messages", "read_messages", "manage_rooms", "share_files"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["base_url", "username", "password"],
            config_schema={},
            rate_limits={
                "list_rooms":       RateLimit(requests_per_minute=30),
                "get_room":         RateLimit(requests_per_minute=30),
                "send_message":     RateLimit(requests_per_minute=20),
                "get_messages":     RateLimit(requests_per_minute=30),
                "create_room":      RateLimit(requests_per_minute=5),
                "delete_room":      RateLimit(requests_per_minute=5),
                "join_room":        RateLimit(requests_per_minute=10),
                "leave_room":       RateLimit(requests_per_minute=10),
                "get_participants": RateLimit(requests_per_minute=20),
                "share_file":       RateLimit(requests_per_minute=5),
            },
            actions=[
                ActionSpec(id="list_rooms", description="List all conversations/rooms the user is in",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_room", description="Get details of a specific conversation room",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("token", "string", "Room token (from list_rooms)", required=True)]),
                ActionSpec(id="send_message", description="Send a text message to a room",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("token", "string", "Room token", required=True),
                        ParamSpec("message", "string", "Message text to send", required=True),
                        ParamSpec("reply_to", "integer", "Message ID to reply to (optional)", required=False),
                    ]),
                ActionSpec(id="get_messages", description="Get recent messages from a room",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("token", "string", "Room token", required=True),
                        ParamSpec("limit", "integer", "Max messages to return (default 20, max 200)", required=False),
                        ParamSpec("last_known_message", "integer", "Only get messages after this message ID", required=False),
                    ]),
                ActionSpec(id="create_room", description="Create a new conversation room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("name", "string", "Room name", required=True),
                        ParamSpec("room_type", "string", "one_to_one|group|public (default: group)", required=False),
                        ParamSpec("invite", "string", "Username to invite for one_to_one rooms", required=False),
                    ]),
                ActionSpec(id="delete_room", description="Delete a conversation room permanently",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[ParamSpec("token", "string", "Room token to delete", required=True)]),
                ActionSpec(id="join_room", description="Join a public conversation room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("token", "string", "Room token to join", required=True)]),
                ActionSpec(id="leave_room", description="Leave a conversation room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("token", "string", "Room token to leave", required=True)]),
                ActionSpec(id="get_participants", description="List participants in a room",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("token", "string", "Room token", required=True)]),
                ActionSpec(id="share_file", description="Share a file from Nextcloud into a conversation",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("token", "string", "Room token", required=True),
                        ParamSpec("file_path", "string", "Nextcloud file path (e.g. /Documents/report.pdf)", required=True),
                        ParamSpec("message", "string", "Optional message to accompany the file", required=False),
                    ]),
            ],
            homepage="https://nextcloud.com/talk/",
            docs_url="https://nextcloud-talk.readthedocs.io/en/latest/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        base_url = (secrets.get("base_url") or "").rstrip("/")
        username = secrets.get("username", "")
        password = secrets.get("password", "")
        if not base_url or not username or not password:
            return self.err("base_url, username, and password secrets are required")

        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Accept":         "application/json",
        }
        api_base = f"{base_url}{_API_PATH}"

        try:
            if action == "list_rooms":       return await self._list_rooms(api_base, headers)
            if action == "get_room":         return await self._get_room(api_base, headers, params)
            if action == "send_message":     return await self._send_message(api_base, headers, params)
            if action == "get_messages":     return await self._get_messages(api_base, headers, params)
            if action == "create_room":      return await self._create_room(api_base, headers, params)
            if action == "delete_room":      return await self._delete_room(api_base, headers, params)
            if action == "join_room":        return await self._join_room(api_base, headers, params)
            if action == "leave_room":       return await self._leave_room(api_base, headers, params)
            if action == "get_participants": return await self._get_participants(api_base, headers, params)
            if action == "share_file":       return await self._share_file(api_base, base_url, headers, params)
        except httpx.ConnectError:
            return self.err(f"Cannot connect to Nextcloud at {base_url}")
        except Exception as exc:
            logger.error("nextcloud_talk.error", action=action, error=str(exc))
            return self.err(f"Nextcloud Talk error: {exc}")

        return self.err(f"Unknown action: {action}")

    def _fmt_room(self, r: dict) -> dict:
        return {
            "token":        r.get("token", ""),
            "name":         r.get("displayName", r.get("name", "")),
            "type":         r.get("type", 0),
            "unread":       r.get("unreadMessages", 0),
            "participants": r.get("participantCount", 0),
            "last_message": r.get("lastMessage", {}).get("message", "") if r.get("lastMessage") else "",
        }

    async def _req(self, method: str, url: str, headers: dict, **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.request(method, url, headers=headers, **kwargs)
            r.raise_for_status()
            data = r.json()
        return data.get("ocs", {}).get("data", data)

    async def _list_rooms(self, base: str, headers: dict) -> dict:
        data  = await self._req("GET", f"{base}/room", headers)
        rooms = [self._fmt_room(r) for r in (data if isinstance(data, list) else [])]
        return self.ok({"rooms": rooms, "count": len(rooms)})

    async def _get_room(self, base: str, headers: dict, params: dict) -> dict:
        token = params.get("token", "")
        if not token:
            return self.err("token is required")
        data = await self._req("GET", f"{base}/room/{token}", headers)
        return self.ok(self._fmt_room(data) if isinstance(data, dict) else {"token": token})

    async def _send_message(self, base: str, headers: dict, params: dict) -> dict:
        token   = params.get("token", "")
        message = params.get("message", "")
        if not token or not message:
            return self.err("token and message are required")
        body: dict[str, Any] = {"message": message}
        if params.get("reply_to"):
            body["replyTo"] = int(params["reply_to"])
        data = await self._req("POST", f"{base}/chat/{token}",
            {**headers, "Content-Type": "application/json"}, json=body)
        return self.ok({"sent": True, "token": token, "message_id": data.get("id") if isinstance(data, dict) else None})

    async def _get_messages(self, base: str, headers: dict, params: dict) -> dict:
        token = params.get("token", "")
        if not token:
            return self.err("token is required")
        limit = min(int(params.get("limit") or 20), 200)
        qp: dict[str, Any] = {"limit": limit, "lookIntoFuture": 0}
        if params.get("last_known_message"):
            qp["lastKnownMessageId"] = int(params["last_known_message"])
        data = await self._req("GET", f"{base}/chat/{token}", headers, params=qp)
        messages = [
            {
                "id":        m.get("id"),
                "from":      m.get("actorDisplayName", m.get("actorId", "")),
                "message":   m.get("message", ""),
                "timestamp": m.get("timestamp"),
                "type":      m.get("messageType", ""),
            }
            for m in (data if isinstance(data, list) else [])
        ]
        return self.ok({"messages": messages, "count": len(messages), "token": token})

    async def _create_room(self, base: str, headers: dict, params: dict) -> dict:
        name      = params.get("name", "")
        room_type = (params.get("room_type") or "group").lower()
        invite    = params.get("invite", "")
        type_map  = {"one_to_one": 1, "group": 2, "public": 3}
        type_id   = type_map.get(room_type, 2)
        body: dict[str, Any] = {"roomType": type_id, "roomName": name}
        if type_id == 1 and invite:
            body["invite"] = invite
        data = await self._req("POST", f"{base}/room",
            {**headers, "Content-Type": "application/json"}, json=body)
        return self.ok({"created": True, "token": data.get("token", "") if isinstance(data, dict) else "", "name": name})

    async def _delete_room(self, base: str, headers: dict, params: dict) -> dict:
        token = params.get("token", "")
        if not token:
            return self.err("token is required")
        await self._req("DELETE", f"{base}/room/{token}", headers)
        return self.ok({"deleted": True, "token": token})

    async def _join_room(self, base: str, headers: dict, params: dict) -> dict:
        token = params.get("token", "")
        if not token:
            return self.err("token is required")
        await self._req("POST", f"{base}/room/{token}/participants/active",
            {**headers, "Content-Type": "application/json"}, json={})
        return self.ok({"joined": True, "token": token})

    async def _leave_room(self, base: str, headers: dict, params: dict) -> dict:
        token = params.get("token", "")
        if not token:
            return self.err("token is required")
        await self._req("DELETE", f"{base}/room/{token}/participants/self", headers)
        return self.ok({"left": True, "token": token})

    async def _get_participants(self, base: str, headers: dict, params: dict) -> dict:
        token = params.get("token", "")
        if not token:
            return self.err("token is required")
        data = await self._req("GET", f"{base}/room/{token}/participants", headers)
        participants = [
            {
                "name":     p.get("displayName", p.get("actorId", "")),
                "type":     p.get("participantType", 0),
                "in_call":  p.get("inCall", 0) > 0,
            }
            for p in (data if isinstance(data, list) else [])
        ]
        return self.ok({"participants": participants, "count": len(participants), "token": token})

    async def _share_file(self, api_base: str, base_url: str, headers: dict, params: dict) -> dict:
        token     = params.get("token", "")
        file_path = params.get("file_path", "")
        message   = params.get("message", "")
        if not token or not file_path:
            return self.err("token and file_path are required")
        share_url = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
        await self._req("POST", share_url,
            {**headers, "Content-Type": "application/json"},
            json={"shareWith": token, "shareType": 10, "path": file_path})
        if message:
            await self._send_message(api_base, headers, {"token": token, "message": message})
        return self.ok({"shared": True, "token": token, "file_path": file_path})
