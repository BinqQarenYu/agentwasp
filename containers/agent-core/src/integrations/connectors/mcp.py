"""MCP (Model Context Protocol) connector — client side.

Connects to a running MCP server over stdio (subprocess) or HTTP (SSE/streamable).
WASP can call tools exposed by any MCP server.

Secrets (stored in vault):
    server_url   — HTTP MCP server base URL (for HTTP transport)
    auth_token   — Optional Bearer token for HTTP transport

Config (non-secret):
    command      — stdio command to launch MCP server (e.g. "npx -y @modelcontextprotocol/server-filesystem /data")
    transport    — "http" or "stdio" (default: "http" if server_url set, else "stdio")

Actions:
    list_tools    — List tools available on the MCP server            (LOW)
    call_tool     — Call a specific MCP tool with arguments           (MEDIUM)
    list_resources— List resources available on the MCP server        (LOW)
    read_resource — Read a specific resource by URI                   (LOW)
    list_prompts  — List prompts available on the MCP server          (LOW)
    get_prompt    — Get a prompt by name with optional arguments       (LOW)
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import structlog

from ..base import (
    ActionSpec, BaseConnector, ConnectorManifest,
    ParamSpec, RateLimit, RiskLevel,
)

logger = structlog.get_logger()
_TIMEOUT = 30.0


class MCPConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id          = "mcp",
            version     = "1.0.0",
            name        = "MCP Client",
            category    = "tools",
            description = "Model Context Protocol client — call tools, read resources, and use prompts from any MCP server.",
            capabilities = [
                "list_tools",
                "call_tools",
                "list_resources",
                "read_resources",
                "list_prompts",
                "get_prompts",
            ],
            risk_level       = RiskLevel.MEDIUM,
            required_secrets = [],  # server_url optional (may use stdio via config)
            config_schema    = {
                "type": "object",
                "properties": {
                    "command":   {"type": "string", "description": "stdio command to launch MCP server"},
                    "transport": {"type": "string", "enum": ["http", "stdio"], "default": "http"},
                },
            },
            rate_limits      = {
                "list_tools":     RateLimit(requests_per_minute=30),
                "call_tool":      RateLimit(requests_per_minute=30),
                "list_resources": RateLimit(requests_per_minute=30),
                "read_resource":  RateLimit(requests_per_minute=20),
                "list_prompts":   RateLimit(requests_per_minute=30),
                "get_prompt":     RateLimit(requests_per_minute=20),
            },
            actions = [
                ActionSpec(
                    id="list_tools", description="List all tools available on the MCP server",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("server_url", "string", "Override MCP server URL for this call", required=False),
                    ],
                ),
                ActionSpec(
                    id="call_tool", description="Call a specific tool on the MCP server",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("name",       "string", "Tool name",                             required=True),
                        ParamSpec("arguments",  "object", "Tool arguments (as defined by the tool)", required=False),
                        ParamSpec("server_url", "string", "Override MCP server URL for this call",   required=False),
                    ],
                ),
                ActionSpec(
                    id="list_resources", description="List all resources available on the MCP server",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("server_url", "string", "Override MCP server URL", required=False),
                    ],
                ),
                ActionSpec(
                    id="read_resource", description="Read a specific resource by URI",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("uri",        "string", "Resource URI",                          required=True),
                        ParamSpec("server_url", "string", "Override MCP server URL for this call", required=False),
                    ],
                ),
                ActionSpec(
                    id="list_prompts", description="List all prompts available on the MCP server",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("server_url", "string", "Override MCP server URL", required=False),
                    ],
                ),
                ActionSpec(
                    id="get_prompt", description="Get a prompt by name with optional arguments",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("name",       "string", "Prompt name",                                   required=True),
                        ParamSpec("arguments",  "object", "Prompt arguments (key-value string pairs)",     required=False),
                        ParamSpec("server_url", "string", "Override MCP server URL for this call",         required=False),
                    ],
                ),
            ],
            homepage = "https://modelcontextprotocol.io",
            docs_url = "https://spec.modelcontextprotocol.io",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        if action == "list_tools":     return await self._list_tools(params, secrets)
        if action == "call_tool":      return await self._call_tool(params, secrets)
        if action == "list_resources": return await self._list_resources(params, secrets)
        if action == "read_resource":  return await self._read_resource(params, secrets)
        if action == "list_prompts":   return await self._list_prompts(params, secrets)
        if action == "get_prompt":     return await self._get_prompt(params, secrets)
        return self.err(f"Unknown action: {action}")

    # ------------------------------------------------------------------
    # JSON-RPC over HTTP transport
    # ------------------------------------------------------------------

    def _get_url(self, params: dict, secrets: dict) -> str | None:
        return params.get("server_url") or secrets.get("server_url") or None

    def _make_request(self, method: str, params_body: Any = None) -> dict:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id":      str(uuid.uuid4()),
            "method":  method,
        }
        if params_body is not None:
            body["params"] = params_body
        return body

    async def _rpc(self, url: str, method: str, params_body: Any, auth_token: str | None) -> dict:
        """Send a JSON-RPC 2.0 request to an HTTP MCP server."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        payload = self._make_request(method, params_body)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"MCP error {data['error'].get('code')}: {data['error'].get('message')}")
        return data.get("result", {})

    async def _list_tools(self, p: dict, secrets: dict) -> dict:
        url = self._get_url(p, secrets)
        if not url:
            return self.err("server_url not configured")
        try:
            result = await self._rpc(url, "tools/list", None, secrets.get("auth_token"))
            tools = result.get("tools", [])
            return self.ok({
                "tools": [
                    {"name": t["name"], "description": t.get("description", ""),
                     "input_schema": t.get("inputSchema", {})}
                    for t in tools
                ],
                "count": len(tools),
            })
        except Exception as exc:
            return self.err(str(exc))

    async def _call_tool(self, p: dict, secrets: dict) -> dict:
        url = self._get_url(p, secrets)
        if not url:
            return self.err("server_url not configured")
        name = p.get("name", "")
        if not name:
            return self.err("Tool name is required")
        try:
            result = await self._rpc(
                url, "tools/call",
                {"name": name, "arguments": p.get("arguments") or {}},
                secrets.get("auth_token"),
            )
            content = result.get("content", [])
            # Flatten text content blocks
            text_parts = [
                c.get("text", "") for c in content
                if c.get("type") == "text"
            ]
            return self.ok({
                "tool":    name,
                "content": content,
                "text":    "\n".join(text_parts)[:8000],
                "is_error": result.get("isError", False),
            })
        except Exception as exc:
            return self.err(str(exc))

    async def _list_resources(self, p: dict, secrets: dict) -> dict:
        url = self._get_url(p, secrets)
        if not url:
            return self.err("server_url not configured")
        try:
            result = await self._rpc(url, "resources/list", None, secrets.get("auth_token"))
            resources = result.get("resources", [])
            return self.ok({
                "resources": [
                    {"uri": r["uri"], "name": r.get("name", ""), "description": r.get("description", ""),
                     "mimeType": r.get("mimeType", "")}
                    for r in resources
                ],
                "count": len(resources),
            })
        except Exception as exc:
            return self.err(str(exc))

    async def _read_resource(self, p: dict, secrets: dict) -> dict:
        url = self._get_url(p, secrets)
        if not url:
            return self.err("server_url not configured")
        uri = p.get("uri", "")
        if not uri:
            return self.err("uri is required")
        try:
            result = await self._rpc(url, "resources/read", {"uri": uri}, secrets.get("auth_token"))
            contents = result.get("contents", [])
            text_parts = [
                c.get("text", "") for c in contents
                if c.get("type") == "text" or "text" in c
            ]
            return self.ok({
                "uri":      uri,
                "contents": contents,
                "text":     "\n".join(text_parts)[:8000],
            })
        except Exception as exc:
            return self.err(str(exc))

    async def _list_prompts(self, p: dict, secrets: dict) -> dict:
        url = self._get_url(p, secrets)
        if not url:
            return self.err("server_url not configured")
        try:
            result = await self._rpc(url, "prompts/list", None, secrets.get("auth_token"))
            prompts = result.get("prompts", [])
            return self.ok({
                "prompts": [
                    {"name": pr["name"], "description": pr.get("description", ""),
                     "arguments": pr.get("arguments", [])}
                    for pr in prompts
                ],
                "count": len(prompts),
            })
        except Exception as exc:
            return self.err(str(exc))

    async def _get_prompt(self, p: dict, secrets: dict) -> dict:
        url = self._get_url(p, secrets)
        if not url:
            return self.err("server_url not configured")
        name = p.get("name", "")
        if not name:
            return self.err("Prompt name is required")
        try:
            result = await self._rpc(
                url, "prompts/get",
                {"name": name, "arguments": {str(k): str(v) for k, v in (p.get("arguments") or {}).items()}},
                secrets.get("auth_token"),
            )
            messages = result.get("messages", [])
            return self.ok({
                "name":        name,
                "description": result.get("description", ""),
                "messages":    messages,
                "text":        "\n".join(
                    m.get("content", {}).get("text", "")
                    for m in messages if isinstance(m.get("content"), dict)
                )[:8000],
            })
        except Exception as exc:
            return self.err(str(exc))
