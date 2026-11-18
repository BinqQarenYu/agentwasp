"""Async client for Agent WASP's isolated `agent-screens` virtual display container.

Gracefully degrades when the optional `agent-screens` container is not running
(returns `available=False` instead of raising), and generates the exact per-bot
environment contract (`DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `AGENT_BROWSER_*`)
used by `agent-browser` / `agent-desktop` inside Linux screen sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any


class ScreenClient:
    """Lightweight stdlib HTTP client to the isolated `agent-screens` worker."""

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        raw_url = base_url if base_url is not None else os.environ.get("AGENT_SCREENS_URL", "http://127.0.0.1:6900")
        self.base_url = raw_url.rstrip("/")
        self.internal_token = (
            internal_token if internal_token is not None else os.environ.get("SCREENS_INTERNAL_TOKEN", "")
        )
        self.timeout = timeout

    def _sync_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        if not self.base_url:
            return False, {"available": False, "error": "AGENT_SCREENS_URL not configured"}
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.internal_token:
            headers["X-Screens-Token"] = self.internal_token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
                parsed = json.loads(raw) if raw else {}
                parsed.setdefault("available", True)
                return True, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace").strip()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"error": raw or exc.reason}
            parsed["available"] = True
            parsed["http_status"] = exc.code
            return False, parsed
        except Exception as exc:
            return False, {"available": False, "error": str(exc)}

    async def health(self) -> dict[str, Any]:
        ok, data = await asyncio.to_thread(self._sync_request, "GET", "/health")
        if not ok:
            return {"ok": False, "available": False, "active_screens": 0, "error": data.get("error", "")}
        return {"ok": True, "available": True, **data}

    async def list_screens(self) -> dict[str, Any]:
        ok, data = await asyncio.to_thread(self._sync_request, "GET", "/screens")
        if not ok:
            return {"ok": False, "available": False, "screens": [], "error": data.get("error", "")}
        return {"ok": True, "available": True, **data}

    async def ensure_screen(self, bot_id: str, busy: bool | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if busy is not None:
            payload["busy"] = busy
        ok, data = await asyncio.to_thread(self._sync_request, "POST", f"/screens/{bot_id}/ensure", payload)
        if not ok:
            return {"ok": False, "available": data.get("available", False), "screen": None, "error": data.get("error", "")}
        return {"ok": True, "available": True, "screen": data.get("screen")}

    async def close_screen(self, bot_id: str) -> dict[str, Any]:
        ok, data = await asyncio.to_thread(self._sync_request, "DELETE", f"/screens/{bot_id}")
        return {"ok": ok, "available": data.get("available", False), **data}

    @staticmethod
    def build_bot_screen_env(screen_info: dict[str, Any]) -> dict[str, str]:
        """Map a screen descriptor into the `agent-browser` / `agent-desktop` env contract."""
        return {
            "DISPLAY": str(screen_info.get("display") or ""),
            "DBUS_SESSION_BUS_ADDRESS": str(screen_info.get("bus") or ""),
            "AGENT_BROWSER_SESSION": str(screen_info.get("browser_session") or ""),
            "AGENT_BROWSER_PROFILE": str(screen_info.get("profile_dir") or ""),
            "AGENT_BROWSER_HEADED": "1",
            "AGENT_BROWSER_NO_XVFB": "1",
            "AGENT_BROWSER_ARGS": "--start-maximized",
            "QT_ACCESSIBILITY": "1",
            "ACCESSIBILITY_ENABLED": "1",
        }
