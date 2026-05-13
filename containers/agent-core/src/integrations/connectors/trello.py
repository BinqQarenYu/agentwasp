"""Trello connector — Trello REST API v1.

Secrets:
    api_key   — Trello Power-Up API key
    token     — Trello member token (OAuth)

Actions:
    get_boards    — List boards for the authenticated member           (LOW)
    get_lists     — Get lists on a board                              (LOW)
    get_cards     — Get cards on a list or board                      (LOW)
    get_card      — Get a single card by ID                           (LOW)
    create_card   — Create a new card on a list                       (MEDIUM)
    update_card   — Update card title, description, due date, etc.    (MEDIUM)
    move_card     — Move card to a different list                      (MEDIUM)
    archive_card  — Archive (close) a card                            (MEDIUM)
    add_comment   — Add a comment to a card                           (MEDIUM)
    get_members   — List members of a board                           (LOW)
    add_label     — Add a label to a card                             (MEDIUM)
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..base import ActionSpec, BaseConnector, ConnectorManifest, ParamSpec, RateLimit, RiskLevel

logger = structlog.get_logger()
_API = "https://api.trello.com/1"
_TIMEOUT = 15.0


class TrelloConnector(BaseConnector):
    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="trello", version="1.0.0", name="Trello", category="productivity",
            description="Manage Trello boards, lists, and cards via REST API.",
            capabilities=["read_boards", "manage_cards", "manage_lists", "comments", "labels"],
            risk_level=RiskLevel.MEDIUM,
            required_secrets=["api_key", "token"],
            config_schema={},
            rate_limits={
                "get_boards":   RateLimit(requests_per_minute=60),
                "get_lists":    RateLimit(requests_per_minute=60),
                "get_cards":    RateLimit(requests_per_minute=60),
                "get_card":     RateLimit(requests_per_minute=60),
                "create_card":  RateLimit(requests_per_minute=30),
                "update_card":  RateLimit(requests_per_minute=30),
                "move_card":    RateLimit(requests_per_minute=30),
                "archive_card": RateLimit(requests_per_minute=30),
                "add_comment":  RateLimit(requests_per_minute=30),
                "get_members":  RateLimit(requests_per_minute=30),
                "add_label":    RateLimit(requests_per_minute=30),
            },
            actions=[
                ActionSpec(id="get_boards", description="List all boards the member has access to",
                    risk_level=RiskLevel.LOW, capability="monitored", params=[]),
                ActionSpec(id="get_lists", description="Get all lists on a board",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("board_id", "string", "Trello board ID", required=True)]),
                ActionSpec(id="get_cards", description="Get cards on a list or entire board",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("list_id", "string", "List ID (use this OR board_id)", required=False),
                        ParamSpec("board_id", "string", "Board ID to get all cards on board", required=False),
                        ParamSpec("limit", "integer", "Max cards to return (default 50)", required=False),
                    ]),
                ActionSpec(id="get_card", description="Get a single card by ID",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("card_id", "string", "Trello card ID", required=True)]),
                ActionSpec(id="create_card", description="Create a new card on a list",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("list_id", "string", "Target list ID", required=True),
                        ParamSpec("name", "string", "Card title", required=True),
                        ParamSpec("desc", "string", "Card description (Markdown)", required=False),
                        ParamSpec("due", "string", "Due date (ISO 8601)", required=False),
                        ParamSpec("pos", "string", "Position: top|bottom (default bottom)", required=False),
                    ]),
                ActionSpec(id="update_card", description="Update a card's fields",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("card_id", "string", "Trello card ID", required=True),
                        ParamSpec("name", "string", "New title", required=False),
                        ParamSpec("desc", "string", "New description", required=False),
                        ParamSpec("due", "string", "Due date (ISO 8601)", required=False),
                        ParamSpec("due_complete", "boolean", "Mark due date complete", required=False),
                    ]),
                ActionSpec(id="move_card", description="Move a card to a different list",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("card_id", "string", "Card ID to move", required=True),
                        ParamSpec("list_id", "string", "Destination list ID", required=True),
                        ParamSpec("pos", "string", "Position in new list: top|bottom", required=False),
                    ]),
                ActionSpec(id="archive_card", description="Archive (close) a card",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[ParamSpec("card_id", "string", "Trello card ID to archive", required=True)]),
                ActionSpec(id="add_comment", description="Add a comment to a card",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("card_id", "string", "Trello card ID", required=True),
                        ParamSpec("text", "string", "Comment text (Markdown)", required=True),
                    ]),
                ActionSpec(id="get_members", description="List members of a board",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[ParamSpec("board_id", "string", "Trello board ID", required=True)]),
                ActionSpec(id="add_label", description="Add a label to a card",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("card_id", "string", "Trello card ID", required=True),
                        ParamSpec("label_id", "string", "Label ID to add", required=True),
                    ]),
            ],
            homepage="https://trello.com",
            docs_url="https://developer.atlassian.com/cloud/trello/rest/",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        key   = secrets.get("api_key", "")
        token = secrets.get("token", "")
        if not key or not token:
            return self.err("api_key and token are required")
        auth = {"key": key, "token": token}

        if action == "get_boards":    return await self._get(f"/members/me/boards", auth, {"fields": "name,id,closed,url"})
        if action == "get_lists":     return await self._get(f"/boards/{params['board_id']}/lists", auth, {"fields": "name,id,closed"})
        if action == "get_card":      return await self._get(f"/cards/{params['card_id']}", auth, {})
        if action == "get_members":   return await self._get(f"/boards/{params['board_id']}/members", auth, {})

        if action == "get_cards":
            if params.get("list_id"):
                limit = min(int(params.get("limit") or 50), 100)
                return await self._get(f"/lists/{params['list_id']}/cards", auth,
                    {"fields": "name,id,desc,due,closed,url,idList", "limit": limit})
            if params.get("board_id"):
                return await self._get(f"/boards/{params['board_id']}/cards", auth,
                    {"fields": "name,id,desc,due,closed,url,idList"})
            return self.err("Provide list_id or board_id")

        if action == "create_card":
            body: dict[str, Any] = {"idList": params["list_id"], "name": params["name"], **auth}
            if params.get("desc"): body["desc"] = params["desc"]
            if params.get("due"):  body["due"]  = params["due"]
            if params.get("pos"):  body["pos"]  = params["pos"]
            return await self._post("/cards", body)

        if action == "update_card":
            body = {**auth}
            if params.get("name"):         body["name"]        = params["name"]
            if params.get("desc"):         body["desc"]        = params["desc"]
            if params.get("due"):          body["due"]         = params["due"]
            if params.get("due_complete") is not None:
                body["dueComplete"] = params["due_complete"]
            return await self._put(f"/cards/{params['card_id']}", body)

        if action == "move_card":
            body = {**auth, "idList": params["list_id"]}
            if params.get("pos"): body["pos"] = params["pos"]
            return await self._put(f"/cards/{params['card_id']}", body)

        if action == "archive_card":
            return await self._put(f"/cards/{params['card_id']}", {**auth, "closed": "true"})

        if action == "add_comment":
            return await self._post(f"/cards/{params['card_id']}/actions/comments", {**auth, "text": params["text"]})

        if action == "add_label":
            return await self._post(f"/cards/{params['card_id']}/idLabels", {**auth, "value": params["label_id"]})

        return self.err(f"Unknown action: {action}")

    async def _get(self, path: str, auth: dict, extra: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API}{path}", params={**auth, **extra})
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, list):
                return self.ok({"items": d, "count": len(d)})
            return self.ok(d)
        return self.err(f"Trello {r.status_code}: {r.text[:200]}")

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_API}{path}", json=body)
        if r.status_code in (200, 201):
            return self.ok(r.json())
        return self.err(f"Trello {r.status_code}: {r.text[:200]}")

    async def _put(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.put(f"{_API}{path}", json=body)
        if r.status_code == 200:
            return self.ok(r.json())
        return self.err(f"Trello {r.status_code}: {r.text[:200]}")
