"""Philips Hue connector — local bridge HTTP API control.

Uses the Philips Hue Local REST API (no cloud subscription needed).
Requires the bridge IP address and a registered application username/key.

To get a username: press the bridge button, then POST to http://<bridge_ip>/api
with {"devicetype": "wasp#agent"}.

Secrets:
    bridge_ip   — IP address of the Philips Hue bridge (e.g. 192.168.1.2)
    username    — Application key registered with the bridge

Actions:
    list_lights     — List all lights with state                     (LOW)
    get_light       — Get state of a specific light                  (LOW)
    set_light       — Set on/off, brightness, color of a light       (MEDIUM)
    toggle_light    — Toggle a light on/off                          (MEDIUM)
    list_groups     — List all groups/rooms                          (LOW)
    get_group       — Get state of a group/room                      (LOW)
    set_group       — Set state for all lights in a group            (MEDIUM)
    list_scenes     — List available scenes                          (LOW)
    activate_scene  — Activate a scene in a group                    (MEDIUM)
    set_all         — Set state for ALL lights at once               (HIGH)
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_TIMEOUT = 10


class PhilipsHueConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="philips-hue", version="1.0.0", name="Philips Hue", category="smart_home",
            description=(
                "Control Philips Hue smart lights via local bridge API. "
                "No cloud required. Supports individual lights, rooms/groups, and scenes."
            ),
            capabilities=["light_control", "group_control", "scene_activation", "brightness_control", "color_control"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["bridge_ip", "username"],
            config_schema={},
            rate_limits={
                "list_lights":    RateLimit(requests_per_minute=30),
                "get_light":      RateLimit(requests_per_minute=30),
                "set_light":      RateLimit(requests_per_minute=20),
                "toggle_light":   RateLimit(requests_per_minute=20),
                "list_groups":    RateLimit(requests_per_minute=30),
                "get_group":      RateLimit(requests_per_minute=30),
                "set_group":      RateLimit(requests_per_minute=20),
                "list_scenes":    RateLimit(requests_per_minute=20),
                "activate_scene": RateLimit(requests_per_minute=10),
                "set_all":        RateLimit(requests_per_minute=5),
            },
            actions=[
                ActionSpec(id="list_lights", description="List all registered lights with their current state",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_light", description="Get the current state of a specific light",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("light_id", "string", "Light ID (1, 2, 3...)", required=True)]),
                ActionSpec(id="set_light", description="Set light on/off, brightness (1-254), and color (hue 0-65535, sat 0-254)",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("light_id", "string", "Light ID", required=True),
                        ParamSpec("on", "boolean", "Turn light on (true) or off (false)", required=False),
                        ParamSpec("brightness", "integer", "Brightness 1-254", required=False),
                        ParamSpec("hue", "integer", "Color hue 0-65535", required=False),
                        ParamSpec("saturation", "integer", "Color saturation 0-254", required=False),
                        ParamSpec("color_temp", "integer", "Color temperature in mirek (153=coolest, 500=warmest)", required=False),
                        ParamSpec("transition_time", "integer", "Transition time in 100ms units (default 4 = 400ms)", required=False),
                    ]),
                ActionSpec(id="toggle_light", description="Toggle a light on or off",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("light_id", "string", "Light ID to toggle", required=True)]),
                ActionSpec(id="list_groups", description="List all groups (rooms and zones)",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_group", description="Get the current state of a group/room",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("group_id", "string", "Group ID (0 = all lights)", required=True)]),
                ActionSpec(id="set_group", description="Set state for all lights in a group/room",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("group_id", "string", "Group ID", required=True),
                        ParamSpec("on", "boolean", "Turn group on/off", required=False),
                        ParamSpec("brightness", "integer", "Brightness 1-254", required=False),
                        ParamSpec("hue", "integer", "Color hue 0-65535", required=False),
                        ParamSpec("saturation", "integer", "Color saturation 0-254", required=False),
                        ParamSpec("color_temp", "integer", "Color temperature in mirek", required=False),
                    ]),
                ActionSpec(id="list_scenes", description="List all available scenes",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="activate_scene", description="Activate a scene in a group",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("scene_id", "string", "Scene ID (from list_scenes)", required=True),
                        ParamSpec("group_id", "string", "Group ID to activate scene in (default 0 = all)", required=False),
                    ]),
                ActionSpec(id="set_all", description="Set state for ALL lights simultaneously",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("on", "boolean", "Turn all lights on/off", required=False),
                        ParamSpec("brightness", "integer", "Brightness 1-254", required=False),
                        ParamSpec("color_temp", "integer", "Color temperature in mirek", required=False),
                    ]),
            ],
            homepage="https://www.philips-hue.com",
            docs_url="https://developers.meethue.com/develop/hue-api/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        bridge_ip = secrets.get("bridge_ip", "")
        username  = secrets.get("username", "")
        if not bridge_ip or not username:
            return self.err("bridge_ip and username secrets are required")

        base = f"http://{bridge_ip}/api/{username}"

        try:
            if action == "list_lights":    return await self._list_lights(base)
            if action == "get_light":      return await self._get_light(base, params["light_id"])
            if action == "set_light":      return await self._set_light(base, params)
            if action == "toggle_light":   return await self._toggle_light(base, params["light_id"])
            if action == "list_groups":    return await self._list_groups(base)
            if action == "get_group":      return await self._get_group(base, params["group_id"])
            if action == "set_group":      return await self._set_group(base, params)
            if action == "list_scenes":    return await self._list_scenes(base)
            if action == "activate_scene": return await self._activate_scene(base, params)
            if action == "set_all":        return await self._set_all(base, params)
        except Exception as exc:
            logger.error("hue.execute_error", action=action, error=str(exc))
            return self.err(f"Hue error: {exc}")

        return self.err(f"Unknown action: {action}")

    async def _get(self, url: str) -> Any:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url)
            return r.json()

    async def _put(self, url: str, body: dict) -> Any:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(url, json=body)
            return r.json()

    def _fmt_light(self, lid: str, light: dict) -> dict:
        state = light.get("state", {})
        return {
            "id":         lid,
            "name":       light.get("name", ""),
            "type":       light.get("type", ""),
            "on":         state.get("on", False),
            "brightness": state.get("bri", 0),
            "hue":        state.get("hue"),
            "saturation": state.get("sat"),
            "color_temp": state.get("ct"),
            "reachable":  state.get("reachable", False),
        }

    async def _list_lights(self, base: str) -> dict:
        data = await self._get(f"{base}/lights")
        if isinstance(data, list):
            return self.err(str(data))
        lights = [self._fmt_light(k, v) for k, v in data.items()]
        return self.ok({"lights": lights, "count": len(lights)})

    async def _get_light(self, base: str, light_id: str) -> dict:
        data = await self._get(f"{base}/lights/{light_id}")
        if isinstance(data, list):
            return self.err(f"Light {light_id} not found")
        return self.ok(self._fmt_light(light_id, data))

    async def _set_light(self, base: str, params: dict) -> dict:
        light_id = params.get("light_id", "")
        body: dict[str, Any] = {}
        if "on" in params and params["on"] is not None:
            body["on"] = bool(params["on"])
        if params.get("brightness"):
            body["bri"] = max(1, min(254, int(params["brightness"])))
        if params.get("hue") is not None:
            body["hue"] = max(0, min(65535, int(params["hue"])))
        if params.get("saturation") is not None:
            body["sat"] = max(0, min(254, int(params["saturation"])))
        if params.get("color_temp"):
            body["ct"] = max(153, min(500, int(params["color_temp"])))
        if params.get("transition_time") is not None:
            body["transitiontime"] = int(params["transition_time"])
        result = await self._put(f"{base}/lights/{light_id}/state", body)
        return self.ok({"light_id": light_id, "applied": body, "result": result})

    async def _toggle_light(self, base: str, light_id: str) -> dict:
        data       = await self._get(f"{base}/lights/{light_id}")
        current_on = data.get("state", {}).get("on", False)
        result     = await self._put(f"{base}/lights/{light_id}/state", {"on": not current_on})
        return self.ok({"light_id": light_id, "on": not current_on, "result": result})

    async def _list_groups(self, base: str) -> dict:
        data = await self._get(f"{base}/groups")
        if isinstance(data, list):
            return self.err(str(data))
        groups = [
            {
                "id":     gid,
                "name":   g.get("name", ""),
                "type":   g.get("type", ""),
                "lights": g.get("lights", []),
                "on":     g.get("action", {}).get("on", False),
            }
            for gid, g in data.items()
        ]
        return self.ok({"groups": groups, "count": len(groups)})

    async def _get_group(self, base: str, group_id: str) -> dict:
        data   = await self._get(f"{base}/groups/{group_id}")
        if isinstance(data, list):
            return self.err(f"Group {group_id} not found")
        state  = data.get("state", {})
        action = data.get("action", {})
        return self.ok({
            "id":         group_id,
            "name":       data.get("name", ""),
            "type":       data.get("type", ""),
            "lights":     data.get("lights", []),
            "all_on":     state.get("all_on", False),
            "any_on":     state.get("any_on", False),
            "brightness": action.get("bri"),
            "color_temp": action.get("ct"),
        })

    async def _set_group(self, base: str, params: dict) -> dict:
        group_id = params.get("group_id", "")
        body: dict[str, Any] = {}
        if "on" in params and params["on"] is not None:
            body["on"] = bool(params["on"])
        if params.get("brightness"):
            body["bri"] = max(1, min(254, int(params["brightness"])))
        if params.get("hue") is not None:
            body["hue"] = max(0, min(65535, int(params["hue"])))
        if params.get("saturation") is not None:
            body["sat"] = max(0, min(254, int(params["saturation"])))
        if params.get("color_temp"):
            body["ct"] = max(153, min(500, int(params["color_temp"])))
        result = await self._put(f"{base}/groups/{group_id}/action", body)
        return self.ok({"group_id": group_id, "applied": body, "result": result})

    async def _list_scenes(self, base: str) -> dict:
        data = await self._get(f"{base}/scenes")
        if isinstance(data, list):
            return self.err(str(data))
        scenes = [
            {
                "id":     sid,
                "name":   s.get("name", ""),
                "group":  s.get("group", ""),
                "lights": s.get("lights", []),
                "type":   s.get("type", ""),
            }
            for sid, s in data.items()
        ]
        return self.ok({"scenes": scenes, "count": len(scenes)})

    async def _activate_scene(self, base: str, params: dict) -> dict:
        scene_id = params.get("scene_id", "")
        group_id = params.get("group_id") or "0"
        if not scene_id:
            return self.err("scene_id is required")
        result = await self._put(f"{base}/groups/{group_id}/action", {"scene": scene_id})
        return self.ok({"scene_id": scene_id, "group_id": group_id, "result": result})

    async def _set_all(self, base: str, params: dict) -> dict:
        return await self._set_group(base, {**params, "group_id": "0"})
