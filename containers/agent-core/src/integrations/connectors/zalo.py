"""Zalo Official Account connector — Vietnamese Zalo OA API."""

from __future__ import annotations

from typing import Any

import httpx

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

_TIMEOUT = 15.0
_BASE_URL = "https://openapi.zalo.me/v2.0/oa"


class ZaloConnector(BaseConnector):
    """Zalo Official Account API (for Zalo OA business accounts)."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="zalo",
            version="1.0.0",
            name="Zalo",
            category="chat",
            description="Send messages and manage followers via the Zalo Official Account API. Requires a Zalo OA account and access token.",
            capabilities=["send_message", "get_profile", "broadcast"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="send_text",
                    description="Send a text message to a Zalo user.",
                    params=[
                        ParamSpec(name="user_id", type="string", description="Zalo user ID."),
                        ParamSpec(name="text", type="string", description="Message text."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_image",
                    description="Send an image message to a Zalo user.",
                    params=[
                        ParamSpec(name="user_id", type="string", description="Zalo user ID."),
                        ParamSpec(name="image_url", type="string", description="URL of the image."),
                        ParamSpec(name="caption", type="string", description="Image caption.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_file",
                    description="Send a file to a Zalo user.",
                    params=[
                        ParamSpec(name="user_id", type="string", description="Zalo user ID."),
                        ParamSpec(name="file_url", type="string", description="URL of the file."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_profile",
                    description="Get a Zalo user's public profile.",
                    params=[
                        ParamSpec(name="user_id", type="string", description="Zalo user ID."),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_followers",
                    description="Get a paginated list of OA followers.",
                    params=[
                        ParamSpec(name="offset", type="integer", description="Pagination offset.", required=False, default=0),
                        ParamSpec(name="count", type="integer", description="Number of followers to return (max 50).", required=False, default=50),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="send_broadcast",
                    description="Send a promotional broadcast message to multiple users. HIGH risk — sends to all targets.",
                    params=[
                        ParamSpec(name="text", type="string", description="Broadcast message text."),
                        ParamSpec(name="target_user_ids", type="array", description="List of Zalo user IDs to target."),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_oa_info",
                    description="Get information about the Official Account.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["app_id", "secret_key", "access_token"],
            config_schema={},
            rate_limits={
                "send_text":      RateLimit(requests_per_minute=20),
                "send_image":     RateLimit(requests_per_minute=10),
                "send_file":      RateLimit(requests_per_minute=10),
                "get_profile":    RateLimit(requests_per_minute=30),
                "get_followers":  RateLimit(requests_per_minute=20),
                "send_broadcast": RateLimit(requests_per_minute=5, requests_per_hour=20),
                "get_oa_info":    RateLimit(requests_per_minute=30),
            },
            homepage="https://developers.zalo.me",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        access_token = secrets.get("access_token", "")
        if not access_token:
            return self.err("Secret 'access_token' is required")

        headers = {
            "access_token":  access_token,
            "Content-Type":  "application/json",
        }

        try:
            async with httpx.AsyncClient(base_url=_BASE_URL, headers=headers, timeout=_TIMEOUT) as client:

                if action == "send_text":
                    user_id = params.get("user_id", "")
                    text    = params.get("text", "")
                    if not user_id or not text:
                        return self.err("user_id and text are required")
                    r = await client.post("/message", json={
                        "recipient": {"user_id": user_id},
                        "message":   {"text": text},
                    })
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_image":
                    user_id   = params.get("user_id", "")
                    image_url = params.get("image_url", "")
                    caption   = params.get("caption", "")
                    if not user_id or not image_url:
                        return self.err("user_id and image_url are required")
                    r = await client.post("/message", json={
                        "recipient": {"user_id": user_id},
                        "message": {
                            "attachment": {
                                "type":    "template",
                                "payload": {"template_type": "media", "elements": [{"media_type": "image", "url": image_url, "caption": caption}]},
                            }
                        },
                    })
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_file":
                    user_id  = params.get("user_id", "")
                    file_url = params.get("file_url", "")
                    if not user_id or not file_url:
                        return self.err("user_id and file_url are required")
                    r = await client.post("/message", json={
                        "recipient": {"user_id": user_id},
                        "message": {
                            "attachment": {
                                "type":    "file",
                                "payload": {"url": file_url},
                            }
                        },
                    })
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_profile":
                    user_id = params.get("user_id", "")
                    if not user_id:
                        return self.err("user_id is required")
                    import json as _json
                    r = await client.get("/getprofile", params={"data": _json.dumps({"user_id": user_id})})
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "get_followers":
                    offset = int(params.get("offset") or 0)
                    count  = min(int(params.get("count") or 50), 50)
                    import json as _json
                    r = await client.get("/getfollowers", params={"data": _json.dumps({"offset": offset, "count": count})})
                    r.raise_for_status()
                    return self.ok(r.json())

                elif action == "send_broadcast":
                    text            = params.get("text", "")
                    target_user_ids = params.get("target_user_ids", [])
                    if not text or not target_user_ids:
                        return self.err("text and target_user_ids are required")
                    results = []
                    for uid in target_user_ids[:100]:  # safety cap
                        r = await client.post("/message", json={
                            "recipient": {"user_id": uid},
                            "message":   {"text": text},
                        })
                        results.append({"user_id": uid, "status": r.status_code, "ok": r.is_success})
                    return self.ok({"sent": len(results), "results": results})

                elif action == "get_oa_info":
                    r = await client.get("/info")
                    r.raise_for_status()
                    return self.ok(r.json())

                else:
                    return self.err(f"Unknown action: {action}")

        except httpx.HTTPStatusError as e:
            return self.err(f"Zalo API error {e.response.status_code}: {e.response.text[:200]}")
        except httpx.ConnectError:
            return self.err("Cannot connect to Zalo API")
        except Exception as e:
            return self.err(str(e))
