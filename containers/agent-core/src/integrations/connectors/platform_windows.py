"""Windows platform bridge connector — requires WASP companion service on Windows."""

from __future__ import annotations

from typing import Any

from ..base import ActionSpec, ConnectorManifest, ParamSpec, RateLimit, RiskLevel
from .platform_base import PlatformBridgeConnector

_DEFAULT_BRIDGE_URL = "http://127.0.0.1:37126"

_KNOWN_ACTIONS = {"get_system_info", "take_screenshot", "list_running_processes", "run_allowlisted_script", "get_clipboard"}


class WindowsBridgeConnector(PlatformBridgeConnector):
    """Windows companion bridge — requires WASP companion service running on Windows host."""

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="platform-windows",
            version="1.0.0",
            name="Windows Bridge",
            category="platform",
            description="Control a Windows machine via the WASP companion service. Supports system info, screenshots, process listing, clipboard, and pre-approved PowerShell scripts.",
            capabilities=["platform_screenshot", "platform_info", "platform_clipboard", "platform_shortcut"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="get_system_info",
                    description="Get Windows system info: CPU, RAM, disk, Windows version.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="take_screenshot",
                    description="Capture a screenshot of the Windows desktop.",
                    params=[],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="list_running_processes",
                    description="List currently running processes (name and PID).",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="run_allowlisted_script",
                    description="Run a pre-approved PowerShell script by name. The script must be in the companion's allowlist.",
                    params=[
                        ParamSpec(name="script_name", type="string", description="Name of the pre-approved script."),
                        ParamSpec(name="args", type="string", description="Optional arguments string.", required=False),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_clipboard",
                    description="Read current clipboard text content.",
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
