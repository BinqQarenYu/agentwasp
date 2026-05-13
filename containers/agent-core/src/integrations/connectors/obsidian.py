"""Obsidian connector — local vault file operations via Obsidian Local REST API.

Requires the "Local REST API" community plugin installed and enabled in Obsidian.
Plugin generates an API key; vault must be open and Obsidian running.
Default port: 27124 (HTTP) or 27123 (HTTPS).

Alternatively, operates in DIRECT FILE MODE if obsidian_rest_url is omitted:
reads/writes markdown files directly from a mounted vault path.

Secrets:
    obsidian_rest_url — Base URL of the REST API plugin (e.g. http://localhost:27124)
    api_key           — API key from the Local REST API plugin settings
    vault_path        — (fallback) Filesystem path to vault for direct file access

Actions:
    list_notes      — List notes in a folder                          (LOW)
    get_note        — Read a note by path                             (LOW)
    create_note     — Create or overwrite a note                      (MEDIUM)
    append_note     — Append content to an existing note              (MEDIUM)
    delete_note     — Delete a note                                   (HIGH)
    search          — Full-text search across vault                   (LOW)
    get_tags        — List all tags in the vault                      (LOW)
    daily_note      — Read or create today's daily note               (MEDIUM)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_TIMEOUT = 15


class ObsidianConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="obsidian", version="1.0.0", name="Obsidian", category="productivity",
            description=(
                "Read and write notes in your Obsidian vault via the Local REST API plugin. "
                "Requires Obsidian running with 'Local REST API' community plugin enabled."
            ),
            capabilities=["read_notes", "write_notes", "search_vault", "daily_notes", "tag_management"],
            risk_level=RiskLevel.HIGH,
            required_secrets=["obsidian_rest_url", "api_key"],
            config_schema={},
            rate_limits={
                "list_notes":  RateLimit(requests_per_minute=30),
                "get_note":    RateLimit(requests_per_minute=30),
                "create_note": RateLimit(requests_per_minute=10),
                "append_note": RateLimit(requests_per_minute=20),
                "delete_note": RateLimit(requests_per_minute=5),
                "search":      RateLimit(requests_per_minute=20),
                "get_tags":    RateLimit(requests_per_minute=10),
                "daily_note":  RateLimit(requests_per_minute=20),
            },
            actions=[
                ActionSpec(id="list_notes", description="List notes in a vault folder (recursive)",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("folder", "string", "Vault folder path (default: root '')", required=False),
                        ParamSpec("limit", "integer", "Max files to return (default 50)", required=False),
                    ]),
                ActionSpec(id="get_note", description="Read the full content of a note by its vault path",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("path", "string", "Note path relative to vault root (e.g. 'Folder/Note.md')", required=True),
                    ]),
                ActionSpec(id="create_note", description="Create a new note or overwrite an existing one",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("path", "string", "Note path (e.g. 'Projects/MyNote.md')", required=True),
                        ParamSpec("content", "string", "Markdown content for the note", required=True),
                    ]),
                ActionSpec(id="append_note", description="Append text to the end of an existing note",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("path", "string", "Note path in vault", required=True),
                        ParamSpec("content", "string", "Content to append", required=True),
                    ]),
                ActionSpec(id="delete_note", description="Permanently delete a note from the vault",
                    risk_level=RiskLevel.HIGH, capability="restricted",
                    params=[
                        ParamSpec("path", "string", "Note path to delete", required=True),
                    ]),
                ActionSpec(id="search", description="Full-text search across the entire vault",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("query", "string", "Search query text", required=True),
                        ParamSpec("context_length", "integer", "Characters of context around match (default 100)", required=False),
                        ParamSpec("limit", "integer", "Max results (default 10)", required=False),
                    ]),
                ActionSpec(id="get_tags", description="Get all tags used in the vault with usage counts",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="daily_note", description="Get or create today's daily note",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("action", "string", "get or create (default: get)", required=False),
                        ParamSpec("content", "string", "Content for new note (only for action=create)", required=False),
                        ParamSpec("date", "string", "Date in YYYY-MM-DD format (default: today)", required=False),
                    ]),
            ],
            homepage="https://obsidian.md",
            docs_url="https://github.com/coddingtonbear/obsidian-local-rest-api",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        base_url = (secrets.get("obsidian_rest_url") or "").rstrip("/")
        api_key  = secrets.get("api_key", "")

        if not base_url:
            vault_path = secrets.get("vault_path", "")
            if not vault_path:
                return self.err("obsidian_rest_url (or vault_path fallback) secret is required")
            return await self._direct_file_mode(action, params, vault_path)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "text/markdown",
            "Accept":        "application/json",
        }

        try:
            if action == "list_notes":  return await self._list_notes(base_url, headers, params)
            if action == "get_note":    return await self._get_note(base_url, headers, params)
            if action == "create_note": return await self._create_note(base_url, headers, params)
            if action == "append_note": return await self._append_note(base_url, headers, params)
            if action == "delete_note": return await self._delete_note(base_url, headers, params)
            if action == "search":      return await self._search(base_url, headers, params)
            if action == "get_tags":    return await self._get_tags(base_url, headers)
            if action == "daily_note":  return await self._daily_note(base_url, headers, params)
        except httpx.ConnectError:
            return self.err("Cannot connect to Obsidian REST API. Is Obsidian running with Local REST API plugin enabled?")
        except Exception as exc:
            logger.error("obsidian.error", action=action, error=str(exc))
            return self.err(f"Obsidian error: {exc}")

        return self.err(f"Unknown action: {action}")

    async def _list_notes(self, base: str, headers: dict, params: dict) -> dict:
        folder  = (params.get("folder") or "").strip("/")
        limit   = min(int(params.get("limit") or 50), 500)
        path    = f"/vault/{folder}/" if folder else "/vault/"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}{path}", headers={**headers, "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
        files = [f for f in data.get("files", []) if f.endswith(".md")][:limit]
        return self.ok({"files": files, "count": len(files), "folder": folder or "/"})

    async def _get_note(self, base: str, headers: dict, params: dict) -> dict:
        path    = params.get("path", "")
        if not path:
            return self.err("path is required")
        encoded = path.replace(" ", "%20")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/vault/{encoded}", headers={**headers, "Accept": "text/markdown"})
            if r.status_code == 404:
                return self.err(f"Note not found: {path}")
            r.raise_for_status()
            content = r.text
        return self.ok({"path": path, "content": content, "chars": len(content)})

    async def _create_note(self, base: str, headers: dict, params: dict) -> dict:
        path    = params.get("path", "")
        content = params.get("content", "")
        if not path:
            return self.err("path is required")
        encoded = path.replace(" ", "%20")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(f"{base}/vault/{encoded}", headers=headers, content=content.encode("utf-8"))
        return self.ok({"path": path, "created": r.status_code in (200, 201, 204), "chars": len(content)})

    async def _append_note(self, base: str, headers: dict, params: dict) -> dict:
        path    = params.get("path", "")
        content = params.get("content", "")
        if not path or not content:
            return self.err("path and content are required")
        encoded = path.replace(" ", "%20")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/vault/{encoded}", headers=headers, content=content.encode("utf-8"))
        return self.ok({"path": path, "appended": r.status_code in (200, 204), "chars": len(content)})

    async def _delete_note(self, base: str, headers: dict, params: dict) -> dict:
        path = params.get("path", "")
        if not path:
            return self.err("path is required")
        encoded = path.replace(" ", "%20")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.delete(f"{base}/vault/{encoded}", headers={**headers, "Content-Type": "application/json"})
        return self.ok({"path": path, "deleted": r.status_code in (200, 204)})

    async def _search(self, base: str, headers: dict, params: dict) -> dict:
        query   = params.get("query", "")
        ctx_len = min(int(params.get("context_length") or 100), 500)
        limit   = min(int(params.get("limit") or 10), 100)
        if not query:
            return self.err("query is required")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{base}/search/simple/",
                headers={**headers, "Content-Type": "application/json"},
                params={"query": query, "contextLength": ctx_len},
            )
            r.raise_for_status()
            data = r.json()
        results = [
            {"filename": item.get("filename", ""), "score": item.get("score", 0), "matches": item.get("matches", [])}
            for item in data[:limit]
        ]
        return self.ok({"query": query, "results": results, "count": len(results)})

    async def _get_tags(self, base: str, headers: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/tags/", headers={**headers, "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
        if isinstance(data, dict):
            tags = [{"tag": k, "count": v} for k, v in sorted(data.items(), key=lambda x: -x[1])]
        else:
            tags = [{"tag": t} for t in data]
        return self.ok({"tags": tags, "count": len(tags)})

    async def _daily_note(self, base: str, headers: dict, params: dict) -> dict:
        action_param = (params.get("action") or "get").lower()
        date_str     = params.get("date") or datetime.now().strftime("%Y-%m-%d")
        endpoint     = f"{base}/periodic/daily/"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if action_param == "create":
                content = params.get("content", f"# {date_str}\n\n")
                r = await client.post(endpoint, headers=headers, content=content.encode("utf-8"))
                return self.ok({"date": date_str, "created": r.status_code in (200, 201, 204)})
            else:
                r = await client.get(endpoint, headers={**headers, "Accept": "text/markdown"})
                if r.status_code == 404:
                    return self.ok({"date": date_str, "exists": False, "content": ""})
                r.raise_for_status()
                content = r.text
                return self.ok({"date": date_str, "exists": True, "content": content, "chars": len(content)})

    # ------------------------------------------------------------------
    # Direct file mode fallback (no REST API, vault on filesystem)
    # ------------------------------------------------------------------

    async def _direct_file_mode(self, action: str, params: dict, vault_path: str) -> dict:
        vault = Path(vault_path)
        if not vault.exists():
            return self.err(f"Vault path not found: {vault_path}")

        if action == "list_notes":
            folder = params.get("folder") or ""
            limit  = min(int(params.get("limit") or 50), 500)
            base   = vault / folder if folder else vault
            files  = [str(p.relative_to(vault)) for p in base.rglob("*.md") if p.is_file()][:limit]
            return self.ok({"files": files, "count": len(files), "folder": folder or "/"})

        if action == "get_note":
            path = params.get("path", "")
            note = vault / path
            if not note.exists():
                return self.err(f"Note not found: {path}")
            content = note.read_text(encoding="utf-8")
            return self.ok({"path": path, "content": content, "chars": len(content)})

        if action == "create_note":
            path    = params.get("path", "")
            content = params.get("content", "")
            note    = vault / path
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(content, encoding="utf-8")
            return self.ok({"path": path, "created": True, "chars": len(content)})

        if action == "append_note":
            path    = params.get("path", "")
            content = params.get("content", "")
            note    = vault / path
            if not note.exists():
                return self.err(f"Note not found: {path}")
            with note.open("a", encoding="utf-8") as f:
                f.write(content)
            return self.ok({"path": path, "appended": True, "chars": len(content)})

        if action == "delete_note":
            path = params.get("path", "")
            note = vault / path
            if not note.exists():
                return self.err(f"Note not found: {path}")
            note.unlink()
            return self.ok({"path": path, "deleted": True})

        if action == "daily_note":
            date_str   = params.get("date") or datetime.now().strftime("%Y-%m-%d")
            daily_path = f"Daily/{date_str}.md"
            note       = vault / daily_path
            if params.get("action") == "create" or not note.exists():
                note.parent.mkdir(parents=True, exist_ok=True)
                content = params.get("content") or f"# {date_str}\n\n"
                note.write_text(content, encoding="utf-8")
                return self.ok({"date": date_str, "created": True})
            content = note.read_text(encoding="utf-8")
            return self.ok({"date": date_str, "exists": True, "content": content})

        return self.err(f"Action '{action}' not supported in direct file mode")
