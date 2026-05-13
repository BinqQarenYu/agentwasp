"""Signal connector — signal-cli REST API bridge.

Requires a running signal-cli with the REST API enabled:
  https://github.com/bbernhard/signal-cli-rest-api

The REST API runs on a configurable port (default 8080).
WASP talks to it over HTTP — signal-cli handles all Signal protocol crypto.

Secrets:
    base_url    — signal-cli REST API base URL (e.g. http://localhost:8080)
    number      — Sender phone number (registered in signal-cli, E.164 format)

Actions:
    send_message    — Send text to a number or group                  (MEDIUM)
    send_attachment — Send message with attachment URL                 (MEDIUM)
    receive         — Fetch queued incoming messages                   (LOW)
    list_groups     — List Signal groups the number belongs to         (LOW)
    list_contacts   — List known contacts                              (LOW)
    send_reaction   — Send emoji reaction to a message                 (LOW)
"""
from __future__ import annotations

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_TIMEOUT = 15.0


class SignalConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="signal", version="1.0.0", name="Signal", category="chat",
            description="Send encrypted messages via Signal using signal-cli REST API bridge.",
            capabilities=["send_messages", "send_attachments", "receive_messages", "list_groups", "reactions"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["base_url", "number"],
            config_schema={},
            rate_limits={
                "send_message":    RateLimit(requests_per_minute=30),
                "send_attachment": RateLimit(requests_per_minute=10),
                "receive":         RateLimit(requests_per_minute=20),
                "list_groups":     RateLimit(requests_per_minute=10),
                "list_contacts":   RateLimit(requests_per_minute=10),
                "send_reaction":   RateLimit(requests_per_minute=30),
            },
            actions=[
                ActionSpec(id="send_message", description="Send text message to a Signal number or group",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("recipients", "array", "List of E.164 phone numbers OR one group ID", required=True),
                        ParamSpec("message", "string", "Message text", required=True),
                    ]),
                ActionSpec(id="send_attachment", description="Send message with file attachment by URL",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("recipients", "array", "List of E.164 phone numbers", required=True),
                        ParamSpec("message", "string", "Message text", required=False),
                        ParamSpec("attachment_urls", "array", "List of public attachment URLs", required=True),
                    ]),
                ActionSpec(id="receive", description="Fetch queued incoming messages (polling mode)",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="list_groups", description="List Signal groups the account belongs to",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="list_contacts", description="List known contacts in signal-cli",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="send_reaction", description="Send emoji reaction to a message",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("recipient", "string", "Target phone number or group ID", required=True),
                        ParamSpec("target_author", "string", "Phone number of the message author", required=True),
                        ParamSpec("timestamp", "integer", "Timestamp of the message to react to", required=True),
                        ParamSpec("emoji", "string", "Emoji reaction character", required=True),
                    ]),
            ],
            homepage="https://signal.org",
            docs_url="https://github.com/bbernhard/signal-cli-rest-api",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        base = secrets.get("base_url", "").rstrip("/")
        number = secrets.get("number", "")
        if not base or not number:
            return self.err("base_url and number are required")

        if action == "send_message":
            return await self._post(f"{base}/v2/send", {
                "number": number,
                "recipients": params.get("recipients", []),
                "message": params.get("message", ""),
            })

        if action == "send_attachment":
            return await self._post(f"{base}/v2/send", {
                "number": number,
                "recipients": params.get("recipients", []),
                "message": params.get("message", ""),
                "base64_attachments": [],
                "urls": params.get("attachment_urls", []),
            })

        if action == "receive":
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{base}/v1/receive/{number}")
            if r.status_code == 200:
                return self.ok({"messages": r.json()})
            return self.err(f"signal-cli {r.status_code}: {r.text[:200]}")

        if action == "list_groups":
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{base}/v1/groups/{number}")
            if r.status_code == 200:
                groups = r.json()
                return self.ok({"groups": groups, "count": len(groups)})
            return self.err(f"signal-cli {r.status_code}")

        if action == "list_contacts":
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{base}/v1/contacts/{number}")
            if r.status_code == 200:
                return self.ok({"contacts": r.json()})
            return self.err(f"signal-cli {r.status_code}")

        if action == "send_reaction":
            return await self._post(f"{base}/v1/reactions/{number}", {
                "recipient": params["recipient"],
                "reaction": params["emoji"],
                "target_author": params["target_author"],
                "timestamp": int(params["timestamp"]),
            })

        return self.err(f"Unknown action: {action}")

    async def _post(self, url: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body)
        if r.status_code in (200, 201):
            try:
                return self.ok(r.json() if r.text.strip() else {})
            except Exception:
                return self.ok({"raw": r.text[:500]})
        return self.err(f"signal-cli {r.status_code}: {r.text[:200]}")
