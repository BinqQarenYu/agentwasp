"""GIF Search connector — Giphy API.

Searches and retrieves animated GIFs from the Giphy catalog.

Secrets:
    api_key     — Giphy API key (free tier: 100 req/day; production: unlimited)

Actions:
    search      — Search GIFs by keyword                              (LOW)
    trending    — Get trending GIFs                                    (LOW)
    random      — Get a random GIF (optionally filtered by tag)        (LOW)
    translate   — Convert a phrase to a single best-match GIF          (LOW)
    get_gif     — Get metadata for a specific GIF by ID               (LOW)
"""
from __future__ import annotations

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_API = "https://api.giphy.com/v1/gifs"
_TIMEOUT = 10.0


def _fmt(gif: dict) -> dict:
    """Extract key fields from a Giphy GIF object."""
    images = gif.get("images", {})
    original = images.get("original", {})
    preview  = images.get("fixed_width_small", {})
    return {
        "id":           gif.get("id"),
        "title":        gif.get("title"),
        "url":          gif.get("url"),
        "gif_url":      original.get("url"),
        "preview_url":  preview.get("url"),
        "width":        original.get("width"),
        "height":       original.get("height"),
        "rating":       gif.get("rating"),
    }


class GifSearchConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="gif-search", version="1.0.0", name="GIF Search", category="media",
            description="Search and retrieve animated GIFs via Giphy API.",
            capabilities=["search_gifs", "trending_gifs", "random_gifs", "phrase_to_gif"],
            risk_level=RiskLevel.LOW,
            required_secrets=["api_key"],
            config_schema={},
            rate_limits={
                "search":    RateLimit(requests_per_minute=30),
                "trending":  RateLimit(requests_per_minute=30),
                "random":    RateLimit(requests_per_minute=30),
                "translate": RateLimit(requests_per_minute=30),
                "get_gif":   RateLimit(requests_per_minute=60),
            },
            actions=[
                ActionSpec(id="search", description="Search Giphy for GIFs matching a keyword or phrase",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query", "string", "Search query", required=True),
                        ParamSpec("limit", "integer", "Max results (default 5)", required=False),
                        ParamSpec("rating", "string", "Content rating: g|pg|pg-13|r (default g)", required=False),
                        ParamSpec("lang", "string", "Language code (default en)", required=False),
                    ]),
                ActionSpec(id="trending", description="Get currently trending GIFs",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("limit", "integer", "Max results (default 5)", required=False),
                        ParamSpec("rating", "string", "Content rating: g|pg|pg-13|r (default g)", required=False),
                    ]),
                ActionSpec(id="random", description="Get a random GIF (optionally filtered by tag)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("tag", "string", "Optional tag to filter random GIF", required=False),
                        ParamSpec("rating", "string", "Content rating (default g)", required=False),
                    ]),
                ActionSpec(id="translate", description="Convert a phrase/word to the best-match single GIF",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("phrase", "string", "Phrase or word to translate to a GIF", required=True),
                    ]),
                ActionSpec(id="get_gif", description="Get metadata for a specific GIF by Giphy ID",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("gif_id", "string", "Giphy GIF ID", required=True)]),
            ],
            homepage="https://giphy.com",
            docs_url="https://developers.giphy.com/docs/api/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        key = secrets.get("api_key", "")
        if not key:
            return self.err("api_key not configured")

        if action == "search":
            return await self._search(params, key)
        if action == "trending":
            return await self._trending(params, key)
        if action == "random":
            return await self._random(params, key)
        if action == "translate":
            return await self._translate(params, key)
        if action == "get_gif":
            return await self._get_gif(params, key)
        return self.err(f"Unknown action: {action}")

    async def _search(self, p: dict, key: str) -> dict:
        limit = min(int(p.get("limit") or 5), 25)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/search", params={
                "api_key": key, "q": p["query"], "limit": limit,
                "rating": p.get("rating") or "g",
                "lang": p.get("lang") or "en",
            })
        if r.status_code == 200:
            gifs = [_fmt(g) for g in r.json().get("data", [])]
            return self.ok({"gifs": gifs, "count": len(gifs), "query": p["query"]})
        return self.err(f"Giphy {r.status_code}")

    async def _trending(self, p: dict, key: str) -> dict:
        limit = min(int(p.get("limit") or 5), 25)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/trending", params={
                "api_key": key, "limit": limit, "rating": p.get("rating") or "g",
            })
        if r.status_code == 200:
            gifs = [_fmt(g) for g in r.json().get("data", [])]
            return self.ok({"gifs": gifs, "count": len(gifs)})
        return self.err(f"Giphy {r.status_code}")

    async def _random(self, p: dict, key: str) -> dict:
        qp: dict = {"api_key": key, "rating": p.get("rating") or "g"}
        if p.get("tag"): qp["tag"] = p["tag"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/random", params=qp)
        if r.status_code == 200:
            return self.ok(_fmt(r.json().get("data", {})))
        return self.err(f"Giphy {r.status_code}")

    async def _translate(self, p: dict, key: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/translate", params={"api_key": key, "s": p["phrase"]})
        if r.status_code == 200:
            return self.ok(_fmt(r.json().get("data", {})))
        return self.err(f"Giphy {r.status_code}")

    async def _get_gif(self, p: dict, key: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}/{p['gif_id']}", params={"api_key": key})
        if r.status_code == 200:
            return self.ok(_fmt(r.json().get("data", {})))
        return self.err(f"Giphy {r.status_code}")
