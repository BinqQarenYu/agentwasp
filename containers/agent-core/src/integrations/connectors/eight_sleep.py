"""8Sleep connector — Pod smart mattress control via 8Sleep API.

Controls temperature, alarm, and reads sleep data from 8Sleep Pod devices.
Uses email/password authentication to obtain session tokens.

Secrets:
    email    — 8Sleep account email
    password — 8Sleep account password

Actions:
    get_status      — Get current Pod status (temperature, heating, etc.)  (LOW)
    set_temperature — Set target temperature for a side (-10 to 10)        (MEDIUM)
    turn_on         — Turn on the Pod                                      (MEDIUM)
    turn_off        — Turn off the Pod                                     (MEDIUM)
    get_sleep_data  — Get sleep stats/intervals for last N nights          (LOW)
    set_alarm       — Set smart alarm time                                 (MEDIUM)
    get_devices     — List all registered devices on the account           (LOW)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_BASE    = "https://client-api.8slp.net/v1"
_AUTH    = "https://auth-api.8slp.net/v1"
_TIMEOUT = 15


class EightSleepConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="eight-sleep", version="1.0.0", name="8Sleep", category="smart_home",
            description=(
                "Control your 8Sleep Pod smart mattress: temperature zones, "
                "alarms, and sleep data tracking."
            ),
            capabilities=["temperature_control", "alarm_control", "sleep_tracking"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["email", "password"],
            config_schema={},
            rate_limits={
                "get_status":      RateLimit(requests_per_minute=20),
                "set_temperature": RateLimit(requests_per_minute=10),
                "turn_on":         RateLimit(requests_per_minute=5),
                "turn_off":        RateLimit(requests_per_minute=5),
                "get_sleep_data":  RateLimit(requests_per_minute=10),
                "set_alarm":       RateLimit(requests_per_minute=5),
                "get_devices":     RateLimit(requests_per_minute=10),
            },
            actions=[
                ActionSpec(id="get_status", description="Get current Pod temperature and heating/cooling state",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("device_id", "string", "Device ID (from get_devices, or auto-detected)", required=False),
                    ]),
                ActionSpec(id="set_temperature", description="Set target temperature level for a side (-10 to 10, 0=neutral)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("level", "integer", "Temperature level -10 (coldest) to 10 (hottest)", required=True),
                        ParamSpec("side", "string", "Bed side: left or right (default left)", required=False),
                        ParamSpec("device_id", "string", "Device ID (auto-detected if omitted)", required=False),
                        ParamSpec("duration_hours", "integer", "Duration in hours (default 8)", required=False),
                    ]),
                ActionSpec(id="turn_on", description="Turn on the Pod heating/cooling",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("side", "string", "left or right (default left)", required=False),
                        ParamSpec("device_id", "string", "Device ID", required=False),
                    ]),
                ActionSpec(id="turn_off", description="Turn off the Pod",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("side", "string", "left or right (default left)", required=False),
                        ParamSpec("device_id", "string", "Device ID", required=False),
                    ]),
                ActionSpec(id="get_sleep_data", description="Get sleep intervals and biometric data for recent nights",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("nights", "integer", "Number of nights to retrieve (default 1, max 7)", required=False),
                        ParamSpec("device_id", "string", "Device ID", required=False),
                    ]),
                ActionSpec(id="set_alarm", description="Set or update the smart alarm time",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("time", "string", "Alarm time in HH:MM format (24h)", required=True),
                        ParamSpec("side", "string", "Bed side: left or right (default left)", required=False),
                        ParamSpec("device_id", "string", "Device ID", required=False),
                        ParamSpec("enabled", "boolean", "Enable or disable the alarm (default true)", required=False),
                    ]),
                ActionSpec(id="get_devices", description="List all 8Sleep devices on the account",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
            ],
            homepage="https://www.eightsleep.com",
            docs_url="https://eightsleep.com",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        email    = secrets.get("email", "")
        password = secrets.get("password", "")
        if not email or not password:
            return self.err("email and password secrets are required")

        try:
            token, user_id = await self._authenticate(email, password)
        except Exception as exc:
            return self.err(f"Authentication failed: {exc}")

        try:
            if action == "get_devices":
                return await self._get_devices(token, user_id)
            device_id = await self._resolve_device(token, user_id, params.get("device_id"))
            if action == "get_status":      return await self._get_status(token, device_id)
            if action == "set_temperature": return await self._set_temperature(token, device_id, params)
            if action == "turn_on":         return await self._turn_on(token, device_id, params)
            if action == "turn_off":        return await self._turn_off(token, device_id, params)
            if action == "get_sleep_data":  return await self._get_sleep_data(token, device_id, user_id, params)
            if action == "set_alarm":       return await self._set_alarm(token, device_id, params)
        except Exception as exc:
            logger.error("eight_sleep.error", action=action, error=str(exc))
            return self.err(f"8Sleep error: {exc}")

        return self.err(f"Unknown action: {action}")

    async def _authenticate(self, email: str, password: str) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{_AUTH}/login", json={"email": email, "password": password})
            r.raise_for_status()
            data = r.json()
        session = data.get("session", data)
        return session.get("token", ""), session.get("userId", "")

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

    async def _get_devices(self, token: str, user_id: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_BASE}/users/{user_id}/devices", headers=self._headers(token))
            r.raise_for_status()
            data = r.json()
        devices = [
            {"id": d.get("deviceId", ""), "model": d.get("model", ""), "online": d.get("online", False)}
            for d in data.get("result", {}).get("devices", [])
        ]
        return self.ok({"devices": devices, "count": len(devices)})

    async def _resolve_device(self, token: str, user_id: str, device_id: str | None) -> str:
        if device_id:
            return device_id
        result  = await self._get_devices(token, user_id)
        devices = result.get("data", {}).get("devices", [])
        if not devices:
            raise ValueError("No devices found on account")
        return devices[0]["id"]

    async def _get_status(self, token: str, device_id: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_BASE}/devices/{device_id}", headers=self._headers(token))
            r.raise_for_status()
            data = r.json()
        device = data.get("result", {})
        left   = device.get("leftKelvin", {})
        right  = device.get("rightKelvin", {})
        return self.ok({
            "device_id": device_id,
            "online":    device.get("online", False),
            "left_side": {
                "on":           device.get("leftHeatingDuration", 0) > 0,
                "target_level": left.get("userTemperature", 0),
                "current_temp": left.get("currentDeviceTemperature", 0),
            },
            "right_side": {
                "on":           device.get("rightHeatingDuration", 0) > 0,
                "target_level": right.get("userTemperature", 0),
                "current_temp": right.get("currentDeviceTemperature", 0),
            },
        })

    async def _set_temperature(self, token: str, device_id: str, params: dict) -> dict:
        level    = max(-10, min(10, int(params.get("level") or 0)))
        side     = (params.get("side") or "left").lower()
        duration = int(params.get("duration_hours") or 8) * 3600
        side_key = "leftHeatingLevel" if side == "left" else "rightHeatingLevel"
        dur_key  = "leftHeatingDuration" if side == "left" else "rightHeatingDuration"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(f"{_BASE}/devices/{device_id}",
                headers=self._headers(token), json={side_key: level, dur_key: duration})
            r.raise_for_status()
        return self.ok({"side": side, "level": level, "duration_seconds": duration})

    async def _turn_on(self, token: str, device_id: str, params: dict) -> dict:
        side    = (params.get("side") or "left").lower()
        dur_key = "leftHeatingDuration" if side == "left" else "rightHeatingDuration"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(f"{_BASE}/devices/{device_id}",
                headers=self._headers(token), json={dur_key: 28800})
            r.raise_for_status()
        return self.ok({"side": side, "on": True, "duration_seconds": 28800})

    async def _turn_off(self, token: str, device_id: str, params: dict) -> dict:
        side    = (params.get("side") or "left").lower()
        dur_key = "leftHeatingDuration" if side == "left" else "rightHeatingDuration"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(f"{_BASE}/devices/{device_id}",
                headers=self._headers(token), json={dur_key: 0})
            r.raise_for_status()
        return self.ok({"side": side, "on": False})

    async def _get_sleep_data(self, token: str, device_id: str, user_id: str, params: dict) -> dict:
        nights = min(int(params.get("nights") or 1), 7)
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(days=nights)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_BASE}/users/{user_id}/intervals",
                headers=self._headers(token),
                params={"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
            )
            r.raise_for_status()
            data = r.json()
        intervals = data.get("result", {}).get("intervals", [])
        summaries = []
        for iv in intervals[-nights:]:
            ts = iv.get("ts", "")
            summaries.append({
                "date":       ts[:10] if ts else "",
                "score":      iv.get("score"),
                "hrv":        iv.get("hrv", {}).get("current"),
                "resp_rate":  iv.get("respiratoryRate", {}).get("current"),
                "heart_rate": iv.get("heartRate", {}).get("current"),
            })
        return self.ok({"nights": nights, "intervals": summaries})

    async def _set_alarm(self, token: str, device_id: str, params: dict) -> dict:
        time_str  = params.get("time", "07:00")
        side      = (params.get("side") or "left").lower()
        enabled   = params.get("enabled", True)
        hour, minute = (int(x) for x in time_str.split(":")[:2])
        alarm_key = "leftAlarm" if side == "left" else "rightAlarm"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(f"{_BASE}/devices/{device_id}",
                headers=self._headers(token),
                json={alarm_key: {"enabled": bool(enabled), "time": {"hour": hour, "minute": minute}, "smartEnabled": True}})
            r.raise_for_status()
        return self.ok({"side": side, "alarm_time": time_str, "enabled": enabled})
