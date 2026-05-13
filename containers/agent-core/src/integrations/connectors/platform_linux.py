"""Linux platform bridge connector — requires WASP companion daemon on Linux host."""

from __future__ import annotations

from typing import Any

from ..base import ActionSpec, ConnectorManifest, ParamSpec, RateLimit, RiskLevel
from .platform_base import PlatformBridgeConnector

_DEFAULT_BRIDGE_URL = "http://127.0.0.1:37127"

_KNOWN_ACTIONS = {"get_system_info", "take_screenshot", "list_running_processes", "run_allowlisted_script", "get_clipboard"}


class LinuxBridgeConnector(PlatformBridgeConnector):
    """Linux companion bridge — requires WASP companion daemon running on the Linux host."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="platform-linux",
            version="1.0.0",
            name="Linux Bridge",
            category="platform",
            description="Control a Linux machine via the WASP companion daemon. Supports system info, screenshots (scrot/gnome-screenshot), process listing, clipboard (xclip/xsel), and pre-approved scripts.",
            capabilities=["platform_screenshot", "platform_info", "platform_clipboard", "platform_shortcut"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="get_system_info",
                    description="Get Linux system info: CPU, RAM, disk, distro, kernel version.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="take_screenshot",
                    description="Capture a screenshot using scrot or gnome-screenshot.",
                    params=[],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="list_running_processes",
                    description="List currently running processes.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="run_allowlisted_script",
                    description="Run a pre-approved shell script by name. Script must be in the companion's allowlist.",
                    params=[
                        ParamSpec(name="script_name", type="string", description="Name of the pre-approved script."),
                        ParamSpec(name="args", type="string", description="Optional arguments string.", required=False),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_clipboard",
                    description="Read clipboard text content via xclip or xsel.",
                    params=[],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
            ],
            required_secrets=["api_key"],
            config_schema={"bridge_url": {"type": "string", "default": _DEFAULT_BRIDGE_URL}},
            rate_limits={
                "get_system_info":        RateLimit(requests_per_minute=30),
                "take_screenshot":        RateLimit(requests_per_minute=10),
                "list_running_processes": RateLimit(requests_per_minute=20),
                "run_allowlisted_script": RateLimit(requests_per_minute=5),
                "get_clipboard":          RateLimit(requests_per_minute=30),
            },
        )

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        bridge_url = secrets.get("bridge_url") or _DEFAULT_BRIDGE_URL
        api_key    = secrets.get("api_key", "")
        if action not in _KNOWN_ACTIONS:
            return self.err(f"Unknown action: {action}")
        return await self._bridge_call(bridge_url, api_key, action, params)
