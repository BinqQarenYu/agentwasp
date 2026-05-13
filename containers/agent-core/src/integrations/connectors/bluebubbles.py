"""BlueBubbles connector — iMessage and SMS via self-hosted BlueBubbles macOS server."""

from __future__ import annotations

from typing import Any

import httpx

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_TIMEOUT = 15.0


class BlueBubblesConnector(BaseConnector):
    """BlueBubbles: open-source macOS server exposing iMessage/SMS via REST API.

    See: https://bluebubbles.app/
    No Apple dependency on the agent side — pure HTTP to the macOS companion.
    """

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="bluebubbles",
            version="1.0.0",
            name="BlueBubbles",
            category="chat",
            description="Send and read iMessage and SMS messages via a self-hosted BlueBubbles server running on macOS.",
            capabilities=["platform_imessage", "send_message", "read_messages"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="send_message",
                    description="Send an iMessage or SMS to a chat.",
                    params=[
                        ParamSpec(name="chat_guid", type="string", description="Chat GUID (e.g. iMessage;-;+15551234567)."),
                        ParamSpec(name="text", type="string", description="Message text."),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_chats",
                    description="List recent chats.",
                    params=[
                        ParamSpec(name="limit", type="integer", description="Max chats.", required=False, default=25),
                        ParamSpec(name="offset", type="integer", description="Pagination offset.", required=False, default=0),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_messages",
                    description="Get messages from a chat.",
                    params=[
                        ParamSpec(name="chat_guid", type="string", description="Chat GUID."),
                        ParamSpec(name="limit", type="integer", description="Max messages.", required=False, default=25),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_attachment",
                    description="Send an attachment by file path on the macOS server.",
                    params=[
                        ParamSpec(name="chat_guid", type="string", description="Chat GUID."),
                        ParamSpec(name="attachment_path", type="string", description="Path to the attachment file on the macOS server."),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_reaction",
                    description="Send an iMessage tapback reaction.",
                    params=[
                        ParamSpec(name="chat_guid", type="string", description="Chat GUID."),
                        ParamSpec(name="message_guid", type="string", description="Message GUID to react to."),
                        ParamSpec(name="reaction", type="string", description="Reaction type: love|like|dislike|laugh|emphasize|question."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_contact",
                    description="Search for a contact by name or number.",
                    params=[
                        ParamSpec(name="query", type="string", description="Search query (name or phone number)."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_server_info",
                    description="Get BlueBubbles server info and status.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["server_url", "password"],
            config_schema={},
            rate_limits={
                "send_message":    RateLimit(requests_per_minute=20),
                "get_chats":       RateLimit(requests_per_minute=30),
                "get_messages":    RateLimit(requests_per_minute=30),
                "send_attachment": RateLimit(requests_per_minute=10),
                "send_reaction":   RateLimit(requests_per_minute=30),
                "get_contact":     RateLimit(requests_per_minute=30),
                "get_server_info": RateLimit(requests_per_minute=60),
            },
            homepage="https://bluebubbles.app",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        server_url = secrets.get("server_url", "").rstrip("/")
        password   = secrets.get("password", "")

        if not server_url:
            return self.err("Secret 'server_url' is required")

        def _url(path: str) -> str:
            return f"{server_url}{path}"

        def _auth_params(extra: dict | None = None) -> dict:
            p = {"password": password}
            if extra:
                p.update(extra)
            return p

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:

                if action == "send_message":
                    chat_guid = params.get("chat_guid", "")
                    text      = params.get("text", "")
                    if not chat_guid or not text:
                        return self.err("chat_guid and text are required")
                    r = await client.post(
                        _url("/api/v1/message/text"),
                        params={"password": password},
                        json={"chatGuid": chat_guid, "message": text, "method": "apple-script"},
                    )
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_chats":
                    limit  = int(params.get("limit") or 25)
                    offset = int(params.get("offset") or 0)
                    r = await client.get(_url("/api/v1/chat"), params=_auth_params({"limit": limit, "offset": offset}))
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_messages":
                    chat_guid = params.get("chat_guid", "")
                    limit     = int(params.get("limit") or 25)
                    if not chat_guid:
                        return self.err("chat_guid is required")
                    r = await client.get(
                        _url(f"/api/v1/chat/{chat_guid}/message"),
                        params=_auth_params({"limit": limit}),
                    )
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_attachment":
                    chat_guid       = params.get("chat_guid", "")
                    attachment_path = params.get("attachment_path", "")
                    if not chat_guid or not attachment_path:
                        return self.err("chat_guid and attachment_path are required")
                    r = await client.post(
                        _url("/api/v1/message/attachment"),
                        params={"password": password},
                        json={"chatGuid": chat_guid, "attachment": attachment_path},
                    )
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_reaction":
                    chat_guid    = params.get("chat_guid", "")
                    message_guid = params.get("message_guid", "")
                    reaction     = params.get("reaction", "")
                    if not chat_guid or not message_guid or not reaction:
                        return self.err("chat_guid, message_guid, and reaction are required")
                    r = await client.post(
                        _url("/api/v1/message/react"),
                        params={"password": password},
                        json={"chatGuid": chat_guid, "selectedMessageGuid": message_guid, "reactionType": reaction},
                    )
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_contact":
                    query = params.get("query", "")
                    if not query:
                        return self.err("query is required")
                    r = await client.get(_url("/api/v1/contact"), params=_auth_params({"search": query}))
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_server_info":
                    r = await client.get(_url("/api/v1/server/info"), params={"password": password})
                    r.raise_for_status()
                    return self.ok(r.json())

                else:
                    return self.err(f"Unknown action: {action}")

        except httpx.HTTPStatusError as e:
            return self.err(f"BlueBubbles API error {e.response.status_code}: {e.response.text[:200]}")
        except httpx.ConnectError:
            return self.err(f"Cannot connect to BlueBubbles server at {server_url}")
        except Exception as e:
            return self.err(str(e))
