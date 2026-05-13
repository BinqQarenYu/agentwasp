"""iOS platform bridge connector — requires WASP companion app on iOS device."""

from __future__ import annotations

from typing import Any

from ..base import ActionSpec, ConnectorManifest, ParamSpec, RateLimit, RiskLevel
from .platform_base import PlatformBridgeConnector

_DEFAULT_BRIDGE_URL = "http://127.0.0.1:37124"

_KNOWN_ACTIONS = {"get_device_info", "take_screenshot", "get_location", "send_shortcut", "list_shortcuts"}


class IOSBridgeConnector(PlatformBridgeConnector):
    """iOS companion bridge — requires WASP iOS companion app installed on the device."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="platform-ios",
            version="1.0.0",
            name="iOS Bridge",
            category="platform",
            description="Control an iOS device via the WASP companion app. Supports screenshots, device info, Shortcuts automation, and location (with permission).",
            capabilities=["platform_screenshot", "platform_shortcut", "platform_info"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="get_device_info",
                    description="Get iOS device info: battery level, iOS version, device model.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="take_screenshot",
                    description="Capture a screenshot of the iOS device screen.",
                    params=[],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_location",
                    description="Get current GPS coordinates. Requires location permission.",
                    params=[],
                    risk_level=RiskLevel.HIGH,
                    capability="controlled",
                ),
                ActionSpec(
                    id="send_shortcut",
                    description="Run an iOS Shortcut by name. Only shortcuts pre-approved in companion settings.",
                    params=[
                        ParamSpec(name="shortcut_name", type="string", description="Name of the Shortcut to run."),
                        ParamSpec(name="input", type="string", description="Optional input for the Shortcut.", required=False),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="controlled",
                ),
                ActionSpec(
                    id="list_shortcuts",
                    description="List available iOS Shortcuts.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["api_key"],
            config_schema={"bridge_url": {"type": "string", "default": _DEFAULT_BRIDGE_URL}},
            rate_limits={
                "get_device_info": RateLimit(requests_per_minute=30),
                "take_screenshot": RateLimit(requests_per_minute=10),
                "get_location":    RateLimit(requests_per_minute=10),
                "send_shortcut":   RateLimit(requests_per_minute=10),
                "list_shortcuts":  RateLimit(requests_per_minute=30),
            },
        )

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        bridge_url = secrets.get("bridge_url") or _DEFAULT_BRIDGE_URL
        api_key    = secrets.get("api_key", "")
        if action not in _KNOWN_ACTIONS:
            return self.err(f"Unknown action: {action}")
        return await self._bridge_call(bridge_url, api_key, action, params)
