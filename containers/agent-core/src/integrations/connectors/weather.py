"""Weather connector — OpenWeatherMap API.

Provides richer weather data than the built-in wttr.in skill:
alerts, air quality, UV index, hourly/daily forecasts.

Secrets:
    api_key     — OpenWeatherMap API key (free tier supports current + forecast)

Actions:
    current         — Current conditions for a city or coordinates    (LOW)
    forecast        — 5-day / 3-hour forecast                         (LOW)
    air_quality     — Air quality index and pollutant levels           (LOW)
    geocode         — City name → latitude/longitude                  (LOW)
    uv_index        — UV index (one call API)                         (LOW)
"""
from __future__ import annotations

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_OWM = "https://api.openweathermap.org"
_TIMEOUT = 10.0


class WeatherConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="weather", version="1.0.0", name="Weather", category="tools",
            description="Real-time weather data, forecasts, and air quality via OpenWeatherMap.",
            capabilities=["current_weather", "forecasts", "air_quality", "geocoding", "uv_index"],
            risk_level=RiskLevel.LOW,
            required_secrets=["api_key"],
            config_schema={},
            rate_limits={
                "current":     RateLimit(requests_per_minute=60),
                "forecast":    RateLimit(requests_per_minute=60),
                "air_quality": RateLimit(requests_per_minute=60),
                "geocode":     RateLimit(requests_per_minute=60),
                "uv_index":    RateLimit(requests_per_minute=60),
            },
            actions=[
                ActionSpec(id="current", description="Get current weather for a city or coordinates",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("city", "string", "City name (e.g. Santiago,CL)", required=False),
                        ParamSpec("lat", "string", "Latitude (use with lon)", required=False),
                        ParamSpec("lon", "string", "Longitude (use with lat)", required=False),
                        ParamSpec("units", "string", "metric|imperial|standard (default metric)", required=False),
                    ]),
                ActionSpec(id="forecast", description="Get 5-day forecast (3-hour intervals)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("city", "string", "City name (e.g. Santiago,CL)", required=False),
                        ParamSpec("lat", "string", "Latitude", required=False),
                        ParamSpec("lon", "string", "Longitude", required=False),
                        ParamSpec("units", "string", "metric|imperial|standard (default metric)", required=False),
                        ParamSpec("days", "integer", "Number of days to return (1-5, default 3)", required=False),
                    ]),
                ActionSpec(id="air_quality", description="Get air quality index and pollutant levels",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("lat", "string", "Latitude", required=True),
                        ParamSpec("lon", "string", "Longitude", required=True),
                    ]),
                ActionSpec(id="geocode", description="Convert city name to lat/lon coordinates",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("city", "string", "City name with optional country code (e.g. Santiago,CL)", required=True),
                        ParamSpec("limit", "integer", "Max results (default 5)", required=False),
                    ]),
                ActionSpec(id="uv_index", description="Get UV index for coordinates",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("lat", "string", "Latitude", required=True),
                        ParamSpec("lon", "string", "Longitude", required=True),
                    ]),
            ],
            homepage="https://openweathermap.org",
            docs_url="https://openweathermap.org/api",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        key = secrets.get("api_key", "")
        if not key:
            return self.err("api_key not configured")

        if action == "current":
            return await self._current(params, key)
        if action == "forecast":
            return await self._forecast(params, key)
        if action == "air_quality":
            return await self._air_quality(params, key)
        if action == "geocode":
            return await self._geocode(params, key)
        if action == "uv_index":
            return await self._uv_index(params, key)
        return self.err(f"Unknown action: {action}")

    def _location_params(self, p: dict, key: str) -> dict:
        qp: dict = {"appid": key, "units": p.get("units") or "metric"}
        if p.get("city"):
            qp["q"] = p["city"]
        elif p.get("lat") and p.get("lon"):
            qp["lat"] = p["lat"]
            qp["lon"] = p["lon"]
        else:
            return {}
        return qp

    async def _current(self, p: dict, key: str) -> dict:
        qp = self._location_params(p, key)
        if not qp:
            return self.err("Provide city or lat+lon")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_OWM}/data/2.5/weather", params=qp)
        if r.status_code == 200:
            d = r.json()
            return self.ok({
                "city": d.get("name"), "country": d.get("sys", {}).get("country"),
                "temp": d["main"]["temp"], "feels_like": d["main"]["feels_like"],
                "humidity": d["main"]["humidity"], "pressure": d["main"]["pressure"],
                "description": d["weather"][0]["description"] if d.get("weather") else "",
                "wind_speed": d.get("wind", {}).get("speed"),
                "visibility": d.get("visibility"),
                "clouds": d.get("clouds", {}).get("all"),
                "sunrise": d.get("sys", {}).get("sunrise"),
                "sunset":  d.get("sys", {}).get("sunset"),
            })
        return self.err(f"OWM {r.status_code}: {r.json().get('message', r.text[:100])}")

    async def _forecast(self, p: dict, key: str) -> dict:
        qp = self._location_params(p, key)
        if not qp:
            return self.err("Provide city or lat+lon")
        days = min(int(p.get("days") or 3), 5)
        qp["cnt"] = days * 8  # 8 × 3h = 24h per day
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_OWM}/data/2.5/forecast", params=qp)
        if r.status_code == 200:
            d = r.json()
            items = [
                {
                    "dt": item["dt_txt"],
                    "temp": item["main"]["temp"],
                    "feels_like": item["main"]["feels_like"],
                    "description": item["weather"][0]["description"] if item.get("weather") else "",
                    "rain_mm": item.get("rain", {}).get("3h", 0),
                    "wind_speed": item.get("wind", {}).get("speed"),
                }
                for item in d.get("list", [])
            ]
            return self.ok({"city": d.get("city", {}).get("name"), "forecast": items})
        return self.err(f"OWM {r.status_code}: {r.json().get('message', '')}")

    async def _air_quality(self, p: dict, key: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_OWM}/data/2.5/air_pollution",
                params={"lat": p["lat"], "lon": p["lon"], "appid": key})
        if r.status_code == 200:
            d = r.json()
            item = d.get("list", [{}])[0]
            aqi_labels = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}
            aqi = item.get("main", {}).get("aqi")
            return self.ok({
                "aqi": aqi, "aqi_label": aqi_labels.get(aqi, "Unknown"),
                "components": item.get("components", {}),
            })
        return self.err(f"OWM {r.status_code}")

    async def _geocode(self, p: dict, key: str) -> dict:
        limit = min(int(p.get("limit") or 5), 10)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_OWM}/geo/1.0/direct",
                params={"q": p["city"], "limit": limit, "appid": key})
        if r.status_code == 200:
            results = [
                {"name": g["name"], "country": g["country"],
                 "lat": g["lat"], "lon": g["lon"],
                 "state": g.get("state", "")}
                for g in r.json()
            ]
            return self.ok({"results": results, "count": len(results)})
        return self.err(f"OWM {r.status_code}")

    async def _uv_index(self, p: dict, key: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_OWM}/data/2.5/uvi",
                params={"lat": p["lat"], "lon": p["lon"], "appid": key})
        if r.status_code == 200:
            d = r.json()
            uv = d.get("value", 0)
            label = "Low" if uv < 3 else "Moderate" if uv < 6 else "High" if uv < 8 else "Very High" if uv < 11 else "Extreme"
            return self.ok({"uv_index": uv, "label": label})
        return self.err(f"OWM {r.status_code}")
