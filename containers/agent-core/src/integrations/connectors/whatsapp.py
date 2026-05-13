"""WhatsApp connector — Meta Cloud API (WhatsApp Business Platform).

Uses the official Meta Cloud API — no Baileys/Node.js required.
Requires a verified Meta Business account with a WhatsApp Business phone number.

Secrets:
    access_token        — Permanent or temporary system user access token
    phone_number_id     — WhatsApp Business phone number ID (from Meta dashboard)

Actions:
    send_text       — Send text message to a recipient               (MEDIUM)
    send_template   — Send approved template message                  (MEDIUM)
    send_image      — Send image via URL                             (MEDIUM)
    send_audio      — Send audio file via URL                        (MEDIUM)
    send_document   — Send document via URL                          (MEDIUM)
    send_reaction   — React to a message                             (LOW)
    mark_as_read    — Mark a message as read                         (LOW)
    get_business_profile — Get WhatsApp Business profile             (LOW)
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_API = "https://graph.facebook.com/v19.0"
_TIMEOUT = 15.0


class WhatsAppConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="whatsapp", version="1.0.0", name="WhatsApp", category="chat",
            description="Send messages via WhatsApp Business Platform (Meta Cloud API).",
            capabilities=["send_text", "send_templates", "send_media", "message_reactions", "read_receipts"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["access_token", "phone_number_id"],
            config_schema={},
            rate_limits={
                "send_text":             RateLimit(requests_per_minute=80),
                "send_template":         RateLimit(requests_per_minute=80),
                "send_image":            RateLimit(requests_per_minute=40),
                "send_audio":            RateLimit(requests_per_minute=40),
                "send_document":         RateLimit(requests_per_minute=40),
                "send_reaction":         RateLimit(requests_per_minute=60),
                "mark_as_read":          RateLimit(requests_per_minute=80),
                "get_business_profile":  RateLimit(requests_per_minute=10),
            },
            actions=[
                ActionSpec(id="send_text", description="Send a text message to a WhatsApp number",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("to", "string", "Recipient phone number in E.164 format (e.g. +56912345678)", required=True),
                        ParamSpec("body", "string", "Message text (max 4096 chars)", required=True),
                        ParamSpec("preview_url", "boolean", "Enable URL preview", required=False),
                    ]),
                ActionSpec(id="send_template", description="Send a pre-approved template message",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("to", "string", "Recipient phone in E.164 format", required=True),
                        ParamSpec("template_name", "string", "Approved template name", required=True),
                        ParamSpec("language_code", "string", "Template language code (e.g. en_US)", required=True),
                        ParamSpec("components", "array", "Template components/variables (JSON array)", required=False),
                    ]),
                ActionSpec(id="send_image", description="Send an image to a WhatsApp number",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("to", "string", "Recipient phone in E.164 format", required=True),
                        ParamSpec("image_url", "string", "Public URL of the image", required=True),
                        ParamSpec("caption", "string", "Optional caption text", required=False),
                    ]),
                ActionSpec(id="send_audio", description="Send an audio file to a WhatsApp number",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("to", "string", "Recipient phone in E.164 format", required=True),
                        ParamSpec("audio_url", "string", "Public URL of the audio (MP3/OGG/AAC)", required=True),
                    ]),
                ActionSpec(id="send_document", description="Send a document to a WhatsApp number",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("to", "string", "Recipient phone in E.164 format", required=True),
                        ParamSpec("document_url", "string", "Public URL of the document", required=True),
                        ParamSpec("filename", "string", "Optional display filename", required=False),
                        ParamSpec("caption", "string", "Optional caption", required=False),
                    ]),
                ActionSpec(id="send_reaction", description="React to a message with an emoji",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("to", "string", "Recipient phone in E.164 format", required=True),
                        ParamSpec("message_id", "string", "Message ID to react to", required=True),
                        ParamSpec("emoji", "string", "Emoji character to use as reaction", required=True),
                    ]),
                ActionSpec(id="mark_as_read", description="Mark a received message as read",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("message_id", "string", "Message ID to mark as read", required=True)]),
                ActionSpec(id="get_business_profile", description="Get WhatsApp Business profile info",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
            ],
            homepage="https://business.whatsapp.com",
            docs_url="https://developers.facebook.com/docs/whatsapp/cloud-api",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        token    = secrets.get("access_token", "")
        phone_id = secrets.get("phone_number_id", "")
        if not token or not phone_id:
            return self.err("access_token and phone_number_id are required")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        base_url = f"{_API}/{phone_id}"

        if action == "send_text":
            body: dict[str, Any] = {
                "messaging_product": "whatsapp", "to": params["to"],
                "type": "text", "text": {"body": params["body"]},
            }
            if params.get("preview_url"):
                body["text"]["preview_url"] = True
            return await self._post(f"{base_url}/messages", headers, body)

        if action == "send_template":
            body = {
                "messaging_product": "whatsapp", "to": params["to"],
                "type": "template",
                "template": {
                    "name": params["template_name"],
                    "language": {"code": params["language_code"]},
                },
            }
            if params.get("components"):
                body["template"]["components"] = params["components"]
            return await self._post(f"{base_url}/messages", headers, body)

        if action == "send_image":
            body = {
                "messaging_product": "whatsapp", "to": params["to"],
                "type": "image", "image": {"link": params["image_url"]},
            }
            if params.get("caption"):
                body["image"]["caption"] = params["caption"]
            return await self._post(f"{base_url}/messages", headers, body)

        if action == "send_audio":
            return await self._post(f"{base_url}/messages", headers, {
                "messaging_product": "whatsapp", "to": params["to"],
                "type": "audio", "audio": {"link": params["audio_url"]},
            })

        if action == "send_document":
            doc: dict[str, Any] = {"link": params["document_url"]}
            if params.get("filename"): doc["filename"] = params["filename"]
            if params.get("caption"):  doc["caption"]  = params["caption"]
            return await self._post(f"{base_url}/messages", headers, {
                "messaging_product": "whatsapp", "to": params["to"],
                "type": "document", "document": doc,
            })

        if action == "send_reaction":
            return await self._post(f"{base_url}/messages", headers, {
                "messaging_product": "whatsapp", "to": params["to"],
                "type": "reaction", "reaction": {"message_id": params["message_id"], "emoji": params["emoji"]},
            })

        if action == "mark_as_read":
            return await self._post(f"{base_url}/messages", headers, {
                "messaging_product": "whatsapp", "status": "read",
                "message_id": params["message_id"],
            })

        if action == "get_business_profile":
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{base_url}/whatsapp_business_profile", headers=headers)
            d = r.json()
            if r.status_code == 200:
                return self.ok(d.get("data", d))
            return self.err(f"WhatsApp API {r.status_code}: {r.text[:200]}")

        return self.err(f"Unknown action: {action}")

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body, headers=headers)
        d = r.json()
        if r.status_code == 200:
            return self.ok(d)
        return self.err(f"WhatsApp API {r.status_code}: {d.get('error', {}).get('message', r.text[:200])}")
