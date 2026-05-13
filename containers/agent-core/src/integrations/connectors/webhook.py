"""Generic configurable outbound webhook connector.

Useful for integrating with any HTTP endpoint that accepts JSON/form payloads,
without needing a dedicated connector.

Secrets (stored in vault):
    url             — Default webhook URL
    auth_header     — Optional: value of the Authorization header
    hmac_secret     — Optional: HMAC-SHA256 signing secret (sent as X-Signature-256)

Actions:
    post_json   — POST a JSON payload to the configured URL            (MEDIUM)
    post_form   — POST form-encoded data                               (MEDIUM)
    get         — GET request with optional query parameters           (LOW)
    custom      — Any method, URL, headers, and body                   (HIGH)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import structlog

from ..base import (
    ActionSpec, BaseConnector, ConnectorManifest,
    ParamSpec, RateLimit, RiskLevel,
)

logger = structlog.get_logger()
_TIMEOUT = 20.0
_MAX_RESPONSE_BYTES = 8192


class WebhookConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "webhook",
            version     = "1.0.0",
            name        = "Webhook",
            category    = "tools",
            description = "Generic outbound HTTP webhook — POST JSON/form or GET any endpoint.",
            capabilities = [
                "http_post_json",
                "http_post_form",
                "http_get",
                "http_custom",
                "hmac_signing",
            ],
            risk_level       = RiskLevel.HIGH,
            required_secrets = ["url"],
            config_schema    = {},
            rate_limits      = {
                "post_json": RateLimit(requests_per_minute=60),
                "post_form": RateLimit(requests_per_minute=60),
                "get":       RateLimit(requests_per_minute=60),
                "custom":    RateLimit(requests_per_minute=20),
            },
            actions = [
                ActionSpec(
                    id="post_json", description="POST a JSON payload to the webhook URL",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("payload",    "object", "JSON payload",                      required=False),
                        ParamSpec("url",        "string", "Override webhook URL",               required=False),
                        ParamSpec("extra_headers","object","Additional HTTP headers",           required=False),
                    ],
                ),
                ActionSpec(
                    id="post_form", description="POST form-encoded data to the webhook URL",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("data",    "object", "Form field key-value pairs",    required=True),
                        ParamSpec("url",     "string", "Override webhook URL",           required=False),
                    ],
                ),
                ActionSpec(
                    id="get", description="GET request with optional query parameters",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("url",    "string", "Override webhook URL or full URL", required=False),
                        ParamSpec("params", "object", "Query string parameters",          required=False),
                    ],
                ),
                ActionSpec(
                    id="custom", description="Fully custom HTTP request (any method, URL, headers, body)",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("method",  "string", "HTTP method (GET/POST/PUT/PATCH/DELETE)", required=True),
                        ParamSpec("url",     "string", "Full target URL",                         required=True),
                        ParamSpec("headers", "object", "HTTP headers",                            required=False),
                        ParamSpec("body",    "object", "JSON body",                               required=False),
                        ParamSpec("form",    "object", "Form-encoded body (alternative to json)",  required=False),
                    ],
                ),
            ],
            homepage = "https://en.wikipedia.org/wiki/Webhook",
            docs_url = "https://en.wikipedia.org/wiki/Webhook",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "post_json": return await self._post_json(params, secrets)
        if action == "post_form": return await self._post_form(params, secrets)
        if action == "get":       return await self._get(params, secrets)
        if action == "custom":    return await self._custom(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    def _build_headers(self, secrets: dict, extra: dict | None = None) -> dict:
        headers: dict[str, str] = {}
        if secrets.get("auth_header"):
            headers["Authorization"] = secrets["auth_header"]
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _sign(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def _truncate(self, text: str) -> str:
        return text[:_MAX_RESPONSE_BYTES] if len(text) > _MAX_RESPONSE_BYTES else text

    async def _post_json(self, p: dict, secrets: dict) -> dict:
        url = p.get("url") or secrets.get("url", "")
        if not url:
            return self.err("url not configured")
        payload = p.get("payload") or {}
        headers = self._build_headers(secrets, p.get("extra_headers"))
        body_bytes = json.dumps(payload).encode()
        if secrets.get("hmac_secret"):
            headers["X-Signature-256"] = self._sign(body_bytes, secrets["hmac_secret"])
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, content=body_bytes, headers={**headers, "Content-Type": "application/json"})
        return self.ok({"status_code": r.status_code, "response": self._truncate(r.text), "url": url})

    async def _post_form(self, p: dict, secrets: dict) -> dict:
        url = p.get("url") or secrets.get("url", "")
        if not url:
            return self.err("url not configured")
        data = p.get("data") or {}
        headers = self._build_headers(secrets)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, data={str(k): str(v) for k, v in data.items()}, headers=headers)
        return self.ok({"status_code": r.status_code, "response": self._truncate(r.text), "url": url})

    async def _get(self, p: dict, secrets: dict) -> dict:
        url = p.get("url") or secrets.get("url", "")
        if not url:
            return self.err("url not configured")
        headers = self._build_headers(secrets)
        qp = p.get("params") or {}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, params=qp, headers=headers)
        return self.ok({"status_code": r.status_code, "response": self._truncate(r.text), "url": url})

    async def _custom(self, p: dict, secrets: dict) -> dict:
        method = (p.get("method") or "GET").upper()
        url = p.get("url") or secrets.get("url", "")
        if not url:
            return self.err("url not configured")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            return self.err(f"Unsupported HTTP method: {method}")
        headers = dict(p.get("headers") or {})
        if secrets.get("auth_header") and "Authorization" not in headers:
            headers["Authorization"] = secrets["auth_header"]
        kwargs: dict[str, Any] = {}
        if p.get("body"):  kwargs["json"] = p["body"]
        if p.get("form"):  kwargs["data"] = {str(k): str(v) for k, v in p["form"].items()}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.request(method, url, headers=headers, **kwargs)
        return self.ok({"status_code": r.status_code, "response": self._truncate(r.text), "url": url, "method": method})
