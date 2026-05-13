"""Notion connector — REST API v1.

Secrets (stored in vault):
    token   — Notion Integration Token (Internal Integration secret)

Actions:
    search         — Search pages and databases                         (LOW)
    get_page       — Fetch a page by ID                                 (LOW)
    create_page    — Create a new page in a database or as child        (MEDIUM)
    update_page    — Update page properties (not content)               (MEDIUM)
    get_database   — Fetch database schema + metadata                   (LOW)
    query_database — Query a database with optional filters             (LOW)
    append_blocks  — Append block content to a page                     (MEDIUM)
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import (
    ActionSpec, BaseConnector, ConnectorManifest,
    ParamSpec, RateLimit, RiskLevel,
)

logger = structlog.get_logger()
_TIMEOUT = 20.0
_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class NotionConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "notion",
            version     = "1.0.0",
            name        = "Notion",
            category    = "productivity",
            description = "Read and write Notion pages and databases via the Notion API.",
            capabilities = [
                "search_content",
                "read_pages",
                "create_pages",
                "update_pages",
                "query_databases",
                "append_content",
            ],
            risk_level       = RiskLevel.MEDIUM,
            required_secrets = ["token"],
            config_schema    = {},
            rate_limits      = {
                "search":         RateLimit(requests_per_minute=30),
                "get_page":       RateLimit(requests_per_minute=60),
                "create_page":    RateLimit(requests_per_minute=20),
                "update_page":    RateLimit(requests_per_minute=20),
                "get_database":   RateLimit(requests_per_minute=30),
                "query_database": RateLimit(requests_per_minute=20),
                "append_blocks":  RateLimit(requests_per_minute=20),
            },
            actions = [
                ActionSpec(
                    id="search", description="Search Notion pages and databases by keyword",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query",  "string",  "Search query text",                   required=True),
                        ParamSpec("filter", "string",  "Filter by: page|database (optional)", required=False),
                        ParamSpec("limit",  "integer", "Max results (default 10)",            required=False),
                    ],
                ),
                ActionSpec(
                    id="get_page", description="Fetch a Notion page by its ID",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("page_id", "string", "Notion page ID (UUID or dashed UUID)", required=True),
                    ],
                ),
                ActionSpec(
                    id="create_page", description="Create a new Notion page",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("parent_id",   "string", "Parent page or database ID",          required=True),
                        ParamSpec("parent_type", "string", "Parent type: page|database",          required=True),
                        ParamSpec("title",       "string", "Page title",                          required=True),
                        ParamSpec("properties",  "object", "Additional properties (database rows)",required=False),
                        ParamSpec("content",     "string", "Optional initial text content",       required=False),
                    ],
                ),
                ActionSpec(
                    id="update_page", description="Update properties of a Notion page (not block content)",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("page_id",    "string",  "Notion page ID",                  required=True),
                        ParamSpec("properties", "object",  "Properties to update",            required=True),
                        ParamSpec("archived",   "boolean", "Archive (true) or restore (false)", required=False),
                    ],
                ),
                ActionSpec(
                    id="get_database", description="Fetch a database schema and metadata",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("database_id", "string", "Notion database ID", required=True),
                    ],
                ),
                ActionSpec(
                    id="query_database", description="Query a Notion database with optional filters",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("database_id", "string",  "Notion database ID",              required=True),
                        ParamSpec("filter",      "object",  "Notion filter object (optional)", required=False),
                        ParamSpec("sorts",       "array",   "Sort specifications (optional)",  required=False),
                        ParamSpec("limit",       "integer", "Max rows to return (default 20)", required=False),
                    ],
                ),
                ActionSpec(
                    id="append_blocks", description="Append block content (paragraphs, headings, etc.) to a page",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("page_id", "string", "Notion page ID",           required=True),
                        ParamSpec("blocks",  "array",  "Array of Notion block objects", required=True),
                    ],
                ),
            ],
            homepage = "https://notion.so",
            docs_url = "https://developers.notion.com/reference/intro",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "search":         return await self._search(params, secrets)
        if action == "get_page":       return await self._get_page(params, secrets)
        if action == "create_page":    return await self._create_page(params, secrets)
        if action == "update_page":    return await self._update_page(params, secrets)
        if action == "get_database":   return await self._get_database(params, secrets)
        if action == "query_database": return await self._query_database(params, secrets)
        if action == "append_blocks":  return await self._append_blocks(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------

    def _headers(self, token: str) -> dict:
        return {
            "Authorization":  f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type":   "application/json",
        }

    async def _req(self, method: str, path: str, token: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            return await c.request(
                method,
                f"{_API}/{path.lstrip('/')}",
                headers=self._headers(token),
                **kwargs,
            )

    def _extract_title(self, page: dict) -> str:
        """Extract plain text title from a Notion page object."""
        props = page.get("properties", {})
        for key in ("Name", "Title", "title"):
            if key in props:
                p = props[key]
                rich = p.get("title") or p.get("rich_text") or []
                return "".join(r.get("plain_text", "") for r in rich) or "(untitled)"
        return "(untitled)"

    async def _search(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        body: dict[str, Any] = {"query": p.get("query", ""), "page_size": min(int(p.get("limit") or 10), 50)}
        if p.get("filter"):
            obj_type = p["filter"].lower()
            if obj_type in ("page", "database"):
                body["filter"] = {"value": obj_type, "property": "object"}
        r = await self._req("POST", "/search", token, json=body)
        if r.status_code == 200:
            results = []
            for item in r.json().get("results", []):
                obj_type = item.get("object")
                title = self._extract_title(item) if obj_type == "page" else (
                    "".join(t.get("plain_text", "") for t in item.get("title", [])) or "(untitled)"
                )
                results.append({"id": item["id"], "type": obj_type, "title": title, "url": item.get("url")})
            return self.ok({"results": results, "count": len(results)})
        return self.err(f"Notion {r.status_code}: {r.text[:300]}")

    async def _get_page(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        r = await self._req("GET", f"/pages/{p['page_id']}", token)
        if r.status_code == 200:
            d = r.json()
            return self.ok({
                "id":         d["id"],
                "title":      self._extract_title(d),
                "url":        d.get("url"),
                "created_at": d.get("created_time"),
                "updated_at": d.get("last_edited_time"),
                "archived":   d.get("archived", False),
            })
        return self.err(f"Notion {r.status_code}")

    async def _create_page(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        parent_type = p.get("parent_type", "page").lower()
        if parent_type == "database":
            parent = {"database_id": p["parent_id"]}
            props: dict[str, Any] = p.get("properties") or {}
            # Ensure title property exists
            if "Name" not in props and "Title" not in props:
                props["Name"] = {"title": [{"text": {"content": p.get("title", "")}}]}
        else:
            parent = {"page_id": p["parent_id"]}
            props = {"title": [{"text": {"content": p.get("title", "")}}]}

        body: dict[str, Any] = {"parent": parent, "properties": props}
        if p.get("content"):
            body["children"] = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": p["content"][:2000]}}]}}]

        r = await self._req("POST", "/pages", token, json=body)
        if r.status_code == 200:
            d = r.json()
            return self.ok({"id": d["id"], "url": d.get("url"), "title": self._extract_title(d)})
        return self.err(f"Notion {r.status_code}: {r.text[:300]}")

    async def _update_page(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        body: dict[str, Any] = {"properties": p.get("properties", {})}
        if p.get("archived") is not None:
            body["archived"] = bool(p["archived"])
        r = await self._req("PATCH", f"/pages/{p['page_id']}", token, json=body)
        if r.status_code == 200:
            d = r.json()
            return self.ok({"id": d["id"], "archived": d.get("archived", False)})
        return self.err(f"Notion {r.status_code}: {r.text[:300]}")

    async def _get_database(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        r = await self._req("GET", f"/databases/{p['database_id']}", token)
        if r.status_code == 200:
            d = r.json()
            title = "".join(t.get("plain_text", "") for t in d.get("title", [])) or "(untitled)"
            properties = {k: v.get("type") for k, v in d.get("properties", {}).items()}
            return self.ok({"id": d["id"], "title": title, "url": d.get("url"), "properties": properties})
        return self.err(f"Notion {r.status_code}")

    async def _query_database(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        body: dict[str, Any] = {"page_size": min(int(p.get("limit") or 20), 100)}
        if p.get("filter"): body["filter"] = p["filter"]
        if p.get("sorts"):  body["sorts"]  = p["sorts"]
        r = await self._req("POST", f"/databases/{p['database_id']}/query", token, json=body)
        if r.status_code == 200:
            rows = [
                {"id": item["id"], "title": self._extract_title(item), "url": item.get("url")}
                for item in r.json().get("results", [])
            ]
            return self.ok({"rows": rows, "count": len(rows)})
        return self.err(f"Notion {r.status_code}: {r.text[:300]}")

    async def _append_blocks(self, p: dict, secrets: dict) -> dict:
        token = secrets.get("token", "")
        if not token:
            return self.err("token not configured")
        blocks = p.get("blocks", [])
        if not blocks:
            return self.err("No blocks provided")
        r = await self._req("PATCH", f"/blocks/{p['page_id']}/children", token, json={"children": blocks})
        if r.status_code == 200:
            d = r.json()
            return self.ok({"appended": len(d.get("results", [])), "page_id": p["page_id"]})
        return self.err(f"Notion {r.status_code}: {r.text[:300]}")
