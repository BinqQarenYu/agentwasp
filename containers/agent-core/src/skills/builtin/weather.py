import httpx

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

# WMO Weather interpretation codes -> human descriptions
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

import re

# Patterns to extract city + country hint
_SEPARATORS = re.compile(r"\s+de\s+|\s+in\s+|\s*,\s*", re.IGNORECASE)


def _parse_city_country(raw: str) -> tuple[str, str]:
    """Extract city name and optional country hint from input.

    'Santiago de Chile' -> ('Santiago', 'chile')
    'Madrid, Spain'     -> ('Madrid', 'spain')
    'Buenos Aires'      -> ('Buenos Aires', '')
    """
    parts = _SEPARATORS.split(raw.strip(), maxsplit=1)
    city = parts[0].strip()
    country_hint = parts[1].strip().lower() if len(parts) > 1 else ""
    return city, country_hint

CURRENT_VARS = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,"
    "weather_code,wind_speed_10m,wind_direction_10m,cloud_cover"
)


def _wind_direction(degrees: float) -> str:
    """Convert wind direction degrees to compass label."""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(degrees / 22.5) % 16
    return dirs[idx]


class GetWeatherSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="get_weather",
            description="Get current weather for a city.",
            params=[
                SkillParam(name="city", param_type=ParamType.STRING, description="City name"),
            ],
            category="utility",
            timeout_seconds=30.0,
            cooldown_seconds=2.0,
        )

    async def execute(self, city: str, **kwargs) -> SkillResult:
        try:
            timeout = httpx.Timeout(20.0, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                # Step 1: Geocode - search by city name, filter by country hint
                search_city, country_hint = _parse_city_country(city)
                geo_resp = await client.get(
                    GEOCODE_URL,
                    params={"name": search_city, "count": 10, "language": "en"},
                )
                geo_resp.raise_for_status()
                geo_data = geo_resp.json()
                results = geo_data.get("results", [])

                if not results:
                    return SkillResult(
                        skill_name="get_weather", success=False,
                        output="", error=f"City not found: {city}",
                    )

                # Filter by country hint if provided
                loc = results[0]
                if country_hint:
                    for r in results:
                        if country_hint in r.get("country", "").lower():
                            loc = r
                            break
                lat = loc["latitude"]
                lon = loc["longitude"]
                city_name = loc.get("name", city)
                country = loc.get("country", "")

                # Step 2: Get current weather
                weather_resp = await client.get(
                    WEATHER_URL,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": CURRENT_VARS,
                        "timezone": "auto",
                    },
                )
                weather_resp.raise_for_status()
                weather_data = weather_resp.json()

            current = weather_data.get("current", {})
            temp = current.get("temperature_2m", "?")
            feels = current.get("apparent_temperature", "?")
            humidity = current.get("relative_humidity_2m", "?")
            wind_speed = current.get("wind_speed_10m", "?")
            wind_deg = current.get("wind_direction_10m", 0)
            wind_dir = _wind_direction(float(wind_deg)) if wind_deg != "?" else ""
            weather_code = current.get("weather_code", -1)
            condition = WMO_CODES.get(weather_code, "Unknown")
            cloud_cover = current.get("cloud_cover", "?")

            output = (
                f"Weather in {city_name}, {country}:\n"
                f"Condition: {condition}\n"
                f"Temperature: {temp}°C (feels like {feels}°C)\n"
                f"Humidity: {humidity}%\n"
                f"Cloud cover: {cloud_cover}%\n"
                f"Wind: {wind_speed} km/h {wind_dir}"
            )
            return SkillResult(skill_name="get_weather", success=True, output=output)
        except Exception as e:
            return SkillResult(skill_name="get_weather", success=False, output="", error=str(e))
