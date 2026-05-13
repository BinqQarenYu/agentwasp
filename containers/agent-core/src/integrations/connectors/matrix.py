"""Matrix connector — Client-Server API.

Supports any Matrix homeserver (matrix.org, Element, Synapse, Dendrite, etc.)

Secrets:
    homeserver_url  — Base URL of the Matrix homeserver (e.g. https://matrix.org)
    access_token    — Matrix access token (from /login or account settings)
    user_id         — Matrix user ID (e.g. @user:matrix.org)

Actions:
    send_message      — Send text to a room                           (MEDIUM)
    send_notice       — Send a server notice (no ping)                (MEDIUM)
    send_image        — Send image message                            (MEDIUM)
    create_room       — Create a new room                             (MEDIUM)
    join_room         — Join a room by alias or ID                    (MEDIUM)
    leave_room        — Leave a room                                  (MEDIUM)
    get_room_messages — Paginate room message history                  (LOW)
    get_joined_rooms  — List rooms the user has joined                 (LOW)
    get_user_profile  — Get profile for a Matrix user                  (LOW)
    set_room_topic    — Update room topic                             (MEDIUM)
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_TIMEOUT = 15.0


class MatrixConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="matrix", version="1.0.0", name="Matrix", category="chat",
            description="Interact with any Matrix homeserver — send messages, manage rooms.",
            capabilities=["send_messages", "create_rooms", "join_rooms", "read_history", "user_profiles"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["homeserver_url", "access_token"],
            config_schema={},
            rate_limits={
                "send_message":      RateLimit(requests_per_minute=30),
                "send_notice":       RateLimit(requests_per_minute=30),
                "send_image":        RateLimit(requests_per_minute=20),
                "create_room":       RateLimit(requests_per_minute=5),
                "join_room":         RateLimit(requests_per_minute=10),
                "leave_room":        RateLimit(requests_per_minute=10),
                "get_room_messages": RateLimit(requests_per_minute=20),
                "get_joined_rooms":  RateLimit(requests_per_minute=10),
                "get_user_profile":  RateLimit(requests_per_minute=30),
                "set_room_topic":    RateLimit(requests_per_minute=10),
            },
            actions=[
                ActionSpec(id="send_message", description="Send a text message to a Matrix room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("room_id", "string", "Room ID (e.g. !abc:matrix.org) or alias (#room:server)", required=True),
                        ParamSpec("body", "string", "Message text", required=True),
                        ParamSpec("formatted_body", "string", "Optional HTML-formatted body", required=False),
                    ]),
                ActionSpec(id="send_notice", description="Send a m.notice message (no client ping)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("room_id", "string", "Room ID or alias", required=True),
                        ParamSpec("body", "string", "Notice text", required=True),
                    ]),
                ActionSpec(id="send_image", description="Send an image to a Matrix room (via URL)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("room_id", "string", "Room ID or alias", required=True),
                        ParamSpec("url", "string", "MXC or public HTTPS URL of the image", required=True),
                        ParamSpec("body", "string", "Alt text / filename", required=False),
                    ]),
                ActionSpec(id="create_room", description="Create a new Matrix room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("name", "string", "Room display name", required=False),
                        ParamSpec("alias", "string", "Local alias (e.g. myroom → #myroom:server)", required=False),
                        ParamSpec("topic", "string", "Room topic", required=False),
                        ParamSpec("invite", "array", "List of Matrix user IDs to invite", required=False),
                        ParamSpec("is_public", "boolean", "Make room publicly discoverable", required=False),
                    ]),
                ActionSpec(id="join_room", description="Join a Matrix room by ID or alias",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("room_id_or_alias", "string", "Room ID or alias", required=True)]),
                ActionSpec(id="leave_room", description="Leave a Matrix room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("room_id", "string", "Room ID", required=True)]),
                ActionSpec(id="get_room_messages", description="Paginate message history of a room",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("room_id", "string", "Room ID", required=True),
                        ParamSpec("limit", "integer", "Max messages to return (default 20)", required=False),
                        ParamSpec("from_token", "string", "Pagination token", required=False),
                    ]),
                ActionSpec(id="get_joined_rooms", description="List rooms the user has joined",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_user_profile", description="Get Matrix user profile",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("user_id", "string", "Matrix user ID (e.g. @user:server)", required=True)]),
                ActionSpec(id="set_room_topic", description="Set or update a room topic",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("room_id", "string", "Room ID", required=True),
                        ParamSpec("topic", "string", "New topic text", required=True),
                    ]),
            ],
            homepage="https://matrix.org",
            docs_url="https://spec.matrix.org/latest/client-server-api/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        base  = secrets.get("homeserver_url", "").rstrip("/")
        token = secrets.get("access_token", "")
        if not base or not token:
            return self.err("homeserver_url and access_token are required")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        api = f"{base}/_matrix/client/v3"

        if action == "send_message":
            return await self._send_event(api, headers, params["room_id"], "m.room.message", {
                "msgtype": "m.text", "body": params["body"],
                **({"format": "org.matrix.custom.html", "formatted_body": params["formatted_body"]}
                   if params.get("formatted_body") else {}),
            })

        if action == "send_notice":
            return await self._send_event(api, headers, params["room_id"], "m.room.message",
                {"msgtype": "m.notice", "body": params["body"]})

        if action == "send_image":
            return await self._send_event(api, headers, params["room_id"], "m.room.message", {
                "msgtype": "m.image",
                "url": params["url"],
                "body": params.get("body", "image"),
            })

        if action == "create_room":
            body: dict[str, Any] = {}
            if params.get("name"):     body["name"]             = params["name"]
            if params.get("alias"):    body["room_alias_name"]  = params["alias"]
            if params.get("topic"):    body["topic"]            = params["topic"]
            if params.get("invite"):   body["invite"]           = params["invite"]
            body["visibility"] = "public" if params.get("is_public") else "private"
            return await self._post(f"{api}/createRoom", headers, body)

        if action == "join_room":
            return await self._post(f"{api}/join/{params['room_id_or_alias']}", headers, {})

        if action == "leave_room":
            return await self._post(f"{api}/rooms/{params['room_id']}/leave", headers, {})

        if action == "get_room_messages":
            limit = min(int(params.get("limit") or 20), 100)
            qp: dict[str, Any] = {"dir": "b", "limit": limit}
            if params.get("from_token"): qp["from"] = params["from_token"]
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{api}/rooms/{params['room_id']}/messages", headers=headers, params=qp)
            if r.status_code == 200:
                d = r.json()
                msgs = [{"sender": m.get("sender"), "body": m.get("content", {}).get("body", ""),
                          "type": m.get("type"), "ts": m.get("origin_server_ts")}
                         for m in d.get("chunk", [])]
                return self.ok({"messages": msgs, "next_batch": d.get("end")})
            return self.err(f"Matrix {r.status_code}")

        if action == "get_joined_rooms":
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{api}/joined_rooms", headers=headers)
            if r.status_code == 200:
                rooms = r.json().get("joined_rooms", [])
                return self.ok({"rooms": rooms, "count": len(rooms)})
            return self.err(f"Matrix {r.status_code}")

        if action == "get_user_profile":
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{api}/profile/{params['user_id']}", headers=headers)
            if r.status_code == 200:
                return self.ok(r.json())
            return self.err(f"Matrix {r.status_code}")

        if action == "set_room_topic":
            return await self._put(f"{api}/rooms/{params['room_id']}/state/m.room.topic",
                headers, {"topic": params["topic"]})

        return self.err(f"Unknown action: {action}")

    async def _send_event(self, api: str, headers: dict, room_id: str, event_type: str, content: dict) -> dict:
        import time
        txn_id = str(int(time.time() * 1000))
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.put(f"{api}/rooms/{room_id}/send/{event_type}/{txn_id}",
                json=content, headers=headers)
        if r.status_code == 200:
            return self.ok({"event_id": r.json().get("event_id")})
        return self.err(f"Matrix {r.status_code}: {r.text[:200]}")

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body, headers=headers)
        if r.status_code in (200, 201):
            return self.ok(r.json())
        return self.err(f"Matrix {r.status_code}: {r.text[:200]}")

    async def _put(self, url: str, headers: dict, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.put(url, json=body, headers=headers)
        if r.status_code == 200:
            return self.ok(r.json())
        return self.err(f"Matrix {r.status_code}: {r.text[:200]}")
