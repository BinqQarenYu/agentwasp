"""Home Assistant connector — REST API.

Secrets (stored in vault):
    base_url      — Home Assistant base URL (e.g. http://homeassistant.local:8123)
    token         — Long-Lived Access Token

Actions:
    get_states      — List all entity states                           (LOW)
    get_state       — Get state for a specific entity                  (LOW)
    call_service    — Call a HA service (e.g. light.turn_on)           (HIGH)
    fire_event      — Fire a custom event on the HA event bus          (MEDIUM)
    get_history     — Get state history for an entity                  (LOW)
    render_template — Render a Jinja2 template                         (LOW)
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


class HomeAssistantConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "home-assistant",
            version     = "1.0.0",
            name        = "Home Assistant",
            category    = "smart_home",
            description = "Control and monitor Home Assistant entities and automations via REST API.",
            capabilities = [
                "read_entity_states",
                "call_services",
                "fire_events",
                "get_history",
                "render_templates",
            ],
            risk_level       = RiskLevel.HIGH,
            required_secrets = ["base_url", "token"],
            config_schema    = {},
            rate_limits      = {
                "get_states":       RateLimit(requests_per_minute=30),
                "get_state":        RateLimit(requests_per_minute=60),
                "call_service":     RateLimit(requests_per_minute=30),
                "fire_event":       RateLimit(requests_per_minute=20),
                "get_history":      RateLimit(requests_per_minute=10),
                "render_template":  RateLimit(requests_per_minute=30),
            },
            actions = [
                ActionSpec(
                    id="get_states", description="List all entity states from Home Assistant",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("domain", "string", "Filter by domain (e.g. light, switch, sensor)", required=False),
                        ParamSpec("limit",  "integer","Max entities to return (default 50)",           required=False),
                    ],
                ),
                ActionSpec(
                    id="get_state", description="Get current state for a specific entity",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("entity_id", "string", "Entity ID (e.g. light.living_room)", required=True),
                    ],
                ),
                ActionSpec(
                    id="call_service", description="Call a Home Assistant service (e.g. turn on a light)",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("domain",      "string", "Service domain (e.g. light, switch, script)", required=True),
                        ParamSpec("service",     "string", "Service name (e.g. turn_on, turn_off)",       required=True),
                        ParamSpec("entity_id",   "string", "Target entity ID",                            required=False),
                        ParamSpec("service_data","object", "Additional service call data",                required=False),
                    ],
                ),
                ActionSpec(
                    id="fire_event", description="Fire a custom event on the Home Assistant event bus",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("event_type", "string", "Event type name",                       required=True),
                        ParamSpec("event_data", "object", "Event data payload (optional)",          required=False),
                    ],
                ),
                ActionSpec(
                    id="get_history", description="Get state history for a specific entity",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("entity_id",   "string", "Entity ID",                                   required=True),
                        ParamSpec("hours",        "integer","How many hours of history (default 24)",      required=False),
                        ParamSpec("minimal",      "boolean","Return minimal response (state+time only)",   required=False),
                    ],
                ),
                ActionSpec(
                    id="render_template", description="Render a Jinja2 template using HA data",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("template", "string", "Jinja2 template string", required=True),
                    ],
                ),
            ],
            homepage = "https://www.home-assistant.io",
            docs_url = "https://developers.home-assistant.io/docs/api/rest/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "get_states":      return await self._get_states(params, secrets)
        if action == "get_state":       return await self._get_state(params, secrets)
        if action == "call_service":    return await self._call_service(params, secrets)
        if action == "fire_event":      return await self._fire_event(params, secrets)
        if action == "get_history":     return await self._get_history(params, secrets)
        if action == "render_template": return await self._render_template(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    def _url(self, secrets: dict, path: str) -> str:
        base = secrets["base_url"].rstrip("/")
        return f"{base}/api/{path.lstrip('/')}"

    def _headers(self, secrets: dict) -> dict:
        return {
            "Authorization": f"Bearer {secrets['token']}",
            "Content-Type":  "application/json",
        }

    async def _req(self, method: str, path: str, secrets: dict, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            return await c.request(method, self._url(secrets, path), headers=self._headers(secrets), **kwargs)

    async def _get_states(self, p: dict, secrets: dict) -> dict:
        r = await self._req("GET", "/states", secrets)
        if r.status_code != 200:
            return self.err(f"HA {r.status_code}: {r.text[:200]}")
        states = r.json()
        domain_filter = p.get("domain", "").lower()
        if domain_filter:
            states = [s for s in states if s["entity_id"].startswith(f"{domain_filter}.")]
        limit = min(int(p.get("limit") or 50), 500)
        states = states[:limit]
        return self.ok({
            "states": [
                {"entity_id": s["entity_id"], "state": s["state"],
                 "friendly_name": s.get("attributes", {}).get("friendly_name", "")}
                for s in states
            ],
            "count": len(states),
        })

    async def _get_state(self, p: dict, secrets: dict) -> dict:
        entity_id = p.get("entity_id", "")
        if not entity_id:
            return self.err("entity_id is required")
        r = await self._req("GET", f"/states/{entity_id}", secrets)
        if r.status_code == 200:
            d = r.json()
            return self.ok({
                "entity_id":      d["entity_id"],
                "state":          d["state"],
                "attributes":     d.get("attributes", {}),
                "last_changed":   d.get("last_changed"),
                "last_updated":   d.get("last_updated"),
            })
        if r.status_code == 404:
            return self.err(f"Entity '{entity_id}' not found")
        return self.err(f"HA {r.status_code}")

    async def _call_service(self, p: dict, secrets: dict) -> dict:
        domain  = p.get("domain", "")
        service = p.get("service", "")
        if not domain or not service:
            return self.err("domain and service are required")
        body: dict[str, Any] = dict(p.get("service_data") or {})
        if p.get("entity_id"):
            body["entity_id"] = p["entity_id"]
        r = await self._req("POST", f"/services/{domain}/{service}", secrets, json=body)
        if r.status_code in (200, 201):
            return self.ok({"called": f"{domain}.{service}", "entity_id": p.get("entity_id")})
        return self.err(f"HA {r.status_code}: {r.text[:300]}")

    async def _fire_event(self, p: dict, secrets: dict) -> dict:
        event_type = p.get("event_type", "")
        if not event_type:
            return self.err("event_type is required")
        body = p.get("event_data") or {}
        r = await self._req("POST", f"/events/{event_type}", secrets, json=body)
        if r.status_code in (200, 201):
            return self.ok({"fired": event_type})
        return self.err(f"HA {r.status_code}")

    async def _get_history(self, p: dict, secrets: dict) -> dict:
        from datetime import datetime, timedelta, timezone
        entity_id = p.get("entity_id", "")
        if not entity_id:
            return self.err("entity_id is required")
        hours = min(int(p.get("hours") or 24), 168)
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        path = f"/history/period/{start}?filter_entity_id={entity_id}&minimal_response=true"
        r = await self._req("GET", path, secrets)
        if r.status_code == 200:
            history = r.json()
            states = history[0] if history else []
            return self.ok({"entity_id": entity_id, "states": states[:100], "count": len(states)})
        return self.err(f"HA {r.status_code}")

    async def _render_template(self, p: dict, secrets: dict) -> dict:
        template = p.get("template", "")
        if not template:
            return self.err("template is required")
        r = await self._req("POST", "/template", secrets, json={"template": template})
        if r.status_code == 200:
            return self.ok({"result": r.text.strip()})
        return self.err(f"HA {r.status_code}: {r.text[:200]}")
