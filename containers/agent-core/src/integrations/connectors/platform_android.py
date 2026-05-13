"""Android platform bridge connector — requires ADB bridge or WASP companion app."""

from __future__ import annotations

from typing import Any

from ..base import ActionSpec, ConnectorManifest, ParamSpec, RateLimit, RiskLevel
from .platform_base import PlatformBridgeConnector

_DEFAULT_BRIDGE_URL = "http://127.0.0.1:37125"

_KNOWN_ACTIONS = {"get_device_info", "take_screenshot", "send_notification", "get_location", "launch_app"}


class AndroidBridgeConnector(PlatformBridgeConnector):
    """Android companion bridge — requires WASP Android companion app or ADB bridge daemon."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="platform-android",
            version="1.0.0",
            name="Android Bridge",
            category="platform",
            description="Control an Android device via the WASP companion app or ADB bridge. Supports screenshots, notifications, device info, and app launching.",
            capabilities=["platform_screenshot", "platform_info"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="get_device_info",
                    description="Get Android device info: battery, Android version, model.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="take_screenshot",
                    description="Capture a screenshot of the Android device screen.",
                    params=[],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="send_notification",
                    description="Push a local notification on the Android device.",
                    params=[
                        ParamSpec(name="title", type="string", description="Notification title."),
                        ParamSpec(name="body", type="string", description="Notification body text."),
                    ],
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
                    id="launch_app",
                    description="Launch an app by package name (e.g. com.example.app).",
                    params=[
                        ParamSpec(name="package_name", type="string", description="Android package name."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
            ],
            required_secrets=["api_key"],
            config_schema={"bridge_url": {"type": "string", "default": _DEFAULT_BRIDGE_URL}},
            rate_limits={
                "get_device_info":   RateLimit(requests_per_minute=30),
                "take_screenshot":   RateLimit(requests_per_minute=10),
                "send_notification": RateLimit(requests_per_minute=20),
                "get_location":      RateLimit(requests_per_minute=10),
                "launch_app":        RateLimit(requests_per_minute=10),
            },
        )

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        bridge_url = secrets.get("bridge_url") or _DEFAULT_BRIDGE_URL
        api_key    = secrets.get("api_key", "")
        if action not in _KNOWN_ACTIONS:
            return self.err(f"Unknown action: {action}")
        return await self._bridge_call(bridge_url, api_key, action, params)
