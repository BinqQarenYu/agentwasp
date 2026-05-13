"""macOS platform bridge connector — full implementation."""

from __future__ import annotations

from typing import Any

from ..base import ActionSpec, ConnectorManifest, ParamSpec, RateLimit, RiskLevel
from .platform_base import PlatformBridgeConnector

_DEFAULT_BRIDGE_URL = "http://127.0.0.1:37123"


class MacOSBridgeConnector(PlatformBridgeConnector):
    """Connect to a WASP macOS companion daemon for system automation.

    Supported actions are strictly allowlisted on both sides.
    The companion daemon must be running on the macOS machine.
    """

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="platform-macos",
            version="1.0.0",
            name="macOS Bridge",
            category="platform",
            description="Control a macOS machine via the WASP companion daemon. Supports iMessage, Notes, Reminders, clipboard, screenshots, and more.",
            capabilities=["platform_imessage", "platform_screenshot", "platform_notes", "platform_reminders", "platform_clipboard", "platform_open_url", "platform_info"],
            risk_level=RiskLevel.HIGH,
            actions=[
                ActionSpec(
                    id="get_system_info",
                    description="Get macOS system info: CPU, RAM, disk usage, OS version, hostname.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="list_running_apps",
                    description="List currently running application names.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_active_window",
                    description="Get the title and app name of the frontmost window.",
                    params=[],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="take_screenshot",
                    description="Capture a screenshot of the current display.",
                    params=[
                        ParamSpec(name="save_path", type="string", description="Optional path to save the screenshot.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="open_url",
                    description="Open a URL in the default browser.",
                    params=[
                        ParamSpec(name="url", type="string", description="URL to open."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="send_imessage",
                    description="Send an iMessage or SMS to a contact.",
                    params=[
                        ParamSpec(name="recipient", type="string", description="Phone number or Apple ID of recipient."),
                        ParamSpec(name="message", type="string", description="Message text."),
                    ],
                    risk_level=RiskLevel.HIGH,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_imessages",
                    description="Get recent iMessages from a contact.",
                    params=[
                        ParamSpec(name="contact", type="string", description="Contact phone or Apple ID."),
                        ParamSpec(name="limit", type="integer", description="Max messages to return.", required=False, default=10),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="create_note",
                    description="Create a new note in the Notes app.",
                    params=[
                        ParamSpec(name="title", type="string", description="Note title."),
                        ParamSpec(name="body", type="string", description="Note body text."),
                        ParamSpec(name="folder", type="string", description="Notes folder name.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_notes",
                    description="List recent notes from the Notes app.",
                    params=[
                        ParamSpec(name="folder", type="string", description="Filter by folder name.", required=False),
                        ParamSpec(name="limit", type="integer", description="Max notes to return.", required=False, default=20),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="create_reminder",
                    description="Create a reminder in the Reminders app.",
                    params=[
                        ParamSpec(name="title", type="string", description="Reminder title."),
                        ParamSpec(name="due_date", type="string", description="Due date in ISO format (e.g. 2024-12-31T10:00:00).", required=False),
                        ParamSpec(name="list_name", type="string", description="Reminders list name.", required=False),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_reminders",
                    description="List reminders from the Reminders app.",
                    params=[
                        ParamSpec(name="list_name", type="string", description="Filter by list name.", required=False),
                        ParamSpec(name="include_completed", type="boolean", description="Include completed reminders.", required=False, default=False),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
                ActionSpec(
                    id="get_clipboard",
                    description="Read the current clipboard text content.",
                    params=[],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="set_clipboard",
                    description="Write text to the clipboard.",
                    params=[
                        ParamSpec(name="text", type="string", description="Text to copy to clipboard."),
                    ],
                    risk_level=RiskLevel.MEDIUM,
                    capability="controlled",
                ),
                ActionSpec(
                    id="get_contacts",
                    description="Search contacts by name or phone number.",
                    params=[
                        ParamSpec(name="query", type="string", description="Search query.", required=False),
                    ],
                    risk_level=RiskLevel.LOW,
                    capability="monitored",
                ),
            ],
            required_secrets=["api_key"],
            config_schema={"bridge_url": {"type": "string", "default": _DEFAULT_BRIDGE_URL}},
            rate_limits={
                "get_system_info":    RateLimit(requests_per_minute=30),
                "list_running_apps":  RateLimit(requests_per_minute=20),
                "get_active_window":  RateLimit(requests_per_minute=60),
                "take_screenshot":    RateLimit(requests_per_minute=10),
                "open_url":           RateLimit(requests_per_minute=20),
                "send_imessage":      RateLimit(requests_per_minute=10),
                "get_imessages":      RateLimit(requests_per_minute=20),
                "create_note":        RateLimit(requests_per_minute=15),
                "get_notes":          RateLimit(requests_per_minute=30),
                "create_reminder":    RateLimit(requests_per_minute=15),
                "get_reminders":      RateLimit(requests_per_minute=30),
                "get_clipboard":      RateLimit(requests_per_minute=30),
                "set_clipboard":      RateLimit(requests_per_minute=20),
                "get_contacts":       RateLimit(requests_per_minute=20),
            },
        )

    async def execute(self, action: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
        bridge_url = secrets.get("bridge_url") or _DEFAULT_BRIDGE_URL
        api_key    = secrets.get("api_key", "")

        _KNOWN_ACTIONS = {
            "get_system_info", "list_running_apps", "get_active_window",
            "take_screenshot", "open_url", "send_imessage", "get_imessages",
            "create_note", "get_notes", "create_reminder", "get_reminders",
            "get_clipboard", "set_clipboard", "get_contacts",
        }
        if action not in _KNOWN_ACTIONS:
            return self.err(f"Unknown action: {action}")

        return await self._bridge_call(bridge_url, api_key, action, params)
