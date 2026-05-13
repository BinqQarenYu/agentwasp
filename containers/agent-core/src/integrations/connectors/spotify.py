"""Spotify connector — Spotify Web API.

Requires a Spotify app with OAuth2 credentials.
Use client_credentials flow for search/catalog; auth_code flow for playback control.

Secrets:
    access_token    — OAuth2 access token (short-lived, refreshable)
    refresh_token   — OAuth2 refresh token (for auto-refresh)
    client_id       — Spotify app client ID
    client_secret   — Spotify app client secret

Actions:
    search              — Search tracks, artists, albums, playlists    (LOW)
    now_playing         — Get currently playing track                   (LOW)
    recently_played     — Get recently played tracks                    (LOW)
    get_track           — Get track metadata by ID                      (LOW)
    get_recommendations — Get track recommendations                     (LOW)
    get_playlists       — List user playlists                           (LOW)
    get_playlist_tracks — Get tracks in a playlist                      (LOW)
    play                — Start/resume playback                         (HIGH)
    pause               — Pause playback                                (MEDIUM)
    next_track          — Skip to next track                            (MEDIUM)
    prev_track          — Skip to previous track                        (MEDIUM)
    queue_track         — Add track to playback queue                   (MEDIUM)
    set_volume          — Set playback volume                           (MEDIUM)
"""
from __future__ import annotations

import base64
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_API = "https://api.spotify.com/v1"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_TIMEOUT = 15.0


class SpotifyConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="spotify", version="1.0.0", name="Spotify", category="media",
            description="Search music, control playback, and manage playlists via Spotify Web API.",
            capabilities=["search_catalog", "playback_control", "queue_management", "playlist_access"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["access_token"],
            config_schema={},
            rate_limits={
                "search":              RateLimit(requests_per_minute=60),
                "now_playing":         RateLimit(requests_per_minute=30),
                "recently_played":     RateLimit(requests_per_minute=20),
                "get_track":           RateLimit(requests_per_minute=60),
                "get_recommendations": RateLimit(requests_per_minute=30),
                "get_playlists":       RateLimit(requests_per_minute=20),
                "get_playlist_tracks": RateLimit(requests_per_minute=20),
                "play":                RateLimit(requests_per_minute=20),
                "pause":               RateLimit(requests_per_minute=30),
                "next_track":          RateLimit(requests_per_minute=30),
                "prev_track":          RateLimit(requests_per_minute=30),
                "queue_track":         RateLimit(requests_per_minute=30),
                "set_volume":          RateLimit(requests_per_minute=20),
            },
            actions=[
                ActionSpec(id="search", description="Search Spotify catalog for tracks, artists, albums, playlists",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query", "string", "Search query", required=True),
                        ParamSpec("type", "string", "track|artist|album|playlist (default track)", required=False),
                        ParamSpec("limit", "integer", "Max results (default 10)", required=False),
                    ]),
                ActionSpec(id="now_playing", description="Get the currently playing track",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="recently_played", description="Get recently played tracks",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("limit", "integer", "Max results (default 10)", required=False)]),
                ActionSpec(id="get_track", description="Get metadata for a track by Spotify ID or URI",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("track_id", "string", "Spotify track ID or URI", required=True)]),
                ActionSpec(id="get_recommendations", description="Get track recommendations",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("seed_tracks", "string", "Comma-separated track IDs (max 5 seeds total)", required=False),
                        ParamSpec("seed_artists", "string", "Comma-separated artist IDs", required=False),
                        ParamSpec("seed_genres", "string", "Comma-separated genres", required=False),
                        ParamSpec("limit", "integer", "Max recommendations (default 10)", required=False),
                    ]),
                ActionSpec(id="get_playlists", description="List the current user's playlists",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("limit", "integer", "Max results (default 20)", required=False)]),
                ActionSpec(id="get_playlist_tracks", description="Get tracks in a playlist",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("playlist_id", "string", "Spotify playlist ID", required=True),
                        ParamSpec("limit", "integer", "Max tracks (default 20)", required=False),
                    ]),
                ActionSpec(id="play", description="Start or resume playback (optionally with a URI)",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("uri", "string", "Spotify URI to play (track, album, or playlist)", required=False),
                        ParamSpec("device_id", "string", "Target device ID (optional)", required=False),
                    ]),
                ActionSpec(id="pause", description="Pause playback",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[ParamSpec("device_id", "string", "Target device ID (optional)", required=False)]),
                ActionSpec(id="next_track", description="Skip to the next track",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("device_id", "string", "Target device ID (optional)", required=False)]),
                ActionSpec(id="prev_track", description="Go back to the previous track",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("device_id", "string", "Target device ID (optional)", required=False)]),
                ActionSpec(id="queue_track", description="Add a track to the playback queue",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("uri", "string", "Spotify track URI", required=True),
                        ParamSpec("device_id", "string", "Target device ID (optional)", required=False),
                    ]),
                ActionSpec(id="set_volume", description="Set playback volume (0-100)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("volume_percent", "integer", "Volume level 0-100", required=True),
                        ParamSpec("device_id", "string", "Target device ID (optional)", required=False),
                    ]),
            ],
            homepage="https://spotify.com",
            docs_url="https://developer.spotify.com/documentation/web-api",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        token = secrets.get("access_token", "")
        if not token:
            return self.err("access_token not configured")
        headers = {"Authorization": f"Bearer {token}"}

        if action == "search":          return await self._search(params, headers)
        if action == "now_playing":     return await self._now_playing(headers)
        if action == "recently_played": return await self._recently_played(params, headers)
        if action == "get_track":       return await self._get_track(params, headers)
        if action == "get_recommendations": return await self._get_recommendations(params, headers)
        if action == "get_playlists":   return await self._get_playlists(params, headers)
        if action == "get_playlist_tracks": return await self._get_playlist_tracks(params, headers)
        if action == "play":            return await self._play(params, headers)
        if action == "pause":           return await self._player_action("pause", params, headers)
        if action == "next_track":      return await self._player_action("next", params, headers)
        if action == "prev_track":      return await self._player_action("previous", params, headers)
        if action == "queue_track":     return await self._queue_track(params, headers)
        if action == "set_volume":      return await self._set_volume(params, headers)
        return self.err(f"Unknown action: {action}")

    async def _search(self, p: dict, h: dict) -> dict:
        limit = min(int(p.get("limit") or 10), 50)
        t = p.get("type") or "track"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/search", headers=h, params={"q": p["query"], "type": t, "limit": limit})
        if r.status_code == 200:
            d = r.json()
            key = t + "s"
            items = d.get(key, {}).get("items", [])
            results = []
            for item in items:
                if t == "track":
                    results.append({"id": item["id"], "name": item["name"],
                        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                        "album": item.get("album", {}).get("name", ""), "uri": item["uri"],
                        "duration_ms": item.get("duration_ms")})
                elif t == "artist":
                    results.append({"id": item["id"], "name": item["name"], "uri": item["uri"],
                        "genres": item.get("genres", []), "popularity": item.get("popularity")})
                else:
                    results.append({"id": item["id"], "name": item["name"], "uri": item["uri"]})
            return self.ok({"type": t, "results": results, "count": len(results)})
        return self.err(f"Spotify {r.status_code}: {r.text[:200]}")

    async def _now_playing(self, h: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/me/player/currently-playing", headers=h)
        if r.status_code == 204:
            return self.ok({"playing": False, "track": None})
        if r.status_code == 200:
            d = r.json()
            item = d.get("item") or {}
            return self.ok({
                "playing": d.get("is_playing"),
                "progress_ms": d.get("progress_ms"),
                "track": {
                    "id": item.get("id"), "name": item.get("name"),
                    "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                    "album": item.get("album", {}).get("name"),
                    "duration_ms": item.get("duration_ms"),
                    "uri": item.get("uri"),
                },
            })
        return self.err(f"Spotify {r.status_code}")

    async def _recently_played(self, p: dict, h: dict) -> dict:
        limit = min(int(p.get("limit") or 10), 50)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/me/player/recently-played", headers=h, params={"limit": limit})
        if r.status_code == 200:
            items = [
                {"name": i["track"]["name"],
                 "artist": ", ".join(a["name"] for a in i["track"].get("artists", [])),
                 "played_at": i["played_at"], "uri": i["track"]["uri"]}
                for i in r.json().get("items", [])
            ]
            return self.ok({"tracks": items, "count": len(items)})
        return self.err(f"Spotify {r.status_code}")

    async def _get_track(self, p: dict, h: dict) -> dict:
        track_id = p["track_id"].split(":")[-1]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/tracks/{track_id}", headers=h)
        if r.status_code == 200:
            d = r.json()
            return self.ok({"id": d["id"], "name": d["name"],
                "artist": ", ".join(a["name"] for a in d.get("artists", [])),
                "album": d.get("album", {}).get("name"), "duration_ms": d.get("duration_ms"),
                "popularity": d.get("popularity"), "uri": d.get("uri")})
        return self.err(f"Spotify {r.status_code}")

    async def _get_recommendations(self, p: dict, h: dict) -> dict:
        qp: dict[str, Any] = {"limit": min(int(p.get("limit") or 10), 100)}
        if p.get("seed_tracks"):  qp["seed_tracks"]  = p["seed_tracks"]
        if p.get("seed_artists"): qp["seed_artists"] = p["seed_artists"]
        if p.get("seed_genres"):  qp["seed_genres"]  = p["seed_genres"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/recommendations", headers=h, params=qp)
        if r.status_code == 200:
            tracks = [{"name": t["name"], "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                "uri": t["uri"]} for t in r.json().get("tracks", [])]
            return self.ok({"tracks": tracks, "count": len(tracks)})
        return self.err(f"Spotify {r.status_code}: {r.text[:200]}")

    async def _get_playlists(self, p: dict, h: dict) -> dict:
        limit = min(int(p.get("limit") or 20), 50)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/me/playlists", headers=h, params={"limit": limit})
        if r.status_code == 200:
            playlists = [{"id": pl["id"], "name": pl["name"], "tracks": pl["tracks"]["total"],
                "uri": pl["uri"]} for pl in r.json().get("items", [])]
            return self.ok({"playlists": playlists, "count": len(playlists)})
        return self.err(f"Spotify {r.status_code}")

    async def _get_playlist_tracks(self, p: dict, h: dict) -> dict:
        limit = min(int(p.get("limit") or 20), 100)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/playlists/{p['playlist_id']}/tracks", headers=h, params={"limit": limit})
        if r.status_code == 200:
            tracks = [{"name": item["track"]["name"] if item.get("track") else "",
                "artist": ", ".join(a["name"] for a in (item.get("track") or {}).get("artists", [])),
                "uri": (item.get("track") or {}).get("uri")}
                for item in r.json().get("items", []) if item.get("track")]
            return self.ok({"tracks": tracks, "count": len(tracks)})
        return self.err(f"Spotify {r.status_code}")

    async def _play(self, p: dict, h: dict) -> dict:
        body: dict[str, Any] = {}
        qp: dict[str, Any] = {}
        if p.get("device_id"): qp["device_id"] = p["device_id"]
        if p.get("uri"):
            uri = p["uri"]
            if ":track:" in uri:
                body["uris"] = [uri]
            else:
                body["context_uri"] = uri
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.put(f"{_API}/me/player/play", headers=h, json=body, params=qp)
        if r.status_code in (200, 204):
            return self.ok({"playing": True})
        return self.err(f"Spotify {r.status_code}: {r.text[:200]}")

    async def _player_action(self, endpoint: str, p: dict, h: dict) -> dict:
        qp: dict[str, Any] = {}
        if p.get("device_id"): qp["device_id"] = p["device_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            if endpoint == "pause":
                r = await c.put(f"{_API}/me/player/pause", headers=h, params=qp)
            else:
                r = await c.post(f"{_API}/me/player/{endpoint}", headers=h, params=qp)
        if r.status_code in (200, 204):
            return self.ok({"action": endpoint})
        return self.err(f"Spotify {r.status_code}")

    async def _queue_track(self, p: dict, h: dict) -> dict:
        qp: dict[str, Any] = {"uri": p["uri"]}
        if p.get("device_id"): qp["device_id"] = p["device_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_API}/me/player/queue", headers=h, params=qp)
        if r.status_code in (200, 204):
            return self.ok({"queued": p["uri"]})
        return self.err(f"Spotify {r.status_code}")

    async def _set_volume(self, p: dict, h: dict) -> dict:
        vol = max(0, min(100, int(p["volume_percent"])))
        qp: dict[str, Any] = {"volume_percent": vol}
        if p.get("device_id"): qp["device_id"] = p["device_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.put(f"{_API}/me/player/volume", headers=h, params=qp)
        if r.status_code in (200, 204):
            return self.ok({"volume": vol})
        return self.err(f"Spotify {r.status_code}")
