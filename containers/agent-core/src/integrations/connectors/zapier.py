"""Zapier connector — webhook triggers + action invocation.

Zapier acts as the universal extension layer: any Zapier-supported app
can be bridged to WASP without writing a new connector.

Secrets (stored in vault):
    webhook_url      — Zapier "Webhooks by Zapier" (Catch Hook) URL for sending
    shared_secret    — Optional HMAC secret for verifying inbound webhooks

Actions:
    trigger  — POST payload to a Zapier webhook URL           (MEDIUM risk)
    test     — POST a test payload to verify connectivity     (LOW risk)
    multi    — POST to multiple webhook URLs (comma-separated)(MEDIUM risk)
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


class ZapierConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "zapier",
            version     = "1.0.0",
            name        = "Zapier",
            category    = "tools",
            description = (
                "Universal automation layer. Trigger Zapier workflows from WASP "
                "or receive Zapier events via inbound webhook."
            ),
            capabilities = [
                "trigger_webhook",
                "multi_webhook",
                "inbound_trigger_processing",
            ],
            risk_level    = RiskLevel.MEDIUM,
            required_secrets = ["webhook_url"],
            config_schema = {
                "type": "object",
                "properties": {
                    "timeout_seconds": {"type": "integer", "default": 15},
                },
            },
            rate_limits = {
                "trigger": RateLimit(requests_per_minute=30),
                "multi":   RateLimit(requests_per_minute=10),
                "test":    RateLimit(requests_per_minute=5),
            },
            actions = [
                ActionSpec(
                    id          = "trigger",
                    description = "POST a JSON payload to the configured Zapier webhook URL",
                    risk_level  = RiskLevel.MEDIUM,
                    capability  = "controlled",
                    params      = [
                        ParamSpec("payload",      "object", "JSON payload to send",      required=False),
                        ParamSpec("message",      "string", "Optional message field",    required=False),
                        ParamSpec("webhook_url",  "string", "Override default webhook URL", required=False),
                    ],
                ),
                ActionSpec(
                    id          = "test",
                    description = "Send a test payload to verify the webhook is reachable",
                    risk_level  = RiskLevel.LOW,
                    capability  = "monitored",
                    params      = [],
                ),
                ActionSpec(
                    id          = "multi",
                    description = "POST payload to multiple webhook URLs (comma-separated in params.urls)",
                    risk_level  = RiskLevel.MEDIUM,
                    capability  = "controlled",
                    params      = [
                        ParamSpec("urls",    "string", "Comma-separated list of webhook URLs", required=True),
                        ParamSpec("payload", "object", "JSON payload to send",                 required=False),
                    ],
                ),
            ],
            homepage = "https://zapier.com",
            docs_url = "https://zapier.com/help/doc/how-to-use-webhooks-in-zapier",
        )

    async def health_check(self) -> bool:
        # Zapier webhooks are fire-and-forget; we can only validate the URL format
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "trigger":
            return await self._trigger(params, secrets)
        if action == "test":
            return await self._test(secrets)
        if action == "multi":
            return await self._multi(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    async def _trigger(self, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        url = params.get("webhook_url") or secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured and not provided in params")

        payload: dict[str, Any] = dict(params.get("payload") or {})
        if params.get("message"):
            payload["message"] = params["message"]
        if not payload:
            payload = {"triggered_by": "wasp", "action": "trigger"}

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)

        return self.ok({
            "status_code": resp.status_code,
            "response":    resp.text[:500],
            "url":         url,
        })

    async def _test(self, secrets: dict[str, str]) -> dict[str, Any]:
        url = secrets.get("webhook_url", "")
        if not url:
            return self.err("webhook_url not configured")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json={"test": True, "source": "wasp"})

        ok = resp.status_code < 400
        return {
            "ok":          ok,
            "data":        {"status_code": resp.status_code, "response": resp.text[:200]},
            "error":       None if ok else f"HTTP {resp.status_code}",
        }

    async def _multi(self, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        urls_raw = params.get("urls", "")
        urls     = [u.strip() for u in urls_raw.split(",") if u.strip()]
        if not urls:
            return self.err("No URLs provided")

        payload = dict(params.get("payload") or {"triggered_by": "wasp"})
        results = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for url in urls[:10]:   # cap at 10
                try:
                    resp = await client.post(url, json=payload)
                    results.append({"url": url, "status": resp.status_code})
                except Exception as exc:
                    results.append({"url": url, "error": str(exc)})

        return self.ok({"results": results, "sent": len(results)})
