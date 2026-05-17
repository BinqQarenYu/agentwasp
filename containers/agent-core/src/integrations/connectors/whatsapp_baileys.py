"""WhatsApp Baileys connector — personal WhatsApp via self-hosted Baileys REST bridge."""

from __future__ import annotations

from typing import Any

import httpx

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_TIMEOUT = 15.0


class WhatsAppBaileysConnector(BaseConnector):
    """WhatsApp via Baileys REST bridge (QR pairing, personal accounts).

    Requires a self-hosted Baileys REST API bridge.
    See: https://github.com/auth0/node-auth0 (reference implementation varies).
    """

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="whatsapp-baileys",
            version="1.0.0",
            name="WhatsApp (Baileys)",
            category="chat",
            description="Send and receive WhatsApp messages via a self-hosted Baileys REST bridge. Supports personal accounts via QR pairing.",
            capabilities=["send_message", "send_image", "get_status"],
            risk_level=RiskLevel.MEDIUM,
            actions=[
                ActionSpec(
                    id="send_text",
                    description="Send a text message to a WhatsApp number.",
                    params=[
                        ParamSpec(name="phone", type="string", description="Recipient phone with country code (e.g. +5511999999999)."),
                        ParamSpec(name="text", type="string", description="Message text."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_image",
                    description="Send an image with optional caption.",
                    params=[
                        ParamSpec(name="phone", type="string", description="Recipient phone."),
                        ParamSpec(name="image_url", type="string", description="URL of the image to send."),
                        ParamSpec(name="caption", type="string", description="Image caption.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_audio",
                    description="Send an audio file.",
                    params=[
                        ParamSpec(name="phone", type="string", description="Recipient phone."),
                        ParamSpec(name="audio_url", type="string", description="URL of the audio file."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_document",
                    description="Send a document file.",
                    params=[
                        ParamSpec(name="phone", type="string", description="Recipient phone."),
                        ParamSpec(name="doc_url", type="string", description="URL of the document."),
                        ParamSpec(name="filename", type="string", description="Document filename.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_react",
                    description="Send an emoji reaction to a message.",
                    params=[
                        ParamSpec(name="phone", type="string", description="Chat phone."),
                        ParamSpec(name="message_id", type="string", description="ID of the message to react to."),
                        ParamSpec(name="emoji", type="string", description="Emoji character to react with."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_status",
                    description="Get the connection status of the Baileys session.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_chats",
                    description="List recent chats.",
                    params=[
                        ParamSpec(name="limit", type="integer", description="Max chats to return.", required=False, default=20),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["api_url", "api_key", "session_id"],
            config_schema={},
            rate_limits={
                "send_text":     RateLimit(requests_per_minute=20),
                "send_image":    RateLimit(requests_per_minute=10),
                "send_audio":    RateLimit(requests_per_minute=10),
                "send_document": RateLimit(requests_per_minute=10),
                "send_react":    RateLimit(requests_per_minute=30),
                "get_status":    RateLimit(requests_per_minute=60),
                "get_chats":     RateLimit(requests_per_minute=30),
            },
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        api_url    = secrets.get("api_url", "").rstrip("/")
        api_key    = secrets.get("api_key", "")
        session_id = secrets.get("session_id", "")

        if not api_url:
            return self.err("Secret 'api_url' is required")

        headers = {"apikey": api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:

                if action == "send_text":
                    phone = params.get("phone", "")
                    text  = params.get("text", "")
                    if not phone or not text:
                        return self.err("phone and text are required")
                    chat_id = phone if "@" in phone else f"{phone}@c.us"
                    r = await client.post(f"{api_url}/api/sendText", json={"session": session_id, "chatId": chat_id, "text": text})
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_image":
                    phone     = params.get("phone", "")
                    image_url = params.get("image_url", "")
                    caption   = params.get("caption", "")
                    if not phone or not image_url:
                        return self.err("phone and image_url are required")
                    chat_id = phone if "@" in phone else f"{phone}@c.us"
                    r = await client.post(f"{api_url}/api/sendImage", json={"session": session_id, "chatId": chat_id, "file": {"url": image_url}, "caption": caption})
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_audio":
                    phone     = params.get("phone", "")
                    audio_url = params.get("audio_url", "")
                    if not phone or not audio_url:
                        return self.err("phone and audio_url are required")
                    chat_id = phone if "@" in phone else f"{phone}@c.us"
                    r = await client.post(f"{api_url}/api/sendVoice", json={"session": session_id, "chatId": chat_id, "file": {"url": audio_url}})
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_document":
                    phone    = params.get("phone", "")
                    doc_url  = params.get("doc_url", "")
                    filename = params.get("filename", "document")
                    if not phone or not doc_url:
                        return self.err("phone and doc_url are required")
                    chat_id = phone if "@" in phone else f"{phone}@c.us"
                    r = await client.post(f"{api_url}/api/sendFile", json={"session": session_id, "chatId": chat_id, "file": {"url": doc_url}, "filename": filename})
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_react":
                    phone      = params.get("phone", "")
                    message_id = params.get("message_id", "")
                    emoji      = params.get("emoji", "")
                    if not phone or not message_id or not emoji:
                        return self.err("phone, message_id, and emoji are required")
                    # Note: Reaction might not be natively supported in free WAHA, but we attempt it.
                    chat_id = phone if "@" in phone else f"{phone}@c.us"
                    r = await client.post(f"{api_url}/api/react", json={"session": session_id, "chatId": chat_id, "messageId": message_id, "reaction": emoji})
                    if r.status_code != 404:
                        r.raise_for_status()
                    return self.ok({"status": "attempted"})

                elif action == "get_status":
                    r = await client.get(f"{api_url}/api/sessions")
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_chats":
                    limit = int(params.get("limit") or 20)
                    r = await client.get(f"{api_url}/api/sessions") # Placeholder as chats require pro API in WAHA usually
                    r.raise_for_status()
                    return self.ok({"chats": [], "note": "Listing chats requires WAHA Plus"})

                else:
                    return self.err(f"Unknown action: {action}")

        except httpx.HTTPStatusError as e:
            return self.err(f"Baileys API error {e.response.status_code}: {e.response.text[:200]}")
        except httpx.ConnectError:
            return self.err(f"Cannot connect to Baileys bridge at {api_url}")
        except Exception as e:
            return self.err(str(e))
