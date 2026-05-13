"""Microsoft Teams connector — Graph API v1.0 via OAuth2 client credentials."""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_TIMEOUT = 15.0
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Module-level token cache: {client_id: (access_token, expires_at_unix)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


async def _get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Fetch or return cached OAuth2 client_credentials token."""
    cached = _TOKEN_CACHE.get(client_id)
    if cached and time.time() < cached[1] - 60:
        return cached[0]

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "https://graph.microsoft.com/.default",
            },
        )
        r.raise_for_status()
        data = r.json()

    token      = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _TOKEN_CACHE[client_id] = (token, time.time() + expires_in)
    return token


class TeamsConnector(BaseConnector):
    """Microsoft Teams via Graph API v1.0."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="teams",
            version="1.0.0",
            name="Microsoft Teams",
            category="chat",
            description="Send and receive messages in Microsoft Teams via the Graph API. Requires Azure AD app registration with Teams permissions.",
            capabilities=["send_message", "read_messages", "list_channels", "send_dm"],
            risk_level=RiskLevel.MEDIUM,
            actions=[
                ActionSpec(
                    id="list_teams",
                    description="List Teams joined by the service account.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="list_channels",
                    description="List channels in a team.",
                    params=[
                        ParamSpec(name="team_id", type="string", description="Team ID."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_channel_message",
                    description="Send a message to a Teams channel.",
                    params=[
                        ParamSpec(name="team_id", type="string", description="Team ID."),
                        ParamSpec(name="channel_id", type="string", description="Channel ID."),
                        ParamSpec(name="text", type="string", description="Message text."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_channel_messages",
                    description="Get recent messages from a Teams channel.",
                    params=[
                        ParamSpec(name="team_id", type="string", description="Team ID."),
                        ParamSpec(name="channel_id", type="string", description="Channel ID."),
                        ParamSpec(name="limit", type="integer", description="Max messages.", required=False, default=20),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="create_channel",
                    description="Create a new channel in a team.",
                    params=[
                        ParamSpec(name="team_id", type="string", description="Team ID."),
                        ParamSpec(name="name", type="string", description="Channel name."),
                        ParamSpec(name="description", type="string", description="Channel description.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="send_dm",
                    description="Send a direct message to a Teams chat.",
                    params=[
                        ParamSpec(name="chat_id", type="string", description="Chat ID (from list_chats)."),
                        ParamSpec(name="text", type="string", description="Message text."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_me",
                    description="Get the authenticated user's profile.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="list_chats",
                    description="List the user's chats (DMs and group chats).",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["tenant_id", "client_id", "client_secret"],
            config_schema={},
            rate_limits={
                "list_teams":            RateLimit(requests_per_minute=30),
                "list_channels":         RateLimit(requests_per_minute=60),
                "send_channel_message":  RateLimit(requests_per_minute=20),
                "get_channel_messages":  RateLimit(requests_per_minute=30),
                "create_channel":        RateLimit(requests_per_minute=5),
                "send_dm":               RateLimit(requests_per_minute=20),
                "get_me":                RateLimit(requests_per_minute=60),
                "list_chats":            RateLimit(requests_per_minute=30),
            },
            homepage="https://developer.microsoft.com/graph",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        tenant_id     = secrets.get("tenant_id", "")
        client_id     = secrets.get("client_id", "")
        client_secret = secrets.get("client_secret", "")

        if not tenant_id or not client_id or not client_secret:
            return self.err("Secrets 'tenant_id', 'client_id', and 'client_secret' are required")

        try:
            token = await _get_token(tenant_id, client_id, client_secret)
        except Exception as e:
            return self.err(f"OAuth token fetch failed: {e}")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

        try:
            async with httpx.AsyncClient(base_url=_GRAPH_BASE, headers=headers, timeout=_TIMEOUT) as client:

                if action == "list_teams":
                    r = await client.get("/me/joinedTeams")
                    r.raise_for_status()
                    teams = [{"id": t["id"], "displayName": t.get("displayName", "")} for t in r.json().get("value", [])]
                    return self.ok({"teams": teams})

                elif action == "list_channels":
                    team_id = params.get("team_id", "")
                    if not team_id:
                        return self.err("team_id is required")
                    r = await client.get(f"/teams/{team_id}/channels")
                    r.raise_for_status()
                    channels = [{"id": c["id"], "displayName": c.get("displayName", "")} for c in r.json().get("value", [])]
                    return self.ok({"channels": channels})

                elif action == "send_channel_message":
                    team_id    = params.get("team_id", "")
                    channel_id = params.get("channel_id", "")
                    text       = params.get("text", "")
                    if not team_id or not channel_id or not text:
                        return self.err("team_id, channel_id, and text are required")
                    r = await client.post(
                        f"/teams/{team_id}/channels/{channel_id}/messages",
                        json={"body": {"content": text}},
                    )
                    r.raise_for_status()
                    return self.ok({"message_id": r.json().get("id")})

                elif action == "get_channel_messages":
                    team_id    = params.get("team_id", "")
                    channel_id = params.get("channel_id", "")
                    limit      = int(params.get("limit") or 20)
                    if not team_id or not channel_id:
                        return self.err("team_id and channel_id are required")
                    r = await client.get(
                        f"/teams/{team_id}/channels/{channel_id}/messages",
                        params={"$top": limit},
                    )
                    r.raise_for_status()
                    msgs = [{"id": m["id"], "body": m.get("body", {}).get("content", ""), "from": m.get("from", {})} for m in r.json().get("value", [])]
                    return self.ok({"messages": msgs})

                elif action == "create_channel":
                    team_id     = params.get("team_id", "")
                    name        = params.get("name", "")
                    description = params.get("description", "")
                    if not team_id or not name:
                        return self.err("team_id and name are required")
                    r = await client.post(
                        f"/teams/{team_id}/channels",
                        json={"displayName": name, "description": description},
                    )
                    r.raise_for_status()
                    return self.ok({"channel": r.json()})

                elif action == "send_dm":
                    chat_id = params.get("chat_id", "")
                    text    = params.get("text", "")
                    if not chat_id or not text:
                        return self.err("chat_id and text are required")
                    r = await client.post(
                        f"/chats/{chat_id}/messages",
                        json={"body": {"content": text}},
                    )
                    r.raise_for_status()
                    return self.ok({"message_id": r.json().get("id")})

                elif action == "get_me":
                    r = await client.get("/me")
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "list_chats":
                    r = await client.get("/me/chats")
                    r.raise_for_status()
                    return self.ok({"chats": r.json().get("value", [])})

                else:
                    return self.err(f"Unknown action: {action}")

        except httpx.HTTPStatusError as e:
            return self.err(f"Graph API error {e.response.status_code}: {e.response.text[:200]}")
        except httpx.ConnectError:
            return self.err("Cannot connect to Microsoft Graph API")
        except Exception as e:
            return self.err(str(e))
