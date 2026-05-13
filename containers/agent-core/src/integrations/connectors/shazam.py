"""Shazam connector — music recognition and track information via RapidAPI.

Uses the Shazam API via RapidAPI (free tier: 500 requests/month).
Can recognize music from audio URLs, search songs, and retrieve artist info.

Secrets:
    rapidapi_key — RapidAPI key (from rapidapi.com, subscribe to Shazam API)

Actions:
    recognize_url   — Recognize music from an audio file URL           (MEDIUM)
    search          — Search for songs, artists, albums                (LOW)
    search_artist   — Search for an artist                             (LOW)
    get_song        — Get detailed track info by Shazam track key      (LOW)
    get_charts      — Get top charts by country/genre                  (LOW)
    get_related     — Get related tracks for a song                    (LOW)
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_BASE    = "https://shazam.p.rapidapi.com"
_TIMEOUT = 30


class ShazamConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="shazam", version="1.0.0", name="Shazam", category="media",
            description=(
                "Music recognition and discovery via Shazam API (RapidAPI). "
                "Recognize songs from audio, search artists/albums, get charts."
            ),
            capabilities=["music_recognition", "song_search", "artist_info", "charts"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["rapidapi_key"],
            config_schema={},
            rate_limits={
                "recognize_url": RateLimit(requests_per_minute=10),
                "search":        RateLimit(requests_per_minute=30),
                "search_artist": RateLimit(requests_per_minute=30),
                "get_song":      RateLimit(requests_per_minute=30),
                "get_charts":    RateLimit(requests_per_minute=20),
                "get_related":   RateLimit(requests_per_minute=20),
            },
            actions=[
                ActionSpec(id="recognize_url", description="Recognize a song from an audio file URL",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("url", "string", "URL of the audio file (MP3, WAV, etc.)", required=True),
                    ]),
                ActionSpec(id="search", description="Search for songs, artists, or albums by query",
                    risk_level=RiskLevel.MEDIUM, capability="monitored",
                    params=[
                        ParamSpec("query", "string", "Search query (artist name, song title, etc.)", required=True),
                        ParamSpec("limit", "integer", "Max results (default 5, max 20)", required=False),
                        ParamSpec("offset", "integer", "Results offset for pagination (default 0)", required=False),
                    ]),
                ActionSpec(id="search_artist", description="Search for an artist and get their top tracks",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("artist", "string", "Artist name to search for", required=True),
                        ParamSpec("limit", "integer", "Max tracks to return (default 5)", required=False),
                    ]),
                ActionSpec(id="get_song", description="Get detailed information about a song by Shazam track key",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("key", "string", "Shazam track key (from search results)", required=True),
                        ParamSpec("locale", "string", "Locale for metadata (default en-US)", required=False),
                    ]),
                ActionSpec(id="get_charts", description="Get top music charts by country and genre",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("country", "string", "ISO 3166-1 alpha-2 country code (default US)", required=False),
                        ParamSpec("genre", "string", "Genre: POP, HIP_HOP_RAP, DANCE, ELECTRONIC, SOUL_RNB, ALTERNATIVE, ROCK, LATIN, FILM_TV, COUNTRY (default POP)", required=False),
                        ParamSpec("limit", "integer", "Max results (default 10, max 50)", required=False),
                    ]),
                ActionSpec(id="get_related", description="Get related tracks for a given song",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("key", "string", "Shazam track key", required=True),
                        ParamSpec("limit", "integer", "Max related tracks (default 5)", required=False),
                    ]),
            ],
            homepage="https://www.shazam.com",
            docs_url="https://rapidapi.com/apidojo/api/shazam/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        api_key = secrets.get("rapidapi_key", "")
        if not api_key:
            return self.err("rapidapi_key secret is required")

        headers = {
            "X-RapidAPI-Key":  api_key,
            "X-RapidAPI-Host": "shazam.p.rapidapi.com",
        }

        try:
            if action == "recognize_url":  return await self._recognize_url(params, headers)
            if action == "search":         return await self._search(params, headers)
            if action == "search_artist":  return await self._search_artist(params, headers)
            if action == "get_song":       return await self._get_song(params, headers)
            if action == "get_charts":     return await self._get_charts(params, headers)
            if action == "get_related":    return await self._get_related(params, headers)
        except httpx.HTTPStatusError as exc:
            return self.err(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        except Exception as exc:
            logger.error("shazam.error", action=action, error=str(exc))
            return self.err(f"Shazam error: {exc}")

        return self.err(f"Unknown action: {action}")

    async def _get(self, path: str, qparams: dict, headers: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{_BASE}{path}", params=qparams, headers=headers)
            r.raise_for_status()
            return r.json()

    def _fmt_track(self, track: dict) -> dict:
        return {
            "key":       track.get("key", ""),
            "title":     track.get("title", ""),
            "artist":    track.get("subtitle", ""),
            "genre":     track.get("genres", {}).get("primary", ""),
            "images":    {k: v for k, v in track.get("images", {}).items() if k in ("coverart", "background")},
            "share_url": track.get("url", ""),
        }

    async def _recognize_url(self, params: dict, headers: dict) -> dict:
        url = params.get("url", "")
        if not url:
            return self.err("url is required")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            audio_r = await client.get(url)
            audio_data = audio_r.content
            r = await client.post(
                f"{_BASE}/songs/v2/detect",
                content=audio_data,
                headers={**headers, "Content-Type": "application/octet-stream"},
                params={"timezone": "America/Chicago", "locale": "en-US"},
            )
            r.raise_for_status()
            data = r.json()
        if data.get("matches") and data.get("track"):
            return self.ok({"recognized": True, "track": self._fmt_track(data["track"])})
        return self.ok({"recognized": False, "matches": data.get("matches", [])})

    async def _search(self, params: dict, headers: dict) -> dict:
        limit  = min(int(params.get("limit") or 5), 20)
        offset = int(params.get("offset") or 0)
        data   = await self._get("/search", {
            "term": params.get("query", ""), "locale": "en-US",
            "offset": offset, "limit": limit,
        }, headers)
        tracks  = data.get("tracks", {}).get("hits", [])
        artists = data.get("artists", {}).get("hits", [])
        return self.ok({
            "tracks":  [self._fmt_track(h.get("track", h)) for h in tracks],
            "artists": [{"id": h.get("artist", {}).get("adamid", ""), "name": h.get("artist", {}).get("name", "")} for h in artists],
            "query":   params.get("query", ""),
        })

    async def _search_artist(self, params: dict, headers: dict) -> dict:
        artist = params.get("artist", "")
        limit  = min(int(params.get("limit") or 5), 20)
        data   = await self._get("/search", {
            "term": artist, "locale": "en-US", "offset": 0, "limit": limit,
        }, headers)
        tracks = data.get("tracks", {}).get("hits", [])
        return self.ok({"artist": artist, "tracks": [self._fmt_track(h.get("track", h)) for h in tracks]})

    async def _get_song(self, params: dict, headers: dict) -> dict:
        key    = params.get("key", "")
        locale = params.get("locale") or "en-US"
        if not key:
            return self.err("key is required")
        data = await self._get("/songs/get-details", {"key": key, "locale": locale}, headers)
        sections = data.get("sections", [])
        lyrics   = sections[0].get("text", []) if sections else []
        return self.ok({"track": self._fmt_track(data), "lyrics": lyrics})

    async def _get_charts(self, params: dict, headers: dict) -> dict:
        country = (params.get("country") or "US").upper()
        genre   = params.get("genre") or "POP"
        limit   = min(int(params.get("limit") or 10), 50)
        data    = await self._get("/charts/track", {
            "locale": "en-US", "pageSize": limit, "startFrom": 0,
            "listId": f"ip-country-chart-{country}-{genre}",
        }, headers)
        tracks = data.get("tracks", [])
        return self.ok({"country": country, "genre": genre, "tracks": [self._fmt_track(t) for t in tracks[:limit]], "count": len(tracks)})

    async def _get_related(self, params: dict, headers: dict) -> dict:
        key   = params.get("key", "")
        limit = min(int(params.get("limit") or 5), 20)
        if not key:
            return self.err("key is required")
        data   = await self._get("/songs/list-recommendations", {
            "key": key, "locale": "en-US", "offset": 0, "limit": limit,
        }, headers)
        tracks = data.get("tracks", [])
        return self.ok({"tracks": [self._fmt_track(t) for t in tracks], "count": len(tracks)})
